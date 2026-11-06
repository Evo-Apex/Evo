"""行进路线：折线压缩、地图点击吸附、状态时间点、数据库，以及全地图局部配准。"""

import os
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))
sys.stdout.reconfigure(encoding="utf-8")
TEMP = Path(tempfile.mkdtemp(prefix="evo-route-"))
atexit.register(shutil.rmtree, TEMP, ignore_errors=True)
os.environ["EVO_DATA_DIR"] = str(TEMP)

import routes  # noqa: E402
from route_matcher import MapMatcher  # noqa: E402
import storage  # noqa: E402


def synthetic_matcher():
    rng = np.random.default_rng(31)
    image = rng.integers(0, 255, (800, 800), dtype=np.uint8)
    for x, y in ((160, 210), (340, 420), (610, 270), (520, 650)):
        cv2.circle(image, (x, y), 24, 255, 4)
        cv2.line(image, (x - 30, y + 30), (x + 35, y - 20), 0, 5)
    path = TEMP / "map.png"
    cv2.imencode(".png", image)[1].tofile(path)
    cfg = {"reference_size": 800, "sample_size": 240, "mask_radius": .49, "center_mask": .03,
           "local_radius": .2, "ratio": .8, "min_matches": 8, "min_inliers": 6,
           "min_inlier_ratio": .4, "ransac_px": 5}
    matcher = MapMatcher(path, cfg)
    crop = cv2.cvtColor(image[280:520, 300:540], cv2.COLOR_GRAY2BGR)
    result = matcher.locate(crop)
    return result


def main():
    out = {}
    points = [
        {"elapsed_ms": 0, "x": 0.1, "y": 0.1, "confidence": .9},
        {"elapsed_ms": 1000, "x": 0.2, "y": 0.2, "confidence": .8},
        {"elapsed_ms": 2000, "x": 0.3, "y": 0.3, "confidence": .7},
    ]
    simple = routes.simplify(points, .001)
    out["共线折线只留首尾"] = [p["elapsed_ms"] for p in simple] == [0, 2000]
    snapped = routes.nearest_on_route(points, .205, .195)
    out["点击吸附到最近线段并插值时间"] = abs(snapped["x"] - .2) < .01 and 900 <= snapped["elapsed_ms"] <= 1100
    timed = routes.point_at_time(points, 1500)
    out["按时间插值路线坐标"] = abs(timed["x"] - .25) < 1e-6 and abs(timed["y"] - .25) < 1e-6
    inferred = routes.infer_evo_event({"evo": 100, "damage": 50}, {"evo": 645, "damage": 170})
    out["EVO 增量扣除伤害后推测地图事件"] = inferred is not None and inferred["key"] == "harvester"

    recorder = object.__new__(routes.RouteRecorder)
    states = [
        {"elapsed_ms": 500, "damage": 0, "kills": 0, "changed": ("damage", "kills"), "ocr_confidence": .9},
        {"elapsed_ms": 1500, "damage": 120, "kills": 1, "changed": ("damage", "kills"), "ocr_confidence": .85},
    ]
    enriched, marks = recorder._with_states(points, states)
    out["状态按时间合并进路线"] = enriched[0].get("damage") is None and enriched[1]["damage"] == 0 and enriched[2]["damage"] == 120
    out["系统状态点按时间排序并生成变化说明"] = (
        [m["elapsed_ms"] for m in marks] == [500, 1500] and "伤害 +120" in marks[1]["label"])

    db_path = TEMP / "route.db"
    conn = storage.connect(db_path)
    match_id = storage.insert_match(conn, {"ended_at": "2026-11-06 20:00:00", "map": "风暴点"})
    storage.save_route(conn, match_id, {"map_name": "风暴点", "map_asset": "storm-point-s31.jpg", "status": "ready",
                                              "started_at": "2026-11-06 19:40:00", "ended_at": "2026-11-06 20:00:00",
                                              "coverage": .8, "processed_at": "2026-11-06 20:00:01"}, enriched, marks)
    out["数据库只保存矢量点和时间点"] = (
        storage.route_of(conn, match_id)["point_count"] == 3
        and len(storage.route_points_of(conn, match_id)) == 3
        and len(storage.route_marks_of(conn, match_id)) == 2)
    manual = routes.nearest_on_route(storage.route_points_of(conn, match_id), .24, .26)
    manual.update({"kind": "manual", "label": "手动标记"})
    mark_id = storage.add_route_mark(conn, match_id, manual)
    storage.delete_route_mark(conn, match_id, mark_id)
    out["手动时间点可以新增和删除，系统点不会误删"] = len(storage.route_marks_of(conn, match_id)) == 2
    conn.close()

    import panel_app
    panel_app.app.config["DB"] = db_path
    client = panel_app.app.test_client()
    page = client.get(f"/matches/{match_id}/route")
    body = page.get_data(as_text=True)
    out["路线页能读取真实数据库并绘制"] = page.status_code == 200 and "route-line" in body and "伤害 +120" in body
    response = client.post(f"/matches/{match_id}/route/marks", data={"token": panel_app.app.config["TOKEN"], "x": .2, "y": .2})
    conn = storage.connect(db_path)
    out["点击路线接口新增手动时间点"] = response.status_code == 200 and len(storage.route_marks_of(conn, match_id)) == 3
    conn.close()

    located = synthetic_matcher()
    out["小地图能配准到预置全地图"] = located is not None and abs(located["x"] - .525) < .04 and abs(located["y"] - .5) < .04

    template = (ROOT / "panel" / "templates" / "route.html").read_text(encoding="utf-8")
    out["路线页支持悬停、点击和时间点表格"] = all(x in template for x in ("pointermove", "add_route_mark", "时间点"))
    for name, ok in out.items():
        print(f"{'✅' if ok else '❌'} {name}")
    return all(out.values())


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
