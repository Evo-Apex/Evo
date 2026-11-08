"""录入历史赛季数据：新表 season_stats（storage.py）、录入模式的开关（season_import.py）、记录程序里读数据页
（live_collect.SeasonWatcher）。

- 新表（不用截图）：读到的存成待核对，同一项再读到就换掉；保存时换掉原来保存的那一行、记下改过哪几项；删除；
  旧数据库（版本 8）打开时自动加上这张表；Evo 认识的名字 = 对局里出现过的
- 录入模式：按一次开 10 分钟，还剩几秒，到时间、点"结束"就关
- 记录程序（用假游戏喂用户 2026-11-01 截的数据页，游戏是 16:10 拉伸，和实际打游戏一样按 16:10 换算，见 test_season_stats.py）：
  - 录入模式开着：S30 全部、S30 排位、S29 全部、生涯合计存成待核对，数字和答案一样；下拉菜单开着的不读；
    被鼠标弹出的说明框挡住的（S27）不存、日志里提醒；0 局的（S13）不存；同一项再点回来、数字没变不再存；
    存一个赛季响一声（生涯合计不响）；停在数据页时每 0.3 秒一张
  - 录入模式关着：一张都不读，数据页上一次识别都不跑
  - 页面上的名字不是 Evo 认识的：不读，日志说一次
  找不到截图时这部分跳过

运行：python tests/test_season_import.py
"""

import csv
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fake_game
import numpy as np
from fake_game import SOUNDS_PLAYED, SHOTS, load, report, run_collect  # 要在项目代码之前导入（设置临时数据文件夹）

import app_settings
import game_aspect
import layout
import live_collect
import ocr
import season_import
import season_stats
import storage

FOLDER = SHOTS / "frames" / "20261101_161135"
ANSWERS = Path(__file__).with_name("season_stats_20261101.csv")
TEMP = Path(tempfile.mkdtemp(prefix="evo-seasons-", dir=storage.DB_PATH.parent.parent))  # 在测试的临时数据文件夹里


def row(season, kind, games, name="ImperialHzd", **more):
    return {"player_name": name, "season": season, "kind": kind, "season_name": "排位" if kind == "ranked" else "X",
            "games": games, "read_at": "2026-11-01 16:12:32", **more}


def table_checks():
    out = {}
    conn = storage.connect(TEMP / "seasons.db")
    first = storage.save_season_pending(conn, row(30, "ranked", 800))
    again = storage.save_season_pending(conn, row(30, "ranked", 821, name="Imperial Hzd"))  # 同一项：名字空格不同也算
    other = storage.save_season_pending(conn, row(30, "all", 821))
    career = storage.save_season_pending(conn, row(None, "career", 10146, approx=["top5"]))
    pending = storage.season_rows(conn, "pending")
    out["同一项再读到：换掉原来待核对的那一行（不管名字里的空格）"] = (
        [r["stat_id"] for r in pending] == [other, again, career] and first not in [r["stat_id"] for r in pending])
    out["排序：赛季从新到旧，全部在排位前面，生涯合计在最后；约数的项记下来"] = (
        [(r["season"], r["kind"]) for r in pending] == [(30, "all"), (30, "ranked"), (None, "career")]
        and pending[2]["approx"] == "top5")
    storage.save_season(conn, again)
    newer = storage.save_season_pending(conn, row(30, "ranked", 830))
    storage.save_season(conn, newer, {"games": 831, "wins": None, "season_name": "不能改"})
    saved = storage.season_rows(conn, "saved")
    out["保存：换掉原来保存的那一行，记下改过哪几项（只记 15 个数）"] = (
        [(r["stat_id"], r["games"], r["edited"], r["season_name"]) for r in saved] == [(newer, 831, "games", "排位")]
        and not storage.save_season(conn, 9999))
    storage.delete_season(conn, other)
    out["删除一行"] = [r["stat_id"] for r in storage.season_rows(conn)] == [newer, career]
    storage.insert_match(conn, {"player_name": "Imperial Hzd", "ended_at": "2026-11-01 16:00:00"})
    out["Evo 认识的名字：对局里出现过的（去掉空格）"] = storage.known_names(conn) == {"ImperialHzd"}
    conn.close()

    old = TEMP / "old.db"
    raw = sqlite3.connect(old)
    raw.execute("CREATE TABLE matches (match_id INTEGER PRIMARY KEY)")
    raw.execute("PRAGMA user_version = 8")
    raw.commit()
    raw.close()
    conn = storage.connect(old)
    out["旧数据库（版本 8）打开时加上历史赛季表"] = (
        conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        and storage.season_rows(conn) == [])
    conn.close()
    return out


def mode_checks():
    now = datetime(2026, 11, 1, 16, 0, 0)
    until = season_import.start(now)
    out = {
        "按一次：开 10 分钟": until == now + timedelta(minutes=10) and season_import.until() == until,
        "还剩几秒；到时间就关": season_import.remaining(now + timedelta(minutes=3)) == 420
        and season_import.active(now + timedelta(minutes=9)) and not season_import.active(now + timedelta(minutes=10)),
    }
    season_import.stop()
    out["点\"结束\"：马上关"] = not season_import.active(now) and season_import.until() is None
    return out


def aspect_checks():
    """4:3：自动检测（读视频设置文件）、设置页手动选、界面坐标换算。不用截图。"""
    out = {}
    path = TEMP / "videoconfig-4x3.txt"
    path.write_text('"VideoConfig"\n{\n'
                    '\t"setting.fullscreen"\t\t"1"\n\t"setting.nowindowborder"\t\t"1"\n'
                    '\t"setting.defaultres"\t\t"1920"\n\t"setting.defaultresheight"\t\t"1440"\n}\n',
                    encoding="utf-16")
    found, why = game_aspect.from_video_config(path)
    out[f"自动检测：全屏 1920×1440 → 4:3（{game_aspect.name(found)}，{why}）"] = (
        found == 4 / 3 and game_aspect.name(found) == "4:3" and "1920×1440" in why)

    box = layout.Mapping((1080, 1920), (1920, 1080), "top", aspect=4 / 3).box([1335, 264, 90, 46])
    name_box = layout.Mapping((1080, 1920), (1920, 1080), "top", aspect=4 / 3).box([440, 96, 1040, 76])
    out[f"1920×1080 截图、游戏 4:3：数据页 ROI 从 y=264 换到 y=198（{box}），玩家名从 96 换到 72（{name_box}）"] = (
        box == (1335, 198, 1425, 232) and name_box == (440, 72, 1480, 129))

    log = []
    clock = [0.0]
    watcher = game_aspect.AspectWatcher(log.append, clock=lambda: clock[0], path=path)
    watcher.check(np.zeros((1080, 1920, 3), np.uint8))
    out[f"记录程序：自动检测出 4:3，日志写出截图尺寸（{log[0] if log else '没有日志'}）"] = (
        layout.game_aspect() == 4 / 3 and bool(log) and "4:3" in log[0] and "1920×1080" in log[0])
    watcher.reset()

    app_settings.set_value(game_aspect.SETTING_KEY, "4:3")
    manual, why = game_aspect.current(path=TEMP / "not-there.txt")
    out[f"设置页手动选 4:3：不看视频设置文件，直接用 4:3（{why}）"] = (
        manual == 4 / 3 and "手动选的 4:3" in why)
    app_settings.set_value(game_aspect.SETTING_KEY, "auto")
    path.unlink()
    return out


SEASON_VALUES = {"games": 100, "wins": 5, "top5": 20, "damage": 2000, "damage_max": 800, "damage_avg": 20.0,
                 "kills": 200, "deaths": 100, "kd": 2.0, "kills_max": 8, "knockdowns": 150, "assists": 50,
                 "win_streak": 3, "revives": 10, "respawns": 5}


class FakeSeasonPage:
    """替身：不跑 OCR，只验证 4:3 下从录入模式到待核对的整条路。"""

    def __init__(self, engine, values=None, on=True, player="ImperialHzd"):
        self.values = dict(SEASON_VALUES if values is None else values)
        self.on, self.player, self.reads = on, player, 0

    def on_page(self, frame):
        return self.on

    def name(self, frame):
        return self.player

    def label(self, frame, memo=None):
        return 30, "all", "诸神烙印", "第30赛季：诸神烙印"

    def read(self, frame, panel="season", memo=None):
        self.reads += 1
        return {"panel": panel, "values": dict(self.values), "approx": {}, "obscured": False}


def feedback_checks():
    """录入模式开着、一直没认出数据页：日志要说清比例、截图尺寸、卡在哪一步；认出后数字稳定两帧才存。"""
    out = {}
    now = datetime(2026, 11, 1, 16, 0, 0)
    frame = np.zeros((1080, 1920, 3), np.uint8)
    conn = storage.connect(TEMP / "feedback.db")
    page = FakeSeasonPage(None, values={})
    log = []
    watcher = live_collect.SeasonWatcher(None, conn, SimpleNamespace(player_name="ImperialHzd"), log.append, page=page)
    season_import.start(now)
    watcher.check_mode(now)
    layout.set_game_aspect(4 / 3)
    watcher.update(None, frame, now, False)  # 认出一次数据页（空数据），之后用不上的画面
    page.on = False
    for i in range(1, 5):
        watcher.update(None, frame, now + timedelta(seconds=i), False)
    lines = [line for line in log if "录入历史赛季数据：" in line]
    out[f"连续几帧没认出：日志说“未识别页面”、4:3、截图 1920×1080（{lines[-1] if lines else '没有'}）"] = (
        any("未识别页面" in line and "4:3" in line and "1920×1080" in line for line in lines)
        and sum("未识别页面" in line for line in lines) == 1)

    page.on, page.player, watcher.owner = True, None, None
    watcher.update(None, frame, now + timedelta(seconds=6), False)
    out["页面上的名字读不出来：日志说“玩家名不匹配”，带上比例和截图尺寸"] = any(
        "玩家名不匹配" in line and "4:3" in line and "1920×1080" in line for line in log)

    page.player, page.values, watcher.owner = "ImperialHzd", dict(SEASON_VALUES), None
    watcher.update(None, frame, now + timedelta(seconds=7), False)
    first = any("数字稳定中" in line for line in log)
    watcher.update(None, frame, now + timedelta(seconds=8), False)
    rows = storage.season_rows(conn)
    out[f"4:3 连续两帧数字一样：存成待核对，日志说“已存为待核对”（{len(rows)} 行）"] = (
        first and any("已存为待核对" in line for line in log)
        and {(r["season"], r["kind"]) for r in rows} == {(30, "all"), (None, "career")})
    conn.close()
    layout.set_game_aspect(None)
    season_import.stop()
    return out


def import_checks():
    """4:3 整条录入：自动检测、设置页手动选，各喂两张数据页，应该各存一项待核对、响一声。"""
    out = {}
    frame = np.zeros((1080, 1920, 3), np.uint8)

    def picture(key):
        return frame

    def rows_of(db):
        conn = storage.connect(db)
        rows = storage.season_rows(conn)
        conn.close()
        return rows

    def run(label, prepare):
        season_import.stop()
        prepare()
        season_import.start()
        SOUNDS_PLAYED.clear()
        with mock.patch.object(season_stats, "SeasonPage", FakeSeasonPage):
            _, _, log = run_collect(["A1", "A2"], picture, game_aspect=None)
        rows = rows_of(fake_game.last_db)
        seasons = [r for r in rows if r["kind"] != "career"]
        out[f"{label}：连续两帧稳定后存下 1 个赛季 + 生涯，待核对（{[(r['season'], r['kind'], r['games']) for r in rows]}）"] = (
            len(seasons) == 1 and seasons[0]["season"] == 30 and seasons[0]["games"] == 100
            and any(r["kind"] == "career" for r in rows) and all(r["status"] == "pending" for r in rows))
        out[f"{label}：成功读取时响一声（{list(SOUNDS_PLAYED)}）"] = list(SOUNDS_PLAYED) == ["summary"]
        out[f"{label}：日志写出 4:3、截图 1920×1080（{[l for l in log if '游戏画面比例' in l]}）"] = any(
            "游戏画面比例：4:3" in line and "1920×1080" in line for line in log)

    config = game_aspect.VIDEO_CONFIG
    config.write_text('"VideoConfig"\n{\n'
                      '\t"setting.fullscreen"\t\t"1"\n\t"setting.nowindowborder"\t\t"1"\n'
                      '\t"setting.defaultres"\t\t"1920"\n\t"setting.defaultresheight"\t\t"1440"\n}\n',
                      encoding="utf-16")
    app_settings.set_value(game_aspect.SETTING_KEY, "auto")
    run("自动检测 4:3", lambda: None)

    config.write_text('"VideoConfig"\n{\n'
                      '\t"setting.fullscreen"\t\t"1"\n\t"setting.nowindowborder"\t\t"1"\n'
                      '\t"setting.defaultres"\t\t"2560"\n\t"setting.defaultresheight"\t\t"1440"\n}\n',
                      encoding="utf-16")
    app_settings.set_value(game_aspect.SETTING_KEY, "4:3")
    run("设置页手动 4:3", lambda: None)

    app_settings.set_value(game_aspect.SETTING_KEY, "auto")
    config.unlink()
    season_import.stop()
    layout.set_game_aspect(None)
    return out


def collect_checks():
    """返回检查结果；没有截图返回 None。"""
    if not FOLDER.is_dir():
        return None
    names = {f.name[:4]: f for f in FOLDER.glob("*.jpg")}
    images = {}

    def picture(key):
        if key not in images:
            images[key] = load(names[key])
        return images[key]

    with ANSWERS.open(encoding="utf-8-sig") as f:
        answers = {r["frame"][:4]: r for r in csv.DictReader(f)}
    # S30 全部 → 下拉菜单开着 → S30 排位 → S27 全部（被说明框挡住）→ S13（0 局）→ S29 全部 → 又点回 S30 全部
    frames = ["0036", "0036", "0037", "0038", "0038", "0049", "0049", "0103", "0103", "0040", "0040", "0039",
              "0036", "0036"]
    ran_on = []
    real_create = ocr.create_engine

    def counting_engine(params=None):
        engine = real_create(params)

        def run(img, **kwargs):
            ran_on.append(fake_game.FakeCamera.showing)
            return engine(img, **kwargs)
        return run

    def rows_of(db):
        conn = storage.connect(db)
        out = storage.season_rows(conn)
        conn.close()
        return out

    out = {}
    season_import.start()
    SOUNDS_PLAYED.clear()
    with mock.patch.object(ocr, "create_engine", counting_engine):
        _, sleeps, log = run_collect(frames, picture, game_aspect="16:10")
    got = rows_of(fake_game.last_db)
    sounds = list(SOUNDS_PLAYED)
    want = {(30, "all"): "0036", (30, "ranked"): "0038", (29, "all"): "0040"}
    wrong = []
    for r in got:
        if r["kind"] == "career":
            if (r["games"], r["damage"], r["approx"]) != (10146, 4678720, "top5"):
                wrong.append(f"生涯合计 {r['games']} 局")
            continue
        answer = answers[want.pop((r["season"], r["kind"]), "????")] if (r["season"], r["kind"]) in want else None
        if answer is None:
            wrong.append(f"多存了第 {r['season']} 赛季 {r['kind']}")
            continue
        bad = [k for k in storage.SEASON_FIELDS if str(r[k]) != answer[k]]
        if bad or r["status"] != "pending" or r["player_name"] != "ImperialHzd" or r["review_note"]:
            wrong.append(f"第 {r['season']} 赛季 {r['kind']}：{bad or r['review_note']}")
    out[f"录入模式开着：S30 全部、S30 排位、S29 全部、生涯合计存成待核对，数字都对（{len(got)} 行；错的：{wrong or want or '无'}）"] = (
        len(got) == 4 and not wrong and not want)
    out["  被说明框挡住的 S27 不存，日志里提醒移开鼠标；0 局的 S13 不存，日志里说一句"] = (
        any("被鼠标弹出的说明框挡住" in line and "第 27 赛季" in line for line in log)
        and any("第 13 赛季 全部 0 局，不记" in line for line in log))
    out[f"  存一个赛季响一声，生涯合计不响，点回 S30 不再响（{sounds}）"] = sounds == ["summary"] * 3
    out["  日志写了开始、存了几项"] = (any("录入历史赛季数据：开始" in line for line in log)
                                and any("这次存了 4 项待核对" in line for line in log))
    stats_sleeps = [s for frame, s in sleeps if frame in ("0036", "0038", "0040")]
    out[f"  停在数据页时每 0.3 秒一张（{sorted(set(stats_sleeps))}）"] = stats_sleeps and max(stats_sleeps) <= 0.3

    season_import.stop()
    ran_on.clear()
    with mock.patch.object(ocr, "create_engine", counting_engine):
        run_collect(frames, picture, game_aspect="16:10")
    out[f"录入模式关着：一项都不存，数据页上一次识别都不跑（{len(ran_on)} 次）"] = (
        rows_of(fake_game.last_db) == [] and not ran_on)

    season_import.start()
    with mock.patch.object(live_collect.storage, "known_names", lambda conn: {"SomeoneElse"}):
        _, _, log = run_collect(frames[:5], picture, game_aspect="16:10")
    season_import.stop()
    told = [line for line in log if "不是 Evo 认识的你的名字" in line]
    out[f"页面上的名字不是 Evo 认识的（好友资料页）：不读，日志只说一次（{len(told)} 次）"] = (
        rows_of(fake_game.last_db) == [] and len(told) == 1)
    return out


def main():
    print("录入历史赛季数据（新表、录入模式、记录程序读数据页）")
    checks = {**table_checks(), **mode_checks(), **aspect_checks(), **feedback_checks(), **import_checks()}
    shots = collect_checks()
    if shots is None:
        print(f"  ⏭ 截图部分跳过：找不到 {FOLDER}（截图只在开发者电脑上，不在仓库里）")
    else:
        checks.update(shots)
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
