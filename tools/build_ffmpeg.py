"""编录像用的精简版 ffmpeg：python tools/build_ffmpeg.py → dist/ffmpeg/（ffmpeg.exe、许可证、BUILD.txt）。

2026-11-20 用户定的（docs/录像.md 定下来的第 4 条）：Claude 自己编精简版，打进测试版的安装包。只在开发电脑上用，
要先装 MSYS2（winget install MSYS2.MSYS2 --location E:\\msys64；装在别处时设环境变量 MSYS2_ROOT）。

1. 装编译要用的 MSYS2 包（PACKAGES）
2. 下载 ffmpeg 的源码（ffmpeg.org），核对 SHA-256（SOURCE_SHA256：2026-11-20 下载时用 FFmpeg 的发布密钥
   FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8 核对过签名），解压到 build/ffmpeg/
3. configure：LGPL（不带 x264 等 GPL 的部分），全部静态链接（只有一个 ffmpeg.exe，不带 dll），先 --disable-everything，
   再只打开录像用得到的（ENABLE：按 recorder.py、panel_recordings.py、tests/test_recorder.py 用到的列；那边加了新的
   滤镜、编码器、格式，这里也要加，不然 test_recorder 会报错）→ make
4. 结果放 dist/ffmpeg/：ffmpeg.exe、licenses/（ffmpeg 的 LGPL 和静态链进去的库的许可证）、BUILD.txt（版本、源码地址、
   configure 参数：LGPL 要求拿到程序的人也能拿到源码、能自己重新编）
5. 自检：ENABLE 里列的都在 ffmpeg.exe 里
带 H.264 和 AV1（AV1 2026-11-26 加回来，用户定的：ffmpeg 的 TS 读写里没有 AV1，AV1 录的时候分段写成 MKV，见 docs/录像.md
定下来的第 6 条）。AV1 要解码器 libdav1d（截缩略图、读第一帧的时刻要解码；MSYS2 包 dav1d，BSD 许可，静态链进去）
build.py 打测试版时把 dist/ffmpeg 复制进 Evo\\ffmpeg（recorder.find_ffmpeg 找得到它）。
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "build" / "ffmpeg"  # 源码、编译的中间文件（build/ 不进 git）
OUT = ROOT / "dist" / "ffmpeg"  # 编好的（dist/ 不进 git）
MSYS2 = Path(os.environ.get("MSYS2_ROOT", r"E:\msys64"))

VERSION = "9.0.2"  # 和第 0 步实测用的 Gyan 版一样（docs/录屏实测.md）
SOURCE_URL = f"https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz"
SOURCE_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"

PACKAGES = [  # MSYS2 的 UCRT64 环境
    "make", "diffutils",
    "mingw-w64-ucrt-x86_64-gcc", "mingw-w64-ucrt-x86_64-nasm", "mingw-w64-ucrt-x86_64-pkgconf",
    "mingw-w64-ucrt-x86_64-ffnvcodec-headers",  # NVIDIA 显卡编码（NVENC）：只有头文件，运行时用显卡驱动里的
    "mingw-w64-ucrt-x86_64-amf-headers",  # AMD 显卡编码（AMF）：同上
    "mingw-w64-ucrt-x86_64-libvpl",  # Intel 显卡编码（QSV）：静态链进去
    "mingw-w64-ucrt-x86_64-dav1d",  # AV1 解码（截缩略图、读第一帧的时刻）：静态链进去
]

ENABLE = {  # configure 的 --enable-<种类>=…（--disable-everything 以后只打开这些）
    "filter": [
        "gfxcapture", "ddagrab",  # 抓画面：Windows Graphics Capture（只抓游戏窗口）、桌面复制
        "hwdownload", "hwmap", "format",  # 显卡里的画面拿到内存（量大小）、Intel 编码前换格式、换像素格式
        "settb", "setpts", "showinfo",  # 时间戳 = 抓到的时刻；读第一帧的时刻、画面大小
        "scale", "null", "anull", "aresample", "aformat",  # 缩略图缩小；ffmpeg 自己插的格式转换
        "color", "testsrc2", "realtime",  # 测试画面（检测编码器、tests/test_recorder.py）
        "amix", "volume",  # 开了麦克风时把两路声音混成第 1 条音轨（2026-11-21）
    ],
    "encoder": [
        "h264_nvenc", "h264_amf", "h264_qsv",  # 显卡编码：NVIDIA、AMD、Intel
        "av1_nvenc", "av1_amf", "av1_qsv",  # AV1（2026-11-26）：RTX 40、RX 7000、Arc 及以后的显卡才编得了
        "aac",  # 游戏声音
        "mjpeg",  # 缩略图 .jpg
        "wrapped_avframe", "pcm_s16le",  # -f null 和测试里读声音
    ],
    "decoder": ["h264", "libdav1d", "aac", "pcm_s16le", "pcm_f32le",
                "wrapped_avframe"],  # wrapped_avframe：测试里 -f lavfi -i testsrc2 出来的画面
    "muxer": ["mpegts", "matroska", "mp4", "mov", "null", "image2", "pcm_s16le"],  # pcm_s16le：测试里 -f s16le 读声音
    "demuxer": ["mpegts", "matroska", "concat", "mov", "wav"],  # matroska、concat：AV1 的分段 .mkv 按拼接列表接起来
    "indev": ["lavfi"],  # 测试里 -f lavfi -i testsrc2
    "protocol": ["file", "concat", "pipe"],  # concat:：H.264 分段的 .ts 首尾相连地读
    "parser": ["h264", "av1", "aac", "mjpeg"],
    "bsf": ["h264_mp4toannexb", "aac_adtstoasc", "extract_extradata", "av1_frame_split", "av1_frame_merge", "null"],
}

CONFIGURE = [
    "--disable-everything", "--disable-autodetect",  # 什么都不自动带：只要下面列的
    "--enable-static", "--disable-shared", "--pkg-config-flags=--static",
    "--extra-ldflags=-static",  # libgcc、libstdc++、winpthreads 也静态链进去：不带任何 dll
    "--extra-libs=-lstdc++",  # libvpl（Intel）是 C++ 写的，它的 pkg-config 没写要 C++ 运行库
    "--disable-doc", "--disable-ffplay", "--disable-ffprobe", "--disable-network", "--disable-debug",
    "--enable-w32threads", "--enable-d3d11va",  # 抓屏要 Direct3D 11
    "--enable-ffnvcodec", "--enable-nvenc", "--enable-amf", "--enable-libvpl", "--enable-libdav1d",
]

LICENSES = {  # dist/ffmpeg/licenses 里的文件名 → 从哪里复制（源码里的、MSYS2 装的）
    "ffmpeg-LICENSE.md": "src:LICENSE.md",
    "ffmpeg-COPYING.LGPLv2.1": "src:COPYING.LGPLv2.1",
    "libvpl-LICENSE": "msys:ucrt64/share/licenses/libvpl/LICENSE",
    "ffnvcodec-headers-LICENSE": "msys:ucrt64/share/licenses/ffnvcodec-headers/LICENSE",
    "amf-headers-LICENSE": "msys:ucrt64/share/licenses/amf-headers/LICENSE",
    "dav1d-COPYING": "msys:ucrt64/share/licenses/dav1d/COPYING",
    "dav1d-PATENTS": "msys:ucrt64/share/licenses/dav1d/PATENTS",
    "mingw-w64-winpthreads-COPYING": "msys:ucrt64/share/licenses/winpthreads/COPYING",
}


def msys_path(path):
    path = Path(path).resolve()
    return f"/{path.drive[0].lower()}{path.as_posix()[2:]}"


def bash(command, cwd):
    """在 MSYS2 的 UCRT64 环境里运行一条命令（出错就停）。"""
    env = {**os.environ, "MSYSTEM": "UCRT64", "CHERE_INVOKING": "1"}
    subprocess.run([str(MSYS2 / "usr" / "bin" / "bash.exe"), "-lc", command], cwd=cwd, env=env, check=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def get_source():
    tarball = WORK / "download" / f"ffmpeg-{VERSION}.tar.xz"
    if not tarball.exists():
        tarball.parent.mkdir(parents=True, exist_ok=True)
        print(f"下载 {SOURCE_URL}")
        part = tarball.with_suffix(".part")
        urllib.request.urlretrieve(SOURCE_URL, part)
        part.replace(tarball)
    if sha256(tarball) != SOURCE_SHA256:
        raise SystemExit(f"{tarball.name} 的 SHA-256 不对：删掉它重新下载；还不对就别用（源码可能被改过）")
    src = WORK / f"ffmpeg-{VERSION}"
    if not (src / "configure").exists():
        bash(f"tar -xJf {msys_path(tarball)}", WORK)
    return src


def configure_args():
    args = list(CONFIGURE)
    for kind, names in ENABLE.items():
        args.append(f"--enable-{kind}=" + ",".join(names))
    return args


LISTED_AS = {("muxer", "pcm_s16le"): "s16le"}  # configure 里的名字和 ffmpeg -muxers 列出来的不一样的


def self_check(exe):
    """ENABLE 里列的都在编好的 ffmpeg.exe 里（configure 缺依赖、名字写错时会悄悄不编某一项）。"""
    lists = {"filter": "-filters", "encoder": "-encoders", "decoder": "-decoders", "muxer": "-muxers",
             "demuxer": "-demuxers", "indev": "-devices", "protocol": "-protocols", "bsf": "-bsfs"}
    missing = []
    for kind, flag in lists.items():
        text = subprocess.run([str(exe), "-hide_banner", flag], capture_output=True, text=True).stdout
        words = set(re.findall(r"[A-Za-z0-9_]+", text))
        missing += [f"{kind} {name}" for name in ENABLE[kind] if LISTED_AS.get((kind, name), name) not in words]
    if missing:
        raise SystemExit("编好的 ffmpeg 里缺了：" + "、".join(missing))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not (MSYS2 / "usr" / "bin" / "bash.exe").exists():
        raise SystemExit(f"找不到 MSYS2（{MSYS2}）：winget install MSYS2.MSYS2 --location E:\\msys64，或设 MSYS2_ROOT")
    print("1. 装编译要用的 MSYS2 包")
    bash("pacman -S --needed --noconfirm " + " ".join(PACKAGES), ROOT)
    print("2. 源码")
    src = get_source()
    print("3. configure、make（几分钟到十几分钟）")
    args = configure_args()
    quoted = " ".join(f"'{a}'" for a in args)
    bash(f"./configure {quoted} && make -j{os.cpu_count() or 4}", src)
    exe = src / "ffmpeg.exe"
    print("4. 放进 dist/ffmpeg")
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "licenses").mkdir(parents=True)
    shutil.copy2(exe, OUT / "ffmpeg.exe")
    for name, where in LICENSES.items():
        kind, rel = where.split(":", 1)
        source = (src if kind == "src" else MSYS2) / rel
        if not source.exists():
            raise SystemExit(f"找不到许可证 {source}")
        shutil.copy2(source, OUT / "licenses" / name)
    (OUT / "BUILD.txt").write_text(
        f"Evo 录像用的精简版 ffmpeg {VERSION}（LGPL 2.1 或更新版本，见 licenses/）。\n"
        f"源码：{SOURCE_URL}（SHA-256 {SOURCE_SHA256}）\n"
        f"怎么编的：Evo 的 tools/build_ffmpeg.py（https://github.com/Evo-Apex/Evo），MSYS2 UCRT64、gcc，静态链接 "
        "libvpl（Intel）、dav1d（AV1 解码）；NVIDIA、AMD 的编码器只用头文件，运行时调用显卡驱动。只带 H.264 和 AV1。\n"
        f"configure 参数：\n  " + "\n  ".join(args) + "\n", encoding="utf-8")
    print("5. 自检")
    self_check(OUT / "ffmpeg.exe")
    size = (OUT / "ffmpeg.exe").stat().st_size
    print(f"\n完成：{OUT / 'ffmpeg.exe'}（{size / 2 ** 20:.1f} MB）")


if __name__ == "__main__":
    main()
