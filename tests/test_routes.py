"""行进路线：折线压缩、地图点击吸附、状态时间点、数据库，以及全地图局部配准。"""

import os
import atexit
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))
sys.stdout.reconfigure(encoding="utf-8")
TEMP = Path(tempfile.mkdtemp(prefix="evo-route-"))
atexit.register(shutil.rmtree, TEMP, ignore_errors=True)
os.environ["EVO_DATA_DIR"] = str(TEMP)

import routes  # noqa: E402
from hud import Hud  # noqa: E402
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
    recovered = matcher.locate(crop, previous=(.05, .05))
    return result, recovered


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
    motion = {"min_step_radius": .035, "max_speed_per_second": .006}
    out["相邻定位允许正常移动"] = routes.route_step_ok(
        {"elapsed_ms": 1000, "x": .5, "y": .5}, {"elapsed_ms": 11000, "x": .54, "y": .53}, motion)
    out["数秒内横跨地图的误匹配会被拒绝"] = not routes.route_step_ok(
        {"elapsed_ms": 1000, "x": .5, "y": .5}, {"elapsed_ms": 5000, "x": .2, "y": .8}, motion)
    worker = object.__new__(routes.RouteWorker)
    worker.cfg = {"matcher": {**motion, "start_confirm_radius": .045, "reset_after_rejects": 3}}
    worker.previous, worker.tentative, worker.rejects = {}, {}, {}
    locate = lambda x, y: {"x": x, "y": y, "confidence": .8, "matches": 10}
    first = worker._accept((1, "残月"), 1000, locate(.5, .5))
    confirmed = worker._accept((1, "残月"), 5000, locate(.51, .51))
    jumped = worker._accept((1, "残月"), 8000, locate(.2, .8))
    out["首个位置连续确认后才采用"] = first is None and confirmed is not None
    out["已确认路线中的瞬移点会被丢弃"] = jumped is None and worker.previous[(1, "残月")]["x"] == .51
    inferred = routes.infer_evo_event({"evo": 1600, "damage": 50}, {"evo": 1055, "damage": 170})
    out["EVO 剩余值下降量扣除伤害后推测地图事件"] = inferred is not None and inferred["key"] == "harvester"
    out["EVO 升级后的剩余值跳高不会误判固定分值事件"] = (
        routes.infer_evo_event({"evo": 20, "damage": 170}, {"evo": 1600, "damage": 170}) is None)
    cfg = routes.load_route_config()
    hud = object.__new__(Hud)
    hud.engine = lambda _image: SimpleNamespace(txts=["下一级：1,609 进化"], scores=[.999])
    evo_crop = np.zeros((69, 387, 3), dtype=np.uint8)
    out["录屏样式的中文 EVO 文本能读成剩余值"] = hud.read_evo(evo_crop, cfg["evo"])[0] == 1609

    recorder = object.__new__(routes.RouteRecorder)
    states = [
        {"elapsed_ms": 500, "damage": 0, "kills": 0, "changed": ("damage", "kills"), "ocr_confidence": .9},
        {"elapsed_ms": 1500, "damage": 120, "kills": 1, "changed": ("damage", "kills"), "ocr_confidence": .85},
    ]
    enriched, marks = recorder._with_states(points, states)
    out["状态按时间合并进路线"] = enriched[0].get("damage") is None and enriched[1]["damage"] == 0 and enriched[2]["damage"] == 120
    out["系统状态点按时间排序并生成变化说明"] = (
        [m["elapsed_ms"] for m in marks] == [500, 1500] and "伤害 +120" in marks[1]["label"])
    damage_only = [
        {"elapsed_ms": 500, "damage": 0, "kills": 0, "changed": ("damage", "kills"), "ocr_confidence": .9},
        {"elapsed_ms": 1500, "damage": 120, "kills": 0, "changed": ("damage",), "ocr_confidence": .85},
    ]
    enriched2, marks2 = recorder._with_states(points, damage_only)
    out["只变伤害不单独记时间点，伤害仍补在路线上"] = len(marks2) == 1 and enriched2[2]["damage"] == 120

    thin = routes.thin_by_time([(i * 1000, i) for i in range(600)], 60)
    gaps = [b[0] - a[0] for a, b in zip(thin, thin[1:])]
    out["状态块满了按时间均匀抽稀，最新的那块留下"] = (
        len(thin) <= 60 and thin[-1][0] == 599000 and min(gaps) >= 9000 and max(gaps) <= 11000)
    out["抽稀后开局不会整段没有状态"] = thin[0][0] <= 10000

    from collections import deque
    from datetime import datetime, timedelta
    rec = object.__new__(routes.RouteRecorder)
    rec.cfg = {"max_state_samples": 60}
    start = datetime(2026, 11, 7, 20, 0, 0)
    rec.current = {"started": start, "top_jobs": deque(), "evo_jobs": deque()}
    for second in range(120):
        rec.record_top(object(), start + timedelta(seconds=second))
    jobs = list(rec.current["top_jobs"])
    out["右上角状态块到上限后仍是按时间均匀的"] = len(jobs) <= 60 and jobs[-1][0] == 119000

    top_hud = Hud(None)
    box_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    quiet = top_hud.top_snip(box_frame)
    same = top_hud.top_snip(box_frame)
    digit = box_frame.copy()
    digit[100:112, 1720:1750] = 255  # 伤害数字所在的一小块
    outside = box_frame.copy()
    outside[60:75, 1770:1800] = 255  # 面板缝里的战场，不是读数
    out["识别右上角时，一个数字变了也算变了"] = (
        not top_hud.changed(quiet, same) and top_hud.changed(quiet, top_hud.top_snip(digit))
        and not top_hud.changed(quiet, top_hud.top_snip(outside)))
    out["分辨率变了直接算变了"] = top_hud.changed(quiet, top_hud.top_snip(np.zeros((720, 1280, 3), dtype=np.uint8)))

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
    conn = storage.connect(db_path)
    view = routes.route_view(conn, match_id)
    conn.close()
    out["路线接口给时间点标出这一刻的击杀"] = any(m.get("events") == ["kills"] for m in view["marks"])
    response = client.post(f"/matches/{match_id}/route/marks", data={"token": panel_app.app.config["TOKEN"], "x": .2, "y": .2})
    conn = storage.connect(db_path)
    out["点击路线接口新增手动时间点"] = response.status_code == 200 and len(storage.route_marks_of(conn, match_id)) == 3
    conn.close()

    located, recovered = synthetic_matcher()
    out["小地图能配准到预置全地图"] = located is not None and abs(located["x"] - .525) < .04 and abs(located["y"] - .5) < .04
    out["局部位置错误时会回退全图重新定位"] = recovered is not None and abs(recovered["x"] - .525) < .04

    template = (ROOT / "panel" / "templates" / "route.html").read_text(encoding="utf-8")
    out["路线页支持悬停、点击和时间点表格"] = all(x in template for x in ("pointermove", "add_route_mark", "时间点"))
    out["路线图支持原生全屏和兼容回退"] = all(x in template for x in
                                           ("requestFullscreen", "fullscreenchange", "is-expanded", "退出全屏"))
    out["路线图支持光标中心滚轮缩放和拖动平移"] = all(x in template for x in
                                                    ('"wheel"', "passive:false", "pointerdown", "route-canvas"))
    out["放大时路线图标和描边反向缩小"] = all(x in template for x in
                                             ("resizeOverlays", "5 / zoom", "9 / zoom", "placeMark") )
    out["放大到地图原图像素就停，不再拉大"] = all(x in template for x in
                                                ("function maxZoom()", "naturalWidth", "devicePixelRatio",
                                                 "Math.min(maxZoom()"))
    out["路线图上击杀画菱形、助攻画三角，并带图例"] = all(x in template for x in
                                                ("route-key", "markKind", "kill", "assist", "M0 -10 L10 0 L0 10 L-10 0 Z"))
    out["地图上只画击杀、助攻、死亡，系统点和手动点不上图"] = (
        "route-key death" in template and "if (!kind) return;" in template and "route-key auto" not in template
        and "route-key manual" not in template)
    tagged = routes.mark_events([
        {"kind": "auto", "label": "剩余小队 20", "kills": 0, "assists": 0, "rp_delta": -20},
        {"kind": "auto", "label": "击杀 +1", "kills": 1, "assists": 0, "rp_delta": -15},
        {"kind": "auto", "label": "击杀 +1、助攻 +1", "kills": 2, "assists": 1, "rp_delta": 131},
        {"kind": "manual", "label": "手动标记", "kills": 2, "assists": 1, "rp_delta": 131},
        {"kind": "auto", "label": "助攻 +1", "kills": 2, "assists": 2, "rp_delta": 223},
    ])
    out["时间点标出这一刻多了击杀还是助攻"] = [m["events"] for m in tagged] == [
        [], ["kills"], ["kills", "assists"], [], ["assists"]]
    out["每个时间点显示当时的 RP"] = [m["rp_delta"] for m in tagged] == [-20, -15, 131, 131, 223]
    out["时间点表格里有右上角 RP 这一列"] = ("右上角 RP" in template and "mark.rp_delta" in template
                                        and "colspan=\"10\"" in template)
    _, death_marks = recorder._with_states(points, states, [(1200, "存活"), (1200, "成盒"), (900, "倒地")])
    deaths = [m for m in death_marks if m["kind"] == "death"]
    out["成盒会变成路线上的死亡标记，位置按时间取"] = (
        len(deaths) == 1 and deaths[0]["label"] == "死亡" and deaths[0]["elapsed_ms"] == 1200
        and abs(deaths[0]["x"] - .22) < .01 and deaths[0]["damage"] == 0)
    out["存活和倒地不单独记成时间点"] = len(death_marks) == len(marks) + 1
    for name, ok in out.items():
        print(f"{'✅' if ok else '❌'} {name}")
    return all(out.values())


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
