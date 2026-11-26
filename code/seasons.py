"""赛季（规则在 config/seasons.json）：每个赛季从什么时候开始，每局、每次大厅读数归到哪个赛季。

面板的"本赛季 / 上赛季"（总览页、数据页）和战绩页的赛季筛选用（2026-11-03 用户决定：新赛季从 0 开始算）。
数据库里的时间是玩家电脑上的时间（"2026-11-03 19:52:24"），配置里的开赛时刻是 UTC，换成电脑上的时间再比。
只用标准库：面板会导入它（见 tests/test_light.py）。
"""

import json
from datetime import datetime

from paths import CONFIG_DIR

STAMP = "%Y-%m-%d %H:%M:%S"  # 数据库里时间的写法


def _utc(utc_text):
    """"2026-11-03T18:00:00Z" → 带时区的 datetime（UTC）。"""
    return datetime.fromisoformat(utc_text.replace("Z", "+00:00"))


def _local(utc_text):
    """"2026-11-03T18:00:00Z" → 电脑上的时间，写成数据库里的样子（"2026-11-03 19:00:00"）。"""
    return _utc(utc_text).astimezone().strftime(STAMP)


def load(path=None):
    """[(赛季编号, 开始时间（电脑上的时间）), ...]，从早到晚。"""
    data = json.loads((path or CONFIG_DIR / "seasons.json").read_text(encoding="utf-8"))
    return sorted((s["number"], _local(s["start"])) for s in data["seasons"])


def load_meta(path=None):
    """{赛季编号: {start, split2_start, end, end_estimated}}：各时刻均为 Unix 秒（float），没有填的是 None。"""
    data = json.loads((path or CONFIG_DIR / "seasons.json").read_text(encoding="utf-8"))
    out = {}
    for s in data["seasons"]:
        out[s["number"]] = {
            "start": _utc(s["start"]).timestamp(),
            "split2_start": _utc(s["split2_start"]).timestamp() if s.get("split2_start") else None,
            "end": _utc(s["end"]).timestamp() if s.get("end") else None,
            "end_estimated": bool(s.get("end_estimated")),
        }
    return out


SEASONS = load()
SEASON_META = load_meta()


def label(number):
    return f"第 {number} 赛季"


def current(now=None, seasons=None):
    """现在是第几赛季（已经开始的最后一个）；第一个赛季开始以前返回 None。"""
    seasons = seasons or SEASONS
    stamp = (now or datetime.now()).strftime(STAMP)
    started = [n for n, start in seasons if start <= stamp]
    return started[-1] if started else None


def previous(now=None, seasons=None):
    """上一个赛季的编号（配置里排在本赛季前面的那个）；没有返回 None。"""
    seasons = seasons or SEASONS
    numbers = [n for n, _ in seasons]
    now_number = current(now, seasons)
    if now_number is None or numbers.index(now_number) == 0:
        return None
    return numbers[numbers.index(now_number) - 1]


def bounds(number, seasons=None):
    """这个赛季的 (开始, 结束)：电脑上的时间，结束是下一个赛季的开始（还没有下一个赛季时为 None）。"""
    seasons = seasons or SEASONS
    numbers = [n for n, _ in seasons]
    i = numbers.index(number)
    return seasons[i][1], (seasons[i + 1][1] if i + 1 < len(seasons) else None)


def contains(number, stamp, seasons=None):
    """这个时间（数据库里的写法）在不在这个赛季里。没有时间的不算。"""
    if not stamp:
        return False
    start, end = bounds(number, seasons)
    return start <= stamp and (end is None or stamp < end)


def of(stamp, seasons=None):
    """这个时间（数据库里的写法）在第几赛季；配置里没有那么早的赛季、没有时间时为 None。"""
    seasons = seasons or SEASONS
    return next((n for n, _ in reversed(seasons) if stamp and contains(n, stamp, seasons)), None)


def pick(rows, number, key="ended_at", seasons=None):
    """只留这个赛季的局（或大厅读数，key="read_at"）。number 为 None（配置里没有这个赛季）时一个都不留。"""
    if number is None:
        return []
    return [r for r in rows if contains(number, r[key], seasons)]


def start_date(number, seasons=None):
    """开赛那天（电脑上的日期，"2026-11-03"），页面上写"11-03 起"用。"""
    return bounds(number, seasons)[0][:10]


def countdown_left(seconds):
    """剩余秒数 → 展示文字：>= 1 天写"18 天 21 小时"，最后 24 小时写"08:31:05"，<= 0 写"已结束"。"""
    s = int(seconds)
    if s <= 0:
        return "已结束"
    if s >= 86400:
        return f"{s // 86400} 天 {(s % 86400) // 3600} 小时"
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def countdown_short(seconds):
    """侧边栏展开小卡片里的紧凑剩余时间：>= 1 天写"18天 21时"，< 1 天写"08:31"，<= 0 写"已结束"。"""
    s = int(seconds)
    if s <= 0:
        return "已结束"
    if s >= 86400:
        return f"{s // 86400}天 {(s % 86400) // 3600}时"
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def countdown_tiny(seconds):
    """侧边栏收起时（70px 宽）的极简剩余时间："18天"、"8时"、"<1时"、"结束"。"""
    s = int(seconds)
    if s <= 0:
        return "结束"
    if s >= 86400:
        return f"{s // 86400}天"
    if s >= 3600:
        return f"{s // 3600}时"
    return "<1时"


def countdown_at(ts, now_dt=None):
    """Unix 秒 → 本地结束时刻：同年写"12-15 19:00"，跨年写"2027-02-09 19:00"。"""
    now_dt = (now_dt or datetime.now()).astimezone()
    when = datetime.fromtimestamp(ts).astimezone()
    fmt = "%m-%d %H:%M" if when.year == now_dt.year else "%Y-%m-%d %H:%M"
    return when.strftime(fmt)


def _progress(start_ts, end_ts, now_ts):
    """已过进度（0–100 整数）。"""
    span = end_ts - start_ts
    if span <= 0:
        return 100
    ratio = max(0.0, min(1.0, (now_ts - start_ts) / span))
    return round(ratio * 100)


def countdown_card(now=None, seasons=None, meta=None):
    """总览页"赛季倒计时"卡片的数据（_season_countdown.html）。

    同时给出排位分赛段（Split 1 / Split 2）和整个赛季的结束倒计时；当前赛季没配 split2_start / end 时返回 None。
    """
    now_dt = (now or datetime.now()).astimezone()
    now_ts = now_dt.timestamp()
    number = current(now_dt.replace(tzinfo=None), seasons)
    if number is None:
        return None
    entry = (SEASON_META if meta is None else meta).get(number)
    if not entry:
        return None
    start_ts = entry["start"]
    split2_ts = entry.get("split2_start")
    end_ts = entry.get("end")
    if not split2_ts and not end_ts:
        return None

    est = bool(entry.get("end_estimated"))
    est_tip = "非官方正式公告，按 Apex Wiki 推算，可能有 ±1 周误差"
    slots = []

    if split2_ts:
        if now_ts < split2_ts:
            left = split2_ts - now_ts
            slots.append({
                "key": "split",
                "name": "排位 · Split 1",
                "short_name": "Split 1",
                "badge": "第 1 赛段",
                "badge_kind": "info",
                "badge_tip": None,
                "start": int(start_ts),
                "end": int(split2_ts),
                "next_start": int(split2_ts) if end_ts else None,
                "next_end": int(end_ts) if end_ts else None,
                "next_estimated": est,
                "left": countdown_left(left),
                "short_left": countdown_short(left),
                "tiny_left": countdown_tiny(left),
                "ended": False,
                "urgent": left <= 3 * 86400,
                "pct": _progress(start_ts, split2_ts, now_ts),
                "end_text": countdown_at(split2_ts, now_dt),
                "estimated": False,
                "note": "排位匹配一般提前 30 分钟关闭",
            })
        elif end_ts:
            left = end_ts - now_ts
            ended = left <= 0
            slots.append({
                "key": "split",
                "name": "排位 · Split 2",
                "short_name": "Split 2",
                "badge": "已结束" if ended else ("预计" if est else "第 2 赛段"),
                "badge_kind": "muted" if ended else ("warn" if est else "info"),
                "badge_tip": est_tip if est and not ended else None,
                "start": int(split2_ts),
                "end": int(end_ts),
                "next_start": None,
                "next_end": None,
                "next_estimated": False,
                "left": countdown_left(left),
                "short_left": countdown_short(left),
                "tiny_left": countdown_tiny(left),
                "ended": ended,
                "urgent": not ended and left <= 3 * 86400,
                "pct": _progress(split2_ts, end_ts, now_ts),
                "end_text": countdown_at(end_ts, now_dt),
                "estimated": est,
                "note": None if ended else "排位匹配一般提前 30 分钟关闭",
            })
        else:
            slots.append({
                "key": "split",
                "name": "排位 · Split 1",
                "short_name": "Split 1",
                "badge": "已结束",
                "badge_kind": "muted",
                "badge_tip": None,
                "start": int(start_ts),
                "end": int(split2_ts),
                "next_start": None,
                "next_end": None,
                "next_estimated": False,
                "left": "已结束",
                "short_left": "已结束",
                "tiny_left": "结束",
                "ended": True,
                "urgent": False,
                "pct": 100,
                "end_text": countdown_at(split2_ts, now_dt),
                "estimated": False,
                "note": None,
            })

    if end_ts:
        left = end_ts - now_ts
        ended = left <= 0
        slots.append({
            "key": "season",
            "name": f"{label(number)}结束",
            "short_name": "本赛季",
            "badge": "已结束" if ended else ("预计" if est else "进行中"),
            "badge_kind": "muted" if ended else ("warn" if est else "accent"),
            "badge_tip": est_tip if est and not ended else None,
            "start": int(start_ts),
            "end": int(end_ts),
            "next_start": None,
            "next_end": None,
            "next_estimated": False,
            "left": countdown_left(left),
            "short_left": countdown_short(left),
            "tiny_left": countdown_tiny(left),
            "ended": ended,
            "urgent": not ended and left <= 3 * 86400,
            "pct": _progress(start_ts, end_ts, now_ts),
            "end_text": countdown_at(end_ts, now_dt),
            "estimated": est,
            "note": None if ended else ("非官方公告，可能有 ±1 周误差" if est else None),
        })

    return {
        "number": number,
        "label": label(number),
        "start_text": datetime.fromtimestamp(start_ts).astimezone().strftime("%m-%d"),
        "slots": slots,
    }
