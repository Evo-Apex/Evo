"""自动剪高光：从一局的整局录像里按事件切出片段（docs/录像.md 定下来的第 10 条，2026-11-21 用户定的，照 Outplayed 的设置页）。

三处用它：
- 录制模式"只录高光"（match_recording.py）：照样录整局，一局结束、击杀助攻读完以后 keep_only：切出片段，再删掉整局
  （一个勾了的事件都没有时整局留着，2026-11-21 用户定的；切不成也留着）
- 录像页上整局录像的"保存高光"按钮（panel_recordings.py）：save，整局留着。已经存过高光的不能再点（用户定的；自己剪的不算）
- 录像页上的"快速剪辑"（2026-11-26 用户要的，docs/录像.md 定下来的第 11 条）：save_edit，玩家在一局里挑几段、排好先后，
  直接切、接成一个视频（"整局_剪辑1"），整局留着。用户定的：直接切不重新压缩、先只剪一局、剪辑的不自动删（硬盘超过上限
  从最旧的删起时跳过它，recorder.enforce_limit）

怎么切：
- 设置页勾哪些事件算高光、每种事件"前几秒、后几秒"（choices：默认在 config/recording.json 的 highlights，玩家改的在
  settings.json 的 recording_highlights）。每个勾了的事件从 t − 前几秒 到 t + 后几秒，前后重叠（或刚好接上）的合成一段（plan）
- 直接切、不重新压缩（ffmpeg -c copy）：开头挪到它前面最近的关键帧（录像每 keyframe_seconds 秒一个，最多早 2 秒），
  关键帧的位置直接读 MP4 里记着的表（keyframe_times，不到 0.1 秒；读不了再用 ffmpeg 只解关键帧，10 分钟约 2.5 秒）。
  2026-11-21 用 N 卡 3 个 B 帧的录像试过：片段开头和原片逐帧一样，声音对得上，几条音轨都在；结尾因为 B 帧少最后两三帧（约 0.05 秒）
- 片段和整局放在同一个月份文件夹里，名字按这一段里最要紧的事件起：整局的名字 + "_击杀1"、"_击倒1"、"_击杀2"……
  （2026-11-21 用户要的），旁边同名的 .json（这一段的事件，时刻是片段里的位置；从整局第几秒到第几秒；这一局的传奇、
  开始时刻，整局删了也能对上战绩）和缩略图 .jpg。哪些是某一局的片段看 .json 的 highlight.of（existing）
- 先切到 .mp4.part 再改名：面板（另一个进程）列录像时不会看到切了一半的

只用标准库和 recorder（面板也导入它）。
"""

import bisect
import json
import math
import re
import struct
import tempfile
from pathlib import Path

import recorder

MODE_KEY = "recording_mode"  # 录制模式：full = 整局录像（默认）/ highlights = 只录高光
MODES = ("full", "highlights")
CHOICES_KEY = "recording_highlights"  # 玩家改过的 {事件: {"on": 勾没勾, "before": 前几秒, "after": 后几秒}}
NAMES = {  # 片段的名字、日志里写的
    "kill": "击杀", "knock": "击倒", "assist_knock": "助攻击倒", "assist": "助攻", "champion": "冠军",
    "knocked": "倒地", "revived": "被扶起", "respawned": "被复活", "dead": "成盒",
}
# 一段里有好几个事件时按哪个起名：越靠前越要紧（和录像页时间轴上合成一个的标记画哪个图标一样）
PRIORITY = ("champion", "kill", "dead", "respawned", "knocked", "revived", "knock", "assist_knock", "assist")
# 切的时候 -ss 正好写关键帧的时刻（精确到微秒）：晚一点点（试过晚 0.0005 秒）那个关键帧在片段里就落在 0 之前，
# 播放器不显示它，片段从下一帧开始（2026-11-21 试的）；早一点点没关系（ffmpeg 按解码时刻找，关键帧的解码时刻还要早几帧）
EDIT_KIND = recorder.KEEP_KIND  # 快速剪辑存的视频：.json 里 highlight.kind 是它（不自动删，recorder.enforce_limit 认这个）
EDIT_NAME = "剪辑"  # 名字：整局的名字 + "_剪辑1"、"_剪辑2"……
EDIT_MAX_PIECES = 30  # 一个剪辑最多几段
EDIT_MIN_SECONDS = 0.5  # 每段至少多长（页面上也按这个挡）


class HighlightError(Exception):
    """切不成，原因在 str(e) 里（写在页面上、日志里）。"""


# ---------- 设置 ----------

def config(rules=None):
    return (rules or recorder.load_rules()).get("highlights") or {"events": {}, "max_seconds": 60}


def mode(settings):
    value = settings.get(MODE_KEY)
    return value if value in MODES else MODES[0]


def choices(settings, rules=None):
    """{事件: {"on", "before", "after"}}，按 config 里的顺序：玩家改过的用玩家的，没改过的、不对的用默认（秒数限在 0 到 max_seconds）。"""
    cfg = config(rules)
    top = cfg.get("max_seconds", 60)
    saved = settings.get(CHOICES_KEY)
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for kind, default in cfg["events"].items():
        mine = saved.get(kind) if isinstance(saved.get(kind), dict) else {}

        def seconds(key):
            value = mine.get(key, default[key])
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= top:
                return default[key]
            return value
        on = mine.get("on", default["on"])
        out[kind] = {"on": on if isinstance(on, bool) else default["on"], "before": seconds("before"), "after": seconds("after")}
    return out


# ---------- 算片段 ----------

def plan(events, duration, picked, keyframes=None):
    """要切的几段 [{"start", "end", "hits": [勾了的事件]}]，按时间排好。每个勾了的事件从 t − 前几秒 到 t + 后几秒（不超出录像），
    开头挪到它前面最近的关键帧（给了 keyframes 的话），前后重叠或刚好接上的合成一段（连着几个击杀是一段）。"""
    windows = []
    for e in events:
        c, t = picked.get(e.get("kind")), e.get("t")
        if not c or not c.get("on") or isinstance(t, bool) or not isinstance(t, (int, float)):
            continue
        start, end = max(0.0, t - c["before"]), min(float(duration), t + c["after"])
        if keyframes:
            start = snap(keyframes, start)
        if end > start:
            windows.append((start, end, e))
    windows.sort(key=lambda w: (w[0], w[1]))
    out = []
    for start, end, e in windows:
        if out and start <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], end)
            out[-1]["hits"].append(e)
        else:
            out.append({"start": start, "end": end, "hits": [e]})
    return out


def snap(keyframes, t):
    """t 前面最近的关键帧（正好在关键帧上就是它）：直接切时片段只能从关键帧开始。"""
    i = bisect.bisect_right(keyframes, t + 1e-6) - 1
    return keyframes[i] if i >= 0 else 0.0


def top_kind(hits):
    """一段里最要紧的事件（PRIORITY），片段按它起名。"""
    return min((e["kind"] for e in hits), key=lambda k: PRIORITY.index(k) if k in PRIORITY else len(PRIORITY))


def names(segments):
    """每一段的名字：最要紧的事件 + 这种事件的第几段（击杀1、击倒1、击杀2……，按时间数）。"""
    seen, out = {}, []
    for seg in segments:
        kind = top_kind(seg["hits"])
        seen[kind] = seen.get(kind, 0) + 1
        out.append((kind, f"{NAMES.get(kind, kind)}{seen[kind]}"))
    return out


# ---------- MP4 里的关键帧 ----------

def _boxes(data, start, end):
    """data[start:end] 里一个挨一个的 box：[(类型, 内容开始, 内容结束)]。"""
    out, i = [], start
    while i + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, i)
        head = 8
        if size == 1:
            if i + 16 > end:
                break
            size, head = struct.unpack_from(">Q", data, i + 8)[0], 16
        elif size == 0:
            size = end - i
        if size < head or i + size > end:
            break
        out.append((kind, i + head, i + size))
        i += size
    return out


def _child(data, start, end, *path):
    for name in path:
        found = next(((a, b) for kind, a, b in _boxes(data, start, end) if kind == name), None)
        if found is None:
            return None
        start, end = found
    return start, end


def _moov(path):
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return None
            size, kind = struct.unpack(">I4s", head)
            head_size = 8
            if size == 1:
                size, head_size = struct.unpack(">Q", f.read(8))[0], 16
            if kind == b"moov":
                return f.read(size - head_size) if size else f.read()
            if size < head_size:
                return None
            f.seek(size - head_size, 1)


def _timescale(data, box):
    """mvhd、mdhd 里的时间单位（每秒多少格）：版本 1 的时刻是 8 字节。"""
    return struct.unpack_from(">I", data, box[0] + (20 if data[box[0]] == 1 else 12))[0]


def _table(data, box, fmt):
    """stts、ctts、stss 这种表：[每一项]（版本、标志 4 字节，项数 4 字节，后面一项一项）。"""
    count = struct.unpack_from(">I", data, box[0] + 4)[0]
    size = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, data, box[0] + 8 + i * size) for i in range(count)]


def keyframe_times(path):
    """MP4 画面里每个关键帧在第几秒（和 ffmpeg、播放器算的一样：解码时刻 + 显示偏移 − 编辑表的起点），从小到大。
    直接读文件开头的索引（moov 里的 stts、ctts、stss、elst），不解码。读不出返回 None。"""
    try:
        moov = _moov(path)
        if not moov:
            return None
        movie_scale = _timescale(moov, _child(moov, 0, len(moov), b"mvhd"))
        for kind, a, b in _boxes(moov, 0, len(moov)):
            hdlr = _child(moov, a, b, b"mdia", b"hdlr") if kind == b"trak" else None
            if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
                continue
            scale = _timescale(moov, _child(moov, a, b, b"mdia", b"mdhd"))
            stbl = _child(moov, a, b, b"mdia", b"minf", b"stbl")
            stts = _table(moov, _child(moov, *stbl, b"stts"), ">II")
            ctts_box, stss_box = _child(moov, *stbl, b"ctts"), _child(moov, *stbl, b"stss")
            ctts = _table(moov, ctts_box, ">Ii") if ctts_box else []
            total = sum(n for n, _ in stts)
            syncs = sorted(n for (n,) in _table(moov, stss_box, ">I")) if stss_box else list(range(1, total + 1))
            delay, shift = 0.0, 0
            elst = _child(moov, a, b, b"edts", b"elst")
            if elst:
                v1 = moov[elst[0]] == 1
                for duration, media_time, _ in _table(moov, elst, ">QqI" if v1 else ">IiI"):
                    if media_time == -1:  # 空的一段：画面晚这么久才开始
                        delay += duration / movie_scale
                    else:
                        shift = media_time
                        break

            def walk(entries, wanted):  # 每个想要的样本（第几个，从 1 数）→ 它在表里的值（stts 累加成解码时刻）
                found, n, acc, j = {}, 1, 0, 0
                for count, value in entries:
                    while j < len(wanted) and wanted[j] < n + count:
                        found[wanted[j]] = acc + (wanted[j] - n) * value if entries is stts else value
                        j += 1
                    n += count
                    acc += count * value
                return found
            dts, offsets = walk(stts, syncs), walk(ctts, syncs)
            return sorted(round((dts[s] + offsets.get(s, 0) - shift) / scale + delay, 6) for s in syncs if s in dts)
    except (OSError, struct.error, TypeError, IndexError, ZeroDivisionError):
        return None
    return None


def keyframes(ffmpeg, video):
    """关键帧的时刻：先读 MP4 的索引，读不出再让 ffmpeg 只解关键帧（-skip_frame nokey）。都不行返回 None。"""
    times = keyframe_times(video)
    if times:
        return times
    _, err = recorder.run_ffmpeg(ffmpeg, ["-skip_frame", "nokey", "-i", str(video), "-map", "0:v:0", "-vf", "showinfo",
                                          "-f", "null", "-"], timeout=300)
    # 用整数的 pts × 时间单位算：showinfo 的 pts_time 只有 6 位有效数字（1000 秒以后只到 0.01 秒，切的时候会差一帧）
    base = re.search(r"time_base: (\d+)/(\d+)", err or "")
    if not base:
        return None
    num, den = int(base[1]), int(base[2])
    times = sorted(round(int(x) * num / den, 6) for x in re.findall(r" pts: *(-?\d+) ", err or ""))
    return times or None


# ---------- 切 ----------

def read_meta(video):
    try:
        return json.loads(Path(video).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_clip(meta):
    return isinstance((meta or {}).get("highlight"), dict)


def is_edit(meta):
    """快速剪辑存的（玩家自己剪的），不是自动剪的高光。"""
    return is_clip(meta) and meta["highlight"].get("kind") == EDIT_KIND


def existing(video, edits=True):
    """这个整局录像已经存下的高光片段（.json 的 highlight.of 是它）：自动剪的按在整局里的先后排，自己剪的（快速剪辑）
    排在后面、按第几个排。edits=False：只要自动剪的（"保存高光"按钮看这个：自己剪过不算存过高光）。"""
    video = Path(video)
    found = []
    for clip in video.parent.glob(f"{glob_escape(video.stem)}_*.mp4"):
        meta = read_meta(clip)
        if is_clip(meta) and meta["highlight"].get("of") == video.name and (edits or not is_edit(meta)):
            hl = meta["highlight"]
            found.append(((1, hl.get("index") or 0) if is_edit(meta) else (0, hl.get("from") or 0), clip.name, clip))
    return [clip for _, _, clip in sorted(found)]


def glob_escape(text):
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def _remove(path):
    for p in (path, path.with_suffix(".json"), path.with_suffix(".jpg")):
        p.unlink(missing_ok=True)


def save(ffmpeg, video, picked, log=print):
    """按 picked（choices 的结果）从整局录像 video 切出高光，存在它旁边。返回片段的路径（按时间排）；
    一个勾了的事件都没有返回 []。切不成抛 HighlightError（切了一半的删掉，整局不动）。"""
    video = Path(video)
    if not ffmpeg:
        raise HighlightError("找不到录像要用的组件（ffmpeg）")
    meta = read_meta(video)
    if meta is None:
        raise HighlightError("这个录像没有记下事件（旁边没有 .json），切不了")
    if is_clip(meta):
        raise HighlightError("这已经是一段高光了")
    duration = meta.get("duration") or recorder.mp4_duration(video)
    if not duration:
        raise HighlightError("读不出录像有多长")
    events = sorted((e for e in meta.get("events") or [] if isinstance(e.get("t"), (int, float))), key=lambda e: e["t"])
    if not plan(events, duration, picked):
        return []
    keys = keyframes(ffmpeg, video)
    if not keys:
        raise HighlightError("读不出录像里的关键帧")
    segments = plan(events, duration, picked, keys)
    titles = recorder.track_title_args(((meta.get("audio") or {}).get("tracks")))
    made = []  # [(片段, 临时文件, 这一段, 名字, 最要紧的事件)]
    try:
        for seg, (kind, name) in zip(segments, names(segments)):
            out = video.with_name(f"{video.stem}_{name}.mp4")
            part = out.with_name(out.name + ".part")
            code, err = recorder.run_ffmpeg(ffmpeg, [
                "-y", "-loglevel", "error", "-ss", f"{seg['start']:.6f}", "-to", f"{seg['end']:.6f}",
                "-i", str(video), "-map", "0", "-c", "copy", *titles, "-movflags", "+faststart", "-f", "mp4", str(part)],
                timeout=300)
            if code != 0 or not part.exists() or part.stat().st_size == 0:
                lines = [x for x in (err or "").strip().splitlines() if x.strip()]
                raise HighlightError(f"切{name}那一段时 ffmpeg 出错（{lines[-1] if lines else code}）")
            made.append((out, part, seg, name, kind))
    except HighlightError:  # 切了一半的（包括出错的那一个）都删掉
        for leftover in video.parent.glob(f"{glob_escape(video.stem)}_*.mp4.part"):
            leftover.unlink(missing_ok=True)
        raise
    clips = []
    for index, (out, part, seg, name, kind) in enumerate(made, 1):
        start, end = seg["start"], seg["end"]
        clip_meta = {
            "version": 1, "video": out.name,
            "highlight": {"of": video.name, "name": name, "kind": kind, "index": index, "count": len(made),
                          "from": round(start, 3), "to": round(end, 3), "of_duration": duration,
                          "hits": [e["kind"] for e in seg["hits"]]},
            "started": meta.get("started"), "ended": meta.get("ended"), "match": meta.get("match") or {},
            "first_frame": meta.get("first_frame"),
            "events": [{**e, "t": round(e["t"] - start, 3)} for e in events if start <= e["t"] <= end],
            "outages": [[round(max(a, start) - start, 2), round(min(b, end) - start, 2)]
                        for a, b in meta.get("outages") or [] if b > start and a < end],
            **{k: meta.get(k) for k in ("options", "encoder", "b_frames", "size", "bitrate", "audio")},
        }
        out.with_suffix(".json").write_text(json.dumps(clip_meta, ensure_ascii=False, indent=1), encoding="utf-8")
        part.replace(out)  # 先写好 .json 再改名：面板看到这个 MP4 时它已经是一段高光
        clip_meta["duration"] = round(recorder.mp4_duration(out) or end - start, 2)
        out.with_suffix(".json").write_text(json.dumps(clip_meta, ensure_ascii=False, indent=1), encoding="utf-8")
        main = next(e for e in seg["hits"] if e["kind"] == kind)
        recorder.make_thumbnail(ffmpeg, out, min(max(0.0, main["t"] - start - 1), clip_meta["duration"]))
        clips.append(out)
    total = sum(seg["end"] - seg["start"] for _, _, seg, _, _ in made)
    log(f"录像：存好了 {len(clips)} 段高光（{'、'.join(name for _, _, _, name, _ in made)}，共 {total:.0f} 秒）")
    return clips


def keep_only(ffmpeg, video, picked, log=print):
    """录制模式"只录高光"：切出高光以后删掉整局（MP4 和旁边的 .json、.jpg）。一个勾了的事件都没有，整局留着
    （2026-11-21 用户定的）；切不成也留着。返回片段的路径。"""
    video = Path(video)
    try:
        clips = save(ffmpeg, video, picked, log)
    except HighlightError as e:
        log(f"录像：剪高光没成（{e}），整局留着")
        return []
    if not clips:
        log("录像：这一局没有勾了的事件，整局留着（录制模式是只录高光）")
        return []
    try:
        _remove(video)
    except OSError as e:
        log(f"录像：高光存好了，整局删不掉（{e}），留着")
        return clips
    log(f"录像：只录高光，整局删了（{video.name}）")
    return clips


# ---------- 快速剪辑（2026-11-26） ----------

def check_pieces(pieces, duration):
    """玩家挑的几段 [[起, 止], …]（按玩家排的先后）→ [(起, 止)]，限在录像里。不对抛 HighlightError（原因写在页面上）。
    可以重叠、可以倒着排（同一段放两遍、先放后面的都行）。"""
    if not isinstance(pieces, list) or not pieces:
        raise HighlightError("还没有挑要剪的段")
    if len(pieces) > EDIT_MAX_PIECES:
        raise HighlightError(f"最多 {EDIT_MAX_PIECES} 段")
    out = []
    for p in pieces:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in p)):
            raise HighlightError("段的起点、终点不对，刷新一下页面再试")
        start, end = max(0.0, float(p[0])), min(float(duration), float(p[1]))
        if end - start < EDIT_MIN_SECONDS:
            raise HighlightError(f"每段至少 {EDIT_MIN_SECONDS:g} 秒（终点要在起点后面）")
        out.append((start, end))
    return out


def _last_line(err):
    lines = [x for x in (err or "").strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def save_edit(ffmpeg, video, pieces, log=print):
    """快速剪辑：从整局录像 video 按玩家挑的几段 pieces（check_pieces）直接切、按顺序接成一个视频，存在整局旁边
    （"整局的名字_剪辑1"，已经有了就 2、3……）。返回它的路径；不成抛 HighlightError（切了一半的删掉，整局不动）。

    每段开头挪到前面最近的关键帧（和剪高光一样，最多早 keyframe_seconds 秒；页面上画的、列的已经是挪过的）。
    先一段一段切到临时文件夹（在月份文件夹里，名字以 _剪辑_ 开头：面板只列月份文件夹里的 MP4，看不到），再按 ffmpeg 的
    拼接列表接起来（同一个录像切的，格式都一样，直接接）。几条音轨照样带上、照写名字。旁边的 .json：highlight.kind 是
    EDIT_KIND（不自动删）、pieces（每段在整局里从第几秒到第几秒），事件换成剪辑里的位置。整局录像没有 .json（崩了拼回来的
    "_未完成"）也能剪，只是没有事件。"""
    video = Path(video).resolve()  # 接起来那一步在临时文件夹里运行：输出要写完整路径
    if not ffmpeg:
        raise HighlightError("找不到录像要用的组件（ffmpeg）")
    meta = read_meta(video) or {}
    if is_clip(meta):
        raise HighlightError("只能从整局录像里剪")
    duration = meta.get("duration") or recorder.mp4_duration(video)
    if not duration:
        raise HighlightError("读不出录像有多长")
    asked = check_pieces(pieces, duration)
    keys = keyframes(ffmpeg, video)
    if not keys:
        raise HighlightError("读不出录像里的关键帧")
    cuts = [(snap(keys, start), end) for start, end in asked]
    index = 1
    while video.with_name(f"{video.stem}_{EDIT_NAME}{index}.mp4").exists():
        index += 1
    name = f"{EDIT_NAME}{index}"
    out = video.with_name(f"{video.stem}_{name}.mp4")
    part = out.with_name(out.name + ".part")
    titles = recorder.track_title_args((meta.get("audio") or {}).get("tracks"))
    lengths = []
    with tempfile.TemporaryDirectory(prefix="_剪辑_", dir=video.parent, ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        for i, (start, end) in enumerate(cuts, 1):
            piece = tmp / f"{i:03d}.mp4"
            # -copypriorss 0：起点以前的不要。不加时声音会多带约 1 秒"预读"（单独放时被编辑表藏起来，按拼接列表接的时候
            # 当成了正常的声音：剪辑变长、后面声音晚 1 秒，2026-11-26 测出来的）；画面正好从关键帧切，前面本来就没有
            code, err = recorder.run_ffmpeg(ffmpeg, [
                "-y", "-loglevel", "error", "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
                "-i", str(video), "-map", "0", "-c", "copy", "-copypriorss", "0", "-f", "mp4", str(piece)], timeout=300)
            if code != 0 or not piece.exists() or piece.stat().st_size == 0:
                raise HighlightError(f"切第 {i} 段时 ffmpeg 出错（{_last_line(err) or code}）")
            lengths.append(recorder.mp4_duration(piece) or end - start)
        (tmp / "list.txt").write_text("ffconcat version 1.0\n" + "".join(f"file '{i:03d}.mp4'\n" for i in range(1, len(cuts) + 1)),
                                      encoding="utf-8")
        code, err = recorder.run_ffmpeg(ffmpeg, [
            "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-map", "0", "-c", "copy", *titles,
            "-movflags", "+faststart", "-f", "mp4", str(part)], timeout=600, cwd=tmp)
        if code != 0 or not part.exists() or part.stat().st_size == 0:
            part.unlink(missing_ok=True)
            raise HighlightError(f"接成一个视频时 ffmpeg 出错（{_last_line(err) or code}）")
    # 每段在剪辑里从第几秒开始：前面几段的长度加起来（拼接列表就是这样一段接一段排的）
    offsets, at = [], 0.0
    for length in lengths:
        offsets.append(at)
        at += length
    events = sorted((e for e in meta.get("events") or [] if isinstance(e.get("t"), (int, float))), key=lambda e: e["t"])
    mapped, gaps = [], []
    for (start, end), offset in zip(cuts, offsets):
        mapped += [{**e, "t": round(e["t"] - start + offset, 3)} for e in events if start <= e["t"] <= end]
        gaps += [[round(max(a, start) - start + offset, 2), round(min(b, end) - start + offset, 2)]
                 for a, b in meta.get("outages") or [] if b > start and a < end]
    clip_meta = {
        "version": 1, "video": out.name,
        "highlight": {"of": video.name, "name": name, "kind": EDIT_KIND, "index": index,
                      "from": round(cuts[0][0], 3), "to": round(cuts[-1][1], 3), "of_duration": duration,
                      "pieces": [[round(s, 3), round(e, 3)] for s, e in cuts]},
        "started": meta.get("started"), "ended": meta.get("ended"), "match": meta.get("match") or {},
        "first_frame": meta.get("first_frame"), "events": mapped, "outages": gaps,
        **{k: meta.get(k) for k in ("options", "encoder", "b_frames", "size", "bitrate", "audio")},
    }
    out.with_suffix(".json").write_text(json.dumps(clip_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    part.replace(out)  # 先写好 .json 再改名：面板看到这个 MP4 时它已经是一个剪辑
    clip_meta["duration"] = round(recorder.mp4_duration(out) or at, 2)
    out.with_suffix(".json").write_text(json.dumps(clip_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    recorder.make_thumbnail(ffmpeg, out, min(recorder.thumbnail_time(sorted(mapped, key=lambda e: e["t"]), clip_meta["duration"]),
                                             max(0.0, clip_meta["duration"] - 0.1)))
    log(f"录像：剪好了 {out.name}（{len(cuts)} 段，共 {clip_meta['duration']:.0f} 秒）")
    return out
