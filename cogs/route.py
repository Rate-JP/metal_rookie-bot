# cogs/dqx_route.py
# -*- coding: utf-8 -*-
"""
Discord Cog: DQX Route
- dqx_route.py のロジックを Discord コマンド化
- notice.py の構成（環境変数, Cog, async setup, 起動時ヘルプ）に準拠

依存:
    pip install pandas openpyxl

環境変数:
    PREFIX                (既定: "!")
    DQX_DB_PATH           (既定: "dqx_map_data.xlsx")
    DQX_ROUTE_CHANNEL_ID  (既定: 未設定)  # 未設定時は CHANNEL_ID をフォールバックで使用
    CHANNEL_ID            (フォールバック用)
"""
import os
import logging
from typing import Dict, List, Tuple, Optional
from collections import defaultdict, deque
from pathlib import Path
import unicodedata
import difflib

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

# 起動時ヘルプの送信先チャンネルID（DQX_ROUTE_CHANNEL_ID優先、無ければCHANNEL_IDを使用）
def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

DQX_ROUTE_CHANNEL_ID = _int_env("DQX_ROUTE_CHANNEL_ID", 0) or _int_env("CHANNEL_ID", 0)

# ---------------------
# 送信ユーティリティ
# ---------------------
async def ensure_channel(client: discord.Client, channel_id: int) -> discord.abc.Messageable:
    ch = client.get_channel(channel_id)
    if ch is None:
        ch = await client.fetch_channel(channel_id)
    return ch

async def safe_send(channel: discord.abc.Messageable, content: str) -> None:
    try:
        content.encode("utf-8")
        await channel.send(content)
        logger.info("メッセージを送信しました。")
    except Exception as e:
        logger.exception(f"メッセージ送信に失敗しました: {e}")

async def safe_reply(ctx: commands.Context, content: str) -> None:
    try:
        content.encode("utf-8")
        await ctx.reply(content, mention_author=False)
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
# ルーティング・ロジック（元 dqx_route.py を移植）
# ---------------------

# 既定ハブ（continents シートが無い場合のフォールバック）
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

def _normalize_from_nodes_edges(xlsx_path: Path):
    """nodes/edges 形式から areas/edges/continents/aliases を生成して返す。"""
    nodes_df = pd.read_excel(xlsx_path, sheet_name="nodes")
    edges_nodes_df = pd.read_excel(xlsx_path, sheet_name="edges")

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
            "is_rura": int(row.get("is_rura", 0)),
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
        sub = areas_df[(areas_df["continent"] == cont) & (areas_df["is_rura"] == 1)]
        if not sub.empty:
            conts.append({"continent": cont, "default_hub": str(sub.iloc[0]["name"])})
            continue
        sub2 = areas_df[areas_df["continent"] == cont]
        if not sub2.empty:
            conts.append({"continent": cont, "default_hub": str(sub2.iloc[0]["name"])})
    continents_df = pd.DataFrame(conts)

    try:
        aliases_df = pd.read_excel(xlsx_path, sheet_name="aliases")
        if not {"alias","canonical"}.issubset(set(aliases_df.columns)):
            aliases_df = pd.DataFrame(columns=["alias", "canonical"])
    except Exception:
        aliases_df = pd.DataFrame(columns=["alias", "canonical"])

    return continents_df, areas_df, edges_df, aliases_df

def load_db(xlsx_path: Path):
    xl = pd.ExcelFile(xlsx_path)
    sheets = set(xl.sheet_names)

    if {"continents", "areas", "edges"}.issubset(sheets):
        continents = pd.read_excel(xlsx_path, sheet_name="continents")
        areas = pd.read_excel(xlsx_path, sheet_name="areas")
        edges = pd.read_excel(xlsx_path, sheet_name="edges")
        try:
            aliases = pd.read_excel(xlsx_path, sheet_name="aliases")
        except Exception:
            aliases = pd.DataFrame(columns=["alias", "canonical"])
        return continents, areas, edges, aliases

    if {"nodes", "edges"}.issubset(sheets):
        return _normalize_from_nodes_edges(xlsx_path)

    raise ValueError("未対応の Excel スキーマです。'continents/areas/edges' または 'nodes/edges' を含めてください。")

def build_graph(edges_df: pd.DataFrame) -> Dict[int, List[Tuple[int, int]]]:
    g = defaultdict(list)
    for _, row in edges_df.iterrows():
        u, v = int(row["from_id"]), int(row["to_id"])
        w = int(row.get("weight", 1))
        g[u].append((v, w))
        g[v].append((u, w))
    return g

def bfs_shortest_path(g: Dict[int, List[Tuple[int, int]]], start: int, goal: int) -> Optional[List[int]]:
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

def suggest_names(query: str, areas_df: pd.DataFrame, aliases_df: pd.DataFrame, k: int = 3, min_score: float = 0.55) -> List[str]:
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

def path_to_names(path: List[int], id2name: Dict[int, str]) -> List[str]:
    return [id2name[i] for i in path]

def compute_route_text(dest_name: str, db_path: Path) -> str:
    """成功時は見出し付きの整形テキスト、未発見時は候補提示テキストを返す。"""
    continents_df, areas_df, edges_df, aliases_df = load_db(db_path)
    id2name = {int(r["id"]): str(r["name"]) for _, r in areas_df.iterrows()}
    name2id = {v: k for k, v in id2name.items()}
    id2continent = {int(r["id"]): str(r["continent"]) for _, r in areas_df.iterrows()}
    id2isrura = {int(r["id"]): int(r.get("is_rura", 0)) for _, r in areas_df.iterrows()}

    resolved_dest = resolve_name(dest_name, areas_df, aliases_df)
    if not resolved_dest:
        suggestions = suggest_names(dest_name, areas_df, aliases_df, k=5, min_score=0.55)
        if suggestions:
            return "❓ 目的地が見つかりませんでした。\n**もしかして**: " + " / ".join(suggestions)
        close = [n for n in areas_df["name"].astype(str) if dest_name in n]
        return "❓ 目的地が見つかりませんでした。\n候補: " + (", ".join(close) if close else "（候補なし）")

    dest_id = name2id[resolved_dest]
    dest_cont = id2continent[dest_id]

    row = continents_df.loc[continents_df["continent"] == dest_cont]
    if row.empty:
        hub_name = DEFAULT_HUBS.get(dest_cont, None)
        if not hub_name or hub_name not in name2id:
            sub = areas_df[(areas_df["continent"] == dest_cont) & (areas_df.get("is_rura", 0) == 1)]
            if not sub.empty:
                hub_name = str(sub.iloc[0]["name"])
            else:
                sub2 = areas_df[areas_df["continent"] == dest_cont]
                if sub2.empty:
                    return f"⚠️ 大陸データが見つかりません: {dest_cont}"
                hub_name = str(sub2.iloc[0]["name"])
    else:
        hub_name = str(row.iloc[0]["default_hub"])

    if hub_name not in name2id:
        return f"⚠️ ハブ地点が areas に未登録です: {hub_name}"
    hub_id = name2id[hub_name]

    same_cont_ids = set(areas_df.loc[areas_df["continent"] == dest_cont, "id"].astype(int))
    sub_edges = edges_df[edges_df[["from_id", "to_id"]].astype(int).apply(
        lambda x: x["from_id"] in same_cont_ids and x["to_id"] in same_cont_ids, axis=1)]
    g = build_graph(sub_edges)

    walk_path_ids = bfs_shortest_path(g, hub_id, dest_id)
    if not walk_path_ids:
        walk_route = "（未接続：徒歩ルートがDBにありません）"
    else:
        walk_names = path_to_names(walk_path_ids, id2name)
        walk_route = " → ".join(walk_names)

    if id2isrura.get(dest_id, 0) == 1:
        rura_point = resolved_dest
        rura_walk = ""
    else:
        rura_candidates = [int(r["id"]) for _, r in areas_df.loc[
            (areas_df["continent"] == dest_cont) & (areas_df.get("is_rura", 0) == 1)
        ].iterrows()]
        best = None
        for rid in rura_candidates:
            p = bfs_shortest_path(g, rid, dest_id)
            if p:
                d = len(p) - 1
                if (best is None) or (d < best[0]):
                    best = (d, p)
        if best is None:
            rura_point = "(同大陸に徒歩接続されたルーラ地点がDB未定義)"
            rura_walk = ""
        else:
            _, pids = best
            path_names = path_to_names(pids, id2name)
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
            text = compute_route_text(dest.strip(), self.db_path)
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
            continents_df, areas_df, _edges_df, aliases_df = load_db(self.db_path)
            cands = suggest_names(query.strip(), areas_df, aliases_df, k=10, min_score=0.50)
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
