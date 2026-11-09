"""离线对比定位算法：原始 SIFT、CLAHE+SIFT、边缘+SIFT；只读录像，不写截图。"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np

import code_path  # noqa: F401
import routes
from replay_route_video import _cut_minimap


def _gray(image, size):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)


def _prepare(gray, variant):
    if variant == "clahe":
        return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    if variant == "edge":
        return cv2.Canny(gray, 60, 140)
    return gray


def _match(map_gray, crop_gray, cfg, variant):
    detector = cv2.SIFT_create(nfeatures=5000, contrastThreshold=0.025)
    map_img = _prepare(_gray(map_gray, int(cfg["reference_size"])), variant)
    crop = _prepare(_gray(crop_gray, int(cfg["sample_size"])), variant)
    mask = np.zeros_like(crop)
    center = crop.shape[0] // 2
    cv2.circle(mask, (center, center), round(crop.shape[0] * cfg["mask_radius"]), 255, -1)
    cv2.circle(mask, (center, center), round(crop.shape[0] * cfg["center_mask"]), 0, -1)
    kp1, des1 = detector.detectAndCompute(crop, mask)
    kp2, des2 = detector.detectAndCompute(map_img, None)
    if des1 is None or des2 is None or len(kp1) < cfg["min_matches"]:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des1, des2, k=2)
    good = [a for a, b in pairs if a.distance < cfg["ratio"] * b.distance]
    if len(good) < cfg["min_matches"]:
        return None
    src = np.float32([kp1[m.queryIdx].pt for m in good])
    dst = np.float32([kp2[m.trainIdx].pt for m in good])
    matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                  ransacReprojThreshold=cfg["ransac_px"])
    if matrix is None or inliers is None:
        return None
    inliers = inliers.reshape(-1).astype(bool)
    count = int(inliers.sum())
    ratio = count / len(good)
    if count < cfg["min_inliers"] or ratio < cfg["min_inlier_ratio"]:
        return None
    center_pt = np.array([crop.shape[0] / 2, crop.shape[0] / 2, 1.0])
    x, y = matrix @ center_pt
    x, y = x / map_img.shape[1], y / map_img.shape[0]
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    predicted = cv2.transform(src[inliers][None, :, :], matrix)[0]
    error = float(np.mean(np.linalg.norm(predicted - dst[inliers], axis=1)))
    confidence = min(1.0, count / (cfg["min_inliers"] * 2)) * ratio / (1 + error / 5)
    return {"x": round(float(x), 5), "y": round(float(y), 5), "confidence": round(confidence, 4),
            "matches": len(good), "inliers": count, "inlier_ratio": round(ratio, 3),
            "reprojection_error": round(error, 3),
            "scale": round(float(np.hypot(matrix[0, 0], matrix[1, 0])), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--times", default="")
    ap.add_argument("--start", type=float, default=45)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--step", type=float, default=60)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    cfg = json.loads((Path(routes.__file__).parent.parent / "config" / "routes.json").read_text(encoding="utf-8"))
    info = cfg["maps"][args.map]
    map_path = Path(routes.ASSET_DIR) / info["file"]
    map_gray = cv2.imdecode(np.fromfile(str(map_path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    times = [float(x) for x in args.times.split(",") if x.strip()] if args.times else None

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    duration = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) / fps
    end = args.end or duration
    if times is None:
        times = []
        t = args.start
        while t <= end:
            times.append(round(t, 1))
            t += args.step
    rows = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        crop = _cut_minimap(frame, cfg)
        row = {"time_s": t}
        for variant in ("baseline", "clahe", "edge"):
            started = time.perf_counter()
            row[variant] = _match(map_gray, crop, cfg["matcher"], variant)
            row[f"{variant}_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if row.get("baseline") and row.get("clahe"):
            row["baseline_clahe_distance"] = round(float(np.hypot(
                row["baseline"]["x"] - row["clahe"]["x"], row["baseline"]["y"] - row["clahe"]["y"])), 4)
        rows.append(row)
    cap.release()

    out = {"video": Path(args.video).name, "map": args.map, "map_file": info["file"],
           "map_version": info.get("version"), "duration_s": round(duration, 1), "rows": rows}
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output} ({len(rows)} frames)")


if __name__ == "__main__":
    main()
