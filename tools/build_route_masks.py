"""从三张 S31 底图生成低分辨率硬障碍掩码。

这是开发期工具，不在对局或发行版运行。输出语义固定为 255=可走、0=硬障碍；阈值只针对
仓库内对应版本底图，换图必须重新生成并人工看叠加效果。
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MAPS = ROOT / "assets" / "maps"
SIDE = 512


def _read(name: str) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(MAPS / name, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(name)
    return cv2.resize(image, (SIDE, SIDE), interpolation=cv2.INTER_AREA)


def _large(mask: np.ndarray, minimum: int) -> np.ndarray:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    out = np.zeros_like(mask, dtype=np.uint8)
    for index in range(1, count):
        if int(stats[index, cv2.CC_STAT_AREA]) >= minimum:
            out[labels == index] = 255
    return out


def _finish(blocked: np.ndarray, *, minimum: int, dilate: int = 1) -> np.ndarray:
    kernel = np.ones((3, 3), np.uint8)
    blocked = cv2.morphologyEx(blocked.astype(np.uint8) * 255, cv2.MORPH_OPEN, kernel)
    blocked = _large(blocked > 0, minimum)
    if dilate:
        blocked = cv2.dilate(blocked, kernel, iterations=dilate)
    return np.where(blocked > 0, 0, 255).astype(np.uint8)


def broken_moon() -> np.ndarray:
    hsv = cv2.cvtColor(_read("broken-moon-s31.jpg"), cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    # 金棕色山体；红色细轨道会在连通域过滤阶段被删掉。
    rock = (h <= 32) & (s >= 96) & (v >= 48)
    # 地图外的青蓝水面。
    outside = (h >= 82) & (h <= 108) & (s >= 105) & (v >= 70)
    return _finish(rock | outside, minimum=75, dilate=1)


def e_district() -> np.ndarray:
    hsv = cv2.cvtColor(_read("e-district-night-s31.jpg"), cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    # 蓝紫海面、地图外山体和大块高亮屋顶。室内锚点会在赛后最多吸附 8 像素；
    # 无法吸附就断线，不拿直线穿楼。
    purple = (h >= 100) & (h <= 160) & (s >= 68) & (v >= 48)
    return _finish(purple, minimum=55, dilate=1)


def write(name: str, source: str, mask: np.ndarray, blocked: list[str]) -> None:
    png = MAPS / f"{name}-mask.png"
    cv2.imencode(".png", mask)[1].tofile(png)
    meta = {
        "_说明": "赛后路线用的共享粗障碍掩码；255=可走、0=硬障碍。找不到可信通路时断线。",
        "source": f"assets/maps/{source}",
        "side": SIDE,
        "blocked": blocked,
        "free_ratio": round(float((mask >= 128).mean()), 4),
        "generator": "tools/build_route_masks.py",
        "limits": ["粗掩码不能表达门、楼层、滑索、跳塔和载具", "底图版本变化后必须重建并用完整录像复核"],
    }
    (MAPS / f"{name}-mask.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name, meta["free_ratio"])


def main() -> None:
    write("broken-moon-s31", "broken-moon-s31.jpg", broken_moon(), ["地图外水面", "大块金棕色山体"])
    write("e-district-night-s31", "e-district-night-s31.jpg", e_district(), ["地图外海面和山体", "大块高亮屋顶"])


if __name__ == "__main__":
    main()
