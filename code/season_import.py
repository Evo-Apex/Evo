"""录入历史赛季数据的"录入模式"（2026-11-01 用户定的）：设置页点按钮 → 接下来 MINUTES 分钟里，记录程序认出游戏里的
数据页就读（见 live_collect.SeasonWatcher、season_stats.py）；到时间或者点"结束"就停。

开关存在设置文件（app_settings）里：录到什么时候。面板写、记录程序每隔几秒读一次（和"游戏语言"一样），
所以按了按钮几秒内生效，游戏还没开时也能先按。这个文件不加载识别用的库：托盘程序和面板也会用它。
"""

from datetime import datetime, timedelta

import app_settings

UNTIL_KEY = "season_import_until"  # 录到什么时候（本地时间，ISO 格式）；没开过、结束了为 None
MINUTES = 10  # 按一次录多久
STATUS_KEY = "season_import_status"  # 记录程序现在卡在哪一步（面板读，见 report）
STATUS_SECONDS = 10  # 状态没变也最多这么久写一次，不能逐帧写文件
STATUS_STALE_SECONDS = 30  # 这么久没更新 = 记录程序没在跑/没响应
MIN_WRITE_SECONDS = 2.0  # 两次写盘至少隔这么久；中间的只留最新一条，等下一次再写

_last_report = None  # (内容, 时刻)：只在记录程序这个进程里用
_pending = None  # (内容, 数据, 时刻)：还没到 2 秒，先留着的最新状态


def start(now=None):
    """开始录入（已经开着就从现在重新算 MINUTES 分钟）。返回录到什么时候。"""
    until = (now or datetime.now()) + timedelta(minutes=MINUTES)
    app_settings.set_value(UNTIL_KEY, until.isoformat(timespec="seconds"))
    clear_status()
    return until


def stop():
    app_settings.set_value(UNTIL_KEY, None)
    clear_status()


def _write(key, payload, now):
    global _last_report, _pending
    _last_report = (key, now)
    _pending = None
    app_settings.set_value(STATUS_KEY, payload)


def report(stage, message, aspect, width, height, now=None, force=False):
    """记录程序报告当前阶段（只写共享设置文件；面板每 3 秒读一次，不另开轮询）。

    stage：waiting_page / page_not_recognized / owner_mismatch / stabilizing / stored / obscured。
    状态没变、没到 STATUS_SECONDS 就不写；普通状态两次写盘至少隔 MIN_WRITE_SECONDS，
    间隔里的新状态只留最新一条，避免 season/career 两栏来回切换时逐帧改 settings.json。
    stored 等关键状态传 force=True，立即写。"""
    global _pending
    now = now or datetime.now()
    key = (stage, message, aspect, int(width), int(height))
    payload = {
        "stage": stage,
        "message": message,
        "game_aspect": aspect,
        "screenshot_size": f"{int(width)}×{int(height)}",
        "updated_at": now.isoformat(timespec="seconds"),
    }
    if force:
        _write(key, payload, now)
        return
    elapsed = (now - _last_report[1]).total_seconds() if _last_report is not None else MIN_WRITE_SECONDS
    if _last_report is not None and key == _last_report[0] and elapsed < STATUS_SECONDS:
        return
    if _last_report is not None and elapsed < MIN_WRITE_SECONDS:
        _pending = (key, payload, now)
        return
    if _pending is not None:
        # 到点了：先把间隔里留下的最新一条写掉；当前这条下一帧还在的话再写。
        old_key, old_payload, old_at = _pending
        _write(old_key, old_payload, now)
        if old_key == key:
            return
        return
    _write(key, payload, now)


def status():
    """面板读到的当前状态；没开、结束了、文件坏了返回 None。"""
    value = app_settings.get(STATUS_KEY)
    if not isinstance(value, dict) or not value.get("stage"):
        return None
    return value


def clear_status():
    global _last_report, _pending
    _last_report = None
    _pending = None
    if status() is not None:
        app_settings.set_value(STATUS_KEY, None)


def until():
    """录到什么时候；没开、设置坏了返回 None。"""
    value = app_settings.get(UNTIL_KEY)
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def remaining(now=None):
    """还剩几秒（整数）；没开或已经过了返回 0。"""
    end = until()
    if end is None:
        return 0
    return max(0, int((end - (now or datetime.now())).total_seconds()))


def active(now=None):
    return remaining(now) > 0
