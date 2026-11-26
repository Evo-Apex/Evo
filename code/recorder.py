"""录像：一局从头录到尾（独立的 ffmpeg 进程），录完拼成一个 MP4，旁边放同名的 .json 记这一局的事件（见 docs/录像.md）。

记录程序这样用：
    rec = Recording(ffmpeg, options, out_dir, log)
    rec.start(apex_pid)        一局开始
    rec.poll()                 对局中每秒一次：ffmpeg 退出了、卡住了就重开（不会卡住调用的人）
    rec.mark("knocked")        发生了什么：倒地、成盒、被救、冠军、击杀……（时刻自己记）
    rec.finish(info)           一局结束：停下、拼成 MP4、写 .json，返回 MP4 的路径（失败返回 None，分段文件留着）

怎么录（2026-11-19、20 实测定的，踩过的坑见 docs/录屏实测.md）：
- 抓画面：默认 Windows Graphics Capture 只抓 Apex 的窗口（ffmpeg 的 gfxcapture，抓的时候就能缩放）；抓不到换桌面复制
  （ddagrab，只能录原画面）。两种都是 Windows 的被动接口，不碰游戏（原则 1）。Apex 的窗口由 find_window 按进程号找，
  只问 Windows 窗口归哪个进程，不打开游戏进程
- 编码：显卡硬件编码（NVENC / AMF / QSV），画面不出显卡；没有硬件编码器就不录。H.264（默认）或 AV1（2026-11-26 加回来，
  RTX 40、RX 7000、Arc 及以后的显卡才编得了，选了编不了时改用 H.264）。开 B 帧（config 的 b_frames，
  2026-11-21 实测同样码率略清楚、文件不变大；显卡不支持时自动不开）
- 时间：每一帧的时间戳 = 抓到它时电脑的时钟 − 开始的时刻 t0（微秒；-enc_time_base filter 不取整，取整会丢帧），
  分段文件里原样保存（TS 用 -mpegts_copyts 1；MKV 本来就照存，只到毫秒）。事件、声音用同一个起点，所以都能直接对上画面
- 文件：每开一次 ffmpeg 写一个分段文件（程序崩了也读得回来）；断了重开就再写一个。结束时接起来拼成 MP4，不重新编码。
  H.264 写 .ts，首尾相连地读（concat: 协议；不用 ffmpeg 的拼接列表，帧间隔不均匀时时间会越接越偏）。AV1 写 .mkv
  （ffmpeg 的 TS 读写里没有 AV1）：MKV 不能首尾相连，按拼接列表接，每段写明"到下一段第一帧有多长"（duration），
  空档原样保留、时间不偏（2026-11-26 实测，见 docs/录像.md）
- 断了重开：切窗口、切显示模式、弹管理员确认框时桌面复制会失效，ffmpeg 直接退出。按 restart_wait_seconds 等一下再开
  （马上重开常被拒）。断掉的那几秒记进 .json
- 声音：process_audio 按进程录 Apex 的声音，每一包带时刻。拼 MP4 时，算出 MP4 第一帧那一刻对应 WAV 里第几个采样再对齐
- ffmpeg 放进作业对象（process_info.kill_with_me）：Evo 崩了、被结束，它也跟着退，不会一直往硬盘写；优先级"正常"
  （记录程序自己是"低于正常"，子进程默认跟着低，CPU 紧张时会掉帧）
"""

import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import game_lang
import paths
import process_info

CONFIG_NAME = "recording.json"
FALLBACK_CODEC = "h264"  # 选的编码格式这台电脑编不了时改用它（哪里都能放）
WORK_DIR_NAME = "_recording"  # 录的时候的分段文件放在录像文件夹里的这个子文件夹（结束后删掉）
# 每种编码格式的分段文件写成什么（没写的 .ts）。AV1 写 MKV：ffmpeg 的 TS 读写里没有 AV1（2026-11-20 看源码）。
# H.264 照旧 .ts（2026-11-26 用户定的：0.7 验证过的不动；H.264 写进 MKV 时间只到毫秒，开头几帧会撞在同一个时刻）。
# ffmpeg 被硬杀（Evo 崩了、卡住被结束）时 MKV 读得回多少（2026-11-26 实测）：-cluster_time_limit 让它每 1 秒写完一块
# （不加时按关键帧 2 秒一块），-flush_packets 1 让写完的马上落到硬盘（不加时攒满 256 KB 才写：录 1.5 秒就被杀，文件是空的）。
# 两个都加：录 1.5 秒杀掉读得回 1 秒、录 4 秒读得回 3 秒，最多丢约 1 秒。TS 不用加（本来就随写随落盘）
SEGMENTS = {"av1": (".mkv", ["-f", "matroska", "-cluster_time_limit", "1000", "-flush_packets", "1"])}
TS_SEGMENT = (".ts", ["-f", "mpegts", "-mpegts_copyts", "1"])
SEGMENT_SUFFIXES = (".ts", ".mkv")
CREATE_NEW_PROCESS_GROUP = 0x00000200  # 终端里按 Ctrl+C 不会直接传给 ffmpeg，由我们发 q 让它收尾
NORMAL_PRIORITY_CLASS = 0x00000020
CREATE_NO_WINDOW = 0x08000000
AUDIO_RATE = 48000
# 拼 MP4 时保留原来的时间戳（-copyts），只把开头挪到 0（-start_at_zero）：断了重开留下的空档原样保留，事件、声音才对得上。
# 不加的话，MPEG-TS 里时间跳了 10 秒以上 ffmpeg 会当成出错、把空档挤掉（-dts_delta_threshold 默认 10 秒）
KEEP_GAPS = ["-copyts", "-start_at_zero", "-dts_delta_threshold", "1000000"]


def load_rules():
    return game_lang.load(CONFIG_NAME)


def find_ffmpeg(preferred=None, data_dir=None):
    """ffmpeg.exe 的路径，找不到返回 None。依次看：preferred（设置里记的）、Evo 自带的（测试版的安装包里
    Evo\\ffmpeg\\ffmpeg.exe，tools/build_ffmpeg.py 编的精简版）、数据文件夹里的 ffmpeg\\ffmpeg.exe、
    环境变量 EVO_FFMPEG、PATH、winget 装的（Gyan.FFmpeg，开发时用）。正式版不带 ffmpeg：一般都找不到。"""
    candidates = [preferred, str(paths.EXE_DIR / "ffmpeg" / "ffmpeg.exe")]
    if data_dir:
        candidates.append(str(Path(data_dir) / "ffmpeg" / "ffmpeg.exe"))
    candidates += [os.environ.get("EVO_FFMPEG"), shutil.which("ffmpeg")]
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe")
    candidates += sorted(glob.glob(pattern))[-1:]
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    return None


def run_ffmpeg(ffmpeg, args, timeout=30, cwd=None):
    """跑一条 ffmpeg 命令（不弹窗口），返回 (退出码, 标准错误的文字)。超时返回 (None, "超时")。"""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, timeout=timeout, cwd=cwd,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, "超时"
    return r.returncode, r.stderr.decode("utf-8", "replace")


@dataclass
class Options:
    """怎么录（设置页的选项，没设过的用 config/recording.json 的默认）。"""
    source: str = "window"  # window = 只抓游戏窗口（Windows Graphics Capture）；desktop = 桌面复制；lavfi:… = 测试用的画面
    size: str = "1080p"  # config 里 sizes 的一项：native / 1080p / 720p（默认 1080p 60 帧标准：2026-11-20 用户看样片选的）
    fps: int = 60
    codec: str = "h264"  # config 里 encoders 的一项：h264（默认，2026-11-26 用户定的：先在游戏里试过 AV1 再说）/ av1
    quality: str = "standard"  # config 里 bits_per_pixel 的一项：best / high / standard / small
    audio: bool = True
    audio_source: str = "game"  # 声音：game = 只录 Apex；system = 电脑上所有的声音（系统内录，2026-11-21 用户要的两档）
    mic: bool = False  # 同时录麦克风（默认关）：开了以后第 1 条音轨是混好的，原声各自另存一条
    mic_volume: int = 100  # 麦克风混进第 1 条音轨时的音量（%，设置页的滑块，0–200）；麦克风原声那条不动
    extra: dict = field(default_factory=dict)  # 测试用：直接指定 encoder、bitrate


AUDIO_SOURCES = ("game", "system")
MIC_VOLUME = (0, 200, 5)  # 麦克风音量滑块：最小、最大、一格（%）
TRACK_TITLES = {"mix": "混音", "game": "游戏", "system": "电脑上所有的声音", "mic": "麦克风"}  # MP4 里每条音轨的名字


def track_title_args(tracks):
    """给 MP4 里每条音轨写上名字的 ffmpeg 参数（tracks：.json 里 audio 的 tracks，如 ["mix", "game", "mic"]）。
    title 在 MP4 里是 name；有的播放器显示 handler_name，两个都写。剪高光（highlights.py）时照抄整局的。"""
    args = []
    for i, title in enumerate(tracks or []):
        name = TRACK_TITLES.get(title, title)
        args += [f"-metadata:s:a:{i}", f"title={name}", f"-metadata:s:a:{i}", f"handler_name={name}"]
    return args


def options_from(settings, rules=None):
    """设置（settings.json 的内容）→ Options。没设过的、配置里已经没有的那一档（比如以前的 "low"）用默认。"""
    rules = rules or load_rules()
    default = Options()

    def pick(key, allowed, fallback, cast=str):
        try:
            value = cast(settings.get(key))
        except (TypeError, ValueError):
            return fallback
        return value if value in allowed else fallback
    return Options(source=settings.get("recording_source", default.source),
                   size=pick("recording_size", rules["sizes"], default.size),
                   fps=pick("recording_fps", rules["fps"], default.fps, int),
                   codec=pick("recording_codec", rules["encoders"], default.codec),
                   quality=pick("recording_quality", rules["bits_per_pixel"], default.quality),
                   audio=bool(settings.get("recording_audio", default.audio)),
                   audio_source=pick("recording_audio_source", AUDIO_SOURCES, default.audio_source),
                   mic=settings.get("recording_mic") is True,
                   mic_volume=pick("recording_mic_volume", range(MIC_VOLUME[0], MIC_VOLUME[1] + 1), default.mic_volume, int))


_user32 = ctypes.WinDLL("user32")
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def find_window(pid):
    """pid 这个进程最大的那个看得见的顶层窗口（窗口句柄），没有返回 None。

    只问 Windows"这个窗口归哪个进程"（GetWindowThreadProcessId），不打开那个进程（原则 1）。不用 ffmpeg 的
    window_exe：它要对每个窗口的进程（包括 Apex）打开一个查询句柄读程序名（2026-11-20 看 ffmpeg 源码发现的）。
    进程号由记录程序给（game_detect 从进程列表里找的，也不打开进程）。"""
    if not pid:
        return None
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
                if area > 0:
                    found.append((area, hwnd))
        return True

    _user32.EnumWindows(_ENUM_PROC(visit), 0)
    return max(found)[1] if found else None


def source_filter(options, rules, size=None, hwnd=None):
    """抓画面的滤镜（ffmpeg filter_complex 的开头）。size=(宽, 高)：Windows Graphics Capture 抓的时候缩放。
    只抓游戏窗口要给窗口句柄 hwnd（find_window），没有返回 None。"""
    if options.source.startswith("lavfi:"):
        return options.source[len("lavfi:"):] + ",format=nv12"
    if options.source == "desktop":
        return f"ddagrab=framerate={options.fps}:draw_mouse=0"
    if not hwnd:
        return None
    scale = f":width={size[0]}:height={size[1]}:resize_mode=scale_aspect:scale_mode=bicubic" if size else ""
    return f"gfxcapture=hwnd={hwnd}:max_framerate={options.fps}:capture_cursor=0{scale}"


def capture_size(ffmpeg, options, rules, hwnd=None):
    """这种抓法抓到的画面多大：(宽, 高)，抓不到返回 (None, 错误信息)。"""
    graph = source_filter(options, rules, hwnd=hwnd)
    if graph is None:
        return None, "找不到游戏的窗口"
    if not options.source.startswith("lavfi:"):
        graph += ",hwdownload,format=bgra"
    code, err = run_ffmpeg(ffmpeg, ["-filter_complex", graph + ",showinfo", "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r" s:(\d+)x(\d+)", err or "")
    if m:
        return (int(m[1]), int(m[2])), None
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    return None, lines[-1] if lines else f"退出码 {code}"


def bitrate(rules, width, height, fps, codec, quality):
    """码率（bps）= 宽 × 高 × 帧数 × 每像素的位数 × 编码格式的系数（见 config/recording.json）。"""
    return int(width * height * fps * rules["bits_per_pixel"][quality] * rules["codec_factor"][codec])


def b_frames_wanted(rules, encoder):
    """这个编码器想开几个 B 帧（config 的 b_frames，按显卡厂家：nvenc / amf / qsv，没写的不开）。"""
    return int(rules.get("b_frames", {}).get(encoder.rsplit("_", 1)[1], 0))


def encoder_args(encoder, fps, rate, keyframe_seconds, b_frames=0):
    """关键帧只按时间放：-force_key_frames 每 keyframe_seconds 秒一个。-g（按帧数）放宽到两倍，只在按时间的那套
    失灵时兜底：两套一样宽时，画面每秒稍多于 fps 帧会让第 fps × keyframe_seconds 帧比整秒早一点到，
    两套各放一个、关键帧成对（2026-11-21 实测，见 docs/录像.md"实测踩过的坑"）。"""
    args = ["-c:v", encoder, "-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2),
            "-g", str(fps * keyframe_seconds * 2), "-bf", str(b_frames),
            "-force_key_frames", f"expr:gte(t,n_forced*{keyframe_seconds})"]
    family = encoder.rsplit("_", 1)[1]
    if family == "nvenc":
        args += ["-preset", "p4", "-rc", "vbr", "-forced-idr", "1"]
        if b_frames:  # B 帧也当参照（2026-11-21 实测比不加略好；老显卡不支持时检测编码器那一步会退回不开 B 帧）
            args += ["-b_ref_mode", "middle"]
    elif family == "amf":
        args += ["-quality", "balanced", "-rc", "vbr_peak"]
    elif family == "qsv":
        args += ["-preset", "medium"]
    return args


def gpu_chain(encoder, source):
    """抓到的画面交给编码器之前：NVENC、AMF 直接收显卡里的纹理；Intel 要先映射成 QSV 的格式。测试画面在内存里，不用管。"""
    if encoder.endswith("_qsv") and not source.startswith("lavfi:"):
        return ",hwmap=derive_device=qsv,format=qsv"
    return ""


_encoder_cache = {}
_no_b_frames = set()  # (ffmpeg, 编码器)：开 B 帧时编不了、不开才行的（老显卡）


def pick_encoder(ffmpeg, options, rules, hwnd=None):
    """这台电脑上这种编码格式能用的硬件编码器（按 config 里的顺序试，结果记住），都不能用返回 None。
    每个编码器先按 config 开 B 帧试，编不了再不开 B 帧试一次（不开才行的记进 _no_b_frames，见 b_frames）。"""
    key = (ffmpeg, options.source, options.codec)
    if key not in _encoder_cache:
        _encoder_cache[key] = None
        for encoder in rules["encoders"].get(options.codec, []):  # 配置里没有这种格式：当作编不了
            graph = source_filter(options, rules, hwnd=hwnd) + gpu_chain(encoder, options.source)
            for bf in dict.fromkeys([b_frames_wanted(rules, encoder), 0]):
                code, _ = run_ffmpeg(ffmpeg, ["-loglevel", "error", "-filter_complex", graph, "-frames:v", "20",
                                              *encoder_args(encoder, 30, 4_000_000, 2, bf), "-f", "null", "-"], timeout=20)
                if code == 0:
                    _encoder_cache[key] = encoder
                    if bf == 0 and b_frames_wanted(rules, encoder):
                        _no_b_frames.add((ffmpeg, encoder))
                    break
            if _encoder_cache[key]:
                break
    return _encoder_cache[key]


def b_frames(ffmpeg, encoder, rules):
    """录的时候开几个 B 帧：按 config；检测编码器时发现开了编不了的，不开。"""
    return 0 if (ffmpeg, encoder) in _no_b_frames else b_frames_wanted(rules, encoder)


def read_progress(path):
    """ffmpeg -progress 写的最后一段完整的：{键: 值}。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    blocks = text.split("progress=")
    if len(blocks) < 2:
        return {}
    return dict(line.split("=", 1) for line in blocks[-2].strip().splitlines() if "=" in line)


def first_frame_time(ffmpeg, path):
    """一个分段文件里第一帧的时间戳（秒，从 t0 算起：TS 录的时候用了 -mpegts_copyts、MKV 本来就照存，文件里就是原来的时间）。
    用整数的 pts × 时间单位算：showinfo 的 pts_time 只有 6 位有效数字（1000 秒以后只到 0.01 秒，拼 AV1 时每段的时长会差）。
    读不到返回 None。"""
    _, err = run_ffmpeg(ffmpeg, ["-copyts", "-i", str(path), "-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"])
    base = re.search(r"config in time_base: (\d+)/(\d+)", err or "")
    pts = re.search(r"\bn:\s*0\s+pts:\s*(-?\d+)", err or "")
    if base and pts and int(base[2]):
        return int(pts[1]) * int(base[1]) / int(base[2])
    m = re.search(r"pts_time:([\d.]+)", err or "")
    return float(m[1]) if m else None


def segment_parts(work):
    """一局录的分段文件（按顺序，空的不算）。"""
    return [p for p in sorted(Path(work).glob("part_*")) if p.suffix in SEGMENT_SUFFIXES and p.stat().st_size > 0]


def joined_input(ffmpeg, parts, work):
    """把分段文件接起来读的 ffmpeg 输入参数（在 work 里运行），和第一帧的时刻（秒，从 t0 算起）：(参数, 时刻)。
    读不出第一帧的时刻返回 (None, None)。两种都保留断了重开留下的空档（KEEP_GAPS），MP4 从第一帧算 0。
    .ts：首尾相连地读（concat: 协议）。.mkv（AV1）：拼接列表，每段写明到下一段第一帧有多长（duration）：拼接列表本来按每段
    自己的长度接、把空档挤掉（帧间隔不均匀时还越接越偏），写明以后下一段正好从它原来的时刻开始。读不出时刻的段（硬杀时
    还没写出一帧）不要。"""
    if parts[0].suffix == ".ts":
        return [*KEEP_GAPS, "-i", "concat:" + "|".join(p.name for p in parts)], first_frame_time(ffmpeg, parts[0])
    timed = [(p, t) for p in parts if (t := first_frame_time(ffmpeg, p)) is not None]
    if not timed:
        return None, None
    lines = ["ffconcat version 1.0"]
    for i, (part, t) in enumerate(timed):
        lines.append(f"file '{part.name}'")
        if i + 1 < len(timed):
            lines.append(f"duration {timed[i + 1][1] - t:.6f}")
    (Path(work) / "parts.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [*KEEP_GAPS, "-f", "concat", "-safe", "0", "-i", "parts.txt"], timed[0][1]


def audio_offset(packets, rate, start_qpc):
    """MP4 第一帧那一刻（QPC 秒）对应 WAV 里的哪个时刻（秒）：用离它最近的一包声音的时刻算，不从 WAV 开头数
    （声卡的时钟和电脑的时钟会慢慢差开）。packets：[(第几个采样, 这一包开头的 QPC 秒)]。负数 = 声音比画面晚开始。"""
    index, when = min(packets, key=lambda p: abs(p[1] - start_qpc))
    return (index + (start_qpc - when) * rate) / rate


def align_wav(src, dst, offset):
    """把 WAV 挪到和画面同一个起点：offset 秒（audio_offset 算的）为正就去掉开头这么多，为负就在前面补这么多静音。
    一块一块地拷，不把整段读进内存（一局 20 分钟约 230 MB）。"""
    import wave
    with wave.open(str(src), "rb") as r, wave.open(str(dst), "wb") as w:
        w.setparams(r.getparams())
        shift = round(offset * r.getframerate())
        if shift >= 0:
            r.setpos(min(shift, r.getnframes()))
        else:
            w.writeframes(bytes(-shift * r.getsampwidth() * r.getnchannels()))
        while chunk := r.readframes(AUDIO_RATE * 10):
            w.writeframes(chunk)


def mp4_duration(path):
    """MP4 有多长（秒），读不出返回 None。只读文件开头（录像拼好时加了 +faststart）的 moov/mvhd，不用 ffmpeg。"""
    import struct
    try:
        with open(path, "rb") as f:
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                size, kind = struct.unpack(">I4s", head)
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0] - 8
                if kind == b"moov":
                    body = f.read(min(size - 8, 1 << 16))
                    at = body.find(b"mvhd")
                    if at < 0:
                        return None
                    version = body[at + 4]
                    if version == 1:
                        scale, length = struct.unpack(">IQ", body[at + 24:at + 36])
                    else:
                        scale, length = struct.unpack(">II", body[at + 16:at + 24])
                    return length / scale if scale else None
                if size < 8:
                    return None
                f.seek(size - 8, 1)
    except (OSError, struct.error, IndexError):
        return None


def make_thumbnail(ffmpeg, video, at, out=None, width=480):
    """在录像的 at 秒处截一张小图（JPG，宽 width），存在录像旁边同名的 .jpg。成功返回路径。"""
    out = Path(out or Path(video).with_suffix(".jpg"))
    code, _ = run_ffmpeg(ffmpeg, ["-y", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(video),
                                  "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)], timeout=60)
    return out if code == 0 and out.exists() else None


def thumbnail_time(events, duration):
    """缩略图截哪一刻：最后一个事件的前 1 秒（冠军、成盒那一下），没有事件取 40% 处。"""
    if events:
        return max(0.0, events[-1]["t"] - 1)
    return (duration or 0) * 0.4


def safe_name(text):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("_") or "录像"


class RecordingUnavailable(Exception):
    """这台电脑录不了（找不到 ffmpeg、没有硬件编码器、抓不到画面），说明在 str(e) 里。"""


class Recording:
    """一局的录像。out_dir：录像文件夹（MP4 放进 out_dir\\年-月\\）。clock：只给测试换（time.time）。"""

    def __init__(self, ffmpeg, options, out_dir, log=print, rules=None, audio_factory=None, clock=time.time):
        self.ffmpeg, self.options, self.out_dir, self.log = ffmpeg, options, Path(out_dir).resolve(), log
        self.rules = rules or load_rules()
        self.audio_factory = audio_factory
        self.clock = clock
        self.proc = None
        self.audio = None  # 游戏（或电脑上所有的声音）
        self.mic = None  # 麦克风
        self.audio_note = None  # 拼 MP4 时每条音轨的说明（写进 .json）
        self.parts = 0
        self.events = []
        self.hud_changes = []  # 右上角变了的时刻（秒，从 t0 算起）：以后从录像里找击杀用
        self.outages = []  # [开始, 结束]（秒，从 t0 算起）：ffmpeg 断了、重开之间录不到的
        self.restarts = 0
        self.stopping = False
        self._last_frames = (0, 0.0)  # (进度里的帧数, 它最后一次变的时刻)
        self._down_since = None  # 断了从什么时候起（秒，从 t0 算起）
        self._next_try = None  # 下一次重开的时刻（time.monotonic）

    # ---------- 开始、对局中 ----------

    def start(self, apex_pid=None):
        """开始录。录不了抛 RecordingUnavailable。"""
        if not self.ffmpeg:
            raise RecordingUnavailable("找不到 ffmpeg")
        opts = self.options
        self.pid = apex_pid
        size = self.rules["sizes"][opts.size]
        if size and opts.source == "desktop":
            self.log("录像：桌面复制不能在抓的时候缩放，按原画面录")
            size = None
        hwnd = find_window(apex_pid) if opts.source == "window" else None
        grabbed, why = capture_size(self.ffmpeg, opts, self.rules, hwnd)
        if grabbed is None and opts.source == "window":
            self.log(f"录像：只抓游戏窗口抓不到（{why}），改用桌面复制")
            opts.source, size = "desktop", None
            grabbed, why = capture_size(self.ffmpeg, opts, self.rules)
        if grabbed is None:
            raise RecordingUnavailable(f"抓不到画面：{why}")
        self.encoder = opts.extra.get("encoder") or pick_encoder(self.ffmpeg, opts, self.rules, hwnd)
        if not self.encoder and opts.codec != FALLBACK_CODEC:  # 设置里选了 AV1、这块显卡编不了：改用 H.264，不要整局不录
            self.log(f"录像：这台电脑编不了 {opts.codec}，改用 {FALLBACK_CODEC}")
            opts.codec = FALLBACK_CODEC
            self.encoder = pick_encoder(self.ffmpeg, opts, self.rules, hwnd)
        if not self.encoder:
            raise RecordingUnavailable("这台电脑上没有能用的显卡硬件编码器")
        self.b_frames = b_frames(self.ffmpeg, self.encoder, self.rules)
        self.size = tuple(size) if size and opts.source == "window" else grabbed
        self.bitrate = opts.extra.get("bitrate") or bitrate(self.rules, *self.size, opts.fps, opts.codec, opts.quality)
        self.started = datetime.now()
        self.t0_us, self.t0_qpc = time.time_ns() // 1000, time.perf_counter()
        self.work = self.out_dir / WORK_DIR_NAME / self.started.strftime("%Y%m%d_%H%M%S")
        self.work.mkdir(parents=True, exist_ok=True)
        if opts.audio and self.rules.get("audio", True):
            if opts.audio_source == "system":
                self._start_audio("system")
            elif apex_pid:
                self._start_audio(apex_pid)
            if opts.mic:
                self._start_audio("mic")
        self._launch()
        sounds = ([{"system": "电脑上所有的声音"}.get(opts.audio_source, "游戏声音")] if self.audio else []) + \
            (["麦克风"] if self.mic else [])
        self.log(f"录像：开始（{'只抓游戏窗口' if opts.source == 'window' else '桌面复制' if opts.source == 'desktop' else '测试画面'}，"
                 f"{self.size[0]}×{self.size[1]} {opts.fps} 帧，{self.encoder}，{self.bitrate / 1e6:.0f} Mbps，"
                 f"{f'B 帧 {self.b_frames}' if self.b_frames else '不开 B 帧'}"
                 f"{'，录' + '和'.join(sounds) if sounds else ''}）")

    def _start_audio(self, source):
        """开一路声音：source 是 Apex 的进程号、"system"（电脑上所有的声音）或 "mic"（麦克风）。录不了只记一句，不影响画面。"""
        factory = self.audio_factory
        if factory is None:
            import process_audio
            factory = process_audio.ProcessAudio
        mic = source == "mic"
        audio = factory(source, self.work / ("mic.wav" if mic else "audio.wav"))
        audio.start()
        audio.ready.wait(15)
        if audio.error:
            what = "麦克风" if mic else {"system": "电脑上的声音"}.get(source, "游戏声音")
            self.log(f"录像：{what}录不了（{audio.error}）")
            return
        if mic:
            self.mic = audio
        else:
            self.audio = audio

    def since_t0(self):
        return self.clock() - self.t0_us / 1e6

    def _command(self):
        """开 ffmpeg 的命令；只抓游戏窗口而窗口现在找不到（游戏切换显示模式时会重建窗口）返回 None。"""
        n = self.parts
        opts = self.options
        size = self.size if opts.source == "window" and self.rules["sizes"][opts.size] else None
        source = source_filter(opts, self.rules, size, find_window(self.pid) if opts.source == "window" else None)
        if source is None:
            return None
        graph = (source + f",settb=AVTB,setpts=RTCTIME-{self.t0_us}"
                 + gpu_chain(self.encoder, opts.source) + "[v]")
        suffix, muxer = SEGMENTS.get(opts.codec, TS_SEGMENT)
        return [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-stats_period", "1",
                "-progress", str(self.work / f"progress_{n}.txt"), "-filter_complex", graph, "-map", "[v]",
                *encoder_args(self.encoder, opts.fps, self.bitrate, self.rules["keyframe_seconds"], self.b_frames),
                "-fps_mode", "vfr", "-enc_time_base:v", "filter",
                *muxer, str(self.work / f"part_{n:03d}{suffix}")]

    def _launch(self):
        cmd = self._command()
        if cmd is None:  # 窗口还没出来：当成断了，过一会儿再试
            self.proc = None
            if self._down_since is None:
                self._down_since = self.since_t0()
                self.log("录像：找不到游戏的窗口，等它出来")
            waits = self.rules["restart_wait_seconds"]
            self._next_try = time.monotonic() + waits[min(self.restarts, len(waits) - 1)]
            self.restarts += 1
            return
        with open(self.work / f"ffmpeg_{self.parts}.log", "wb") as err:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                         creationflags=CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW)
        if not process_info.kill_with_me(self.proc):
            self.log("录像：没能把 ffmpeg 放进作业对象（Evo 被强行结束时它不会跟着退）")
        self.progress = self.work / f"progress_{self.parts}.txt"
        self.parts += 1
        self._last_frames = (0, time.monotonic())

    def _last_error(self):
        try:
            lines = (self.work / f"ffmpeg_{self.parts - 1}.log").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return next((line.strip() for line in reversed(lines) if line.strip()), "")

    def poll(self):
        """对局中每秒调用一次：ffmpeg 退出了或卡住了就结束它，等一下再重开（不阻塞）。返回现在录着没有。"""
        if self.stopping or self.proc is None:
            return False
        now = time.monotonic()
        if self._next_try is not None:  # 在等着重开
            if now < self._next_try:
                return False
            self._next_try = None
            self._launch()
            return False
        if self.proc is None:
            return False
        frames = int(read_progress(self.progress).get("frame", 0) or 0)
        if frames != self._last_frames[0]:
            self._last_frames = (frames, now)
            if self._down_since is not None:  # 重开以后第一次有新帧
                self.outages.append([round(self._down_since, 2), round(self.since_t0(), 2)])
                self.log(f"录像：接上了（断了 {self.outages[-1][1] - self.outages[-1][0]:.1f} 秒）")
                self._down_since = None
                self.restarts = 0
            return True
        dead = self.proc.poll() is not None
        limit = self.rules["stall_seconds_window" if self.options.source == "window" else "stall_seconds"]
        stuck = now - self._last_frames[1] > limit
        if not (dead or stuck):
            return True
        why = f"ffmpeg 退出了（{self._last_error() or self.proc.returncode}）" if dead else "ffmpeg 卡住了"
        if self._down_since is None:
            self._down_since = self.since_t0() - (now - self._last_frames[1])  # 从最后一次有新帧算起
            self.log(f"录像：{why}，重开")
        self._kill()
        waits = self.rules["restart_wait_seconds"]
        self._next_try = now + waits[min(self.restarts, len(waits) - 1)]
        self.restarts += 1
        return False

    def mark(self, kind, **info):
        """记一个事件（倒地、成盒、被救、冠军、击杀……），时刻用现在。info 是附带的说明（如 kills=3）。
        返回记下的那一项（之后还能改，比如击倒改成助攻击倒），还没开始录返回 None。"""
        if hasattr(self, "t0_us"):
            self.events.append({"t": round(self.since_t0(), 3), "kind": kind, **info})
            return self.events[-1]
        return None

    def hud_change(self):
        """右上角变了（击杀、伤害、剩余小队数……）：记下时刻。"""
        if hasattr(self, "t0_us"):
            self.hud_changes.append(round(self.since_t0(), 2))

    # ---------- 结束 ----------

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass

    def _stop_ffmpeg(self):
        """让 ffmpeg 收尾（写完最后几帧）再退，15 秒还不退就结束它。"""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self._kill()

    def finish(self, info=None):
        """停下，拼成 MP4、写 .json，删掉分段文件。返回 MP4 的路径；拼不成返回 None（分段文件留在 _recording 里）。
        info：这一局的说明（如 {"match_id": 12, "mode": "排位", "legend": "恶灵"}），写进 .json，也用来起文件名。"""
        self.stopping = True
        self._stop_ffmpeg()
        if self._down_since is not None:
            self.outages.append([round(self._down_since, 2), round(self.since_t0(), 2)])
        for audio in (self.audio, self.mic):
            if audio:
                audio.stop()
        parts = segment_parts(self.work)
        if not parts:
            self.log("录像：一帧都没录到")
            shutil.rmtree(self.work, ignore_errors=True)
            return None
        joined, start = joined_input(self.ffmpeg, parts, self.work)
        if start is None:
            self.log(f"录像：读不到第一帧的时间，分段文件留在 {self.work}")
            return None
        info = info or {}
        label = "_".join(safe_name(info[k]) for k in ("mode", "legend") if info.get(k))
        folder = self.out_dir / self.started.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{self.started:%Y-%m-%d_%H%M%S}{'_' + label if label else ''}.mp4"
        cmd = ["-y", *joined] + self._audio_args(start)
        audio_note = self.audio_note
        cmd += ["-c:v", "copy", "-movflags", "+faststart", str(out)]
        code, err = run_ffmpeg(self.ffmpeg, cmd, timeout=600, cwd=self.work)
        if code != 0 or not out.exists():
            self.log(f"录像：拼成 MP4 失败（{(err or '').strip().splitlines()[-1:] or code}），分段文件留在 {self.work}")
            return None
        meta = {
            "version": 1, "video": out.name, "started": self.started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"), "match": info,
            "first_frame": round(start, 4),  # MP4 第一帧在开始录以后第几秒（下面的时刻都已经减掉它）
            "events": [{**e, "t": round(e["t"] - start, 3)} for e in self.events],  # 时刻：MP4 里的位置（秒）
            "outages": [[round(a - start, 2), round(b - start, 2)] for a, b in self.outages],
            "hud_changes": [round(t - start, 2) for t in self.hud_changes],
            "options": {**asdict(self.options), "extra": None}, "encoder": self.encoder, "b_frames": self.b_frames,
            "size": list(self.size),
            "bitrate": self.bitrate, "parts": len(parts), "audio": audio_note,
        }
        meta["duration"] = round(mp4_duration(out) or 0, 2)
        make_thumbnail(self.ffmpeg, out, thumbnail_time(meta["events"], meta["duration"]))
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.rmtree(self.work, ignore_errors=True)
        self.log(f"录像：存好了 {out}（{out.stat().st_size / 2 ** 20:.0f} MB，{len(self.events)} 个事件）")
        return out

    def _audio_args(self, start):
        """拼 MP4 时声音那部分的 ffmpeg 参数：每一路按自己的时刻对齐画面（align_wav）。只有一路：一条音轨；
        游戏（或电脑上所有的声音）和麦克风都有：第 1 条是两路混好的（Evo 里、别的播放器默认放这条），原声各自另存一条
        （剪辑时单独调），每条写上名字（TRACK_TITLES）。结果的说明放 self.audio_note（写进 .json）。"""
        tracks = []  # [(名字, 对齐后的 WAV, 说明)]
        for kind, audio, name in (("main", self.audio, "audio"), ("mic", self.mic, "mic")):
            packets = getattr(audio, "packets", None) if audio else None
            if packets and (self.work / f"{name}.wav").exists():
                offset = audio_offset(packets, AUDIO_RATE, start + self.t0_qpc)
                align_wav(self.work / f"{name}.wav", self.work / f"{name}_aligned.wav", offset)
                title = "mic" if kind == "mic" else self.options.audio_source
                tracks.append((title, f"{name}_aligned.wav",
                               {"source": title, "offset": round(offset, 4), "gaps": getattr(audio, "gaps", None)}))
        self.audio_note = None
        if not tracks:
            return []
        args = []
        for _, wav, _ in tracks:
            args += ["-i", wav]
        self.audio_note = {"tracks": ["mix"] * (len(tracks) > 1) + [t for t, _, _ in tracks],
                           **{t: note for t, _, note in tracks}}
        if len(tracks) == 1:
            args += ["-map", "0:v", "-map", "1:a"]
        else:
            mix = self.rules.get("mix", {})
            gain = {t: mix.get(t, 1.0) * (self.options.mic_volume / 100 if t == "mic" else 1) for t, _, _ in tracks}
            inputs = "".join(f"[{i + 1}:a]volume={gain[t]:g}[a{i}];" for i, (t, _, _) in enumerate(tracks))
            graph = inputs + "".join(f"[a{i}]" for i in range(len(tracks))) + \
                f"amix=inputs={len(tracks)}:duration=longest:normalize=0[mix]"
            args += ["-filter_complex", graph, "-map", "0:v", "-map", "[mix]"]
            args += [x for i in range(len(tracks)) for x in ("-map", f"{i + 1}:a")]
        args += track_title_args(self.audio_note["tracks"])
        args += ["-disposition:a:0", "default", "-c:a", "aac", "-b:a", "160k"]
        return args

    def abort(self):
        """不要这一局了：停下、删掉分段文件。"""
        self.stopping = True
        self._kill()
        for audio in (self.audio, self.mic):
            if audio:
                audio.stop()
        shutil.rmtree(getattr(self, "work", self.out_dir / WORK_DIR_NAME / "_none"), ignore_errors=True)


def recover_leftovers(ffmpeg, out_dir, log=print):
    """上次 Evo 崩了、没来得及拼的分段文件：拼成 MP4（只有画面，名字后面加"_未完成"），拼不成就留着。返回拼好的路径。"""
    done = []
    for work in sorted((Path(out_dir) / WORK_DIR_NAME).glob("*")):
        parts = segment_parts(work)
        if not parts:
            shutil.rmtree(work, ignore_errors=True)
            continue
        try:
            started = datetime.strptime(work.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        joined, _ = joined_input(ffmpeg, parts, work)
        if joined is None:  # 一帧都读不出来：留着
            continue
        folder = Path(out_dir) / started.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{started:%Y-%m-%d_%H%M%S}_未完成.mp4"
        code, _ = run_ffmpeg(ffmpeg, ["-y", *joined, "-map", "0:v",
                                      "-c:v", "copy", "-movflags", "+faststart", str(out)], timeout=600, cwd=work)
        if code == 0 and out.exists():
            shutil.rmtree(work, ignore_errors=True)
            done.append(out)
            log(f"录像：上次没拼完的录像拼好了 {out}")
    return done


def enforce_limit(out_dir, max_bytes, log=print):
    """录像文件夹超过上限时，从最旧的录像删起（MP4 和同名的 .json、.jpg 一起删），直到不超过。返回删掉的 MP4。"""
    videos = sorted(Path(out_dir).glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in videos)
    removed = []
    for video in videos:
        if total <= max_bytes:
            break
        total -= video.stat().st_size
        video.unlink(missing_ok=True)
        video.with_suffix(".json").unlink(missing_ok=True)
        video.with_suffix(".jpg").unlink(missing_ok=True)  # 缩略图（录完时截的）
        removed.append(video)
    if removed:
        log(f"录像：超过上限 {max_bytes / 2 ** 30:.0f} GB，删掉最旧的 {len(removed)} 个")
    return removed
