# -*- coding: utf-8 -*-
"""
dqx_route.py

DQX Ver1〜Ver2 の「徒歩」前提ルートと、推奨ルーラ地点を提案します。

使い方:
    python dqx_route.py --dest "ウルベア地下遺跡" --db "dqx_map_data.xlsx"

このスクリプトは Excel スキーマの両対応です:
- 旧: sheets = continents / areas / edges / aliases(任意)
- 新: sheets = nodes / edges (+ aliases 任意)
  - nodes: name, continent, category, is_rura
  - edges: src, dst[, weight]

※ 新スキーマでも内部的に旧スキーマ相当へ正規化してからルーティングを行います。
"""
import argparse
import sys
from typing import Dict, List, Tuple, Optional
import pandas as pd
from collections import defaultdict, deque
from pathlib import Path

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

    # id を採番して areas を構築
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
            "version": str(row.get("version", "")),  # 無ければ空
            "category": str(row.get("category", "")),
            "is_rura": int(row.get("is_rura", 0)),
        })
    areas_df = pd.DataFrame(areas_rows)

    # edges を name → id に写像
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

    # continents を推定（DEFAULT_HUBS を優先、無ければ is_rura=1 の先頭 or 先頭ノード）
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

    # aliases（任意）
    try:
        aliases_df = pd.read_excel(xlsx_path, sheet_name="aliases")
        if not {"alias","canonical"}.issubset(set(aliases_df.columns)):
            aliases_df = pd.DataFrame(columns=["alias", "canonical"])
    except Exception:
        aliases_df = pd.DataFrame(columns=["alias", "canonical"])

    return continents_df, areas_df, edges_df, aliases_df

def load_db(xlsx_path: Path):
    xl = pd.ExcelFile(xlsx_path)  # シート一覧を調査
    sheets = set(xl.sheet_names)

    # 旧スキーマ
    if {"continents", "areas", "edges"}.issubset(sheets):
        continents = pd.read_excel(xlsx_path, sheet_name="continents")
        areas = pd.read_excel(xlsx_path, sheet_name="areas")
        edges = pd.read_excel(xlsx_path, sheet_name="edges")
        try:
            aliases = pd.read_excel(xlsx_path, sheet_name="aliases")
        except Exception:
            aliases = pd.DataFrame(columns=["alias", "canonical"])
        return continents, areas, edges, aliases

    # 新スキーマ（nodes/edges）
    if {"nodes", "edges"}.issubset(sheets):
        return _normalize_from_nodes_edges(xlsx_path)

    raise SystemExit("未対応の Excel スキーマです。'continents/areas/edges' もしくは 'nodes/edges' を含めてください。")

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

def path_to_names(path: List[int], id2name: Dict[int, str]) -> List[str]:
    return [id2name[i] for i in path]

def compute_route(dest_name: str, db_path: Path):
    continents_df, areas_df, edges_df, aliases_df = load_db(db_path)
    id2name = {int(r["id"]): str(r["name"]) for _, r in areas_df.iterrows()}
    name2id = {v: k for k, v in id2name.items()}
    id2continent = {int(r["id"]): str(r["continent"]) for _, r in areas_df.iterrows()}
    id2isrura = {int(r["id"]): int(r.get("is_rura", 0)) for _, r in areas_df.iterrows()}

    resolved_dest = resolve_name(dest_name, areas_df, aliases_df)
    if not resolved_dest:
        close = [n for n in areas_df["name"].astype(str) if dest_name in n]
        msg = "目的地が見つかりませんでした。候補: " + (", ".join(close) if close else "なし")
        raise SystemExit(msg)

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
                    raise SystemExit(f"大陸データが見つかりません: {dest_cont}")
                hub_name = str(sub2.iloc[0]["name"])
    else:
        hub_name = str(row.iloc[0]["default_hub"])

    if hub_name not in name2id:
        raise SystemExit(f"ハブ地点が areas に未登録です: {hub_name}")
    hub_id = name2id[hub_name]

    same_cont_ids = set(areas_df.loc[areas_df["continent"] == dest_cont, "id"].astype(int))
    sub_edges = edges_df[edges_df[["from_id", "to_id"]].astype(int).apply(lambda x: x["from_id"] in same_cont_ids and x["to_id"] in same_cont_ids, axis=1)]
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
        rura_candidates = [int(r["id"]) for _, r in areas_df.loc[(areas_df["continent"] == dest_cont) & (areas_df.get("is_rura", 0) == 1)].iterrows()]
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

    print(f"移動ルート : {walk_route}")
    if rura_walk:
        print(f"ルーラ     : {rura_point}（徒歩: {rura_walk}）")
    else:
        print(f"ルーラ     : {rura_point}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="目的地（例: ウルベア地下遺跡）")
    ap.add_argument("--db", default="dqx_map_data.xlsx", help="DB Excel パス")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)

    compute_route(args.dest, db_path)

if __name__ == "__main__":
    main()
