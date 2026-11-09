"""从录像里找玩家打开全地图的候选帧，供人工真值标注。

只输出得分最高的少量截图；不参与生产采集，也不会把录像或截图提交进仓库。
"""

from __future__ import annotations

import argparse
import heapq
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
import routes


def _read_gray(path: Path, width: int) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"读不到图片：{path}")
    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def find(video: Path, map_name: str, output: Path, step: float, keep: int) -> list[tuple]:
    cfg = routes.load_route_config()
    info = cfg["maps"][map_name]
    reference = _read_gray(Path(routes.ASSET_DIR) / info["file"], 640)
    detector = cv2.SIFT_create(nfeatures=1800, contrastThreshold=0.025)
    ref_points, ref_desc = detector.detectAndCompute(reference, None)
    if ref_desc is None:
        raise ValueError("地图没有足够特征")

    capture = cv2.VideoCapture(str(video))
    fps = capture.get(cv2.CAP_PROP_FPS) or 60.0
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frames / fps
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    best: list[tuple] = []
    timestamp = 0.0
    while timestamp < duration:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = capture.read()
        if not ok:
            timestamp += step
            continue
        height = max(1, round(frame.shape[0] * 640 / frame.shape[1]))
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (640, height),
                          interpolation=cv2.INTER_AREA)
        points, desc = detector.detectAndCompute(gray, None)
        good = []
        if desc is not None:
            for pair in matcher.knnMatch(desc, ref_desc, k=2):
                if len(pair) == 2 and pair[0].distance < 0.70 * pair[1].distance:
                    good.append(pair[0])
        # 打开全地图时应有大量分散在画面各处的地图特征。只看匹配数容易被 HUD 图标骗过。
        quadrants = 0
        spread = 0.0
        if good:
            query = np.float32([points[item.queryIdx].pt for item in good])
            quadrants = len({(p[0] >= 320, p[1] >= height / 2) for p in query})
            spread = float(np.ptp(query[:, 0]) * np.ptp(query[:, 1]) / max(1, 640 * height))
        score = len(good) * min(1.0, spread * 3) * (quadrants / 4)
        item = (score, timestamp, len(good), spread, frame)
        if len(best) < keep:
            heapq.heappush(best, item)
        elif score > best[0][0]:
            heapq.heapreplace(best, item)
        timestamp += step
    capture.release()

    output.mkdir(parents=True, exist_ok=True)
    ranked = sorted(best, reverse=True)
    for rank, (score, timestamp, matches, spread, frame) in enumerate(ranked, 1):
        label = f"{rank:02d}-{timestamp:08.2f}s-m{matches}-s{spread:.3f}-q{score:.1f}.jpg"
        cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tofile(output / label)
        print(label)
    return ranked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step", type=float, default=2.0)
    parser.add_argument("--keep", type=int, default=20)
    args = parser.parse_args()
    find(args.video, args.map, args.output, max(0.25, args.step), max(1, args.keep))


if __name__ == "__main__":
    main()
