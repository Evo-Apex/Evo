"""导出匿名路线诊断 JSON：只含地图、矢量点、分段和处理配置。

不导出玩家名、队友名、账号 ID、伤害、截图或任何原始画面。用于离线复现路线问题。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_path  # noqa: F401,E402

from paths import DB_PATH  # noqa: E402
from routes import load_route_config  # noqa: E402

POINT_FIELDS = ("elapsed_ms", "x", "y", "confidence", "segment_id")
CONFIG_FIELDS = ("sample_seconds", "max_sample_seconds", "simplify_tolerance", "min_route_points")


def _rows(conn, sql, match_id):
    try:
        return [dict(row) for row in conn.execute(sql, (match_id,))]
    except sqlite3.OperationalError:
        return None


def _point_rows(conn, table, match_id):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return None
    if not cols:
        return None
    seg = "segment_id" if "segment_id" in cols else "0 AS segment_id"
    return [dict(row) for row in conn.execute(
        f"SELECT elapsed_ms,x,y,confidence,{seg} FROM {table} WHERE match_id=? ORDER BY seq",
        (match_id,))]


def export(conn, match_id, cfg=None):
    cfg = cfg or load_route_config()
    conn.row_factory = sqlite3.Row
    route = conn.execute(
        "SELECT map_name, map_asset, status, started_at, ended_at, coverage, point_count "
        "FROM match_routes WHERE match_id = ?", (match_id,)).fetchone()
    if route is None:
        raise ValueError(f"找不到第 {match_id} 局的路线")
    item = dict(route)
    asset = item.get("map_asset") or ""
    version = ""
    for info in cfg.get("maps", {}).values():
        if info.get("file") == asset:
            version = info.get("version", "")
            break
    raw = _point_rows(conn, "route_raw_points", match_id)
    points = _point_rows(conn, "route_points", match_id)
    marks = _rows(conn, "SELECT elapsed_ms,kind,label,confidence,x,y FROM route_marks "
                        "WHERE match_id=? ORDER BY elapsed_ms", match_id)
    data = {
        "format": "evo-route-diag/1",
        "match": {
            "match_id": match_id,
            "map_name": item.get("map_name"),
            "map_asset": asset,
            "map_version": version,
            "status": item.get("status"),
            "started_at": item.get("started_at"),
            "ended_at": item.get("ended_at"),
            "coverage": item.get("coverage"),
            "point_count": item.get("point_count"),
        },
        "config": {k: cfg.get(k) for k in CONFIG_FIELDS if k in cfg}
                  | {"matcher": {k: v for k, v in cfg.get("matcher", {}).items() if not k.startswith("_")}},
        "route_raw_points": raw if raw is not None else [],
        "route_points": [{k: p.get(k) for k in POINT_FIELDS} for p in (points or [])],
        "route_marks": marks or [],
        "raw_available": raw is not None,
        "note": ("原始定位点不可用：这局是旧版本数据库，只存了简化点。" if raw is None else ""),
    }
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="导出匿名路线诊断 JSON")
    parser.add_argument("--match", type=int, required=True, help="对局编号")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"数据库（默认 {DB_PATH}）")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        data = export(conn, args.match)
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {args.output}：{len(data['route_points'])} 个简化点，"
          f"{len(data['route_raw_points'])} 个原始点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
