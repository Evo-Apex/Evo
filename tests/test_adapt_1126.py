"""网友 2026-11-26 在群里征集的截图（第 31 赛季）：16:9 简体两位（1920×1080）、16:9 繁体一位（2560×1440）、
16:10 简体一位（拉伸到 2560×1440）。每位截了大厅、开局横幅（排位、匹配、外卡）、对局中、击倒、助攻击倒、总结页、经验值页、
观战队友、游戏里的数据页。

截图放在 captures/界面适配/2026-11-26（不在仓库里：有别的玩家的名字；找不到就跳过），文件名写着比例、语言和画面
（"16比9繁中排位击倒.png"），旁边的 清单.csv 是网友那份表格导出的。这次看出来、改了的：
- 外卡模式的开局横幅"外卡"（简体、繁体一样）以前一张都认不出：加了 screens.json 的 match_start_wildcard，模式记"其他"
- 繁体中间那一行"擊倒"以前没有参照图，击倒、助攻击倒都认不出：加了 config/templates/zh-TW/hud/knock.png（"助攻"和简体一样）
- 繁体右上角的剩余小队数：繁体"剩餘小隊數"短一个字、整条往右挪，以前只框到两位数的前一位（"16"读成"1"）
另外每一张都按文件名对一遍认出的画面，简体 16:9、16:10 的击倒、助攻击倒也在里面（新分辨率的位置照旧对）。
不写网友的名字：只比段位、数字。

运行：python tests/test_adapt_1126.py
"""

from fake_game import SHOTS, load, report  # 要在项目代码之前导入

import layout
from hud import Hud
from match_start import read_match_start
from ocr import SMALL_CROP_PARAMS, create_engine, load_config
from screens import GameScreens

FOLDER = SHOTS / "界面适配" / "2026-11-26"
# 文件名里的画面 → 应该认成哪个画面（None：都不是，比如对局中、游戏里的数据页）
KINDS = (("大厅", "lobby"), ("进入游戏", "match_start"), ("总结页", "summary"), ("经验值页", "xp"), ("历史战绩", None))
# 截图本身不是文件名说的那个画面：繁体那张"总结页"其实是"死亡回顧"标签页（右下角有"返回大廳"）
NOT_AS_NAMED = {"16比9繁中排位总结页.png": "squad_out"}


def setup(name):
    """按文件名设好游戏画面比例，返回这张截图的游戏语言。"""
    layout.set_game_aspect(16 / 10 if name.startswith("16比10") else 16 / 9)
    return "zh-TW" if "繁中" in name else "zh-CN"


def screen_checks(engine):
    detector = GameScreens(engine)
    wrong = []
    files = sorted(p for p in FOLDER.iterdir() if p.suffix.lower() in (".png", ".jpg"))
    for path in files:
        detector.use_choice(setup(path.name))
        want = NOT_AS_NAMED.get(path.name, next((screen for key, screen in KINDS if key in path.name), None))
        got = detector.detect(load(path))
        if got != want:
            wrong.append(f"{path.name} 认成 {got}（应该是 {want}）")
    layout.set_game_aspect(None)
    return {f"{len(files)} 张截图都认成文件名说的画面（{'；'.join(wrong) or '全对'}）": not wrong and len(files) >= 50}


def wildcard_checks(engine):
    got = {}
    for name in ("16比9简中进入游戏界面外卡.png", "16比9简中进入游戏界面外卡_2.png", "16比10简中进入游戏界面外卡.jpg",
                 "16比9繁中进入游戏界面外卡.png"):
        lang = setup(name)
        r = read_match_start(engine, load(FOLDER / name), load_config("match_start.json", lang))
        got[name] = (r["mode"], r["map"])
    layout.set_game_aspect(None)
    return {f"外卡的开局横幅：模式记'其他'、地图风暴点（{list(got.values())}）":
            all(v == ("other", "风暴点") for v in got.values())}


def knock_checks(engine):
    """中间那一行：(找到'击倒'了没有, 是不是助攻击倒)。"""
    huds = {lang: Hud(engine, lang=lang) for lang in ("zh-CN", "zh-TW")}
    want = {"16比9简中排位击倒.jpg": (True, False), "16比9简中排位击倒_2.png": (True, False),
            "16比9简中排位助攻击倒.jpg": (True, True), "16比9简中排位助攻击倒_2.png": (True, True),
            "16比10简中排位击倒.jpg": (True, False), "16比10简中排位助攻击倒.jpg": (True, True),
            "16比9繁中排位击倒.png": (True, False), "16比9繁中排位助攻击倒.png": (True, True),
            "16比9简中排位游戏中界面_2.png": (False, False), "16比9繁中排位游戏中界面.png": (False, False)}
    got = {}
    for name in want:
        hud = huds[setup(name)]
        frame = load(FOLDER / name)
        x = hud.knock(frame)
        got[name] = (x is not None, x is not None and hud.knock_assist(frame, x))
    layout.set_game_aspect(None)
    bad = [f"{n} {got[n]}" for n in want if got[n] != want[n]]
    return {f"中间那一行'击倒 / 擊倒'：16:9 简体两位、16:10 简体、16:9 繁体都找得到，助攻击倒分得出，没有击倒的不认（{'；'.join(bad) or '全对'}）":
            not bad}


def squads_checks(engine):
    hud = Hud(engine, lang="zh-TW")
    want = {"16比9繁中排位游戏中界面.png": 16, "16比9繁中排位击倒.png": 16, "16比9繁中排位助攻击倒.png": 14}
    got = {}
    for name in want:
        setup(name)
        values, _, _ = hud.read_top(hud.top_snip(load(FOLDER / name)))
        got[name] = values["squads_left"]
    layout.set_game_aspect(None)
    return {f"繁体右上角的剩余小队数读全两位（{list(got.values())}；以前读成 1、没读出、1）": got == want}


def main():
    print("网友 11-26 征集的截图（第 31 赛季：16:9 简体两位、16:9 繁体、16:10 简体）")
    if not FOLDER.is_dir():
        print(f"  ⏭ 跳过：找不到 {FOLDER}（截图不在仓库里）")
        return None
    engine = create_engine(SMALL_CROP_PARAMS)
    checks = screen_checks(engine)
    checks.update(wildcard_checks(engine))
    checks.update(knock_checks(engine))
    checks.update(squads_checks(engine))
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
