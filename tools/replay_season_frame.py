"""用一张用户主动提供的截图复现 4:3 数据页识别（开发时排查用，不在正常运行时保存截图）。

示例：
  python tools/replay_season_frame.py --image 数据页.png --aspect 4:3
  python tools/replay_season_frame.py --image 数据页.png --aspect auto --annotate 标注.png

显式 --aspect 4:3 时不会再因为截图是 1920×1080 就退回 16:9；找真实截图、核对卡在哪一步时用这个。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import code_path  # noqa: F401  （先加 code/ 到 import 路径）

import cv2
import game_aspect
import layout
import ocr
import season_replay
from ocr import load_config
from paths import CONFIG_DIR

CONFIG_NAME = "season_stats.json"


def load_frame(path):
    frame = cv2.imdecode(__import__("numpy").fromfile(path, dtype="uint8"), cv2.IMREAD_COLOR)
    if frame is None:
        raise SystemExit(f"读不出图片：{path}")
    return frame


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="复现一张截图上的 4:3 数据页识别")
    parser.add_argument("--image", required=True, help="用户主动提供的单张截图")
    parser.add_argument("--aspect", default="auto", choices=["auto", "16:9", "16:10", "4:3"],
                        help="游戏画面比例；auto 才读视频设置")
    parser.add_argument("--annotate", help="可选：输出 ROI 标注图（玩家名/ID 那块涂黑）")
    args = parser.parse_args(argv)

    frame = load_frame(args.image)
    detected = None
    if args.aspect == "auto":
        detected, _ = game_aspect.from_video_config()
    aspect, why = season_replay.resolve_aspect(args.aspect, frame, detected)
    layout.set_game_aspect(aspect)

    cfg = load_config(CONFIG_NAME)

    print(f"图：{Path(args.image)}")
    print(f"比例来源：{why}")
    engine = ocr.create_engine(ocr.SMALL_CROP_PARAMS)
    for line in season_replay.inspect(engine, frame, cfg, aspect, args.annotate):
        print(line)
    if args.annotate:
        print(f"标注图：{Path(args.annotate)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
