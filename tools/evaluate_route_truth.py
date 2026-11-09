"""把人工路线真值与生产回放的接受锚点按时间合并，输出逐点误差和每图指标。"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

import route_accuracy


OUTPUT_FIELDS = [
    "source_video", "source_video_sha256", "frame_index", "video_time", "map",
    "manual_x", "manual_y", "visible", "indoor", "occluded", "basis", "annotator",
    "alg_x", "alg_y", "alg_elapsed_ms", "alg_time_delta_s", "alg_confidence",
    "segment_id", "no_result", "error",
]


def load_truth(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_replays(paths):
    replays = {}
    for path in paths:
        with Path(path).open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        replays[data["map_name"]] = data
    return replays


def nearest_anchor(points, elapsed_ms, maximum_delta_ms):
    if not points:
        return None
    point = min(points, key=lambda item: abs(int(item["elapsed_ms"]) - elapsed_ms))
    return point if abs(int(point["elapsed_ms"]) - elapsed_ms) <= maximum_delta_ms else None


def merge(truth, replays, maximum_delta_seconds):
    output = []
    for source in truth:
        row = dict(source)
        replay = replays.get(row.get("map"))
        elapsed_ms = round(float(row["video_time"]) * 1000)
        point = nearest_anchor((replay or {}).get("raw_points", []), elapsed_ms,
                               round(maximum_delta_seconds * 1000))
        if point is None:
            row.update({"alg_x": None, "alg_y": None, "alg_elapsed_ms": None,
                        "alg_time_delta_s": None, "alg_confidence": None,
                        "segment_id": None, "no_result": True, "error": None})
        else:
            manual = row.get("manual_x") not in (None, "") and row.get("manual_y") not in (None, "")
            error = None
            if manual:
                error = math.hypot(float(row["manual_x"]) - float(point["x"]),
                                   float(row["manual_y"]) - float(point["y"]))
            row.update({
                "alg_x": point["x"], "alg_y": point["y"],
                "alg_elapsed_ms": point["elapsed_ms"],
                "alg_time_delta_s": round(abs(int(point["elapsed_ms"]) - elapsed_ms) / 1000, 3),
                "alg_confidence": point.get("confidence"),
                "segment_id": point.get("segment_id"), "no_result": False,
                "error": round(error, 6) if error is not None else None,
            })
        output.append(row)
    return output


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", required=True)
    parser.add_argument("--replay", action="append", required=True,
                        help="每张地图的 production replay JSON；可重复")
    parser.add_argument("--points-output", required=True)
    parser.add_argument("--metrics-output", required=True)
    parser.add_argument("--max-time-delta", type=float, default=12.0)
    args = parser.parse_args()

    rows = merge(load_truth(args.truth), load_replays(args.replay), args.max_time_delta)
    write_csv(args.points_output, rows)
    by_map = defaultdict(list)
    for row in rows:
        by_map[row.get("map") or "unknown"].append(row)
    metrics = {
        "max_time_delta_seconds": args.max_time_delta,
        "maps": {name: route_accuracy.summarize(items) for name, items in by_map.items()},
    }
    metrics["passed"] = bool(metrics["maps"]) and all(
        item["over_008"] == 0 and item["precision_le_002"] >= 0.90
        and item["eligible_coverage"] >= 0.60 for item in metrics["maps"].values()
    )
    target = Path(args.metrics_output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
