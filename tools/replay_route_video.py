"""离线回放一条 Apex 录像：每 3 秒裁一次小地图，强制指定地图，输出 P0 路线诊断。

只读录像；逐帧小地图不落盘，结束后只留匿名 JSON 和路线图。
运行示例：
  python tools/replay_route_video.py --video "风暴点.mp4" --map 风暴点 --out-dir out
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import code_path  # noqa: F401,E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import route_planner  # noqa: E402
import route_matcher  # noqa: E402
import routes  # noqa: E402

FONT = r"C:\Windows\Fonts\msyh.ttc"
SEGMENT_COLORS = [(76, 148, 255), (74, 200, 130), (235, 184, 74), (190, 120, 255),
                  (255, 120, 180), (100, 220, 220)]


def _font(size):
    return ImageFont.truetype(FONT, size)


def _hud_signature(frame):
    """右上角状态区的小缩略图；只用来找“HUD 有变化”的时间，不做 OCR。"""
    crop = frame[70:230, 1500:1900]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA).astype("int16")


def replay(video, map_name, cfg, interval=3.0, limit=None):
    info = cfg["maps"].get(map_name)
    if info is None:
        raise ValueError(f"地图名不在 config/routes.json：{map_name}")
    asset = Path(routes.ASSET_DIR) / info["file"]
    if not asset.exists():
        raise ValueError(f"地图底图不存在：{asset}")
    route_matcher.configure_worker()
    matcher = route_matcher.MapMatcher(str(asset), cfg["matcher"])
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"录像打不开：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frames / fps
    step_frames = max(1, round(interval * fps))
    state = SimpleNamespace(cfg=cfg, previous={}, tentative={}, rejects={}, segments={})
    r = cfg["minimap"]
    roi = r["roi"]
    points, hud, rejected, tentative, resets, missed = [], [], 0, 0, 0, 0
    last_sig = None
    timings = []
    tracemalloc.start()
    try:
        index = 0
        while index < frames:
            ms = index / fps * 1000
            if limit is not None and ms > limit * 1000:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[roi[1]:roi[1] + roi[3], roi[0]:roi[0] + roi[2]]
            sig = _hud_signature(frame)
            if last_sig is not None and float(np.abs(sig - last_sig).mean()) >= 6:
                hud.append({"elapsed_ms": round(ms), "change": round(float(np.abs(sig - last_sig).mean()), 1)})
            last_sig = sig
            started = time.perf_counter()
            result = matcher.locate(crop, None)
            timings.append((time.perf_counter() - started) * 1000)
            before = state.previous.get("video")
            old_segment = state.segments.get("video", -1)
            accepted = routes.RouteWorker._accept(state, "video", round(ms), result)
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
                points.append(accepted)
            index += step_frames
    finally:
        cap.release()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    sampled = len(timings)
    simplified = routes.simplify(points, cfg["simplify_tolerance"]) if points else []
    segments = sorted({int(p["segment_id"]) for p in points})
    report = {
        "video": Path(video).name,
        "map_name": map_name,
        "map_asset": info["file"],
        "map_version": info.get("version"),
        "sample_seconds": interval,
        "duration_seconds": round(duration, 1),
        "frames": frames,
        "fps": round(fps, 2),
        "sampled": sampled,
        "matched": len(points),
        "missed": missed,
        "tentative": tentative,
        "rejected": rejected,
        "resets": resets,
        "segments": segments,
        "coverage": round(len(points) / max(1, sampled), 4),
        "mean_match_ms": round(sum(timings) / max(1, len(timings)), 2),
        "peak_tracemalloc_kb": round(peak / 1024, 1),
        "confidence": {
            "min": round(min((p["confidence"] for p in points), default=0), 3),
            "mean": round(sum(p.get("confidence", 0) for p in points) / max(1, len(points)), 3),
            "max": round(max((p["confidence"] for p in points), default=0), 3),
        },
        "raw_points": [{"elapsed_ms": p["elapsed_ms"], "x": round(p["x"], 5), "y": round(p["y"], 5),
                        "confidence": round(p["confidence"], 4), "segment_id": p.get("segment_id", 0)}
                       for p in points],
        "route_points": [{"elapsed_ms": p["elapsed_ms"], "x": round(p["x"], 5), "y": round(p["y"], 5),
                          "confidence": round(p["confidence"], 4), "segment_id": p.get("segment_id", 0)}
                         for p in simplified],
        "hud_timeline": hud,
    }
    report["suspected_crossings"] = _crossings(points, map_name, cfg)
    return report


def _crossings(points, map_name, cfg):
    mask_path = Path(routes.ASSET_DIR) / "storm-point-s31-mask.png"
    if map_name != "风暴点" or not mask_path.exists() or not points:
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


def draw(report, out_path, cfg):
    asset = Path(routes.ASSET_DIR) / report["map_asset"]
    base = Image.open(asset).convert("RGB").resize((1600, 1600), Image.LANCZOS)
    d = ImageDraw.Draw(base, "RGBA")
    d.rectangle((0, 0, 1600, 120), fill=(16, 18, 22, 215))
    d.text((36, 24), f"{report['map_name']} · 录像离线回放（每 {report['sample_seconds']:g} 秒）",
           font=_font(38), fill=(240, 242, 246))
    d.text((36, 74), f"定位 {report['matched']}/{report['sampled']} 帧，覆盖 {report['coverage']:.1%}；"
                     f"拒绝 {report['rejected']}，待确认 {report['tentative']}，重定位 {report['resets']}",
           font=_font(21), fill=(180, 186, 196))
    px = lambda p: (int(p["x"] * 1599), int(p["y"] * 1599))
    for p in report["raw_points"]:
        x, y = px(p)
        d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(230, 232, 236, 170))
    for seg in report["segments"]:
        pts = [px(p) for p in report["route_points"] if p["segment_id"] == seg]
        if len(pts) >= 2:
            d.line(pts, fill=SEGMENT_COLORS[seg % len(SEGMENT_COLORS)] + (255,), width=7, joint="curve")
            for a, b in zip(pts, pts[1:]):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                d.line((mx - 5, my - 5, mx + 5, my + 5), fill=(255, 255, 255, 220), width=2)
    for p in report["route_points"]:
        x, y = px(p)
        if len([q for q in report["route_points"] if q["segment_id"] == p["segment_id"]]) >= 2:
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 255, 255, 230))
    for cross in report.get("suspected_crossings", []):
        a, b = px({"x": cross["from"][0], "y": cross["from"][1]}), px({"x": cross["to"][0], "y": cross["to"][1]})
        _dash(d, a, b, (240, 156, 62, 255), 5)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        d.line((mx - 10, my - 10, mx + 10, my + 10), fill=(255, 90, 90, 255), width=5)
        d.line((mx - 10, my + 10, mx + 10, my - 10), fill=(255, 90, 90, 255), width=5)
    for left, right in zip(report["segments"], report["segments"][1:]):
        a = next((p for p in reversed(report["route_points"]) if p["segment_id"] == left), None)
        b = next((p for p in report["route_points"] if p["segment_id"] == right), None)
        if a and b:
            ax, ay = px(a)
            bx, by = px(b)
            for x, y in ((ax, ay), (bx, by)):
                d.ellipse((x - 13, y - 13, x + 13, y + 13), outline=(240, 156, 62, 255), width=5)
            d.text(((ax + bx) / 2 + 12, (ay + by) / 2 - 10), "断点", font=_font(20), fill=(240, 156, 62))
    legend = [("原始定位点（灰）", (230, 232, 236, 255)),
              ("确定路线（按 segment 分色）", SEGMENT_COLORS[0] + (255,)),
              ("疑似穿越硬障碍（橙虚线 + 红叉）", (240, 156, 62, 255))]
    for i, (text, color) in enumerate(legend):
        y = 1350 + i * 42
        d.rectangle((36, y + 10, 80, y + 28), fill=color)
        d.text((94, y), text, font=_font(21), fill=(235, 238, 244))
    d.text((36, 1500), "橙色圆圈 = segment 断点，两段之间不连线。", font=_font(20), fill=(240, 156, 62))
    d.text((36, 1542), "跳塔/载具/重生等二维不可确认的移动仍按 uncertain 处理；本图不当作已修复穿墙。",
           font=_font(18), fill=(190, 196, 206))
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
    parser = argparse.ArgumentParser(description="离线回放 Apex 录像，输出 P0 路线诊断")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--map", required=True, dest="map_name", help="强制地图名，如 风暴点")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--limit", type=float, default=None, help="只回放前 N 秒（调试用）")
    args = parser.parse_args(argv)
    cfg = routes.load_route_config()
    report = replay(args.video, args.map_name, cfg, args.interval, args.limit)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.video.stem
    json_path = args.out_dir / f"{stem}-route.json"
    png_path = args.out_dir / f"{stem}-route.png"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    draw(report, png_path, cfg)
    print(f"{stem}: {report['matched']}/{report['sampled']} 点，段 {report['segments']}，"
          f"拒绝 {report['rejected']}，疑似穿障碍 {len(report['suspected_crossings'])}")
    print(json_path)
    print(png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
