"""离线回放一条 Apex 录像：用生产同一套定位/接受/重定位/分段逻辑，输出路线和 HUD 诊断。

只读录像；逐帧小地图不落盘，结束后只留匿名 JSON 和路线图。
两种采样：
  production  按 RouteWorker 的 interval（取决于平均匹配耗时和队列压力）
  fixed-3s    固定每 3 秒，用来看算法上限
运行示例：
  python tools/replay_route_video.py --video "风暴点.mp4" --map 风暴点 --out-dir out \
      --mode production --start 50 --end 1053 --hud --summary-time 1058
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_path  # noqa: F401,E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import hud as hud_mod  # noqa: E402
import layout  # noqa: E402
import ocr  # noqa: E402
import route_matcher  # noqa: E402
import route_planner  # noqa: E402
import route_tracking  # noqa: E402
import routes  # noqa: E402
import summary_page  # noqa: E402

FONT = r"C:\Windows\Fonts\msyh.ttc"
SEGMENT_COLORS = [(76, 148, 255), (74, 200, 130), (235, 184, 74), (190, 120, 255),
                  (255, 120, 180), (100, 220, 220)]
HUD_FIELDS = hud_mod.TOP_FIELDS


def _font(size):
    return ImageFont.truetype(FONT, size)


def _bar_top(frame):
    """录像上下可能有黑边；小地图 ROI 以游戏画面为基准，先跳过顶部黑边。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    rows = gray.mean(axis=1)
    for i, value in enumerate(rows):
        if value > 20:
            return i
    return 0


def _cut_minimap(frame, cfg):
    m = cfg["minimap"]
    x, y, w, h = m["roi"]
    return frame[y + _bar_top(frame):y + h + _bar_top(frame), x:x + w]


def _read_at(cap, seconds, fps):
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, round(seconds * fps)))
    ok, frame = cap.read()
    return frame if ok else None


def _percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def replay(video, map_name, cfg, mode="production", start=0.0, end=None, fixed=3.0):
    info = cfg["maps"].get(map_name)
    if info is None:
        raise ValueError(f"地图名不在 config/routes.json：{map_name}")
    asset = Path(routes.ASSET_DIR) / info["file"]
    if not asset.exists():
        raise ValueError(f"地图底图不存在：{asset}")
    route_matcher.configure_worker()
    matcher = route_matcher.MapMatcher(str(asset), cfg["matcher"])
    tracker = route_tracking.RouteTracker(cfg)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"录像打不开：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frames / fps
    if end is None or end > duration:
        end = duration
    points, timings, scopes, sample_times = [], [], [], []
    tentative = rejected = resets = missed = 0
    average_ms = 0.0
    last_elapsed = None
    current = start
    tracemalloc.start()
    try:
        while current <= end + 1e-6:
            frame = _read_at(cap, current, fps)
            if frame is None:
                break
            crop = _cut_minimap(frame, cfg)
            started = time.perf_counter()
            result, accepted = tracker.process(matcher, crop, "video", round(current * 1000))
            took = (time.perf_counter() - started) * 1000
            timings.append(took)
            sample_times.append(round(current, 3))
            scopes.append(matcher.last_scope or "none")
            before = tracker.previous.get("video")
            old_segment = tracker.segments.get("video", -1)
            if result is None:
                missed += 1
            elif accepted is None:
                if before is None:
                    tentative += 1
                else:
                    rejected += 1
            else:
                if old_segment >= 0 and accepted["segment_id"] != old_segment:
                    resets += 1
                accepted["trace"] = dict(matcher.last_metrics, scope=matcher.last_scope)
                points.append(accepted)
            average_ms = took if not average_ms else average_ms * 0.9 + took * 0.1
            last_elapsed = current
            if mode == "fixed-3s":
                current += fixed
            else:
                current += route_tracking.sample_interval(cfg, average_ms, 0.0)
    finally:
        cap.release()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    sampled = len(timings)
    simplified = routes.simplify(points, cfg["simplify_tolerance"]) if points else []
    raw_crossings = _crossings(simplified, map_name, cfg)
    planned = simplified
    uncertain_edges = 0
    planning_reasons = {}
    mask_name = info.get("mask")
    if simplified and mask_name:
        mask_path = Path(routes.ASSET_DIR) / mask_name
        if mask_path.exists():
            planner = cfg.get("planner", {})
            legs = route_planner.route_with_mask(
                simplified, route_planner.load_mask(str(mask_path)),
                max_nodes=int(planner.get("max_nodes", 30000)),
                max_seconds=float(planner.get("max_seconds_per_edge", 0.35)),
                max_detour=float(planner.get("max_detour", 2.2)),
                snap_px=int(planner.get("snap_px", 8)),
                anchor_snap_px=int(planner.get("anchor_snap_px", 0)),
            )
            for leg in legs:
                for point in leg.get("points", []):
                    reason = point.get("uncertain_reason")
                    if reason:
                        planning_reasons[reason] = planning_reasons.get(reason, 0) + 1
            planned, uncertain_edges = route_planner.flatten_legs(legs)
    segments = sorted({int(p.get("segment_id", 0)) for p in planned})
    intervals = [b - a for a, b in zip(sample_times, sample_times[1:])]
    displacement = [((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5
                    for a, b in zip(points, points[1:]) if a.get("segment_id") == b.get("segment_id")]
    report = {
        "video": Path(video).name,
        "map_name": map_name,
        "map_asset": info["file"],
        "map_version": info.get("version"),
        "mode": mode,
        "start_seconds": round(start, 2),
        "end_seconds": round(end, 2),
        "included_seconds": round((last_elapsed or start) - start, 2),
        "duration_seconds": round(duration, 1),
        "frames": frames,
        "fps": round(fps, 2),
        "sample_seconds": fixed if mode == "fixed-3s" else None,
        "sampled": sampled,
        "matched": len(points),
        "missed": missed,
        "tentative": tentative,
        "rejected": rejected,
        "resets": resets,
        "segments": segments,
        "coverage": round(len(points) / max(1, sampled), 4),
        "timing_ms": {
            "mean": round(sum(timings) / max(1, len(timings)), 2),
            "p95": round(_percentile(timings, 0.95), 2),
        },
        "scope": {name: scopes.count(name) for name in ("local", "full", "none", "local_low", "local_low_kept")},
        "matcher": {
            "matches_mean": round(sum(p["trace"].get("matches", 0) for p in points if p.get("trace")) / max(1, len(points)), 1),
            "inliers_mean": round(sum(p["trace"].get("inliers", 0) for p in points if p.get("trace")) / max(1, len(points)), 1),
            "inlier_ratio_mean": round(sum(p["trace"].get("inlier_ratio", 0) for p in points if p.get("trace")) / max(1, len(points)), 3),
            "reprojection_error_mean": round(sum(p["trace"].get("reprojection_error", 0) for p in points if p.get("trace")) / max(1, len(points)), 3),
            "scale_mean": round(sum(p["trace"].get("scale", 0) for p in points if p.get("trace")) / max(1, len(points)), 3),
        },
        "actual_interval_seconds": {
            "mean": round(sum(intervals) / max(1, len(intervals)), 2),
            "max": round(max(intervals, default=0.0), 2),
        },
        "max_step": {
            "distance": round(max(displacement, default=0.0), 5),
            "seconds": round(max(intervals, default=0.0), 2),
        },
        "simplified_points": len(simplified),
        "planned_points": len(planned),
        "uncertain_edges": uncertain_edges,
        "planning_reasons": planning_reasons,
        "peak_tracemalloc_kb": round(peak / 1024, 1),
        "confidence": {
            "min": round(min((p["confidence"] for p in points), default=0), 3),
            "mean": round(sum(p.get("confidence", 0) for p in points) / max(1, len(points)), 3),
            "max": round(max((p["confidence"] for p in points), default=0), 3),
        },
        "raw_points": [{"elapsed_ms": p["elapsed_ms"], "x": round(p["x"], 5), "y": round(p["y"], 5),
                        "confidence": round(p["confidence"], 4), "segment_id": p.get("segment_id", 0),
                        "trace": p.get("trace")}
                       for p in points],
        "route_points": [{"elapsed_ms": p["elapsed_ms"], "x": round(p["x"], 5), "y": round(p["y"], 5),
                          "confidence": round(p.get("confidence", 0), 4), "segment_id": p.get("segment_id", 0)}
                         for p in planned],
        "raw_suspected_crossings": raw_crossings,
        "suspected_crossings": _crossings(planned, map_name, cfg),
        "crossings_status": "checked" if mask_name else "not_checked",
    }
    return report


def _crossings(points, map_name, cfg):
    info = cfg.get("maps", {}).get(map_name, {})
    mask_name = info.get("mask")
    mask_path = Path(routes.ASSET_DIR) / mask_name if mask_name else None
    if mask_path is None or not mask_path.exists() or not points:
        return []
    mask = route_planner.load_mask(str(mask_path))
    out = []
    side = mask.shape[1]
    for a, b in zip(points, points[1:]):
        if a.get("segment_id", 0) != b.get("segment_id", 0):
            continue
        ax, ay = round(a["x"] * (side - 1)), round(a["y"] * (side - 1))
        bx, by = round(b["x"] * (side - 1)), round(b["y"] * (side - 1))
        if not route_planner._visible(mask, ax, ay, bx, by):
            out.append({"from_ms": a["elapsed_ms"], "to_ms": b["elapsed_ms"],
                        "from": [round(a["x"], 4), round(a["y"], 4)],
                        "to": [round(b["x"], 4), round(b["y"], 4)]})
    return out


def _hud_crop(frame, cfg):
    spec = cfg["evo"]
    return layout.cut(frame, spec["roi"], cfg["base_resolution"], spec.get("anchor"))


def read_hud(video, cfg, start, end, hud_cfg, engine, step=1.0):
    """按 1 秒读生产 HUD：只有解析出的字段变了才记一个状态点。"""
    hud = hud_mod.Hud(engine, cfg=hud_cfg)
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    states, raw_events, impossible = [], [], []
    last_top = None
    last_evo = None
    previous = {}
    filled = attempts = 0
    try:
        t = start
        while t <= end:
            frame = _read_at(cap, t, fps)
            if frame is None:
                break
            snip = hud.top_snip(frame)
            evo_crop = _hud_crop(frame, cfg)
            changed_top = hud.changed(last_top, snip)
            changed_evo = (last_evo is None or float(cv2.absdiff(cv2.cvtColor(last_evo, cv2.COLOR_BGR2GRAY),
                                                                  cv2.cvtColor(evo_crop, cv2.COLOR_BGR2GRAY)).mean()) >= 1.0)
            values = dict(previous)
            scores = {}
            shown = set()
            if changed_top or changed_evo:
                fresh = {}
                if changed_top:
                    fresh, scores, shown = hud.read_top(snip)
                    for name, value in fresh.items():
                        if value is not None:
                            values[name] = value
                    last_top = snip
                raw = {k: list(v) for k, v in hud.last_raw.items()}
                if changed_evo:
                    evo, evo_score = hud.read_evo(evo_crop, cfg["evo"])
                    values["evo"], scores["evo"] = evo, evo_score
                    last_evo = evo_crop
                    if "evo" in hud.last_raw:
                        raw["evo"] = list(hud.last_raw["evo"])
                if changed_top:
                    raw_events.append({"elapsed_ms": round(t * 1000), "raw": raw})
                after = {k: values.get(k) for k in (*HUD_FIELDS, "evo")}
                if after != previous:
                    if previous:
                        for name in ("damage", "kills", "assists", "participation"):
                            a, b = previous.get(name), after.get(name)
                            if a is not None and b is not None and b < a:
                                impossible.append({"field": name, "from": a, "to": b,
                                                   "elapsed_ms": round(t * 1000)})
                        a, b = previous.get("squads_left"), after.get("squads_left")
                        if a is not None and b is not None and b > a:
                            impossible.append({"field": "squads_left", "from": a, "to": b,
                                               "elapsed_ms": round(t * 1000)})
                    attempts += len(HUD_FIELDS)
                    filled += sum(1 for k in HUD_FIELDS if after.get(k) is not None)
                    states.append({"elapsed_ms": round(t * 1000), **after,
                                   "scores": {k: round(float(v), 3) for k, v in scores.items() if v is not None},
                                   "raw": raw, "changed": [k for k in after if after[k] != previous.get(k)]})
                    previous = after
            t += step
    finally:
        cap.release()
    return {
        "states": states,
        "field_fill_rate": round(filled / max(1, attempts), 3),
        "impossible_jumps": impossible,
        "raw_events": raw_events,
    }


def read_summary(video, seconds, frame_cfg):
    if seconds is None:
        return None
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    frame = _read_at(cap, seconds, fps)
    cap.release()
    if frame is None:
        return {"status": "no_frame"}
    engine = ocr.create_engine(ocr.SMALL_CROP_PARAMS)
    results, problems = summary_page.read_summary(engine, frame, frame_cfg)
    keep = {}
    for name, item in results.items():
        value = item.get("value")
        keep[name] = {"status": item.get("status"), "value": list(value) if isinstance(value, tuple) else value,
                      "confidence": item.get("confidence"), "raw": item.get("raw")}
    return {"status": "ok", "fields": keep, "problems": problems}


def draw(report, out_path):
    asset = Path(routes.ASSET_DIR) / report["map_asset"]
    base = Image.open(asset).convert("RGB").resize((1600, 1600), Image.LANCZOS)
    d = ImageDraw.Draw(base, "RGBA")
    d.rectangle((0, 0, 1600, 132), fill=(16, 18, 22, 220))
    title = f"{report['map_name']} · {report['mode']} 回放"
    d.text((36, 22), title, font=_font(38), fill=(240, 242, 246))
    d.text((36, 76), f"纳入 {report['start_seconds']:g}–{report['end_seconds']:g}s；定位 {report['matched']}/{report['sampled']}"
                     f"（{report['coverage']:.1%}）；拒绝 {report['rejected']}；待确认 {report['tentative']}；"
                     f"重定位 {report['resets']}；段 {report['segments']}",
           font=_font(20), fill=(184, 190, 200))
    px = lambda p: (int(p["x"] * 1599), int(p["y"] * 1599))
    for p in report["raw_points"]:
        x, y = px(p)
        d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(232, 234, 238, 175), outline=(20, 22, 26, 120))
    for seg in report["segments"]:
        pts = [px(p) for p in report["route_points"] if p["segment_id"] == seg]
        if len(pts) >= 2:
            d.line(pts, fill=SEGMENT_COLORS[seg % len(SEGMENT_COLORS)] + (255,), width=7, joint="curve")
            for a, b in zip(pts, pts[1:]):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                d.line((mx - 4, my - 4, mx + 4, my + 4), fill=(255, 255, 255, 210), width=2)
    for cross in report.get("suspected_crossings", []):
        a = px({"x": cross["from"][0], "y": cross["from"][1]})
        b = px({"x": cross["to"][0], "y": cross["to"][1]})
        _dash(d, a, b, (240, 156, 62, 255), 5)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        d.line((mx - 9, my - 9, mx + 9, my + 9), fill=(255, 90, 90, 255), width=4)
        d.line((mx - 9, my + 9, mx + 9, my - 9), fill=(255, 90, 90, 255), width=4)
    for left, right in zip(report["segments"], report["segments"][1:]):
        a = next((p for p in reversed(report["route_points"]) if p["segment_id"] == left), None)
        b = next((p for p in report["route_points"] if p["segment_id"] == right), None)
        if a and b:
            ax, ay = px(a)
            bx, by = px(b)
            for x, y in ((ax, ay), (bx, by)):
                d.ellipse((x - 13, y - 13, x + 13, y + 13), outline=(240, 156, 62, 255), width=5)
            d.text(((ax + bx) / 2 + 12, (ay + by) / 2 - 10), "segment 断点", font=_font(20), fill=(240, 156, 62))
    for point in report.get("confirmed_errors", []):
        x, y = int(point["x"] * 1599), int(point["y"] * 1599)
        d.rectangle((x - 12, y - 12, x + 12, y + 12), outline=(255, 70, 70, 255), width=5)
    legend = [("原始匹配锚点", "dot", (232, 234, 238, 255)),
              ("赛后可通行路线（按 segment 分色）", "line", SEGMENT_COLORS[0] + (255,)),
              ("segment 断点（橙圈，不连线）", "ring", (240, 156, 62, 255))]
    if report.get("crossings_status") == "checked":
        legend.append(("疑似障碍穿越（橙虚线 + 红叉）", "cross", (240, 156, 62, 255)))
    else:
        legend.append(("疑似障碍穿越：未检测（该地图没有掩码）", "none", (150, 156, 166, 255)))
    if report.get("confirmed_errors"):
        legend.append(("人工已确认错误（红框）", "error", (255, 70, 70, 255)))
    for i, (text, kind, color) in enumerate(legend):
        y = 1288 + i * 38
        if kind == "dot":
            d.ellipse((44, y + 9, 60, y + 25), fill=color)
        elif kind == "line":
            d.line((36, y + 17, 68, y + 17), fill=color, width=6)
        elif kind == "ring":
            d.ellipse((40, y + 7, 64, y + 27), outline=color, width=4)
        elif kind == "cross":
            d.line((36, y + 17, 68, y + 17), fill=color, width=3)
            d.line((45, y + 8, 59, y + 26), fill=(255, 90, 90, 255), width=3)
            d.line((45, y + 26, 59, y + 8), fill=(255, 90, 90, 255), width=3)
        elif kind == "error":
            d.rectangle((40, y + 8, 64, y + 26), outline=color, width=4)
        d.text((94, y), text, font=_font(20), fill=(235, 238, 244))
    d.text((36, 1508), f"原始直线穿障碍 {len(report.get('raw_suspected_crossings', []))} 处；"
           f"规划后残留 {len(report.get('suspected_crossings', []))} 处；不确定连接已断开 {report.get('uncertain_edges', 0)} 处。",
           font=_font(19), fill=(190, 196, 206))
    base.save(out_path)
    return out_path


def _dash(d, a, b, fill, width):
    x1, y1 = a
    x2, y2 = b
    length = max(1.0, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    t = 0
    while t < length:
        e = min(t + 12, length)
        d.line((x1 + ux * t, y1 + uy * t, x1 + ux * e, y1 + uy * e), fill=fill, width=width)
        t += 22


def main(argv=None):
    parser = argparse.ArgumentParser(description="生产等价的录像回放：路线 + HUD 离线诊断")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--map", required=True, dest="map_name", help="强制地图名，如 风暴点")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("production", "fixed-3s"), default="production")
    parser.add_argument("--start", type=float, default=0.0, help="纳入路线的开始秒（跳过跳伞/加载）")
    parser.add_argument("--end", type=float, default=None, help="纳入路线的结束秒（总结页之前）")
    parser.add_argument("--fixed", type=float, default=3.0, help="fixed-3s 模式的间隔")
    parser.add_argument("--hud", action="store_true", help="离线读真实 HUD（会加载 OCR，较慢）")
    parser.add_argument("--summary-time", type=float, default=None, help="总结页所在秒，用于核对最终字段")
    parser.add_argument("--suffix", default="", help="输出文件名后缀，如 -production")
    args = parser.parse_args(argv)
    cfg = routes.load_route_config()
    report = replay(args.video, args.map_name, cfg, args.mode, args.start, args.end, args.fixed)
    if args.hud:
        hud_cfg = ocr.load_config("hud.json")
        engine = ocr.create_engine(ocr.SMALL_CROP_PARAMS)
        report["hud"] = read_hud(args.video, cfg, args.start, args.end or report["end_seconds"], hud_cfg, engine)
    else:
        report["hud"] = None
    report["summary"] = read_summary(args.video, args.summary_time, ocr.load_config("summary.json"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.video.stem + args.suffix
    json_path = args.out_dir / f"{stem}-route.json"
    png_path = args.out_dir / f"{stem}-route.png"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    draw(report, png_path)
    print(f"{stem}: {report['mode']}，{report['matched']}/{report['sampled']} 点，覆盖 {report['coverage']:.1%}，"
          f"段 {report['segments']}，疑似穿障碍 {len(report['suspected_crossings'])}")
    print(json_path)
    print(png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
