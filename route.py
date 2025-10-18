import os
import logging
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from pathlib import Path
import unicodedata
import difflib
import heapq
import time

import discord
from discord.ext import commands

# ---- Excel / DataFrame
try:
    import pandas as pd
except ImportError as e:
    raise SystemExit("pandas が必要です。`pip install pandas openpyxl` を実行してください。") from e

logger = logging.getLogger("dqx-route-bot")

# ---------------------
# 環境変数
# ---------------------
PREFIX = os.getenv("PREFIX", "!")
DQX_DB_PATH = os.getenv("DQX_DB_PATH", "dqx_map_data.xlsx")

def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

# 起動時ヘルプの送信先チャンネルID（DQX_ROUTE_CHANNEL_ID優先、無ければCHANNEL_IDを使用）
DQX_ROUTE_CHANNEL_ID = _int_env("DQX_ROUTE_CHANNEL_ID", 0) or _int_env("CHANNEL_ID", 0)

# ---------------------
# 定数 / 既定ハブ
# ---------------------
MAX_DISCORD_LEN = 2000  # Discord のメッセージ上限
CHUNK_SAFE = 1990       # 余白をもたせて分割

DEFAULT_HUBS = {
    "ドワチャッカ大陸": "岳都ガタラ",
    "プクランド大陸": "オルフェアの町",
    "ウェナ諸島": "ジュレットの町",
    "エルトナ大陸": "風の町アズラン",
    "オーグリード大陸": "グレン城下町",
    "レンダーシア大陸": "グランゼドーラ王国",
    "真レンダーシア": "真グランゼドーラ王国",
    "その他": "港町レンドア",
}

# ---------------------
# 送信ユーティリティ
# ---------------------
def chunk_text(text: str, limit: int = CHUNK_SAFE) -> List[str]:
    """Discord 文字数上限に抵触しないよう、適宜改行で分割する。"""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    buf = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        if len(line) > limit:  # 1行が極端に長い場合は強制分割
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            if line:
                buf.append(line)
                size = len(line)
        else:
            buf.append(line)
            size += len(line)
    if buf:
        parts.append("".join(buf))
    return parts

async def ensure_channel(client: discord.Client, channel_id: int) -> discord.abc.Messageable:
    ch = client.get_channel(channel_id)
    if ch is None:
        ch = await client.fetch_channel(channel_id)
    return ch

async def safe_send(channel: discord.abc.Messageable, content: str) -> None:
    try:
        content.encode("utf-8")
        for part in chunk_text(content):
            await channel.send(part)
        logger.info("メッセージを送信しました。")
    except Exception as e:
        logger.exception(f"メッセージ送信に失敗しました: {e}")

async def safe_reply(ctx: commands.Context, content: str) -> None:
    try:
        content.encode("utf-8")
        for part in chunk_text(content):
            await ctx.reply(part, mention_author=False)
    except Exception as e:
        logger.exception(f"返信に失敗: {e}")

def build_help_text() -> str:
    return "\n".join(
        [
            "**【NEW】🧭 DQX ルート検索コマンド**",
            f"• `{PREFIX}route <目的地>` — ハブ→目的地の徒歩ルートと推奨ルーラ地点を表示",
            f"• `{PREFIX}route_suggest <キーワード>` — 曖昧な地名から候補を提案",
            f"• `{PREFIX}route_help` — このヘルプを表示",
            "",
        ]
    )

# ---------------------
# 正規化 & サジェスト
# ---------------------
def _canon(s: str) -> str:
    """類似度評価用に軽く正規化（NFKC、空白/中黒/ハイフン/「の」を除去）。"""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    remove = {
        ord(" "): None, ord("　"): None,
        ord("・"): None, ord("-"): None, ord("―"): None, ord("‐"): None, ord("–"): None, ord("—"): None,
        ord("の"): None,
    }
    return t.translate(remove)

def resolve_name(name: str, areas_df: pd.DataFrame, aliases_df: pd.DataFrame) -> Optional[str]:
    names = set(areas_df["name"].astype(str))
    if name in names:
        return name
    for _, row in aliases_df.iterrows():
        if str(row.get("alias", "")) == name:
            canon = str(row.get("canonical", ""))
            if canon in names:
                return canon
    cands = [n for n in names if name in n]
    if len(cands) == 1:
        return cands[0]
    return None

def suggest_names(query: str, areas_df: pd.DataFrame, _aliases_df: pd.DataFrame, k: int = 3, min_score: float = 0.55) -> List[str]:
    names = list(pd.Series(areas_df["name"].astype(str)).dropna().unique())
    qn = _canon(query)
    scored = []
    for nm in names:
        nn = _canon(nm)
        if not nn:
            continue
        score = difflib.SequenceMatcher(None, qn, nn).ratio()
        if qn and (qn in nn or nn in qn):
            score += 0.05
        scored.append((score, nm))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [nm for sc, nm in scored if sc >= min_score][:k]

# ---------------------
# Excel 読み込み / 正規化
# ---------------------
def _read_excel(path: Path, sheet: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet, engine="openpyxl")

def _normalize_from_nodes_edges(xlsx_path: Path):
    """nodes/edges 形式から areas/edges/continents/aliases を生成して返す。"""
    nodes_df = _read_excel(xlsx_path, "nodes")
    edges_nodes_df = _read_excel(xlsx_path, "edges")

    areas_rows = []
    name2id = {}
    for i, row in nodes_df.reset_index(drop=True).iterrows():
        nid = 10_000 + i
        nm = str(row["name"])
        name2id[nm] = nid
        areas_rows.append({
            "id": nid,
            "name": nm,
            "continent": str(row.get("continent", "")),
            "version": str(row.get("version", "")),
            "category": str(row.get("category", "")),
            "is_rura": int(row.get("is_rura", 0) or 0),
        })
    areas_df = pd.DataFrame(areas_rows)

    e_rows = []
    for _, r in edges_nodes_df.iterrows():
        su = str(r["src"])
        sv = str(r["dst"])
        if su not in name2id or sv not in name2id:
            continue
        w = r.get("weight", 1)
        try:
            w = int(w)
        except Exception:
            w = 1
        e_rows.append({"from_id": name2id[su], "to_id": name2id[sv], "weight": w, "via": str(r.get("note", "徒歩"))})
    edges_df = pd.DataFrame(e_rows)

    conts = []
    for cont in sorted(areas_df["continent"].dropna().unique()):
        hub = DEFAULT_HUBS.get(cont, "")
        if hub and hub in name2id:
            conts.append({"continent": cont, "default_hub": hub})
            continue
        sub = areas_df[(areas_df["continent"] == cont) & (areas_df["is_rura"].astype(int) == 1)]
        if not sub.empty:
            conts.append({"continent": cont, "default_hub": str(sub.iloc[0]["name"])})
            continue
        sub2 = areas_df[areas_df["continent"] == cont]
        if not sub2.empty:
            conts.append({"continent": cont, "default_hub": str(sub2.iloc[0]["name"])})
    continents_df = pd.DataFrame(conts)

    try:
        aliases_df = _read_excel(xlsx_path, "aliases")
        if not {"alias", "canonical"}.issubset(set(aliases_df.columns)):
            aliases_df = pd.DataFrame(columns=["alias", "canonical"])
    except Exception:
        aliases_df = pd.DataFrame(columns=["alias", "canonical"])

    return continents_df, areas_df, edges_df, aliases_df

def load_db(xlsx_path: Path):
    xl = pd.ExcelFile(xlsx_path, engine="openpyxl")
    sheets = set(xl.sheet_names)

    if {"continents", "areas", "edges"}.issubset(sheets):
        continents = _read_excel(xlsx_path, "continents")
        areas = _read_excel(xlsx_path, "areas")
        edges = _read_excel(xlsx_path, "edges")
        try:
            aliases = _read_excel(xlsx_path, "aliases")
        except Exception:
            aliases = pd.DataFrame(columns=["alias", "canonical"])
    elif {"nodes", "edges"}.issubset(sheets):
        continents, areas, edges, aliases = _normalize_from_nodes_edges(xlsx_path)
    else:
        raise ValueError("未対応の Excel スキーマです。'continents/areas/edges' または 'nodes/edges' を含めてください。")

    # 型の整備
    if "is_rura" not in areas.columns:
        areas["is_rura"] = 0
    areas["is_rura"] = pd.to_numeric(areas["is_rura"], errors="coerce").fillna(0).astype(int)
    areas["id"] = pd.to_numeric(areas["id"], errors="coerce").astype(int)

    for col in ("from_id", "to_id"):
        edges[col] = pd.to_numeric(edges[col], errors="coerce").astype(int)
    if "weight" not in edges.columns:
        edges["weight"] = 1
    edges["weight"] = pd.to_numeric(edges["weight"], errors="coerce").fillna(1).astype(int)

    return continents, areas, edges, aliases

# ---------------------
# グラフユーティリティ
# ---------------------
def build_graph(edges_df: pd.DataFrame) -> Dict[int, List[Tuple[int, int]]]:
    g: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for _, row in edges_df.iterrows():
        u, v = int(row["from_id"]), int(row["to_id"])
        w = int(row.get("weight", 1))
        g[u].append((v, w))
        g[v].append((u, w))
    return g

def bfs_shortest_path(g: Dict[int, List[Tuple[int, int]]], start: int, goal: int) -> Optional[List[int]]:
    """後方互換のために残置。内部では未使用（重みを考慮するため Dijkstra を採用）。"""
    from collections import deque
    q = deque([start])
    parent = {start: None}
    while q:
        u = q.popleft()
        if u == goal:
            break
        for v, _w in g.get(u, []):
            if v not in parent:
                parent[v] = u
                q.append(v)
    if goal not in parent:
        return None
    path = []
    cur = goal
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    return path

def _reconstruct_path(parent: Dict[int, Optional[int]], start: int, goal: int) -> Optional[List[int]]:
    """parent は start からの経路木（parent[start] is None）"""
    if goal not in parent:
        return None
    path = []
    cur = goal
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    if path and path[0] == start:
        return path
    return None

def dijkstra_all(g: Dict[int, List[Tuple[int, int]]], start: int) -> Tuple[Dict[int, int], Dict[int, Optional[int]]]:
    """単一始点最短路（重み >= 1 前提）"""
    dist: Dict[int, int] = {start: 0}
    parent: Dict[int, Optional[int]] = {start: None}
    pq: List[Tuple[int, int]] = [(0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, w in g.get(u, []):
            nd = d + w
            if v not in dist or nd < dist[v]:
                dist[v] = nd
                parent[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, parent

def dijkstra_shortest_path(g: Dict[int, List[Tuple[int, int]]], start: int, goal: int) -> Optional[List[int]]:
    dist, parent = dijkstra_all(g, start)
    if goal not in dist:
        return None
    return _reconstruct_path(parent, start, goal)

# ---------------------
# ルーティングの補助
# ---------------------
def resolve_hub_for_continent(dest_cont: str, continents_df: pd.DataFrame, areas_df: pd.DataFrame, name2id: Dict[str, int]) -> Optional[str]:
    row = continents_df.loc[continents_df["continent"] == dest_cont]
    if not row.empty:
        return str(row.iloc[0]["default_hub"])

    # continents に無い場合はフォールバック
    hub_name = DEFAULT_HUBS.get(dest_cont, None)
    if hub_name and hub_name in name2id:
        return hub_name

    # 大陸内のルーラ地点があればそれを
    sub = areas_df[(areas_df["continent"] == dest_cont) & (areas_df["is_rura"].astype(int) == 1)]
    if not sub.empty:
        return str(sub.iloc[0]["name"])

    # それもなければ最初のエリア名
    sub2 = areas_df[areas_df["continent"] == dest_cont]
    if not sub2.empty:
        return str(sub2.iloc[0]["name"])

    return None

def path_to_names(path: List[int], id2name: Dict[int, str]) -> List[str]:
    return [id2name[i] for i in path]

# ---------------------
# DB キャッシュ
# ---------------------
class MapDB:
    """Excel DB を監視し、変更があれば再読み込み。大陸サブグラフもキャッシュ。"""
    def __init__(self, path: Path):
        self.path = path
        self._mtime: Optional[float] = None
        self.continents: pd.DataFrame = pd.DataFrame()
        self.areas: pd.DataFrame = pd.DataFrame()
        self.edges: pd.DataFrame = pd.DataFrame()
        self.aliases: pd.DataFrame = pd.DataFrame()
        self.id2name: Dict[int, str] = {}
        self.name2id: Dict[str, int] = {}
        self.id2continent: Dict[int, str] = {}
        self.id2isrura: Dict[int, int] = {}
        self._graphs_by_continent: Dict[str, Dict[int, List[Tuple[int, int]]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"DB が見つかりません: {self.path}")
        self.continents, self.areas, self.edges, self.aliases = load_db(self.path)
        self.id2name = {int(r["id"]): str(r["name"]) for _, r in self.areas.iterrows()}
        self.name2id = {v: k for k, v in self.id2name.items()}
        self.id2continent = {int(r["id"]): str(r["continent"]) for _, r in self.areas.iterrows()}
        self.id2isrura = {int(r["id"]): int(r.get("is_rura", 0)) for _, r in self.areas.iterrows()}
        self._graphs_by_continent.clear()
        self._mtime = self.path.stat().st_mtime
        logger.info("DB を読み込みました。areas=%d edges=%d", len(self.areas), len(self.edges))

    def maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            raise FileNotFoundError(f"DB が見つかりません: {self.path}")
        if self._mtime is None or m > self._mtime:
            logger.info("DB の更新を検知: 再読み込みします。")
            self._load()

    def subgraph(self, continent: str) -> Dict[int, List[Tuple[int, int]]]:
        if continent in self._graphs_by_continent:
            return self._graphs_by_continent[continent]
        same_ids = set(self.areas.loc[self.areas["continent"] == continent, "id"].astype(int))
        sub_edges = self.edges[
            self.edges["from_id"].isin(same_ids) & self.edges["to_id"].isin(same_ids)
        ]
        g = build_graph(sub_edges)
        self._graphs_by_continent[continent] = g
        return g

# ---------------------
# ルート作成（メイン）
# ---------------------
def compute_route_text(dest_name: str, db_path: Path, db: Optional[MapDB] = None) -> str:
    """
    成功時は見出し付きの整形テキスト、未発見時は候補提示テキストを返す。
    ※ 重みつき最短経路（Dijkstra）で徒歩ルート/ルーラ推奨を算出。
    """
    db = db or MapDB(db_path)
    db.maybe_reload()

    resolved_dest = resolve_name(dest_name, db.areas, db.aliases)
    if not resolved_dest:
        suggestions = suggest_names(dest_name, db.areas, db.aliases, k=5, min_score=0.55)
        if suggestions:
            return "❓ 目的地が見つかりませんでした。\n**もしかして**: " + " / ".join(suggestions)
        close = [n for n in db.areas["name"].astype(str) if dest_name in n]
        return "❓ 目的地が見つかりませんでした。\n候補: " + (", ".join(close) if close else "（候補なし）")

    dest_id = db.name2id[resolved_dest]
    dest_cont = db.id2continent.get(dest_id, "")

    hub_name = resolve_hub_for_continent(dest_cont, db.continents, db.areas, db.name2id)
    if not hub_name:
        return f"⚠️ 大陸データが見つかりません: {dest_cont}"
    if hub_name not in db.name2id:
        return f"⚠️ ハブ地点が areas に未登録です: {hub_name}"
    hub_id = db.name2id[hub_name]

    # 大陸サブグラフを構築
    g = db.subgraph(dest_cont)

    # 徒歩ルート（ハブ→目的地）
    walk_path_ids = dijkstra_shortest_path(g, hub_id, dest_id)
    if not walk_path_ids:
        walk_route = "（未接続：徒歩ルートがDBにありません）"
    else:
        walk_names = path_to_names(walk_path_ids, db.id2name)
        walk_route = " → ".join(walk_names)

    # 推奨ルーラ
    if db.id2isrura.get(dest_id, 0) == 1:
        rura_point = resolved_dest
        rura_walk = ""
    else:
        # 目的地から単一始点 Dijkstra を一度だけ回して最短ルーラ候補を決定
        dists_from_dest, parent_from_dest = dijkstra_all(g, dest_id)
        rura_candidates = [
            int(r["id"]) for _, r in db.areas.loc[
                (db.areas["continent"] == dest_cont) & (db.areas["is_rura"].astype(int) == 1)
            ].iterrows()
        ]
        best = None
        for rid in rura_candidates:
            if rid in dists_from_dest:
                d = dists_from_dest[rid]
                if (best is None) or (d < best[0]):
                    best = (d, rid)
        if best is None:
            rura_point = "(同大陸に徒歩接続されたルーラ地点がDB未定義)"
            rura_walk = ""
        else:
            _d, rid = best
            # parent は「dest から各頂点まで」の木なので、dest→rid を復元してから反転
            path_dest_to_rid = _reconstruct_path(parent_from_dest, dest_id, rid)
            if not path_dest_to_rid:
                rura_point = db.id2name[rid]
                rura_walk = ""
            else:
                path_rid_to_dest = list(reversed(path_dest_to_rid))
                path_names = path_to_names(path_rid_to_dest, db.id2name)
                rura_point = path_names[0]
                rura_walk = " → ".join(path_names[1:]) if len(path_names) >= 2 else ""

    lines = [
        f"**🎯 目的地:** {resolved_dest}（大陸: {dest_cont}）",
        f"**🚶 徒歩ルート:** {walk_route}",
    ]
    if rura_walk:
        lines.append(f"**🧭 推奨ルーラ:** {rura_point}（徒歩: {rura_walk}）")
    else:
        lines.append(f"**🧭 推奨ルーラ:** {rura_point}")
    return "\n".join(lines)

# ---------------------
# Cog 本体
# ---------------------
class DQXRouteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = Path(DQX_DB_PATH)
        self.channel_id = DQX_ROUTE_CHANNEL_ID
        self._ready_once = False  # 起動時ヘルプの一度きり送信に使用
        self._db: Optional[MapDB] = None
        try:
            self._db = MapDB(self.db_path)
        except Exception as e:
            logger.error("DB 初期化に失敗しました: %s", e)

    # ---- Bot ready → 起動時に一度だけヘルプ送付
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True

        if self.channel_id == 0:
            logger.error("環境変数 DQX_ROUTE_CHANNEL_ID もしくは CHANNEL_ID が未設定です。起動時ヘルプを送信できません。")
            return

        try:
            ch = await ensure_channel(self.bot, self.channel_id)
            await safe_send(ch, build_help_text())
        except Exception as e:
            logger.exception(f"起動時ヘルプ送信に失敗しました: {e}")

    @commands.command(name="route", aliases=["ルート"])
    async def route_cmd(self, ctx: commands.Context, *, dest: Optional[str] = None):
        """!route <目的地> — ルート検索"""
        if dest is None or not dest.strip():
            await safe_reply(ctx, f"使い方: `{PREFIX}route <目的地>` 例: `{PREFIX}route ウルベア地下遺跡`")
            return

        if not self.db_path.exists():
            await safe_reply(ctx, f"❌ DB が見つかりません: `{self.db_path}`。環境変数 `DQX_DB_PATH` を確認してください。")
            return

        try:
            if self._db is None:
                self._db = MapDB(self.db_path)
            else:
                self._db.maybe_reload()
            text = compute_route_text(dest.strip(), self.db_path, db=self._db)
            await safe_reply(ctx, text)
        except Exception as e:
            logger.exception(e)
            await safe_reply(ctx, "❌ ルート計算でエラーが発生しました。ログを確認してください。")

    @commands.command(name="route_suggest", aliases=["ルート候補"])
    async def route_suggest_cmd(self, ctx: commands.Context, *, query: Optional[str] = None):
        """!route_suggest <キーワード> — 候補を提案"""
        if query is None or not query.strip():
            await safe_reply(ctx, f"使い方: `{PREFIX}route_suggest <キーワード>` 例: `{PREFIX}route_suggest グレン`")
            return

        if not self.db_path.exists():
            await safe_reply(ctx, f"❌ DB が見つかりません: `{self.db_path}`。環境変数 `DQX_DB_PATH` を確認してください。")
            return

        try:
            if self._db is None:
                self._db = MapDB(self.db_path)
            else:
                self._db.maybe_reload()
            cands = suggest_names(query.strip(), self._db.areas, self._db.aliases, k=10, min_score=0.50)
            if cands:
                await safe_reply(ctx, "🔎 候補: " + " / ".join(cands))
            else:
                await safe_reply(ctx, "🔎 候補なし")
        except Exception as e:
            logger.exception(e)
            await safe_reply(ctx, "❌ 候補生成でエラーが発生しました。ログを確認してください。")

    @commands.command(name="route_help")
    async def route_help_cmd(self, ctx: commands.Context):
        await safe_reply(ctx, build_help_text())

# 拡張エントリ（discord.py v2.x 用）
async def setup(bot: commands.Bot):
    await bot.add_cog(DQXRouteCog(bot))
