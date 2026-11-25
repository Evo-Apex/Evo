"""Evo 窗口的"录像"页（2026-11-20 用户要求；录像怎么录见 recorder.py、match_recording.py、docs/录像.md）：整局录像的列表和播放。

列表 /recordings（recordings.html）
- 每局一张卡片：缩略图（录完时截的；没有的话第一次打开时用 ffmpeg 截，存在录像旁边）、时长、传奇、模式和地图、时间，
  对上的那一局战绩（名次徽章、击杀、伤害）和事件数。从新到旧。上面一行：共几个、占多大、上限、"打开文件夹"
- 上次 Evo 崩了拼回来的（名字带"_未完成"，没有 .json）也列出来，标"未完成"
播放 /recordings/watch?v=<年-月/文件名>（recordings_watch.html）
- 播放器（不自动播放）、下面自己画的时间轴：事件标在对应的位置（击倒对手、助攻击倒、倒地、被扶起、成盒、被复活、冠军、击杀、助攻），
  断掉的那几段画成斜线；点事件从它前 EVENT_LEAD 秒开始放。挨得很近的几个（相隔不到 GROUP_GAP 的时长、至少 GROUP_MIN_SECONDS 秒，
  在进度条上会叠在一起）合成一个标记：上下分成几段颜色、角上写个数，悬停写出全部，点了从第一个前 EVENT_LEAD 秒开始放（2026-11-21
  用户选的）。事件也按时间一条一条列在下面（按钮，键盘、读屏软件都能用）
- 网址带 t=秒：从那一刻开始（战绩页以后可以直接跳到某个事件）
- "在资源管理器中显示"、"删除"（先确认；删 MP4 和同名的 .json、.jpg）
高光片段（highlights.py，2026-11-21）：片段也是一个录像（旁边的 .json 里有 highlight），按它是哪一局的（highlight.of）和整局归成一组：
- 列表一局一张卡片（groups）：有片段的写"高光 N 段"；整局删了（只录高光）的标"只有高光"，点了放第一段
- 播放页下面"高光片段"：这一局的每一段（缩略图、名字"击杀 1"、在整局里的哪一段、时长、里面的事件），正在放的那段标出来；
  整局录像上"保存高光"（POST /recordings/highlights，按设置页现在勾的切，整局留着；已经存过的不能再点，2026-11-21 用户定的）
录像文件 /recordings/video?v=…（send_file 支持 Range：播放器能拖进度条）、缩略图 /recordings/thumb?v=…
和战绩对上：按时间（.json 里的开始时刻 + 时长），战绩里那一局的结束时刻落在录像里（往后留 MATCH_SLACK）就算同一局。
不改数据库。战绩页有录像的局显示一个小按钮（match_links）
导航上的"录像"：设置里打开了录像、或者已经有录像时才显示（visible）：没打开的玩家看不到这一页（内测中）
设置页的"录像"一栏（_settings_recording.html，模板里调 recording_settings()）：找不到 ffmpeg、也没开着时整栏不显示（0.7.0b 只有测试版
带 ffmpeg；0.7.0 起正式版也带，2026-11-21 用户定的，照旧标"实验"）。开关（默认关，原则 2）；打开以后才显示画质
（分辨率、帧数、画质、编码格式，下面估一局多大）、硬盘上限、保存位置。打开时检测一次这台电脑能用哪些显卡硬件编码器
（CHECK_KEY，记在 settings.json），没有 ffmpeg、没有硬件编码器时打不开。编码格式只有一种（现在只有 H.264，AV1 2026-11-20
先拿掉了）时不显示那一项；有几种时显卡编不了的那种不能选。录制模式（整局录像 / 只录高光）、高光包括哪些事件、每种事件前后录几秒
（滑块，2026-11-21 用户要的），一起保存（/settings/recording/highlights）。
改了从下一局开始生效（match_recording 每局开录时才读设置）。保存的结果写在那一项下面（NOTE_KEY），不用页面最上面的提示
（保存完跳回这一栏，最上面看不到）
这个文件不加载识别用的库。规则见 docs/面板.md"录像页"。
"""

import atexit
import ctypes
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)

import app_settings
import data_move
import highlights
import recorder
import storage
from paths import DATA_DIR, EXE_DIR

bp = Blueprint("recordings", __name__)
EVENTS = {  # 事件 → (显示名, 样式)：颜色以外还写着名字（时间轴上悬停、下面的列表），不只靠颜色分
    "knocked": ("倒地", "ev-knocked"),
    "revived": ("被扶起", "ev-revived"),
    "dead": ("成盒", "ev-dead"),
    "respawned": ("被复活", "ev-respawned"),
    "knock": ("击倒", "ev-knock"),
    "assist_knock": ("助攻击倒", "ev-assist-knock"),
    "champion": ("冠军", "ev-champion"),
    "kill": ("击杀", "ev-kill"),
    "assist": ("助攻", "ev-assist"),
}
EVENT_LEAD = 3  # 点事件时从它前几秒开始放：看得到是怎么发生的
GROUP_GAP = 0.024  # 进度条上相隔不到时长的这么多（约一个标记宽：标记 2026-11-21 换成 22 像素的圆形图标）的事件合成一个标记
GROUP_MIN_SECONDS = 1.0  # 再短的录像，相隔这么近的也合成一个
# 合成一个的标记画哪个事件的图标：越靠前越要紧（击杀不会被旁边的击倒、助攻盖住）
MARK_PRIORITY = ("ev-champion", "ev-kill", "ev-dead", "ev-respawned", "ev-knocked", "ev-revived", "ev-knock", "ev-assist-knock",
                 "ev-assist")
MATCH_SLACK = timedelta(minutes=2)  # 战绩里那一局的结束时刻比录像的结尾晚这么多以内，也算同一局（结算画面上才记下）
MODES = {"ranked": "排位", "pubs": "匹配", "other": "其他"}
DEFAULT_LIMIT_GB = 30  # 和 match_recording.DEFAULT_LIMIT_GB 一样（那个文件加载识别用的库，这里不导入）

# ---- 设置页"录像"一栏 ----
ENABLED_KEY = "recording_enabled"  # 和 match_recording.SETTING 一样
CHECK_KEY = "recording_check"  # {"ffmpeg": 路径, "h264": 能用的编码器或 None, …（config 里每种编码格式一项）}：打开录像时检测，ffmpeg 换了重新检测
NOTE_KEY = "recording_note"  # 刚保存的结果 {"where": 哪一项, "text": …, "error": 是不是出错}，显示在那一项下面一次
CHECK_SOURCE = "lavfi:color=black:s=640x360:r=30"  # 检测编码器用的测试画面（不用抓屏、不用开着游戏，约 0.5 秒）
SIZE_NAMES = {"native": "原画面", "1080p": "1080p（推荐）", "720p": "720p"}
QUALITY_NAMES = {"best": "很高", "high": "高", "standard": "标准（推荐）", "small": "省空间"}
CODEC_NAMES = {"h264": "H.264", "av1": "AV1"}
AUDIO_SOURCE_NAMES = {"game": "只录 Apex 的声音", "system": "电脑上所有的声音"}  # 2026-11-21 用户定的两档
MIC_TEST_SECONDS = 5  # 测试麦克风录几秒（2026-11-21 用户要的，像 Zoom 那样：录一段、按滑块的音量放给自己听）
MIC_TEST_DIR = Path(tempfile.gettempdir())  # 测试录音只放临时文件夹：每次一个新文件（上一段可能还被播放占着，Windows 删不掉），
# 旧的能删就删，面板关掉时再删一遍
MIC_FACTORY = None  # 测试用：换掉录音（默认 process_audio.ProcessAudio）
FAMILY_NAMES = {"nvenc": "NVIDIA", "amf": "AMD", "qsv": "Intel"}
MATCH_MINUTES = 20  # 估一局多大时按多少分钟算（打满一局约 20 分钟）
LIMIT_RANGE = (1, 10000)  # 硬盘上限能填多少（GB）
MODE_NAMES = {"full": "整局录像", "highlights": "只录高光"}  # 录制模式（2026-11-21 用户定的，默认整局）
HIGHLIGHT_NAMES = {"knocked": "自己倒地"}  # 设置页"高光包括"里和事件名不一样的（倒地 → 自己倒地，免得以为是击倒对手）
CLIP_NOTE_KEY = "recording_clip_note"  # 刚点完"保存高光"的结果，显示在播放页"高光片段"下面一次


def folder():
    """录像文件夹：设置里选的，没选是数据文件夹里的 recordings。"""
    return Path(app_settings.get("recording_dir") or DATA_DIR / "recordings")


def visible():
    """导航上显示"录像"吗：设置里打开了录像，或者文件夹里已经有录像。"""
    return bool(app_settings.get("recording_enabled")) or any(_videos(folder()))


def _videos(base):
    if not base.is_dir():
        return []
    return [v for v in base.glob("*/*.mp4") if v.parent.name != recorder.WORK_DIR_NAME]


def clock(seconds):
    """4:02、1:02:03。"""
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def size_text(n):
    return f"{n / 2 ** 30:.1f} GB" if n >= 2 ** 30 else f"{n / 2 ** 20:.0f} MB"


def when_text(dt):
    if dt.date() == date.today():
        return f"今天 {dt:%H:%M}"
    if dt.date() == date.today() - timedelta(days=1):
        return f"昨天 {dt:%H:%M}"
    return f"{dt:%m-%d %H:%M}"


def _started(video, meta):
    if meta.get("started"):
        try:
            return datetime.fromisoformat(meta["started"])
        except ValueError:
            pass
    try:
        return datetime.strptime(video.stem[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(video.stat().st_mtime)


def load(video):
    """一个录像的信息（字典）。"""
    meta = {}
    sidecar = video.with_suffix(".json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    started = _started(video, meta)
    duration = meta.get("duration") or recorder.mp4_duration(video) or 0
    events = []
    for e in sorted(meta.get("events", []), key=lambda e: e.get("t") or 0):  # 击杀是赛后才补进去的，按时间排
        if e.get("kind") in EVENTS and isinstance(e.get("t"), (int, float)):
            label, css = EVENTS[e["kind"]]
            if e["kind"] in ("kill", "assist") and (e.get("count") or 1) > 1:  # 一下子多了几个（两次截图之间连着两个）
                label = f"{label} ×{e['count']}"
            events.append({"t": e["t"], "clock": clock(e["t"]), "label": label, "css": css,
                           "left": min(100.0, max(0.0, e["t"] / duration * 100)) if duration else 0})
    marks = group_marks(events, duration)
    outages = [{"left": a / duration * 100, "width": max(0.4, (b - a) / duration * 100), "seconds": b - a}
               for a, b in meta.get("outages", []) if duration and b > a]
    legend = (meta.get("match") or {}).get("legend")
    # 高光片段：开始时刻、"哪一局"都按整局算（和战绩对上、和整局归成一组）
    hl = meta["highlight"] if highlights.is_clip(meta) else None
    span = (hl.get("of_duration") if hl else None) or duration
    clip = None
    if hl:
        start, stop = hl.get("from") or 0, hl.get("to") or 0
        clip = {"name": spaced(hl.get("name") or "高光"), "index": hl.get("index") or 0, "from": start,
                "range": f"{clock(start)}–{clock(stop)}", "kinds": list(dict.fromkeys(e["css"] for e in events))}
    group = f"{video.parent.name}/{Path(hl['of']).stem}" if hl and hl.get("of") else f"{video.parent.name}/{video.stem}"
    return {
        "id": f"{video.parent.name}/{video.stem}", "path": video, "started": started,
        "end": started + timedelta(seconds=(meta.get("first_frame") or 0) + span),
        "when": when_text(started), "duration": duration, "clock": clock(duration),
        "size": video.stat().st_size, "size_text": size_text(video.stat().st_size),
        "legend": legend, "title": legend or "录像", "unfinished": "_未完成" in video.stem,
        "events": events, "marks": marks, "outages": outages, "match": None,
        "clip": clip, "group": group,
    }


def spaced(name):
    """"击杀1" → "击杀 1"（汉字和数字之间空一格，和面板别处一样）。"""
    return re.sub(r"(\D)(\d)", r"\1 \2", name)


def groups(items):
    """列表页一局一张卡片：整局录像和它的高光片段归成一组。每组是领头的那个（有整局是整局，没有是第一段）加上
    "clips"（片段，按在整局里的先后）、"only_clips"（整局删了）、"group_size"（整局和片段一共多大）。"""
    by = {}
    for r in items:
        g = by.setdefault(r["group"], {"full": None, "clips": []})
        if r["clip"]:
            g["clips"].append(r)
        else:
            g["full"] = r
    out = []
    for g in by.values():
        clips = sorted(g["clips"], key=lambda r: r["clip"]["from"])
        lead = g["full"] or clips[0]
        card = {**lead, "clips": clips, "only_clips": g["full"] is None,
                "group_size": sum(r["size"] for r in clips) + (g["full"]["size"] if g["full"] else 0)}
        if card["only_clips"]:  # 只有高光：时长写几段加起来的
            card["clock"] = clock(sum(r["duration"] for r in clips))
        out.append(card)
    return sorted(out, key=lambda r: r["started"], reverse=True)


def group_marks(events, duration):
    """时间轴上的标记：events（按时间排好）里挨得很近的（和前一个相隔不到 GROUP_GAP 的时长、至少 GROUP_MIN_SECONDS 秒）
    合成一个。每个标记 {"t": 第一个的时刻, "left": 位置, "events": [...], "css": 画哪个事件（MARK_PRIORITY 里最靠前的），
    "label": 悬停写的}。"""
    gap = max(GROUP_MIN_SECONDS, (duration or 0) * GROUP_GAP)
    groups = []
    for e in events:
        if groups and e["t"] - groups[-1][-1]["t"] <= gap:
            groups[-1].append(e)
        else:
            groups.append([e])
    def rank(e):
        return MARK_PRIORITY.index(e["css"]) if e["css"] in MARK_PRIORITY else len(MARK_PRIORITY)

    return [{"t": g[0]["t"], "left": g[0]["left"], "events": g, "css": min(g, key=rank)["css"],
             "label": f"{g[0]['label']} {g[0]['clock']}" if len(g) == 1 else " · ".join(f"{e['clock']} {e['label']}" for e in g)}
            for g in groups]


def entries():
    """全部录像，从新到旧。"""
    return sorted((load(v) for v in _videos(folder())), key=lambda r: r["started"], reverse=True)


def _db():
    if "db" not in g:
        g.db = storage.connect(current_app.config["DB"])  # panel_app 在请求结束时关掉
    return g.db


def attach_matches(items):
    """给每个录像找战绩里的那一局（结束时刻落在录像里，往后留 MATCH_SLACK），找到的放进 item["match"]。"""
    if not items:
        return items
    lo = min(r["started"] for r in items)
    hi = max(r["end"] for r in items) + MATCH_SLACK
    rows = _db().execute(
        "SELECT match_id, placement, kills, damage, mode, map, legend, ended_at FROM matches "
        "WHERE ended_at BETWEEN ? AND ? ORDER BY ended_at",
        (f"{lo:%Y-%m-%d %H:%M:%S}", f"{hi:%Y-%m-%d %H:%M:%S}")).fetchall()
    for r in items:
        best = None
        for m in rows:
            ended = datetime.strptime(m["ended_at"], "%Y-%m-%d %H:%M:%S")
            if r["started"] <= ended <= r["end"] + MATCH_SLACK:
                gap = abs((ended - r["end"]).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, m)
        if best:
            m = best[1]
            r["match"] = {"id": m["match_id"], "placement": m["placement"], "kills": m["kills"],
                          "damage": m["damage"], "mode": MODES.get(m["mode"]), "map": m["map"]}
            r["legend"] = r["legend"] or m["legend"]
            r["title"] = r["legend"] or "录像"
    return items


def match_links(rows):
    """战绩页：{战绩编号: 录像的 id}（有录像的局）。没打开录像、也没有录像时不扫文件夹，返回 {}。"""
    if not visible():
        return {}
    links = {}  # 战绩编号 → (整局排在片段前面、片段按先后, 录像的 id)：有整局去整局，只有高光去第一段
    for r in attach_matches(entries()):
        if r["match"]:
            rank = (1, r["clip"]["from"]) if r["clip"] else (0, 0)
            if r["match"]["id"] not in links or rank < links[r["match"]["id"]][0]:
                links[r["match"]["id"]] = (rank, r["id"])
    return {row["match_id"]: links[row["match_id"]][1] for row in rows if row["match_id"] in links}


def resolve(rid):
    """网址里的录像 id（年-月/文件名）→ MP4 的路径；不在录像文件夹里、不存在都返回 None（不让网址指到别的文件）。"""
    if not rid or "\\" in rid or rid.count("/") != 1:
        return None
    base = folder().resolve()
    video = (base / f"{rid}.mp4").resolve()
    if video.parent.parent != base or video.parent.name == recorder.WORK_DIR_NAME or not video.is_file():
        return None
    return video


def _badge(placement):
    if placement is None:
        return "p-none"
    return "p1" if placement == 1 else "p2" if placement <= 3 else "p3" if placement <= 10 else "p4"


def _decorate(r):
    """模板要用的几项：传奇小头像、名次徽章、副标题。"""
    from panel_app import LEGEND_ICONS  # panel_app 导入本文件，放在这里才不会互相导入
    m = r["match"]
    r["legend_icon"] = LEGEND_ICONS.get(r["legend"])
    r["badge"] = _badge(m["placement"]) if m else None
    r["subtitle"] = " · ".join(x for x in ((m or {}).get("mode"), (m or {}).get("map"), r["when"]) if x)
    return r


@bp.route("/recordings")
def page():
    items = attach_matches(entries())
    cards = [_decorate(r) for r in groups(items)]
    total = sum(r["size"] for r in items)
    limit = float(app_settings.get("recording_limit_gb", DEFAULT_LIMIT_GB))
    return render_template("recordings.html", items=cards, total=size_text(total), limit=f"{limit:g} GB",
                           enabled=bool(app_settings.get("recording_enabled")))


def _full_path(item):
    """这个录像是哪一局的整局录像（片段的 highlight.of；整局就是它自己）的路径；那个文件可能已经删了。"""
    return folder() / f"{item['group']}.mp4"


@bp.route("/recordings/watch")
def watch():
    video = resolve(request.args.get("v"))
    if video is None:
        flash("找不到这个录像（可能已经删掉了）。", "error")
        return redirect(url_for("recordings.page"))
    item = _decorate(attach_matches([load(video)])[0])
    start = request.args.get("t", type=float)
    full = _full_path(item)
    clips = [load(c) for c in highlights.existing(full)]
    note = session.pop(CLIP_NOTE_KEY, None)
    return render_template("recordings_watch.html", item=item, lead=EVENT_LEAD,
                           start=start if start is not None and 0 <= start < item["duration"] else None,
                           clips=clips, full_id=item["group"] if full.is_file() else None,
                           clip_total=clock(sum(c["duration"] for c in clips)),
                           clip_size=size_text(sum(c["size"] for c in clips)),
                           can_save=not item["clip"] and not item["unfinished"] and bool(item["events"]),
                           clip_note=note if isinstance(note, dict) else None)


@bp.post("/recordings/highlights")
def save_clips():
    """整局录像上的"保存高光"：按设置页现在勾的事件、前后秒数切，整局留着。已经存过的不再切（2026-11-21 用户定的：按钮不能点）。"""
    rid = request.form.get("v")
    path = resolve(rid)
    if path is None:
        flash("找不到这个录像（可能已经删掉了）。", "error")
        return redirect(url_for("recordings.page"))

    def done(text, error=False):
        session[CLIP_NOTE_KEY] = {"text": text, "error": error}
        return redirect(url_for("recordings.watch", v=rid) + "#h-clips")
    if highlights.existing(path):
        return done("这一局已经存过高光了", error=True)
    try:
        clips = highlights.save(_ffmpeg(), path, highlights.choices(app_settings.load()), log=lambda line: None)
    except highlights.HighlightError as e:
        return done(f"没存成：{e}", error=True)
    if not clips:
        return done("这一局没有设置页里勾选的事件，没有存", error=True)
    seconds = sum(recorder.mp4_duration(c) or 0 for c in clips)
    return done(f"存好了 {len(clips)} 段高光（共 {clock(seconds)}，{size_text(sum(c.stat().st_size for c in clips))}）")


@bp.route("/recordings/video")
def video():
    path = resolve(request.args.get("v"))
    if path is None:
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)  # conditional：支持 Range，能拖进度条


@bp.route("/recordings/thumb")
def thumb():
    path = resolve(request.args.get("v"))
    if path is None:
        abort(404)
    jpg = path.with_suffix(".jpg")
    if not jpg.exists():  # 录完时没截成（或者是以前的录像）：现在截一张存着
        ffmpeg = recorder.find_ffmpeg(app_settings.get("recording_ffmpeg"), DATA_DIR)
        item = load(path)
        if not ffmpeg or not recorder.make_thumbnail(
                ffmpeg, path, recorder.thumbnail_time([{"t": e["t"]} for e in item["events"]], item["duration"])):
            abort(404)
    return send_file(jpg, mimetype="image/jpeg", max_age=0)


@bp.post("/recordings/delete")
def delete():
    path = resolve(request.form.get("v"))
    if path is None:
        flash("找不到这个录像（可能已经删掉了）。", "error")
        return redirect(url_for("recordings.page"))
    try:
        path.unlink()
    except OSError:
        flash("删不掉：这个录像可能正在播放，关掉播放器再删。", "error")
        return redirect(url_for("recordings.watch", v=request.form.get("v")))
    for extra in (path.with_suffix(".json"), path.with_suffix(".jpg")):
        extra.unlink(missing_ok=True)
    flash(f"删掉了 {path.name}", "ok")
    back = request.form.get("back")  # 删一段高光：回到整局（或者这一局别的片段）那一页（录像的 id）
    if back and resolve(back):
        return redirect(url_for("recordings.watch", v=back) + "#h-clips")
    return redirect(url_for("recordings.page"))


def _select_in_explorer(path):
    shell = ctypes.OleDLL("shell32")
    ole = ctypes.OleDLL("ole32")
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.restype = None
    item = ctypes.c_void_p()
    ole.CoInitialize(None)
    try:
        shell.SHParseDisplayName(ctypes.c_wchar_p(str(path)), None, ctypes.byref(item), 0, None)
        shell.SHOpenFolderAndSelectItems(item, 0, None, 0)
    finally:
        ole.CoTaskMemFree(item)
        ole.CoUninitialize()


@bp.post("/recordings/show")
def show():
    """在资源管理器里打开录像文件夹；带 v 时选中那个录像。"""
    rid = request.form.get("v")
    back = request.form.get("next", "")
    if not back.startswith(("/recordings", "/settings")):
        back = url_for("recordings.page")
    try:
        if not rid:
            base = folder()
            base.mkdir(parents=True, exist_ok=True)
            os.startfile(base)
        else:
            path = resolve(rid)
            if path is None:
                flash("找不到这个录像（可能已经删掉了）。", "error")
                return redirect(url_for("recordings.page"))
            try:
                _select_in_explorer(path)
            except OSError:
                os.startfile(path.parent)
                flash("已打开录像所在文件夹，但 Windows 未能选中该文件。", "error")
    except OSError:
        current_app.logger.warning("无法打开录像位置", exc_info=True)
        flash("无法打开录像位置，请检查录像保存目录是否可用。", "error")
    return redirect(back)


# ---------- 设置页"录像"一栏 ----------

def _ffmpeg():
    return recorder.find_ffmpeg(app_settings.get("recording_ffmpeg"), DATA_DIR)


def check_encoders(ffmpeg):
    """检测这台电脑上每种编码格式能用的硬件编码器（用测试画面编 20 帧），记进设置里。"""
    rules = recorder.load_rules()
    found = {"ffmpeg": ffmpeg}
    for codec in rules["encoders"]:
        found[codec] = recorder.pick_encoder(ffmpeg, recorder.Options(source=CHECK_SOURCE, codec=codec), rules)
    app_settings.set_value(CHECK_KEY, found)
    return found


def encoders(ffmpeg, run=False):
    """记着的检测结果（ffmpeg 换了不算）；没有时 run=True 就现在检测，否则返回 None（不知道）。"""
    found = app_settings.get(CHECK_KEY)
    if isinstance(found, dict) and found.get("ffmpeg") == ffmpeg:
        return found
    return check_encoders(ffmpeg) if run and ffmpeg else None


def current_options(rules=None):
    """设置里现在的画质（和开录时一样按 recorder.options_from：没设过的、配置里已经没有的那一档用默认）。"""
    opts = recorder.options_from(app_settings.load(), rules)
    return {"size": opts.size, "fps": opts.fps, "quality": opts.quality, "codec": opts.codec,
            "audio_source": opts.audio_source, "mic": opts.mic, "mic_volume": opts.mic_volume}


def limit_gb():
    try:
        return float(app_settings.get("recording_limit_gb", DEFAULT_LIMIT_GB))
    except (TypeError, ValueError):
        return float(DEFAULT_LIMIT_GB)


def estimate(rules, native, size, fps, quality, codec, limit):
    """这样设大约多大：码率、一局（MATCH_MINUTES 分钟）多少 GB、上限能存几局。"""
    width, height = rules["sizes"][size] or native
    rate = recorder.bitrate(rules, width, height, fps, codec, quality)
    per_match = rate / 8 * MATCH_MINUTES * 60
    keep = int(limit * 2 ** 30 // per_match)
    return (f"按 {width} × {height} 算约 {rate / 1e6:.1f} Mbps，一局（{MATCH_MINUTES} 分钟）约 "
            f"{size_text(per_match)}，{limit:g} GB 能存约 {keep} 局")


def folder_problem(raw):
    """选的保存位置能不能用：不能用返回原因（和"更改数据位置"一样检查：完整路径、不在程序文件夹里、能写入）。"""
    if not raw:
        return "请先选一个文件夹"
    target = Path(raw)
    if not target.is_absolute() or not target.drive:
        return "请写完整的路径，比如 D:\\Evo 录像"
    if data_move._inside(target, EXE_DIR):
        return "这里在 Evo 的程序文件夹里，换新版本时会被一起删掉，请选程序文件夹外面的地方"
    return data_move._writable(target)


def _same(a, b):
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _free_text(base):
    try:
        free = shutil.disk_usage(data_move._existing_ancestor(base)).free
    except OSError:
        return None
    return f"{base.drive.rstrip(':')} 盘还剩 {size_text(free)}" if base.drive else None


@bp.app_template_global("recording_settings")
def settings_view():
    """设置页"录像"一栏要显示的（_settings_recording.html）。打开着、还没检测过编码器时现在检测一次（约 0.5 秒）。"""
    from panel_app import screen_size  # panel_app 导入本文件，放在这里才不会互相导入
    rules = recorder.load_rules()
    enabled = bool(app_settings.get(ENABLED_KEY))
    ffmpeg = _ffmpeg()
    found = encoders(ffmpeg, run=enabled) if ffmpeg else None
    blocked = None
    if not ffmpeg:
        blocked = "找不到录像要用的组件（ffmpeg）"
    elif found and not found.get(recorder.FALLBACK_CODEC):
        blocked = "这台电脑上没有能用的显卡硬件编码器（NVIDIA、AMD、Intel 的都试过了），录不了"
    encoder = found.get(recorder.FALLBACK_CODEC) if found else None
    native = screen_size() or (1920, 1080)  # 原画面按屏幕分辨率估（全屏时游戏窗口就这么大）
    limit = limit_gb()
    opts = current_options(rules)
    estimates = {f"{size}|{fps}|{quality}|{codec}": estimate(rules, native, size, fps, quality, codec, limit)
                 for size in rules["sizes"] for fps in rules["fps"]
                 for quality in rules["bits_per_pixel"] for codec in rules["encoders"]}
    base = folder()
    videos = _videos(base)
    used = sum(v.stat().st_size for v in videos)
    note = session.pop(NOTE_KEY, None)
    settings = app_settings.load()
    picked = highlights.choices(settings, rules)
    return {
        # 剪高光（2026-11-21）：录制模式、高光包括哪些事件、每种事件前后录几秒（滑块）
        "mode": highlights.mode(settings), "modes": [{"value": k, "name": MODE_NAMES.get(k, k)} for k in highlights.MODES],
        "hl_events": [{"kind": k, "name": HIGHLIGHT_NAMES.get(k) or EVENTS.get(k, (k, ""))[0], "css": EVENTS.get(k, ("", ""))[1], **c}
                      for k, c in picked.items()],
        "hl_max": highlights.config(rules).get("max_seconds", 60),
        "shown": bool(ffmpeg) or enabled,  # 正式版不带 ffmpeg：没开着就整栏不显示（开着时照样显示，能关掉）
        "enabled": enabled, "blocked": blocked,
        "encoder": f"{FAMILY_NAMES.get(encoder.rsplit('_', 1)[-1], encoder)} 显卡" if encoder else None,
        "sizes": [{"value": k, "name": SIZE_NAMES.get(k, k)} for k in rules["sizes"]],
        "fps": rules["fps"],
        "qualities": [{"value": k, "name": QUALITY_NAMES.get(k, k)} for k in rules["bits_per_pixel"]],
        "codecs": [{"value": k, "name": CODEC_NAMES.get(k, k), "ready": found is None or bool(found.get(k))}
                   for k in rules["encoders"]],
        "audio_sources": [{"value": k, "name": AUDIO_SOURCE_NAMES.get(k, k)} for k in recorder.AUDIO_SOURCES],
        "mic_volume_range": recorder.MIC_VOLUME, "mic_test_seconds": MIC_TEST_SECONDS,
        "current": opts, "estimates": estimates,
        "estimate": estimates.get(f"{opts['size']}|{opts['fps']}|{opts['quality']}|{opts['codec']}"),
        "limit": f"{limit:g}", "limit_range": LIMIT_RANGE,
        "count": len(videos), "used": size_text(used), "free": _free_text(base),
        "folder": str(base), "custom_folder": bool(app_settings.get("recording_dir")),
        "note": note if isinstance(note, dict) else None,
    }


class MicTest:
    """设置页的"测试麦克风"：录 MIC_TEST_SECONDS 秒默认麦克风（带实时音量），录完页面取回去按滑块的音量放。一次只录一段。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.rec, self.until, self.error = None, 0.0, None
        self.count, self.path = 0, None

    def clean(self, keep=None):
        """删掉以前的测试录音（被占着删不掉的跳过，下次再删）。"""
        for old in MIC_TEST_DIR.glob(f"evo_mic_test_{os.getpid()}_*.wav"):
            if old != keep:
                try:
                    old.unlink()
                except OSError:
                    pass

    def start(self):
        with self.lock:
            if self.rec is not None and self.rec.is_alive():
                return "正在录，等这一段录完"
            factory = MIC_FACTORY
            if factory is None:
                import process_audio
                factory = process_audio.ProcessAudio
            self.count += 1
            self.path = MIC_TEST_DIR / f"evo_mic_test_{os.getpid()}_{self.count}.wav"
            self.clean()
            rec = factory("mic", self.path, meter=True)
            rec.start()
            rec.ready.wait(10)
            if rec.error:
                self.rec, self.error = None, rec.error
                return f"麦克风用不了：{rec.error}"
            self.rec, self.until, self.error = rec, time.monotonic() + MIC_TEST_SECONDS, None
            threading.Timer(MIC_TEST_SECONDS, rec.stop).start()
            return None

    def status(self):
        rec = self.rec
        if self.error:
            return {"state": "error", "error": self.error}
        if rec is None:
            return {"state": "idle"}
        if rec.is_alive():
            return {"state": "recording", "left": max(0.0, round(self.until - time.monotonic(), 1)),
                    "level": round(getattr(rec, "level", 0.0), 3)}
        if rec.error:
            return {"state": "error", "error": rec.error}
        return {"state": "done" if self.path and self.path.exists() else "idle"}


mic_test = MicTest()
atexit.register(mic_test.clean)


def _done(where, text, error=False, **extra):
    session[NOTE_KEY] = {"where": where, "text": text, "error": error, **extra}
    return redirect(url_for("settings") + "#h-video")


@bp.post("/settings/recording")
def toggle():
    """打开、关掉录像。打开时先看有没有 ffmpeg、检测显卡硬件编码器，都有才打开。"""
    on = request.form.get("on") == "1"
    if on:
        ffmpeg = _ffmpeg()
        if not ffmpeg:
            return _done("toggle", "打不开：找不到录像要用的组件（ffmpeg）", error=True)
        if not check_encoders(ffmpeg).get(recorder.FALLBACK_CODEC):
            return _done("toggle", "打不开：这台电脑上没有能用的显卡硬件编码器", error=True)
    app_settings.set_value(ENABLED_KEY, on)
    if on:
        return _done("toggle", "打开了：从下一局开始录，录好的在左边的“录像”页看")
    if any(_videos(folder())):
        return _done("toggle", "关掉了：以后不录。已经录好的还在，“录像”页照样能看")
    return _done("toggle", "关掉了：以后不录")


@bp.post("/settings/recording/quality")
def save_quality():
    rules = recorder.load_rules()
    form = request.form
    size, fps, quality, codec = form.get("size"), form.get("fps", type=int), form.get("quality"), form.get("codec")
    audio_source = form.get("audio_source", recorder.Options.audio_source)
    if size not in rules["sizes"] or fps not in rules["fps"] or quality not in rules["bits_per_pixel"] \
            or codec not in rules["encoders"] or audio_source not in recorder.AUDIO_SOURCES:
        return _done("quality", "没保存：选项不对，刷新一下页面再试", error=True)
    found = encoders(_ffmpeg(), run=True)
    if found and not found.get(codec):
        return _done("quality", f"没保存：这台电脑的显卡编不了 {CODEC_NAMES.get(codec, codec)}", error=True)
    for key, value in (("recording_size", size), ("recording_fps", fps), ("recording_quality", quality),
                       ("recording_codec", codec), ("recording_audio_source", audio_source)):
        app_settings.set_value(key, value)
    return _done("quality", "保存了：从下一局开始按这个录")


@bp.post("/settings/recording/highlights")
def save_highlights():
    """录制模式、高光包括哪些事件、每种事件前后录几秒，一起保存（玩家改的存 settings.json，默认在 config/recording.json）。
    reset=1（高级选项里的"恢复默认"）：秒数改回默认，勾的照表单上的。只录高光时至少要勾一种。"""
    rules = recorder.load_rules()
    cfg = highlights.config(rules)
    form, top = request.form, cfg.get("max_seconds", 60)
    mode = form.get("mode")
    if mode not in highlights.MODES:
        return _done("highlights", "没保存：选项不对，刷新一下页面再试", error=True)
    reset = bool(form.get("reset"))
    picked = {}
    for kind, default in cfg["events"].items():
        seconds = {}
        for key in ("before", "after"):
            value = default[key] if reset else form.get(f"{key}_{kind}", type=float)
            if value is None or not 0 <= value <= top:
                return _done("highlights", f"没保存：前后几秒要在 0 到 {top} 之间", error=True)
            seconds[key] = int(value) if value == int(value) else value
        picked[kind] = {"on": form.get(f"on_{kind}") == "1", **seconds}
    if mode == "highlights" and not any(c["on"] for c in picked.values()):
        return _done("highlights", "没保存：只录高光时至少要勾一种事件", error=True)
    app_settings.set_value(highlights.MODE_KEY, mode)
    app_settings.set_value(highlights.CHOICES_KEY, picked)
    if reset:
        return _done("highlights", "每种事件前后录几秒改回默认了")
    return _done("highlights", "保存了：从下一局开始只录高光" if mode == "highlights" else "保存了")


@bp.post("/settings/recording/mic")
def save_mic():
    """录不录麦克风（开关）。开关自己写着已开启 / 已关闭，下面不另写结果（2026-11-21 用户删了"打开了：从下一局开始录麦克风"）。"""
    app_settings.set_value("recording_mic", request.form.get("on") == "1")
    return redirect(url_for("settings") + "#h-video")


@bp.post("/settings/recording/mic-volume")
def save_mic_volume():
    """麦克风音量滑块：松手时页面用脚本提交（不刷新页面），回答存下的值；没有脚本时跳回设置页。"""
    lo, hi, _ = recorder.MIC_VOLUME
    value = request.form.get("volume", type=int)
    if value is None or not lo <= value <= hi:
        if request.headers.get("X-Evo-Fetch") == "1":
            return jsonify({"ok": False}), 400
        return _done("mic", f"没保存：音量要在 {lo}% 到 {hi}% 之间", error=True)
    app_settings.set_value("recording_mic_volume", value)
    if request.headers.get("X-Evo-Fetch") == "1":
        return jsonify({"ok": True, "volume": value})
    return _done("mic", "保存了")


@bp.post("/settings/recording/mic-test")
def start_mic_test():
    """开始测试麦克风：录 MIC_TEST_SECONDS 秒。回答 {ok, seconds} 或 {ok: false, error}。"""
    problem = mic_test.start()
    if problem:
        return jsonify({"ok": False, "error": problem})
    return jsonify({"ok": True, "seconds": MIC_TEST_SECONDS})


@bp.get("/settings/recording/mic-test")
def mic_test_status():
    """测试录到哪了：{state: recording（还剩几秒、音量 0–1）/ done / error / idle}。"""
    return jsonify(mic_test.status())


@bp.get("/settings/recording/mic-test.wav")
def mic_test_audio():
    """测试录好的那一段（WAV）：页面取回去按滑块的音量放。"""
    if mic_test.status()["state"] != "done":
        abort(404)
    return send_file(mic_test.path, mimetype="audio/wav", max_age=0)


@bp.post("/settings/recording/limit")
def save_limit():
    gb = request.form.get("gb", type=float)
    lo, hi = LIMIT_RANGE
    if gb is None or not lo <= gb <= hi:
        return _done("limit", f"没保存：要填 {lo} 到 {hi} 之间的数", error=True)
    app_settings.set_value("recording_limit_gb", int(gb) if gb == int(gb) else gb)
    used = sum(v.stat().st_size for v in _videos(folder()))
    if used > gb * 2 ** 30:  # 现在不删（和平时一样，录完一局才删）：先说清楚
        return _done("limit", f"保存了。现在的录像共 {size_text(used)}，超过了上限：录完下一局时会从最旧的删起")
    return _done("limit", "保存了")


@bp.post("/settings/recording/folder")
def save_folder():
    """换保存位置（reset=1：回到数据文件夹里的 recordings）。已经录好的不搬，说清楚还在哪。"""
    old = folder()
    if request.form.get("reset"):
        app_settings.set_value("recording_dir", None)
    else:
        raw = (request.form.get("folder") or "").strip().strip('"')
        problem = folder_problem(raw)
        if problem:
            return _done("folder", problem, error=True, chosen=raw)  # 填的留在输入框里，改一改再保存
        app_settings.set_value("recording_dir", None if _same(raw, DATA_DIR / "recordings") else str(Path(raw)))
    if _same(old, folder()):
        return _done("folder", "保存位置没变")
    left = len(_videos(old))
    text = "改回默认位置了" if request.form.get("reset") else "保存了"
    text += "：从下一局开始存到这里"
    if left:
        text += f"。原来的 {left} 个录像没有搬，还在 {old}；想在录像页里看，把里面按月份分的文件夹搬到新位置"
    return _done("folder", text)
