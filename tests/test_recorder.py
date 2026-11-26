"""录像的录制核心（code/recorder.py、process_audio.py、process_info.kill_with_me）。

不开游戏、不抓屏幕：画面用 ffmpeg 自带的测试画面（testsrc2，按真实时间出帧），声音用假的录音（按给定的时刻写 WAV）。
- 算码率、对齐声音（audio_offset、align_wav）、起文件名、读进度、抓屏滤镜、超过上限删最旧的：不用 ffmpeg
- B 帧：按显卡厂家开几个，老显卡开了编不了时退回不开（假的 ffmpeg）；真录出来的 MP4 里有 B 帧
- 录 3 秒拼成 MP4：时长、大小、.json 里事件的时刻（MP4 里的位置）
- 中途 ffmpeg 退出：自己重开，断掉的那段记进 .json，MP4 里空档原样保留（断 11 秒也保留：ffmpeg 默认会把 10 秒以上的空档挤掉）
- 声音：假录音在第 2 秒"嘀"一声，MP4 里嘀声的位置对得上画面（30 毫秒以内）
- 上次崩了没拼的分段文件：拼成"_未完成"的 MP4
- AV1（2026-11-26，显卡编得了才跑）：分段写 .mkv、按拼接列表拼，录 3 秒、断 11 秒空档保留、声音对齐、崩了补拼、剪高光照样跑一遍
- 作业对象：开 ffmpeg 的程序被强行结束，ffmpeg 也跟着退
- 找窗口：按进程号找到一个测试窗口（不打开那个进程），只抓这个窗口抓得到
- 接进记录程序（match_recording.py）：开关默认关；一局开始开录、事件（倒地、被扶起、成盒、被复活、冠军、击倒对手、击杀）、右上角变了的时刻、
  结束再录 3 秒、同一局不重开、靶场也录、录不了只说一次
- 剪高光（highlights.py）：默认勾哪些、前后几秒，算片段（重叠的合成一段、开头挪到关键帧），按事件起名；真录一段再剪：关键帧读得对、
  片段开头是关键帧、画面和原片逐帧一样、时长对、声音对得上；只录高光切完删整局，没有勾了的事件、切不成时整局留着；
  记录程序里只录高光等击杀读完才剪，整局录像不剪
要 ffmpeg（找法见 recorder.find_ffmpeg）和显卡硬件编码器；没有就只跑不用 ffmpeg 的那些。

运行：python tests/test_recorder.py
"""

import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import process_info
import recorder

TEST_SOURCE = "lavfi:testsrc2=s=640x360:r=60,realtime"  # realtime：按真实时间出帧，和抓屏一样
TOOLS = Path(__file__).resolve().parent.parent / "tools"


def tone_wav(path, seconds, beep_at, rate=48000):
    """写一段立体声 WAV：静音，第 beep_at 秒开始嘀 0.1 秒（1 kHz）。"""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        t = i / rate
        v = int(8000 * math.sin(2 * math.pi * 1000 * t)) if beep_at <= t < beep_at + 0.1 else 0
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


class FakeAudio:
    """假的按进程录音：stop() 时写一段 WAV，声音从 start_qpc 开始，第 beep_at 秒（从 start_qpc 算）嘀一声。"""

    def __init__(self, beep_at):
        self.beep_at = beep_at

    def __call__(self, pid, wav_path):
        self.wav_path = Path(wav_path)
        self.ready = threading.Event()
        self.ready.set()
        self.error = None
        self.gaps = 0
        self.packets = []
        return self

    def start(self):
        self.start_qpc = time.perf_counter()

    def stop(self):
        seconds = time.perf_counter() - self.start_qpc
        tone_wav(self.wav_path, seconds, self.beep_at)
        self.packets = [(i * 480, self.start_qpc + i * 0.01) for i in range(int(seconds * 100))]  # 每包 10 毫秒


class FakeSources:
    """假的几路声音：每次调用（Apex 的进程号、"system" 或 "mic"）给一路新的 FakeAudio，嘀声的时刻按 beeps[来源]。"""

    def __init__(self, beeps):
        self.beeps, self.made = beeps, {}

    def __call__(self, source, wav_path):
        audio = FakeAudio(self.beeps.get(source, 9))(source, wav_path)
        self.made[source] = audio
        return audio


def beep_times(ffmpeg, path, stream):
    """MP4 里第 stream 条音轨上所有响声的开始时刻（秒，响声之间至少隔 0.3 秒）。"""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", f"0:a:{stream}", "-ac", "1", "-ar", "48000",
                        "-f", "s16le", "-"], capture_output=True)
    data = r.stdout
    out, last = [], -1.0
    for i in range(0, len(data) - 1, 2):
        if abs(struct.unpack_from("<h", data, i)[0]) > 2000:
            t = i / 2 / 48000
            if t - last > 0.3:
                out.append(round(t, 2))
            last = t
    return out


def peak_near(ffmpeg, path, stream, t, window=0.15):
    """MP4 第 stream 条音轨在 t 秒附近（前后 window 秒）最响的采样。"""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", f"0:a:{stream}", "-ac", "1", "-ar", "48000",
                        "-f", "s16le", "-"], capture_output=True)
    data = r.stdout
    lo, hi = max(0, int((t - window) * 48000)), int((t + window) * 48000)
    values = struct.unpack_from(f"<{max(0, min(hi, len(data) // 2) - lo)}h", data, lo * 2) if hi > lo else ()
    return max((abs(v) for v in values), default=0)


def audio_titles(ffmpeg, path):
    """MP4 里每条音轨的名字（ffmpeg 显示成 name 或 title）。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path)])
    out = []
    for block in re.split(r"\n\s+Stream #", err or "")[1:]:
        if "Audio:" in block.split("\n")[0]:
            m = re.search(r"^\s+(?:name|title)\s+:\s+(.+)$", block, re.M)
            out.append(m.group(1).strip() if m else None)
    return out


def b_frame_checks(rules):
    """B 帧（不用 ffmpeg）：按显卡厂家开几个；老显卡开了 B 帧编不了时检测编码器那一步退回不开。"""
    out = {}
    nv = recorder.encoder_args("h264_nvenc", 60, 5_000_000, 2, 3)
    off = recorder.encoder_args("h264_nvenc", 60, 5_000_000, 2)
    out["B 帧：N 卡 3 个（B 帧也当参照）、AMD 2 个、Intel 不开；不开时 -bf 0、不带 b_ref_mode"] = (
        [recorder.b_frames_wanted(rules, e) for e in ("h264_nvenc", "h264_amf", "h264_qsv")] == [3, 2, 0]
        and nv[nv.index("-bf") + 1] == "3" and nv[nv.index("-b_ref_mode") + 1] == "middle"
        and off[off.index("-bf") + 1] == "0" and "-b_ref_mode" not in off)
    out["关键帧只按时间放（每 2 秒）：按帧数的 -g 放宽到两倍（60 帧时 240），两套碰上会成对"] = (
        nv[nv.index("-g") + 1] == "240" and nv[nv.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)")

    def fake_run(accepts_b):  # 假的 ffmpeg：accepts_b=False 像老显卡，开了 B 帧就报错
        tried = []

        def run(ffmpeg, args, timeout=30, cwd=None):
            bf = args[args.index("-bf") + 1]
            tried.append(bf)
            return (0 if accepts_b or bf == "0" else 1), ""
        return run, tried
    real = recorder.run_ffmpeg
    try:
        opts = recorder.Options(source="lavfi:testsrc2")
        recorder.run_ffmpeg, tried_old = fake_run(False)
        old_enc = recorder.pick_encoder("假ffmpeg_老显卡", opts, rules)
        recorder.run_ffmpeg, tried_new = fake_run(True)
        new_enc = recorder.pick_encoder("假ffmpeg_新显卡", opts, rules)
    finally:
        recorder.run_ffmpeg = real
    out[f"老显卡开了 B 帧编不了：再不开 B 帧试一次，照样能录、录的时候不开（试了 {tried_old}）；新显卡开 B 帧（试了 {tried_new}）"] = (
        old_enc == new_enc == "h264_nvenc" and tried_old == ["3", "0"] and tried_new == ["3"]
        and recorder.b_frames("假ffmpeg_老显卡", old_enc, rules) == 0
        and recorder.b_frames("假ffmpeg_新显卡", new_enc, rules) == 3)
    return out


def pure_checks(tmp):
    out = {}
    rules = recorder.load_rules()
    out["码率：2560×1440 60 帧 H.264 高画质约 22 Mbps，1080p 约 12 Mbps，AV1 打六折"] = (
        round(recorder.bitrate(rules, 2560, 1440, 60, "h264", "best") / 1e6) == 22
        and round(recorder.bitrate(rules, 1920, 1080, 60, "h264", "best") / 1e6) == 12
        and round(recorder.bitrate(rules, 1920, 1080, 60, "h264", "standard") / 1e5) == 56
        and recorder.bitrate(rules, 1920, 1080, 60, "av1", "best") == int(1920 * 1080 * 60 * 0.1 * 0.6))
    out.update(b_frame_checks(rules))
    default = recorder.options_from({}, rules)
    old = recorder.options_from({"recording_quality": "low", "recording_size": "4k", "recording_fps": "144",
                                 "recording_codec": "h265"}, rules)
    kept = recorder.options_from({"recording_quality": "small", "recording_size": "720p", "recording_fps": "30"}, rules)
    sound = recorder.options_from({"recording_audio_source": "system", "recording_mic": True}, rules)
    bad_sound = recorder.options_from({"recording_audio_source": "everything", "recording_mic": "yes"}, rules)
    out["声音设置：默认只录 Apex、不录麦克风；选了电脑上所有的声音、麦克风照用；乱填的用默认"] = (
        (default.audio_source, default.mic) == ("game", False) and (sound.audio_source, sound.mic) == ("system", True)
        and (bad_sound.audio_source, bad_sound.mic) == ("game", False))
    out["画质设置：默认 1080p 60 帧标准；以前的、乱填的那一档用默认；设过的照用（帧数存成字符串也认）"] = (
        (default.size, default.fps, default.quality, default.codec) == ("1080p", 60, "standard", "h264")
        and (old.size, old.fps, old.quality, old.codec) == ("1080p", 60, "standard", "h264")
        and (kept.size, kept.fps, kept.quality) == ("720p", 30, "small"))
    window = recorder.source_filter(recorder.Options(source="window", size="1080p"), rules, (1920, 1080), hwnd=123)
    no_window = recorder.source_filter(recorder.Options(source="window"), rules)
    desktop = recorder.source_filter(recorder.Options(source="desktop"), rules)
    out["抓屏滤镜：只抓游戏窗口时用窗口句柄（不按程序名找：ffmpeg 会打开每个窗口的进程）、抓的时候缩放；"
        "找不到窗口不抓；桌面复制不缩放"] = (
        window.startswith("gfxcapture=hwnd=123:") and "window_exe" not in window and "width=1920:height=1080" in window
        and "resize_mode=scale_aspect" in window and no_window is None
        and desktop.startswith("ddagrab=") and "width" not in desktop)
    packets = [(0, 100.0), (48000, 101.0), (96000, 102.002)]  # 第三包比按采样数算的晚 2 毫秒（声卡时钟慢）
    out["对齐声音：用最近的一包算（声卡时钟和电脑时钟差开也对），画面比声音晚开始是正数、早开始是负数"] = (
        abs(recorder.audio_offset(packets, 48000, 100.5) - 0.5) < 1e-9
        and abs(recorder.audio_offset(packets, 48000, 102.5) - (2 + 0.498)) < 1e-6
        and abs(recorder.audio_offset(packets, 48000, 99.75) + 0.25) < 1e-9)
    src = tmp / "a.wav"
    tone_wav(src, 2.0, 1.0)
    recorder.align_wav(src, tmp / "cut.wav", 0.5)
    recorder.align_wav(src, tmp / "pad.wav", -0.25)

    def onset(path):
        with wave.open(str(path)) as w:
            data = w.readframes(w.getnframes())
            n = w.getnframes()
        first = next(i for i in range(0, len(data), 4) if data[i:i + 2] != b"\0\0") // 4
        return n, first

    (n_cut, beep_cut), (n_pad, beep_pad) = onset(tmp / "cut.wav"), onset(tmp / "pad.wav")
    out["挪 WAV：去掉开头 0.5 秒 / 前面补 0.25 秒静音，嘀声跟着挪"] = (
        n_cut == 72000 and abs(beep_cut - 24000) <= 1 and n_pad == 108000 and abs(beep_pad - 60000) <= 1)
    out["文件名：去掉 Windows 文件名里不能有的字符"] = recorder.safe_name('排位/恶灵: "R-301"') == "排位_恶灵_R-301"
    prog = tmp / "progress.txt"
    prog.write_text("frame=10\nfps=60\nprogress=continue\nframe=70\nfps=60\nprogress=continue\nframe=1", encoding="utf-8")
    out["读进度：只认最后一段完整的（正在写的那段不算）"] = recorder.read_progress(prog).get("frame") == "70"
    lib = tmp / "lib"
    for i, size in enumerate([300, 200, 100]):  # 最旧的最大
        month = lib / "2026-11"
        month.mkdir(parents=True, exist_ok=True)
        video = month / f"v{i}.mp4"
        video.write_bytes(b"\0" * size)
        video.with_suffix(".json").write_text("{}", encoding="utf-8")
        video.with_suffix(".jpg").write_bytes(b"jpg")  # 缩略图
        t = time.time() - 1000 + i * 10
        import os
        os.utime(video, (t, t))
    removed = recorder.enforce_limit(lib, 350, log=lambda *_: None)
    out["超过上限：从最旧的删起，MP4 和 .json、.jpg 一起删，删到不超过就停"] = (
        [p.name for p in removed] == ["v0.mp4"] and not (lib / "2026-11" / "v0.json").exists()
        and not (lib / "2026-11" / "v0.jpg").exists() and (lib / "2026-11" / "v1.mp4").exists()
        and (lib / "2026-11" / "v1.jpg").exists())
    return out


def probe_duration(ffmpeg, path):
    """MP4 里每一路（v / a）的 (开始, 时长)，用 ffmpeg 读（不另外要 ffprobe）。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-f", "null", "-"])
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def frame_times(ffmpeg, path):
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-map", "0:v", "-vf", "showinfo", "-f", "null", "-"], timeout=120)
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", err or "")]


def frame_types(ffmpeg, path):
    """MP4 里每种帧（I / P / B）各有几个。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-map", "0:v", "-vf", "showinfo", "-f", "null", "-"], timeout=120)
    kinds = re.findall(r"\btype:([IPB])\b", err or "")
    return {k: kinds.count(k) for k in "IPB"}


def beep_time(ffmpeg, path):
    """MP4 里声音第一次响的时刻（秒）。"""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                       capture_output=True)
    data = r.stdout
    for i in range(0, len(data) - 1, 2):
        if abs(struct.unpack_from("<h", data, i)[0]) > 2000:
            return i / 2 / 48000
    return None


def video_codec(ffmpeg, path):
    """MP4 里画面的编码格式（h264 / av1），读不出返回 None。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path)])
    m = re.search(r"Video: (\w+)", err or "")
    return m[1] if m else None


def record_checks(ffmpeg, tmp, codec="h264"):
    """真录（测试画面）。codec="av1"（2026-11-26）：分段写 .mkv、按拼接列表拼，前 4 项照样跑一遍，项目名前面加"AV1："。"""
    out = {}
    rules = recorder.load_rules()
    quiet = []
    p, lib = ("", "") if codec == "h264" else (f"{codec.upper()}：", f"-{codec}")
    # 画质固定"很高"，不跟着默认变：测试画面只有 640×360，默认的"标准"码率约 0.6 Mbps，下面开录 1.5 秒就结束 ffmpeg 的
    # 那两项里 ffmpeg 还没把画面写进文件，第一段是空的（2026-11-20 默认改成标准时遇到）
    best = {"source": TEST_SOURCE, "quality": "best", "codec": codec}

    # 1. 录 3 秒，中间记两个事件
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / f"lib{lib}", log=quiet.append,
                             rules=rules)
    rec.start()
    time.sleep(1.0)
    rec.mark("knocked")
    time.sleep(1.0)
    rec.mark("kill", kills=1)
    for _ in range(10):
        rec.poll()
        time.sleep(0.1)
    video = rec.finish({"mode": "排位", "legend": "恶灵"})
    meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8")) if video else {}
    duration = probe_duration(ffmpeg, video) if video else None
    out[p + "录 3 秒拼成 MP4：在 录像文件夹\\年-月\\ 里，文件名带模式和传奇，分段文件删了"] = (
        video is not None and video.parent.parent == (tmp / f"lib{lib}").resolve() and video.name.endswith("_排位_恶灵.mp4")
        and not any((tmp / f"lib{lib}" / recorder.WORK_DIR_NAME).glob("*/part_*")))
    out[f"{p}MP4 约 3 秒（{duration}）、640×360"] = duration is not None and 2.5 < duration < 3.6 and \
        meta.get("size") == [640, 360]
    events = meta.get("events", [])
    out[f"{p}.json 里的事件：时刻是 MP4 里的位置（倒地约 1 秒、击杀约 2 秒处；{[e['t'] for e in events]}），带着附加的说明"] = (
        [e["kind"] for e in events] == ["knocked", "kill"] and events[1].get("kills") == 1
        and 0.6 < events[0]["t"] < 1.1 and 1.6 < events[1]["t"] < 2.1 and meta.get("outages") == [])
    want_b = recorder.b_frames(ffmpeg, meta.get("encoder", ""), rules)
    kinds = frame_types(ffmpeg, video) if video else {}
    if codec == "h264":
        out[f"B 帧：{meta.get('encoder')} 开 {meta.get('b_frames')} 个（写进 .json），MP4 里有 B 帧（{kinds}）"] = (
            meta.get("b_frames") == want_b and (kinds.get("B", 0) > 0 if want_b else kinds.get("B") == 0)
            and kinds.get("I", 0) > 0)
    else:  # AV1 没有 B 帧这个说法（编码器的 B 帧在 AV1 里是不显示的参照帧，解出来都是 I / P）
        made = video_codec(ffmpeg, video) if video else None
        out[f"{p}MP4 里是 {made}，编码器 {meta.get('encoder')}（写进 .json，B 帧 {meta.get('b_frames')}），解得出来（{kinds}）"] = (
            made == codec and meta.get("options", {}).get("codec") == codec
            and meta.get("encoder") in rules["encoders"][codec] and meta.get("b_frames") == want_b and kinds.get("I", 0) > 0)

    # 2. 中途 ffmpeg 退出：重开，空档保留（断 11 秒：超过 ffmpeg 默认挤掉空档的 10 秒）
    long_rules = {**rules, "restart_wait_seconds": [11]}
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / f"lib2{lib}", log=quiet.append,
                             rules=long_rules)
    rec.start()
    time.sleep(1.5)
    rec.proc.kill()
    end = time.monotonic() + 16
    while time.monotonic() < end and not rec.outages:
        rec.poll()
        time.sleep(0.2)
    rec.mark("after_gap")
    time.sleep(1.0)
    video2 = rec.finish()
    meta2 = json.loads(video2.with_suffix(".json").read_text(encoding="utf-8")) if video2 else {}
    gap = (meta2.get("outages") or [[0, 0]])[0]
    times = frame_times(ffmpeg, video2) if video2 else []
    jump = max((b - a for a, b in zip(times, times[1:])), default=0)
    out[f"{p}ffmpeg 中途退出：自己重开，两个分段，断掉的那段记进 .json（{gap}）"] = (
        meta2.get("parts") == 2 and 10.5 < gap[1] - gap[0] < 13)
    out[f"{p}MP4 里空档原样保留（画面里最大的间隔 {jump:.1f} 秒），空档后的事件位置对得上"] = (
        10.5 < jump < 13 and abs(meta2["events"][0]["t"] - gap[1]) < 1.0)

    # 3. 声音：假录音在第 2 秒嘀一声
    fake = FakeAudio(beep_at=2.0)
    rec = recorder.Recording(ffmpeg, recorder.Options(**best), tmp / f"lib3{lib}", log=quiet.append, rules=rules,
                             audio_factory=fake)
    rec.start(apex_pid=1234)
    time.sleep(3.5)
    rec.poll()
    video3 = rec.finish()
    meta3 = json.loads(video3.with_suffix(".json").read_text(encoding="utf-8")) if video3 else {}
    beep = beep_time(ffmpeg, video3) if video3 else None
    # 嘀声在 MP4 里的位置 = 嘀声的时刻 - MP4 第一帧的时刻（都换成 QPC：第一帧 = t0 + first_frame）
    want = (fake.start_qpc + 2.0) - (rec.t0_qpc + meta3["first_frame"]) if "first_frame" in meta3 else None
    out[f"{p}声音：合进 MP4，嘀声在 MP4 里的位置对得上画面（在 {beep} 秒，应在 {want and round(want, 3)} 秒）"] = (
        meta3.get("audio") is not None and beep is not None and want is not None and abs(beep - want) < 0.03)

    # 4. 上次崩了没拼的分段文件（AV1 的 .mkv 被硬杀：没写完的那一块不要，前面的照样读得回来）
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / f"lib4{lib}", log=quiet.append,
                             rules=rules)
    rec.start()
    time.sleep(2.5 if codec == "av1" else 1.5)  # MKV 每 1 秒才写出一块：多录一会儿，硬杀时文件里已经有画面
    rec._kill()  # 像 Evo 崩了一样：不收尾
    done = recorder.recover_leftovers(ffmpeg, tmp / f"lib4{lib}", log=quiet.append)
    left = probe_duration(ffmpeg, done[0]) if done else None
    out[f"{p}上次崩了没拼的分段文件：拼成名字带'_未完成'的 MP4（{left} 秒），分段文件删掉"] = (
        len(done) == 1 and done[0].name.endswith("_未完成.mp4") and done[0].stat().st_size > 0 and bool(left)
        and not any((tmp / f"lib4{lib}" / recorder.WORK_DIR_NAME).iterdir()))
    if codec != "h264":  # 下面两项（混音、编不了 AV1 改用 H.264）和编码格式无关
        return out

    # 6. 电脑上所有的声音 + 麦克风：第 1 条混好的（两声都有），原声各自另存一条，每条有名字，时刻都对得上画面；
    #    麦克风音量 50%：混音里麦克风那一声是原声的一半，麦克风原声那条不变
    sources = FakeSources({"system": 1.0, "mic": 2.0})
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio_source="system", mic=True, mic_volume=50), tmp / "lib6",
                             log=quiet.append, rules=rules, audio_factory=sources)
    rec.start(apex_pid=1234)
    time.sleep(3.0)
    rec.poll()
    video6 = rec.finish()
    meta6 = json.loads(video6.with_suffix(".json").read_text(encoding="utf-8")) if video6 else {}
    first = meta6.get("first_frame", 0)
    want = {k: round((a.start_qpc + beep) - (rec.t0_qpc + first), 2) for k, a, beep in
            (("system", sources.made.get("system"), 1.0), ("mic", sources.made.get("mic"), 2.0)) if a}
    mixed, system, mic = (beep_times(ffmpeg, video6, i) if video6 else [] for i in range(3))
    titles = audio_titles(ffmpeg, video6) if video6 else []
    out[f"电脑上所有的声音 + 麦克风：录了系统内录和麦克风（{sorted(sources.made)}），没录 Apex 的进程"] = (
        sorted(sources.made) == ["mic", "system"])
    out[f"三条音轨：混音 {mixed}、电脑上所有的声音 {system}、麦克风 {mic}（应在 {want}），名字 {titles}"] = (
        len(mixed) == 2 and len(system) == 1 and len(mic) == 1 and want.get("system") is not None
        and abs(system[0] - want["system"]) < 0.05 and abs(mic[0] - want["mic"]) < 0.05
        and abs(mixed[0] - want["system"]) < 0.05 and abs(mixed[1] - want["mic"]) < 0.05
        and titles == ["混音", "电脑上所有的声音", "麦克风"] and meta6.get("audio", {}).get("tracks") == ["mix", "system", "mic"])
    mixed_mic = peak_near(ffmpeg, video6, 0, want.get("mic", 0)) if video6 else 0
    raw_mic = peak_near(ffmpeg, video6, 2, want.get("mic", 0)) if video6 else 0
    mixed_sys = peak_near(ffmpeg, video6, 0, want.get("system", 0)) if video6 else 0
    raw_sys = peak_near(ffmpeg, video6, 1, want.get("system", 0)) if video6 else 0
    out[f"麦克风音量 50%：混音里麦克风那一声 {mixed_mic}、原声 {raw_mic}（一半）；另一路不变（{mixed_sys} / {raw_sys}）"] = (
        raw_mic > 0 and 0.4 < mixed_mic / raw_mic < 0.6 and raw_sys > 0 and 0.9 < mixed_sys / raw_sys < 1.1)

    # 5. 设置里选了 AV1、这块显卡编不了（这里用一个不存在的编码器假装）：改用 H.264 照样录，不是整局不录
    no_av1 = {**rules, "encoders": {**rules["encoders"], "av1": ["av1_evo_test_missing"]}}
    said = []
    # 检测结果按 (ffmpeg, 画面, 编码格式) 记着：前后都清掉，这台电脑真编得了 AV1 时不串（下面 AV1 真录那几项还要真检测）
    recorder._encoder_cache.pop((ffmpeg, TEST_SOURCE, "av1"), None)
    rec = recorder.Recording(ffmpeg, recorder.Options(**{**best, "codec": "av1"}, audio=False), tmp / "lib5",
                             log=said.append, rules=no_av1)
    rec.start()
    time.sleep(1.0)
    video5 = rec.finish()
    recorder._encoder_cache.pop((ffmpeg, TEST_SOURCE, "av1"), None)
    out[f"选了 AV1 但编不了：改用 H.264 录（{rec.encoder}），日志里写着"] = (
        video5 is not None and rec.encoder in rules["encoders"]["h264"] and rec.options.codec == "h264"
        and any("改用 h264" in line for line in said))
    return out


class FakeInMatch:
    """InMatchWatcher 里 match_recording 要看的那几项。"""

    def __init__(self):
        self.started, self.active, self.done, self.state, self.tops, self.legend = None, False, False, None, [], None
        self.hud = type("H", (), {"legends": type("L", (), {"name": staticmethod(lambda k: {7: "恶灵"}.get(k))})()})()

    @property
    def pending(self):
        return self.started is not None and not self.done


class FakeRecording:
    made = []

    def __init__(self, ffmpeg, options, out_dir, log=None, rules=None):
        self.options, self.out_dir, self.events, self.hud_changes, self.polls = options, out_dir, [], 0, 0
        self.finished = None
        self.fail = FakeRecording.fail
        FakeRecording.made.append(self)

    def start(self, pid):
        if self.fail:
            raise recorder.RecordingUnavailable(self.fail)
        self.pid = pid

    def mark(self, kind, **info):
        self.events.append({"kind": kind, **info})
        return self.events[-1]

    def hud_change(self):
        self.hud_changes += 1

    def poll(self):
        self.polls += 1

    def since_t0(self):
        return time.monotonic()

    def finish(self, info):
        self.finished = info
        return None


def match_checks(tmp):
    import match_recording
    out = {}
    clock = [0.0]
    settings = {}
    logs = []

    def make():
        FakeRecording.made, FakeRecording.fail = [], None
        return match_recording.MatchRecorder(logs.append, settings=lambda: dict(settings), data_dir=tmp,
                                             factory=FakeRecording, clock=lambda: clock[0], find_pid=lambda: 4321)

    def step(m, screen, im, seconds=0.3):
        clock[0] += seconds
        m.update(screen, im)

    # 开关关着：开局画面、自己的界面都不录
    m, im = make(), FakeInMatch()
    step(m, "match_start", im)
    im.started, im.active, im.state = 1, True, "存活"
    step(m, None, im)
    out["开关关着（默认）：一局开始、对局中都不录"] = FakeRecording.made == []

    # 开着：一整局
    settings.update({"recording_enabled": True, "recording_size": "1080p"})
    m, im = make(), FakeInMatch()
    step(m, "match_start", im)
    step(m, "match_start", im)  # 开局横幅还在
    im.started, im.active, im.state, im.legend = 1, True, "存活", 7
    step(m, None, im)
    rec = FakeRecording.made[0] if FakeRecording.made else None
    for state in ("倒地", "存活", "成盒", "存活"):
        im.state = state
        step(m, None, im)
    im.tops = ["a"]
    step(m, None, im)
    step(m, None, im)  # 右上角没变
    im.tops = ["a", "b"]
    step(m, None, im)
    step(m, "champion", im)
    step(m, "champion", im)
    im.done, im.active = True, False
    step(m, "summary", im)
    still = rec is not None and rec.finished is None
    step(m, "summary", im, seconds=3.5)  # 结束以后再录 3 秒
    m.close()
    out["开着：看到开局画面就开录（只开一次，横幅还在也不重开），设置里的分辨率带过去、录 Apex 的声音"] = (
        len(FakeRecording.made) == 1 and rec.options.size == "1080p" and rec.pid == 4321)
    out["事件：倒地、被扶起、成盒、被复活、冠军（冠军只记一次）"] = rec is not None and [e["kind"] for e in rec.events] == [
        "knocked", "revived", "dead", "respawned", "champion"]
    out["右上角变了才记时刻（没变不记）"] = rec is not None and rec.hud_changes == 2
    out["一局结束再录 3 秒才停，拼 MP4 时带上传奇（文件名用）；对局中按时看 ffmpeg"] = (
        still and rec.finished == {"legend": "恶灵"} and rec.polls >= 2)
    step(m, None, im)  # 同一局结束以后：不再开录
    out["同一局结束以后不重开"] = len(FakeRecording.made) == 1
    im2 = FakeInMatch()  # 下一局：开局画面（InMatchWatcher 已经清空）
    step(m, "match_start", im2)
    out["下一局开局：再开录"] = len(FakeRecording.made) == 2
    m.close()

    # 靶场：没有开局画面，看到自己的界面也录
    m, im = make(), FakeInMatch()
    im.started, im.active, im.state = 5, True, "存活"
    step(m, None, im)
    out["没有开局画面（靶场）：看到自己的界面就开录"] = len(FakeRecording.made) == 1
    m.close()

    # 击杀：kill_events（击杀数只会变多，读错的不标）、add_events（写进 .json，换成 MP4 里的位置）
    ke = match_recording.kill_events
    out["找击杀：击杀数变大才算，读不出的跳过，一下子多两个记两个"] = ke(
        [(1, 0), (2, None), (3, 1), (4, 1), (5, 3), (6, 3)]) == [(3, 1, 1), (5, 3, 2)]
    out["找击杀：读错的不标（紧接着下一块比它小、一下子多出太多）"] = ke(
        [(1, 0), (2, 7), (3, 1), (4, 1), (5, 40), (6, 2)], max_jump=5) == [(3, 1, 1), (6, 2, 1)]
    sidecar = tmp / "kills.json"
    sidecar.write_text(json.dumps({"first_frame": 0.5, "events": [{"t": 10.0, "kind": "knock"}]}), encoding="utf-8")
    ok = match_recording.add_events(tmp / "kills.mp4", [{"t": 12.5, "kind": "kill", "kills": 1, "count": 1},
                                                       {"t": 8.5, "kind": "kill", "kills": 2, "count": 1}])
    events = json.loads(sidecar.read_text(encoding="utf-8"))["events"]
    out["找击杀：写进录像旁边的 .json，时刻换成 MP4 里的位置（减掉第一帧），按时间排好"] = ok and [
        (e["kind"], e["t"]) for e in events] == [("kill", 8.0), ("knock", 10.0), ("kill", 12.0)]

    # 击杀、助攻：对局中那两格有一格变了才存，回到大厅读，读完写进 .json
    m, im = make(), FakeInMatch()
    cells = {"a": np.zeros((28, 26), np.uint8), "b": np.full((28, 26), 255, np.uint8)}
    plan = []  # 每一帧：(击杀那一格, 助攻那一格, 读出来的击杀数, 助攻数)
    im.hud.kp_snip = lambda frame: plan[0]
    im.hud.kp_cells = lambda snip: {"kills": None if snip[0] is None else cells[snip[0]],
                                    "assists": None if snip[1] is None else cells[snip[1]]}
    im.hud.read_kp = lambda snip: {"kills": snip[2], "assists": snip[3]}
    im.hud.knock, im.hud.cfg = (lambda frame: None), {}
    step(m, "match_start", im)
    im.started, im.active, im.state = 8, True, "存活"
    rec = FakeRecording.made[-1]
    frames = [(None, None, 0, 0), ("a", "a", 0, 0), ("a", "a", 0, 0), ("b", "a", 1, 0), ("b", "a", 1, 0),
              ("b", "b", 1, 1), ("a", "b", 2, 1)]  # 第 4 帧击杀、第 6 帧助攻、第 7 帧又一个击杀
    for n, item in enumerate(frames):
        plan[:] = [item]
        im.tops = list(im.tops) + [n]  # InMatchWatcher 存了新的一块右上角
        clock[0] += 0.3
        m.update(None, im, frame="截图")
    stored = len(m.kp_snips)
    video = tmp / "lib-kill" / "match.mp4"
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b"0")
    video.with_suffix(".json").write_text(json.dumps({"first_frame": 0.0, "events": []}), encoding="utf-8")
    rec.finish = lambda info: video
    im.done, im.active = True, False
    step(m, "summary", im)
    step(m, "summary", im, seconds=3.5)
    for w in m.workers:
        w.join(5)
    before = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))["events"]
    for _ in range(3):
        step(m, "lobby", im)
    events = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))["events"]
    kills = [e for e in events if e["kind"] == "kill"]
    assists = [e for e in events if e["kind"] == "assist"]
    out[f"击杀、助攻：对局中那两格有一格变了才存（存了 {stored} 块），结算画面上不读，回到大厅读完写进 .json（{events}）"] = (
        stored == 4 and before == [] and [(e["kills"], e["count"]) for e in kills] == [(1, 1), (2, 1)]
        and [(e["assists"], e["count"]) for e in assists] == [(1, 1)]
        and not m.pending and any("标上了 2 次击杀、1 次助攻" in line for line in logs))
    m.close()

    # 击倒对手：中间那一行的"击倒"（hud.knock 给的位置）；左边有"助攻，"的是助攻击倒（hud.knock_assist）
    m, im = make(), FakeInMatch()
    script, assists = [], []
    im.hud.knock = lambda frame: script.pop(0) if script else None
    im.hud.knock_assist = lambda frame, x: assists.pop(0) if assists else False
    im.hud.cfg = {"knock": {"rearm_seconds": 0.6, "moved": 20}}
    step(m, "match_start", im)
    im.started, im.active, im.state = 7, True, "存活"
    rec = FakeRecording.made[0]
    # 每一帧：("击倒"的位置, 左边有没有"助攻，")。第 1 行自己击倒；第 2 行一出现就是助攻；第 3 行（挪了）第二帧才看出是助攻
    seen = [(None, False), (299, False), (299, False), (300, False), (299, False), (None, False), (None, False),
            (None, False), (330, True), (330, True), (None, False), (None, False), (None, False),
            (352, False), (352, True), (None, False)]
    for x, assist in seen:
        script.append(x)
        if x is not None:
            assists.append(assist)
        clock[0] += 0.3
        m.update(None, im, frame="一张截图")
    clock[0] += 0.3
    m.update("summary", im, frame="结算画面")  # 不在对局中的画面不看
    kinds = [e["kind"] for e in rec.events if e["kind"] in ("knock", "assist_knock")]
    out[f"击倒对手：同一行显示着只算一次，隔一会儿又出现算下一次；左边有'助攻，'记助攻击倒，第二帧才看出来也改过来（{kinds}）"] = (
        kinds == ["knock", "assist_knock", "assist_knock"])
    m.close()

    # 录不了：一局最多试两次（横幅、看到自己的界面），日志里只说一次，下一局再试
    m = make()
    FakeRecording.fail = "这台电脑上没有能用的显卡硬件编码器"
    im = FakeInMatch()
    for _ in range(5):
        step(m, "match_start", im)  # 横幅在屏幕上好几秒
    im.started, im.active, im.state = 9, True, "存活"
    for _ in range(3):
        step(m, None, im)
    first_match = len(FakeRecording.made)
    im.done, im.active = True, False
    step(m, "lobby", im)
    for _ in range(3):
        step(m, "match_start", FakeInMatch())
    said = [line for line in logs if "录不了" in line]
    out[f"录不了：一局最多试两次（横幅一次、看到自己的界面一次，试了 {first_match} 次），日志里说一次原因，下一局开始时再试"] = (
        first_match == 2 and len(said) == 1 and len(FakeRecording.made) == 3)
    return out


def highlight_checks():
    """剪高光（highlights.py）不用 ffmpeg 的部分：设置、算片段、起名字。"""
    import highlights
    out = {}
    rules = recorder.load_rules()
    default = highlights.choices({}, rules)
    on = [k for k, c in default.items() if c["on"]]
    out[f"高光的默认：勾上击杀、击倒、助攻击倒、冠军、自己倒地、成盒（{on}），秒数照 Outplayed（击杀前 30 后 10、被复活前 9 后 20）"] = (
        sorted(on) == sorted(["kill", "knock", "assist_knock", "champion", "knocked", "dead"])
        and list(default) == ["kill", "knock", "assist_knock", "assist", "champion", "knocked", "revived", "respawned", "dead"]
        and (default["kill"]["before"], default["kill"]["after"]) == (30, 10)
        and (default["respawned"]["before"], default["respawned"]["after"]) == (9, 20)
        and (default["dead"]["before"], default["dead"]["after"]) == (10, 5))
    mine = highlights.choices({"recording_highlights": {"kill": {"on": False, "before": 5, "after": 999},
                                                        "dead": {"on": "yes", "before": True}, "bogus": {"on": True}}}, rules)
    out["玩家改过的用玩家的；超出 0–60 秒、不是数、不是真假的用默认；不认得的事件不要"] = (
        mine["kill"] == {"on": False, "before": 5, "after": 10} and mine["dead"] == default["dead"] and "bogus" not in mine)
    out["录制模式：默认整局录像，设成只录高光才是只录高光，不认得的当整局"] = (
        highlights.mode({}) == "full" and highlights.mode({"recording_mode": "highlights"}) == "highlights"
        and highlights.mode({"recording_mode": "其他"}) == "full")

    events = [{"t": 10, "kind": "knock"}, {"t": 25, "kind": "kill"}, {"t": 26, "kind": "assist"}, {"t": 40, "kind": "knock"},
              {"t": 100, "kind": "dead"}]
    segs = highlights.plan(events, 104, default)
    out[f"算片段：每个勾了的事件前后几秒，重叠的合成一段，不超出录像，没勾的（助攻）不算（{[(s['start'], s['end']) for s in segs]}）"] = (
        [(s["start"], s["end"]) for s in segs] == [(0.0, 50), (90, 104.0)]
        and [[e["kind"] for e in s["hits"]] for s in segs] == [["knock", "kill", "knock"], ["dead"]])
    keys = [float(k) for k in range(0, 120, 2)]
    snapped = highlights.plan([{"t": 50.7, "kind": "kill"}, {"t": 85.5, "kind": "knock"}], 104, default, keys)
    out[f"开头挪到前面最近的关键帧（击杀 50.7 秒前 30 秒 → 20.7 → 20、击倒 85.5 前 20 → 65.5 → 64；{[(s['start'], s['end']) for s in snapped]}）"] = (
        [(s["start"], s["end"]) for s in snapped] == [(20.0, 60.7), (64.0, 95.5)])
    touching = highlights.plan([{"t": 20, "kind": "dead"}, {"t": 35, "kind": "dead"}], 100, default)
    out["刚好接上的两段（前一段到 25 秒、后一段从 25 秒）也合成一段"] = [(s["start"], s["end"]) for s in touching] == [(10, 40)]
    out["一个勾了的事件都没有：没有片段"] = highlights.plan([{"t": 5, "kind": "assist"}], 60, default) == []
    named = highlights.names([{"hits": [{"kind": "knock"}, {"kind": "kill"}]}, {"hits": [{"kind": "knock"}]},
                              {"hits": [{"kind": "kill"}, {"kind": "champion"}]}, {"hits": [{"kind": "kill"}]}])
    out[f"片段按里面最要紧的事件起名，每种各自数（{[n for _, n in named]}）"] = (
        [n for _, n in named] == ["击杀1", "击倒1", "冠军1", "击杀2"])
    return out


def checksums(ffmpeg, path):
    """MP4 画面里每一帧：(时刻, 帧的种类 I/P/B, 校验和)。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-map", "0:v", "-vf", "showinfo", "-f", "null", "-"], timeout=120)
    return [(float(t), k, c) for t, k, c in re.findall(r"pts_time:(-?[\d.]+) .*?type:([IPB]).*?checksum:([0-9A-F]+)", err or "")]


def highlight_record_checks(ffmpeg, tmp, codec="h264"):
    """真录一段（测试画面、N 卡 B 帧、第 5 秒嘀一声），再剪高光：开头落在关键帧、画面和原片逐帧一样、声音对得上。
    codec="av1"（2026-11-26）：剪的那几项照样跑一遍，项目名前面加"AV1："（只录高光删整局那几项和编码格式无关，不重复）。"""
    import highlights
    import shutil
    out, quiet = {}, []
    rules = recorder.load_rules()
    p, hl = ("", "hl") if codec == "h264" else (f"{codec.upper()}：", f"hl-{codec}")
    fake = FakeAudio(beep_at=5.0)
    rec = recorder.Recording(ffmpeg, recorder.Options(source=TEST_SOURCE, quality="best", codec=codec), tmp / hl,
                             log=quiet.append, rules=rules, audio_factory=fake)
    rec.start(apex_pid=1234)
    time.sleep(1.6)
    rec.mark("knock")
    time.sleep(4.0)
    rec.mark("kill", kills=1)
    time.sleep(2.6)
    rec.poll()
    video = rec.finish({"mode": "排位", "legend": "恶灵"})
    if video is None:
        return {p + "剪高光：先录一段测试录像": False}
    meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
    events = {e["kind"]: e["t"] for e in meta["events"]}

    keys = highlights.keyframe_times(video)
    real = highlights.keyframe_times
    highlights.keyframe_times = lambda path: None  # 读不了索引时用 ffmpeg 只解关键帧
    try:
        by_ffmpeg = highlights.keyframes(ffmpeg, video)
    finally:
        highlights.keyframe_times = real
    out[f"{p}关键帧：直接读 MP4 的索引，和 ffmpeg 解出来的一样（{keys}）"] = (
        bool(keys) and by_ffmpeg is not None and len(keys) == len(by_ffmpeg) >= 3
        and all(abs(a - b) < 1e-5 for a, b in zip(keys, by_ffmpeg)) and keys[0] == 0)
    gaps = [round(b - a, 3) for a, b in zip(keys or [], (keys or [])[1:])]
    out[f"{p}关键帧每 2 秒一个、没有挨着的一对（间隔 {gaps}；以前 -g 和按时间的两套碰上时会差一两帧成对）"] = (
        bool(gaps) and all(1.8 < g < 2.2 for g in gaps))

    picked = {k: {"on": False, "before": 0, "after": 0} for k in highlights.choices({}, rules)}
    picked["knock"] = {"on": True, "before": 1, "after": 1}
    picked["kill"] = {"on": True, "before": 1, "after": 1.5}
    said = []
    clips = highlights.save(ffmpeg, video, picked, log=said.append)
    metas = [json.loads(c.with_suffix(".json").read_text(encoding="utf-8")) for c in clips]
    out[f"{p}剪出两段，按事件起名（{[c.name for c in clips]}），旁边有 .json、缩略图，整局留着"] = (
        [c.name for c in clips] == [video.stem + "_击倒1.mp4", video.stem + "_击杀1.mp4"]
        and all(c.with_suffix(".jpg").exists() for c in clips) and video.exists()
        and highlights.existing(video) == clips and not list(video.parent.glob("*.part")))
    if len(clips) != 2:
        return out
    h = metas[1]["highlight"]
    kill_from = max(k for k in keys if k <= events["kill"] - 1 + 1e-6)
    out[f"{p}击杀那一段：从击杀前 1 秒之前最近的关键帧（{kill_from}）到击杀后 1.5 秒（{h['from']}–{h['to']}），记着第几段、哪一局"] = (
        abs(h["from"] - kill_from) < 1e-3 and abs(h["to"] - (events["kill"] + 1.5)) < 1e-3
        and h["of"] == video.name and h["index"] == 2 and h["count"] == 2 and h["name"] == "击杀1"
        and metas[1]["match"] == {"mode": "排位", "legend": "恶灵"} and metas[1]["started"] == meta["started"])
    clip_events = {e["kind"]: e["t"] for e in metas[1]["events"]}
    out[f"{p}片段里事件的时刻是片段里的位置（击杀在 {clip_events.get('kill')} 秒）"] = (
        abs(clip_events.get("kill", -1) - (events["kill"] - h["from"])) < 0.01 and "knock" not in clip_events)
    whole, part = checksums(ffmpeg, video), checksums(ffmpeg, clips[1])
    at = next((i for i, f in enumerate(whole) if abs(f[0] - h["from"]) < 0.002), None)
    n = len(part) - 4  # 结尾因为 B 帧可能少两三帧
    out[f"{p}片段开头是关键帧、在 0 秒（{part[0][:2] if part else None}），前面 {n} 帧和原片逐帧一样（没有花屏、不缺帧）"] = (
        bool(part) and part[0][1] == "I" and abs(part[0][0]) < 1e-6 and at is not None
        and [f[2] for f in part[:n]] == [f[2] for f in whole[at:at + n]])
    duration = metas[1]["duration"]
    out[f"{p}片段的时长对（{duration} 秒，应约 {h['to'] - h['from']:.2f} 秒）"] = abs(duration - (h["to"] - h["from"])) < 0.15
    beep_whole, beep_clip = beep_time(ffmpeg, video), beep_time(ffmpeg, clips[1])
    out[f"{p}声音对得上：嘀声在整局 {beep_whole} 秒、片段 {beep_clip} 秒（差 {h['from']}）"] = (
        beep_whole is not None and beep_clip is not None and abs((beep_whole - h["from"]) - beep_clip) < 0.03)
    if codec != "h264":
        return out

    # 只录高光：切好再删整局；一个勾了的事件都没有、切不成：整局留着
    def copy_to(name):
        folder = tmp / name / video.parent.name
        folder.mkdir(parents=True)
        for suffix in (".mp4", ".json"):
            shutil.copy(video.with_suffix(suffix), folder / video.with_suffix(suffix).name)
        return folder / video.name
    only = copy_to("hl-only")
    kept = highlights.keep_only(ffmpeg, only, picked, log=said.append)
    out["只录高光：切出高光以后整局删了（MP4、.json 都删）"] = (
        len(kept) == 2 and not only.exists() and not only.with_suffix(".json").exists() and all(c.exists() for c in kept))
    none = copy_to("hl-none")
    nothing = {k: {**c, "on": False} for k, c in picked.items()}
    out["只录高光、一个勾了的事件都没有：整局留着，日志里说一句"] = (
        highlights.keep_only(ffmpeg, none, nothing, log=said.append) == [] and none.exists()
        and any("整局留着" in line for line in said))
    broken = copy_to("hl-broken")
    real_run = recorder.run_ffmpeg
    recorder.run_ffmpeg = lambda *a, **k: (1, "假装切的时候出错")
    try:
        failed = highlights.keep_only(ffmpeg, broken, picked, log=said.append)
    finally:
        recorder.run_ffmpeg = real_run
    out["只录高光、切不成：整局留着，切了一半的删掉"] = (
        failed == [] and broken.exists() and sorted(p.name for p in broken.parent.glob("*.mp4*")) == [broken.name]
        and any("剪高光没成" in line for line in said))
    return out


def match_highlight_checks(tmp):
    """录制模式"只录高光"接进记录程序：什么时候剪（击杀读完以后）、整局录像时不剪、停止记录时也剪完。"""
    import highlights
    import match_recording
    out, logs, clock = {}, [], [0.0]
    settings = {"recording_enabled": True, "recording_mode": "highlights"}
    calls = []

    def fake_keep_only(ffmpeg, video, picked, log=print):
        kinds = [e["kind"] for e in json.loads(Path(video).with_suffix(".json").read_text(encoding="utf-8"))["events"]]
        calls.append({"video": video, "picked": picked, "kinds": kinds})
        return []

    def play(snips, lobby=True, close=True):
        """打一局：snips=True 时对局中击杀那一格变一次（回大厅才读）。返回 (MatchRecorder, 读之前剪了几次)。"""
        FakeRecording.made, FakeRecording.fail = [], None
        m = match_recording.MatchRecorder(logs.append, settings=lambda: dict(settings), data_dir=tmp, factory=FakeRecording,
                                          clock=lambda: clock[0], find_pid=lambda: 1)
        im = FakeInMatch()
        cells = {"a": np.zeros((28, 26), np.uint8), "b": np.full((28, 26), 255, np.uint8)}
        current = [None]
        im.hud.kp_snip = lambda frame: current[0]
        im.hud.kp_cells = lambda snip: {"kills": cells[snip[0]], "assists": None}
        im.hud.read_kp = lambda snip: {"kills": snip[1], "assists": None}
        im.hud.knock, im.hud.cfg = (lambda frame: None), {}
        clock[0] += 0.3
        m.update("match_start", im)
        im.started, im.active, im.state = 3, True, "存活"
        rec = FakeRecording.made[-1]
        video = tmp / f"hl-match-{len(calls)}-{clock[0]:.0f}" / "match.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"0")
        video.with_suffix(".json").write_text(json.dumps({"first_frame": 0.0, "events": []}), encoding="utf-8")
        rec.finish = lambda info: video
        for item in ([("a", 0), ("b", 1)] if snips else []):  # 右上角击杀那一格：0 → 1
            current[0] = item
            im.tops = list(im.tops) + [item]
            clock[0] += 0.3
            m.update(None, im, frame="截图")
        im.done, im.active = True, False
        for seconds in (0.3, 3.5):
            clock[0] += seconds
            m.update("summary", im)
        for w in m.workers:
            w.join(5)
        before = len(calls)
        if lobby:
            for _ in range(3):
                clock[0] += 0.3
                m.update("lobby", im)
            for w in m.workers:
                w.join(5)
        if close:
            m.close()
        return m, before

    real = highlights.keep_only
    highlights.keep_only = fake_keep_only
    try:
        play(snips=False)
        out["只录高光、这一局没有要读的击杀：MP4 拼好就剪，勾了哪些事件按开录时的设置"] = (
            len(calls) == 1 and calls[0]["picked"] == highlights.choices({}))
        calls.clear()
        _, before = play(snips=True)
        out[f"只录高光、有击杀要读：结算画面上不剪，回到大厅读完、标上击杀以后才剪（剪的时候 .json 里已经有 {calls[0]['kinds'] if calls else None}）"] = (
            before == 0 and len(calls) == 1 and calls[0]["kinds"] == ["kill"])
        calls.clear()
        _, before = play(snips=True, lobby=False)
        out["只录高光、没回大厅就停止记录：停止时读完击杀、剪完"] = before == 0 and len(calls) == 1
        calls.clear()
        settings.pop("recording_mode")
        play(snips=True)
        play(snips=False)
        out["整局录像（默认）：不剪"] = calls == []
    finally:
        highlights.keep_only = real
    return out


WINDOW_CHILD = r"""
import tkinter as tk
root = tk.Tk()
root.title("Evo 录像测试窗口")
root.geometry("400x300+40+40")
root.configure(bg="#3060c0")
root.after(8000, root.destroy)
print("ready", flush=True)
root.mainloop()
"""


def knock_checks():
    """hud.knock：在中间那条横带里找"击倒"两个字（参照图贴到不同的位置都找得到），没有就是 None。"""
    import numpy as np
    import hud
    reader = hud.Hud(None)
    k = reader.cfg["knock"]
    tpl = reader.knock_template
    out = {}
    if tpl is None:
        return {"中间那一行的'击倒'：有参照图 config/templates/hud/knock.png": False}
    found = []
    for at in (300, 450):  # 名字长短不同，这一行左右挪
        frame = np.full((1080, 1920, 3), 30, np.uint8)
        x0, y0 = k["band"][0] + at, k["band"][1] + 31
        frame[y0:y0 + tpl.shape[0], x0:x0 + tpl.shape[1]] = tpl[:, :, None]
        found.append(reader.knock(frame))
    blank = reader.knock(np.full((1080, 1920, 3), 30, np.uint8))
    out[f"中间那一行的'击倒'：贴在横带里不同的位置都找得到（{found}），没有时是空"] = (
        found[0] is not None and abs(found[0] - 300) <= 2 and found[1] is not None and abs(found[1] - 450) <= 2
        and blank is None)
    helper = reader.knock_assist_template
    if helper is None:
        out["助攻击倒：有参照图 config/templates/hud/knock_assist.png"] = False
        return out
    frame = np.full((1080, 1920, 3), 30, np.uint8)
    x0, y0 = k["band"][0] + 400, k["band"][1] + 31
    frame[y0:y0 + tpl.shape[0], x0:x0 + tpl.shape[1]] = tpl[:, :, None]
    plain = reader.knock_assist(frame, reader.knock(frame))
    frame[y0:y0 + helper.shape[0], x0 - 73:x0 - 73 + helper.shape[1]] = helper[:, :, None]  # "助攻"在"击倒"左边 73 像素
    with_helper = reader.knock_assist(frame, reader.knock(frame))
    out[f"助攻击倒：'击倒'左边贴上'助攻'才算（没贴 {plain}、贴了 {with_helper}）"] = plain is False and with_helper is True
    return out


def window_checks(ffmpeg):
    """开一个测试窗口：按进程号找到它（不打开那个进程），只抓这个窗口抓得到。"""
    base = getattr(sys, "_base_executable", sys.executable)  # 虚拟环境的 python.exe 会再开一个真正的 Python：窗口在那个进程里
    child = subprocess.Popen([base, "-c", WINDOW_CHILD], stdout=subprocess.PIPE, text=True)
    try:
        child.stdout.readline()
        time.sleep(0.5)
        hwnd = recorder.find_window(child.pid)
        size, why = recorder.capture_size(ffmpeg, recorder.Options(source="window"), recorder.load_rules(), hwnd)
        nobody = recorder.find_window(4)  # System 进程：没有窗口
    finally:
        child.kill()
    return {f"找窗口：按进程号找到测试窗口（{hwnd}），只抓这个窗口抓得到（{size or why}）；没有窗口的进程返回空":
            bool(hwnd) and size is not None and size[0] >= 390 and nobody is None}


JOB_CHILD = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import process_info
p = subprocess.Popen([sys.argv[2], "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=r=5",
                      "-f", "null", "-"], stdin=subprocess.DEVNULL)
print(p.pid, process_info.kill_with_me(p), flush=True)
time.sleep(60)
"""


def job_checks(ffmpeg):
    child = subprocess.Popen([sys.executable, "-c", JOB_CHILD, str(Path(recorder.__file__).parent), ffmpeg],
                             stdout=subprocess.PIPE, text=True)
    pid, ok = child.stdout.readline().split()
    alive_before = int(pid) in process_info.snapshot()
    child.kill()
    child.wait()
    time.sleep(1.5)
    return {"作业对象：开 ffmpeg 的程序被强行结束，ffmpeg 也跟着退": ok == "True" and alive_before
            and int(pid) not in process_info.snapshot()}


def main():
    print("录像的录制核心")
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        checks = pure_checks(tmp)
        checks.update(highlight_checks())
        checks.update(match_checks(tmp))
        checks.update(match_highlight_checks(tmp))
        checks.update(knock_checks())
        ffmpeg = recorder.find_ffmpeg()
        if not ffmpeg:
            print("  （电脑上没有 ffmpeg：跳过真的录的那些）")
            return report(checks)
        checks.update(job_checks(ffmpeg))
        if not recorder.pick_encoder(ffmpeg, recorder.Options(source=TEST_SOURCE), recorder.load_rules()):
            print("  （这台电脑上没有能用的显卡硬件编码器：跳过真的录的那些）")
            return report(checks)
        checks.update(window_checks(ffmpeg))
        checks.update(record_checks(ffmpeg, tmp))
        checks.update(highlight_record_checks(ffmpeg, tmp))
        if recorder.pick_encoder(ffmpeg, recorder.Options(source=TEST_SOURCE, codec="av1"), recorder.load_rules()):
            checks.update(record_checks(ffmpeg, tmp, "av1"))
            checks.update(highlight_record_checks(ffmpeg, tmp, "av1"))
        else:
            print("  （这台电脑的显卡编不了 AV1：跳过 AV1 真录的那些）")
    return report(checks)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
