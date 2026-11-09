"""匿名路线诊断导出：字段、隐私、旧库兼容。

运行：python tests/test_route_diag.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "code"))
sys.stdout.reconfigure(encoding="utf-8")

import export_route_diag  # noqa: E402

CFG = {
    "sample_seconds": 3.0,
    "simplify_tolerance": 0.003,
    "min_route_points": 3,
    "maps": {"风暴点": {"file": "storm-point-s31.jpg", "version": "s31"}},
    "matcher": {"min_matches": 8, "max_speed_per_second": 0.006, "_说明": "x"},
}


def _new_db(path, with_raw=True):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE match_routes (match_id INTEGER PRIMARY KEY, map_name TEXT, map_asset TEXT, "
              "status TEXT, started_at TEXT, ended_at TEXT, coverage REAL, point_count INTEGER)")
    c.execute("CREATE TABLE route_points (match_id INTEGER, seq INTEGER, elapsed_ms INTEGER, x REAL, y REAL, "
              "confidence REAL, segment_id INTEGER)")
    if with_raw:
        c.execute("CREATE TABLE route_raw_points (match_id INTEGER, seq INTEGER, elapsed_ms INTEGER, x REAL, "
                  "y REAL, confidence REAL, segment_id INTEGER)")
    c.execute("CREATE TABLE route_marks (match_id INTEGER, elapsed_ms INTEGER, kind TEXT, label TEXT, "
              "confidence REAL, x REAL, y REAL)")
    c.execute("INSERT INTO match_routes VALUES (7,'风暴点','storm-point-s31.jpg','ready','a','b',0.5,3)")
    c.execute("INSERT INTO route_points VALUES (7,0,0,0.1,0.2,0.9,0)")
    c.execute("INSERT INTO route_points VALUES (7,1,3000,0.3,0.4,0.8,1)")
    c.execute("INSERT INTO route_marks VALUES (7,1000,'auto','击杀',0.9,0.15,0.25)")
    if with_raw:
        c.execute("INSERT INTO route_raw_points VALUES (7,0,0,0.1,0.2,0.9,0)")
        c.execute("INSERT INTO route_raw_points VALUES (7,1,3000,0.3,0.4,0.8,1)")
    c.commit()
    c.close()


def main():
    checks = {}
    with tempfile.TemporaryDirectory(prefix="evo-r04-diag-") as tmp:
        path = Path(tmp) / "evo.db"
        _new_db(path)
        conn = sqlite3.connect(path)
        data = export_route_diag.export(conn, 7, CFG)
        conn.close()
        checks["地图/素材/版本"] = data["match"]["map_name"] == "风暴点" and data["match"]["map_asset"] == "storm-point-s31.jpg" and data["match"]["map_version"] == "s31"
        checks["原始点保留"] = len(data["route_raw_points"]) == 2 and data["raw_available"] is True
        checks["简化点带 segment"] = [p["segment_id"] for p in data["route_points"]] == [0, 1]
        checks["标记保留"] = data["route_marks"][0]["kind"] == "auto"
        text = json.dumps(data, ensure_ascii=False)
        checks["不导出玩家名"] = "player" not in text.lower() and "玩家" not in text and "MOMoNa" not in text
        checks["不导出伤害"] = "damage" not in text.lower() and "伤害" not in text
        checks["不导出截图"] = "screenshot" not in text.lower() and "截图" not in text
        checks["配置含采样/简化"] = data["config"]["sample_seconds"] == 3.0 and data["config"]["simplify_tolerance"] == 0.003
        checks["说明里的下划线配置不导出"] = "_说明" not in json.dumps(data["config"], ensure_ascii=False)

        old = Path(tmp) / "old.db"
        _new_db(old, with_raw=False)
        c = sqlite3.connect(old)
        c.execute("ALTER TABLE route_points DROP COLUMN segment_id")
        c.commit()
        olddata = export_route_diag.export(c, 7, CFG)
        c.close()
        checks["旧库：没有原始点也不崩"] = olddata["raw_available"] is False and olddata["route_raw_points"] == []
        checks["旧库：缺 segment_id 时按 0"] = [p["segment_id"] for p in olddata["route_points"]] == [0, 0]
        checks["旧库：明确写原始点不可用"] = "旧版本" in olddata["note"]
    for name, ok in checks.items():
        print(("  ✅ " if ok else "  ❌ ") + name)
    print(f"\n{sum(checks.values())} 通过、{len(checks) - sum(checks.values())} 失败、0 跳过")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
