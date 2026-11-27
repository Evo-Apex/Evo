"""外卡（Wildcard）模式：简中、繁中实拍整局重放，检查开局、总结页和统计口径。

简中和繁中各用一局 16:9、2560x1440 实拍分别验证自动识别和手动指定语言。英文仍待实拍核对。

繁中那一局的正确答案（2026-11-23 人工核对）：风暴点、第 3 名、小队 13 杀、
本人 5 杀 / 1 助攻 / 6 击倒、伤害 1324、生存 11:19。外卡的最后两行（繁中"最後倖存""自我急救次數"）不是普通急救和重生，
不写进 matches / teammates 的对应列。

运行：python tests/test_wildcard.py
"""

import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fake_game import MissingShots, SHOTS, load, report, run_collect  # 要在项目代码之前导入

import fake_game
import modes
import storage
import summary_page
from live_collect import ScreenWatcher, SummaryWatcher
from ocr import SMALL_CROP_PARAMS, LazyEngine, load_config
from post_match import PostMatch, SOURCE_SUMMARY
from screens import ScreenDetector
from summary_page import detect_mode, read_summary, skip_fields, summary_readings, summary_to_match
from teammates import read_teammates

FOLDER = SHOTS / "frames" / "wildcard_20261123_124314"
COUNT = 787
BANNER = FOLDER / "0030_124345_2560x1440.jpg"  # 开局横幅"外卡"，下一行"暴風點"
SUMMARY = FOLDER / "0765_125616_2560x1440.jpg"  # 总结页（第 3 名、5/1/6、1324、11:19）
FIELDS = ("mode", "map", "placement", "squad_kills", "kills", "assists", "knockdowns", "damage", "survival_time",
          "revives", "respawns", "needs_review")
WANT = dict(zip(FIELDS, ("wildcard", "风暴点", 3, 13, 5, 1, 6, 1324, 679, None, None, 0)))
CN_FOLDER = SHOTS / "frames" / "wildcard_20261126_104642"
CN_COUNT = 837
CN_SUMMARY = CN_FOLDER / "0835_110046_2560x1440.jpg"
CN_WANT = dict(zip(FIELDS[:-1], ("wildcard", "风暴点", 3, 21, 6, 5, 9, 1515, 712, None, None)))


def config_checks():
    """不用截图：三语的开局横幅、总结页标签和模式口径（哪些算大逃杀、哪些算排位）。"""
    zh = load_config("match_start.json", "zh-TW")
    en = load_config("match_start.json", "en")
    cn = load_config("match_start.json")
    summary_tw = load_config("summary.json", "zh-TW")
    summary_en = load_config("summary.json", "en")
    summary_cn = load_config("summary.json")
    return {
        "繁中、简中的开局横幅都把'外卡'记成 wildcard": zh["modes"].get("外卡") == "wildcard"
            and cn["modes"].get("外卡") == "wildcard",
        "英文还没有实拍样本：只继承了'外卡'这两个字（英文横幅上不会出现），没写猜测的英文写法":
            {k: v for k, v in en["modes"].items() if v == "wildcard"} == {"外卡": "wildcard"},
        "繁中的总结页有两行外卡标签（最后倖存、自我急救次数）":
            summary_tw["wildcard"]["labels"] == {"revives": ["最後倖存"], "respawns": ["自我急救次數", "自我急救次数"]},
        "简中结算采用实拍的最后幸存和自我急救":
            summary_cn["wildcard"]["labels"] == {"revives": ["最后幸存"], "respawns": ["自我急救"]},
        "英文结算的外卡标签待实拍，两个列表留空":
            summary_en["wildcard"]["labels"] == {"revives": [], "respawns": []},
        "三种语言的总结页都跳过外卡的最后两行（不写进急救、重生）":
            all(set(skip_fields(cfg, modes.WILDCARD)) == {"revives", "respawns"} for cfg in (summary_cn, summary_tw, summary_en)),
        "模式口径：大逃杀是排位、匹配、外卡；排位那套只在排位下用":
            modes.BR_MODES == ("ranked", "pubs", "wildcard") and modes.is_br("wildcard")
            and not modes.is_ranked("wildcard") and modes.is_ranked("ranked")
            and modes.label("wildcard") == "外卡" and modes.normalize("随便写的") == "other",
    }


def banner_checks():
    """真截图：开局横幅认成外卡、总结页认成外卡、别的画面不会被认成外卡。"""
    if not BANNER.is_file() or not SUMMARY.is_file():
        raise MissingShots(FOLDER)
    engine = LazyEngine(SMALL_CROP_PARAMS)
    detector = ScreenDetector(engine, lang="zh-TW")
    summary_cfg = load_config("summary.json", "zh-TW")
    frame = load(SUMMARY)
    results, problems = read_summary(engine, frame, summary_cfg)
    wildcard = detect_mode(engine, frame, summary_cfg)
    return {
        "开局横幅'外卡'认成开局画面（不是别的画面）": detector.detect(load(BANNER)) == "match_start",
        "总结页上两行都是外卡的标签：认成外卡": wildcard == "wildcard",
        f"总结页读数对得上基准（第 3 名、13 杀、5/1/6、1324、11:19）":
            {k: results[k]["value"] for k in ("placement", "squad_kills", "kda", "damage", "survival_time")}
            == {"placement": 3, "squad_kills": 13, "kda": (5, 1, 6), "damage": 1324, "survival_time": "11:19"},
        "外卡的总结页不写急救、重生两项":
            summary_to_match(results, problems, None, None, "wildcard", skip_fields(summary_cfg, "wildcard"))["revives"] is None
            and summary_to_match(results, problems, None, None, "wildcard", skip_fields(summary_cfg, "wildcard"))["respawns"] is None,
    }


def ordinary_summary_checks():
    """用已核对的普通结算实拍做负例，先检查画面类别。"""
    root = Path(os.environ.get("EVO_TEST_ORDINARY_SUMMARIES", SHOTS))
    samples = {"排位": root / "live/20261113_211842/summary/01_212910.jpg",
               "匹配": root / "live/20261121_134351/summary/04_142712.jpg"}
    missing = [str(path) for path in samples.values() if not path.is_file()]
    if missing:
        raise MissingShots("、".join(missing))
    engine = LazyEngine(SMALL_CROP_PARAMS)
    detector = ScreenDetector(engine, lang="zh-TW")
    cfg = load_config("summary.json", "zh-TW")
    checks = {}
    for mode, path in samples.items():
        frame = load(path)
        checks[f"繁中普通{mode}总结页识别为总结页，外卡模式为空"] = (
            detector.detect(frame) == "summary" and detect_mode(engine, frame, cfg) is None
        )
    return checks


def round_boundary_checks():
    """模拟识别出的画面，验证横幅重现和真实返回大厅的分界。"""
    conn = storage.connect(":memory:")
    try:
        post = PostMatch(conn, log=lambda _: None)
        screens = load_config("screens.json", "zh-CN")
        watcher = ScreenWatcher(None, screens, None, None, None, post, None, lambda _: None, keep_all=False)
        now = datetime(2026, 11, 26)
        first = {"mode": "wildcard", "map": "风暴点", "raw_map": "风暴点"}
        second = {"mode": "pubs", "map": "奥林匹斯", "raw_map": "奥林匹斯"}
        with mock.patch("live_collect.read_match_start", side_effect=[first, second]) as read:
            for offset, screen in enumerate(("match_start", None, "match_start")):
                watcher.update(screen, fake_game.BLACK, now + timedelta(seconds=offset))
            kept = read.call_count == 1 and post.info == {"mode": "wildcard", "map": "风暴点"}
            watcher.update("lobby", fake_game.BLACK, now + timedelta(seconds=3))
            no_match = not storage.all_matches(conn)
            watcher.update("match_start", fake_game.BLACK, now + timedelta(seconds=4))
        match_id = storage.insert_match(conn, {"ended_at": str(now)})
        post.on_summary_saved(match_id, {}, now)
        row = storage.get_match(conn, match_id)
        return {"同一局横幅重新出现时保留已读信息": kept,
                "只看到外卡横幅就回大厅时不生成战绩，下一局写入自己的模式和地图":
                no_match and (row["mode"], row["map"]) == ("pubs", "奥林匹斯")}
    finally:
        conn.close()


def summary_mode_upgrade_checks():
    """模拟同局反复打开结算时，数字和模式标签分别漏读。"""
    def results(place):
        values = {"placement": place, "squad_kills": 10, "kda": (4, 1, 1), "damage": 900,
                  "survival_time": "6:20", "revives": 8, "respawns": 4}
        return {key: {"value": value, "score": 1.0, "status": "ok" if value is not None else "missing"}
                for key, value in values.items()}

    checks = {}
    cfg = load_config("summary.json", "zh-CN")
    now = datetime(2026, 11, 26)
    for initial, again, first_place, second_place in (("wildcard", None, None, 3),
                                                      (None, "wildcard", None, 3),
                                                      (None, "wildcard", 3, 3)):
        conn = storage.connect(":memory:")
        try:
            with TemporaryDirectory() as folder, mock.patch("live_collect.save_png"):
                post = PostMatch(conn, log=lambda _: None)
                watcher = SummaryWatcher(None, cfg, conn, post, Path(folder), lambda _: None, 10)
                frame = fake_game.BLACK
                watcher._record(results(first_place), ["名次没读到"] if first_place is None else [],
                                frame, now, summary_mode=initial)
                watcher._record(results(second_place), [], frame, now + timedelta(seconds=5),
                                summary_mode=again)
                with mock.patch("live_collect.read_teammates", return_value=[]) as team, \
                        mock.patch("live_collect.is_champion", return_value=False):
                    watcher.catch_up()
                rows = storage.all_matches(conn)
                readings = storage.readings_of(conn, rows[0]["match_id"])
                saved_fields = {r["field"]: r["value"] for r in readings if r["source"] == SOURCE_SUMMARY}
                checks[f"重读总结页 {initial} → {again}，名次 {first_place} → {second_place} 时按外卡保存"] = (
                    len(rows) == 1 and rows[0]["mode"] == "wildcard" and rows[0]["placement"] == 3
                    and (rows[0]["revives"], rows[0]["respawns"]) == (None, None)
                    and saved_fields["revives"] is None and saved_fields["respawns"] is None
                    and team.call_args.args[-1] == "wildcard"
                )
        finally:
            conn.close()
    return checks


def teammate_skip_checks():
    """队友的急救、重生：普通局照常读取，外卡局跳过。
    用假的 read_field 看问没问这两项，不用截图。"""
    import teammates as tm
    cfg = load_config("summary.json", "zh-TW")
    asked = []

    def field(engine, img, name, spec, cfg, strict):
        asked.append(name)
        if name == "kda":
            return {"status": "ok", "value": (1, 0, 0)}
        return {"status": "ok", "value": "10:00" if name == "survival_time" else 7}

    with mock.patch.object(tm, "read_field", field), \
            mock.patch.object(tm, "read_name", lambda *a, **k: None), \
            mock.patch.object(tm, "read_player_id", lambda *a, **k: None):
        tm.read_teammates(None, None, cfg)
        plain = list(asked)
        asked.clear()
        tm.read_teammates(None, None, cfg, mode="wildcard")
        wild = list(asked)
    return {
        "队友的急救、重生：普通的局照常读": "revives" in plain and "respawns" in plain,
        "队友的急救、重生：外卡才跳过（留空，不读、不当作 0）": "revives" not in wild and "respawns" not in wild,
    }


def new_mode_interface_checks():
    """用测试模式 demo 验证注册表和配置能驱动总结页识别、字段跳过、分组和筛选：
    - 注册表里的属性生效（is_br / is_ranked / stats_mode / mode_values）
    - 结算页最后两行按 config/summary.json 的 demo 段认成 demo
    - skip_fields 按 demo 段取，summary_to_match 写模式并留空那几项"""
    import modes
    cfg = load_config("summary.json", "en")  # 使用英文配置，隔离其他语言的外卡标签。
    cfg = {**cfg, "demo": {"labels": {"revives": ["演示甲"], "respawns": ["演示乙"]},
                           "roi": cfg["wildcard"]["roi"], "skip_fields": ["revives", "respawns"]}}
    demo = {"label": "演示玩法", "br": False, "ranked": False, "stats": True, "choices": True, "summary": "demo"}
    registry = {**modes.MODES, "demo": demo}

    class FakeEngine:
        def __init__(self):
            self.texts = iter(["演示甲", "演示乙"])

        def __call__(self, piece):
            return type("R", (), {"txts": [next(self.texts)]})()

    with mock.patch.object(modes, "MODES", registry), mock.patch.object(summary_page, "crop", lambda *a: None):
        detected = summary_page.detect_mode(FakeEngine(), None, cfg)
        skip = summary_page.skip_fields(cfg, "demo")
        results = {name: {"value": 1 if name in ("revives", "respawns") else None}
                   for name in ("placement", "squad_kills", "kda", "damage", "survival_time", "revives", "respawns")}
        match = summary_page.summary_to_match(results, [], None, None, "demo", skip)
        return {
            "新模式只加注册表一条：大逃杀/排位/统计分组/筛选都跟着变":
                not modes.is_br("demo") and not modes.is_ranked("demo")
                and modes.stats_mode("demo") == "demo" and modes.mode_values("demo") == ["demo"]
                and modes.summary_key("demo") == "demo",
            "新模式的结算页两行对得上就认成这个模式（不用改记录程序）": detected == "demo",
            "新模式要跳过的字段从配置里取，写模式时留空": skip == ("revives", "respawns")
                and match["mode"] == "demo" and match["revives"] is None and match["respawns"] is None,
        }


def frames():
    found = sorted(FOLDER.glob("*.jpg"))
    if len(found) != COUNT:
        raise MissingShots(f"{FOLDER}（要 {COUNT} 张，找到 {len(found)} 张）")
    return found


def replay_checks(files):
    """整段喂给记录程序：只记一局外卡，各字段和基准一致。"""
    rows, _, log = run_collect(files, load, game_aspect="16:9", keep_all=False)
    said = [line.split("] ", 1)[-1].strip() for line in log]
    conn = storage.connect(fake_game.last_db)
    mates = {m["slot"]: m for r in rows for m in storage.teammates_of(conn, r["match_id"])}
    conn.close()
    wrong = [{k: (row[k], WANT[k]) for k in FIELDS if row[k] != WANT[k]} for row in rows]
    return {
        f"记下 1 局（{len(rows)}）": len(rows) == 1,
        f"模式、地图、名次、击杀、伤害全对（对不上的：{[w for w in wrong if w] or '无'}）":
            len(rows) == 1 and not any(wrong),
        "日志里开局画面读到外卡、风暴点": "开局画面：读到 外卡、风暴点" in said,
        "繁中外卡整局用右上角圆标核对模式": any("外卡圆标与本局模式一致" in line for line in log),
        "外卡的最后两行不写进急救、重生（本人）": len(rows) == 1 and rows[0]["revives"] is None
            and rows[0]["respawns"] is None,
        "外卡的队友也照同一规则：急救、重生留空（不当 0）":
            len(mates) == 2 and all(m["revives"] is None and m["respawns"] is None for m in mates.values()),
        "外卡不算排位：RP 空着、不补段位": len(rows) == 1 and rows[0]["rp_delta"] is None
            and rows[0]["rank_tier"] is None,
    }


def banner_only_checks(files):
    """总结页的两行标签没读到时，只靠开局横幅认成外卡：
    模式照样记 wildcard，最后两行（本人和队友）也不写进急救、重生。
    用繁中的这一局模拟：把总结页认外卡关掉，横幅照常认。"""
    with mock.patch("live_collect.detect_mode", return_value=None):
        rows, _, _ = run_collect(files, load, game_aspect="16:9", keep_all=False)
    conn = storage.connect(fake_game.last_db)
    mates = {m["slot"]: m for r in rows for m in storage.teammates_of(conn, r["match_id"])}
    conn.close()
    return {
        "只靠开局横幅认出外卡（总结页标签还没核对）：模式还是 wildcard":
            len(rows) == 1 and rows[0]["mode"] == "wildcard",
        "只看横幅时，外卡的最后两行也不写进自己的急救、重生":
            len(rows) == 1 and rows[0]["revives"] is None and rows[0]["respawns"] is None,
        "只看横幅时，队友的急救、重生也不读（留空）":
            len(mates) == 2 and all(m["revives"] is None and m["respawns"] is None for m in mates.values()),
    }


def missed_start_checks():
    """漏了开局画面（只看到总结页）：靠总结页最后两行认出外卡，模式照样记 wildcard，地图留空（不猜）。"""
    if not SUMMARY.is_file():
        raise MissingShots(FOLDER)
    frames = [fake_game.BLACK, SUMMARY, SUMMARY, fake_game.BLACK]
    rows, _, log = run_collect(frames, load, keep_all=False)
    said = [line.split("] ", 1)[-1].strip() for line in log]
    return {
        "漏开局、只看到总结页：靠两行标签认成外卡，地图留空":
            len(rows) == 1 and rows[0]["mode"] == "wildcard" and rows[0]["map"] is None,
        "漏开局时日志里这一局标着外卡": any("伤害 1324（外卡）" in line for line in said),
    }


def language_entry_checks(files):
    """验证繁中实拍在自动识别语言和手动指定繁中时得到相同的外卡战绩。"""
    import app_settings
    import game_lang
    try:
        app_settings.set_value(game_lang.SETTING_KEY, "zh-TW")
        manual, _, manual_log = run_collect(files, load, game_aspect="16:9", keep_all=False)
        manual_said = [line.split("] ", 1)[-1].strip() for line in manual_log]
    finally:
        app_settings.set_value(game_lang.SETTING_KEY, game_lang.AUTO)
    return {
        "手动指定繁體中文：照样认成外卡、各项对得上":
            len(manual) == 1 and all(manual[0][k] == WANT[k] for k in FIELDS),
        "手动指定语言时日志写明是设置页选的":
            any("设置页选的" in line for line in manual_said),
    }


def regression_checks():
    """只有开局横幅时不保存战绩，重复打开同一总结页时只保存一局。"""
    if not BANNER.is_file() or not SUMMARY.is_file():
        return {}
    out = {}
    frames = [BANNER, BANNER, fake_game.BLACK, fake_game.BLACK]
    rows, _, _ = run_collect(frames, load, keep_all=False)
    out["漏结算（只看到开局横幅）：不凭空记一局"] = len(rows) == 0
    frames = [fake_game.BLACK, SUMMARY, SUMMARY, fake_game.BLACK, SUMMARY, SUMMARY, fake_game.BLACK]
    rows, _, log = run_collect(frames, load, keep_all=False)
    out["重复打开结算页（同一张总结页来回切）：只记一局"] = len(rows) == 1
    out["重复打开时那一局还是外卡、数字没变"] = len(rows) == 1 and rows[0]["mode"] == "wildcard" \
        and rows[0]["placement"] == 3 and rows[0]["damage"] == 1324
    return out


def simplified_checks():
    """2026-11-26 简中实拍，回放原始序列和仅出现一帧总结页的情况。"""
    import app_settings
    import game_lang

    files = sorted(CN_FOLDER.glob("*.jpg"))
    if len(files) != CN_COUNT or not CN_SUMMARY.is_file():
        raise MissingShots(f"{CN_FOLDER}（要 {CN_COUNT} 张，找到 {len(files)} 张）")

    checks = {}
    try:
        for language, label in ((game_lang.AUTO, "自动识别简中"), ("zh-CN", "手动指定简中")):
            app_settings.set_value(game_lang.SETTING_KEY, language)
            rows, _, log = run_collect(files, load, game_aspect="16:9", keep_all=False)
            actual = [{key: row[key] for key in CN_WANT} for row in rows]
            checks[f"{label}整局只记一条，读到外卡和风暴点，结算符合截图（{actual}）"] = (
                actual == [CN_WANT] and any("开局画面：读到 外卡、风暴点" in line for line in log)
                and rows[0]["rp_delta"] is None and rows[0]["rank_tier"] is None
            )
            checks[f"{label}用右上角圆标核对本局模式"] = any("外卡圆标与本局模式一致" in line for line in log)
        rows, _, _ = run_collect([fake_game.BLACK, CN_SUMMARY, fake_game.BLACK], load,
                                 game_aspect="16:9", keep_all=False)
        conn = storage.connect(fake_game.last_db)
        mates = [mate for row in rows for mate in storage.teammates_of(conn, row["match_id"])]
        readings = [reading for row in rows for reading in storage.readings_of(conn, row["match_id"])]
        conn.close()
        checks["漏开局且总结页只出现一帧时认成外卡，地图留空"] = (
            [{key: row[key] for key in CN_WANT} for row in rows] == [{**CN_WANT, "map": None}]
        )
        checks["简中外卡专属数字未写入普通急救和重生（队友及识别读数）"] = (
            len(mates) == 2 and all(mate["revives"] is None and mate["respawns"] is None for mate in mates)
            and all(reading["value"] is None for reading in readings if reading["field"] in ("revives", "respawns"))
        )
    finally:
        app_settings.set_value(game_lang.SETTING_KEY, game_lang.AUTO)
    return checks


def main():
    print("外卡（Wildcard）：简中、繁中实拍整局重放和模式口径")
    checks = {**config_checks(), **teammate_skip_checks(), **new_mode_interface_checks(),
              **round_boundary_checks(), **summary_mode_upgrade_checks()}
    skipped = False
    try:
        checks.update(ordinary_summary_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过普通结算页负例：找不到截图 {e}")
        skipped = True
    try:
        checks.update(banner_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过单张截图部分：找不到截图 {e}")
    try:
        checks.update(missed_start_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过漏开局部分：找不到截图 {e}")
    try:
        checks.update(simplified_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过简中整局回放：找不到截图 {e}")
        skipped = True
    try:
        files = frames()
    except MissingShots as e:
        print(f"  ⏭ 跳过整局重放部分：找不到截图 {e}（截图只在开发者电脑上，不在仓库里）")
        skipped = True
        return None if report(checks) else False
    checks.update(replay_checks(files))
    checks.update(banner_only_checks(files))
    checks.update(language_entry_checks(files))
    try:
        checks.update(regression_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过共同回归部分：找不到截图 {e}")
    ok = report(checks)
    print("  ⚠ 英文外卡仍缺真实整局回放，本组只检查英文配置")
    return None if ok and skipped else ok


if __name__ == "__main__":
    raise SystemExit(1 if main() is False else 0)
