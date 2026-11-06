"""下载并压缩行进路线使用的全地图底图；每张地图只存一份，不随对局复制。"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "maps"
USER_AGENT = "Evo-map-assets/0.1 (https://github.com/AugustHuangMX/Evo)"
MAPS = {
    "storm-point-s31.jpg": "https://apexlegends.wiki.gg/wiki/Special:Redirect/file/Storm_Point_MU2.png",
    "e-district-night-s31.jpg": "https://apexlegends.wiki.gg/wiki/Special:Redirect/file/E-District.png",
    "broken-moon-s31.jpg": "https://apexlegends.wiki.gg/wiki/Special:Redirect/file/Broken_Moon_MU1.png",
}


def download(name, url, force=False):
    target = DEST / name
    if target.is_file() and not force:
        print(f"已有：{name}")
        return target
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
    response.raise_for_status()
    image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    if image is None or min(image.shape[:2]) < 1500:
        raise ValueError(f"{name} 下载到的不是预期全地图（{len(response.content)} 字节）")
    image = cv2.resize(image, (1536, 1536), interpolation=cv2.INTER_AREA)
    encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])[1]
    DEST.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    encoded.tofile(part)
    os.replace(part, target)
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    print(f"写好：{name}（{len(encoded) / 2**20:.1f} MB，SHA-256 {digest}…）")
    return target


def main():
    parser = argparse.ArgumentParser(description="准备 Evo 行进路线用的全地图底图")
    parser.add_argument("--force", action="store_true", help="已经有的也重新下载")
    args = parser.parse_args()
    try:
        for name, url in MAPS.items():
            download(name, url, args.force)
    except (OSError, requests.RequestException, ValueError) as exc:
        print(f"地图没有准备好：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
