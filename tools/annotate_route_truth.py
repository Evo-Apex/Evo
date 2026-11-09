"""离线人工标注路线真值：同时看完整录像帧、小地图和全地图，不显示算法坐标。"""

import argparse
import csv
import hashlib
import sys
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import code_path  # noqa: F401
import routes
from replay_route_video import _cut_minimap


FIELDS = [
    "source_video", "source_video_sha256", "frame_index", "video_time", "map",
    "map_asset_sha256", "manual_x", "manual_y", "visible", "indoor", "occluded",
    "basis", "annotator",
]
BASIS = ["minimap+scene", "minimap", "full-map-frame"]
WINDOW = "Evo route truth (no algorithm result)"
MAP_SIDE = 900
CANVAS_W, CANVAS_H = 1600, 900


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_map(map_name):
    cfg = routes.load_route_config()
    info = cfg["maps"][map_name]
    path = Path(routes.ASSET_DIR) / info["file"]
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"地图图片打不开：{path}")
    shown = cv2.resize(image, (MAP_SIDE, MAP_SIDE), interpolation=cv2.INTER_AREA)
    return shown, cfg, path


def resize_fit(image, width, height):
    h, w = image.shape[:2]
    scale = min(width / w, height / h)
    resized = cv2.resize(image, (max(1, round(w * scale)), max(1, round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), np.uint8)
    x, y = (width - resized.shape[1]) // 2, (height - resized.shape[0]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


def read_rows(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def sample_times(start, end, step, count):
    if count:
        if count == 1:
            return [float(start)]
        return [float(start) + (float(end) - float(start)) * i / (count - 1) for i in range(count)]
    values, current = [], float(start)
    while current <= float(end) + 1e-6:
        values.append(current)
        current += float(step)
    return values


def draw(map_image, frame, minimap, index, total, timestamp, indoor, basis, message):
    canvas = np.zeros((CANVAS_H, CANVAS_W, 3), np.uint8)
    canvas[:, :MAP_SIDE] = map_image
    canvas[:394, MAP_SIDE:] = resize_fit(frame, CANVAS_W - MAP_SIDE, 394)
    mini = resize_fit(minimap, 390, 390)
    canvas[420:810, MAP_SIDE:MAP_SIDE + 390] = mini
    text_x = MAP_SIDE + 405
    lines = [
        f"{index + 1}/{total}  time={timestamp:.1f}s",
        f"indoor={'YES' if indoor else 'NO'}",
        f"basis={basis}",
        "Click LEFT MAP = visible point",
        "i indoor   o occluded   n invisible",
        "b basis    u undo       q save/quit",
        message,
    ]
    for line_no, line in enumerate(lines):
        color = (70, 220, 255) if line_no == len(lines) - 1 else (235, 235, 235)
        cv2.putText(canvas, line, (text_x, 455 + line_no * 42), cv2.FONT_HERSHEY_SIMPLEX,
                    0.62, color, 1, cv2.LINE_AA)
    return canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", type=float, default=45)
    parser.add_argument("--end", type=float)
    parser.add_argument("--step", type=float, default=30)
    parser.add_argument("--count", type=int)
    parser.add_argument("--annotator", default="manual")
    args = parser.parse_args()

    video = Path(args.video).resolve()
    output = Path(args.output).resolve()
    map_image, cfg, map_path = load_map(args.map)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"录像打不开：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    end = min(duration, args.end if args.end is not None else duration)
    times = sample_times(args.start, end, args.step, args.count)
    video_hash, map_hash = sha256(video), sha256(map_path)
    rows = read_rows(output)
    completed = {(row.get("map"), round(float(row["video_time"]), 3)) for row in rows}
    index = next((i for i, value in enumerate(times)
                  if (args.map, round(value, 3)) not in completed), len(times))
    state = {"index": index, "indoor": False, "basis_index": 0, "message": "", "photo": None}
    root = tk.Tk()
    root.title(WINDOW)
    root.geometry(f"{CANVAS_W}x{CANVAS_H}")
    root.resizable(False, False)
    surface = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H, highlightthickness=0)
    surface.pack()

    def current_frame():
        timestamp = times[state["index"]]
        frame_index = round(timestamp * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if not ok:
            state["message"] = "Frame read failed; use n"
            frame = np.zeros((1080, 1920, 3), np.uint8)
        return timestamp, frame_index, frame

    def render():
        if state["index"] >= len(times):
            save_rows(output, rows)
            root.destroy()
            return
        timestamp, _frame_index, frame = current_frame()
        minimap = _cut_minimap(frame, cfg)
        canvas = draw(map_image, frame, minimap, state["index"], len(times), timestamp,
                      state["indoor"], BASIS[state["basis_index"]], state["message"])
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        state["photo"] = ImageTk.PhotoImage(Image.fromarray(rgb))
        surface.delete("all")
        surface.create_image(0, 0, image=state["photo"], anchor="nw")
        root.after_idle(root.focus_force)

    def record(kind, xy=("", "")):
        timestamp, frame_index, _frame = current_frame()
        x, y = xy
        rows.append({
            "source_video": video.name,
            "source_video_sha256": video_hash,
            "frame_index": frame_index,
            "video_time": round(timestamp, 3),
            "map": args.map,
            "map_asset_sha256": map_hash,
            "manual_x": round(x, 6) if x != "" else "",
            "manual_y": round(y, 6) if y != "" else "",
            "visible": kind == "point",
            "indoor": state["indoor"],
            "occluded": kind == "occluded",
            "basis": BASIS[state["basis_index"]] if kind == "point" else kind,
            "annotator": args.annotator,
        })
        save_rows(output, rows)
        state.update(index=state["index"] + 1, indoor=False, message="Saved")
        render()

    def click(event):
        if event.x < MAP_SIDE and event.y < MAP_SIDE:
            record("point", (event.x / MAP_SIDE, event.y / MAP_SIDE))

    def key(event):
        pressed = event.keysym.lower()
        if pressed == "i":
            state["indoor"] = not state["indoor"]
            state["message"] = "Indoor toggled"
            render()
        elif pressed == "b":
            state["basis_index"] = (state["basis_index"] + 1) % len(BASIS)
            state["message"] = "Basis changed"
            render()
        elif pressed == "o":
            record("occluded")
        elif pressed == "n":
            record("invisible")
        elif pressed == "u" and rows and state["index"] > 0:
            rows.pop()
            state.update(index=state["index"] - 1, indoor=False, message="Previous annotation removed")
            save_rows(output, rows)
            render()
        elif pressed in {"q", "escape"}:
            save_rows(output, rows)
            root.destroy()

    surface.bind("<Button-1>", click)
    root.bind("<Key>", key)
    root.protocol("WM_DELETE_WINDOW", lambda: (save_rows(output, rows), root.destroy()))
    try:
        render()
        root.mainloop()
    finally:
        cap.release()
    print(f"saved {output} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
