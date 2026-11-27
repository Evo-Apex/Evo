"""对局模式（`matches.mode` 存的值）：有哪几种、显示成什么名字、算不算大逃杀、要不要用排位那套（RP、段位、走势）。

模式的值是英文小写，数据库和页面都存这个：
- ranked 排位、pubs 匹配：大逃杀（config/match_start.json 的开局横幅读出来的）
- wildcard 外卡：大逃杀的一种轮换玩法（2026-11-23 起）。结算页有自己的标签（繁体的"最後倖存""自我急救次數"，
  见 summary.json 的 wildcard），末两项不是普通急救和重生，所以外卡不写 revives / respawns（见 post_match）
- other 其他：认出来了、但不是上面几种（比如别的轮换玩法还没做时的兜底）
- None 还不知道：没认出模式（老数据里很多，简体匹配局原来没认出）

**加一种新玩法只改这里的一条 + 配置**（外卡就是这么接进来的，步骤见 docs/识别经验.md"新模式接入"）：
`MODES` 里加模式值和它的属性；开局横幅的文字写进 config/match_start.json 的 modes；参照图/关键字写进
config/screens.json（`as` 是开局画面）；总结页最后两行的标签写进 config/summary.json 的 <summary> 段和各语言配置。
结算页兜底认模式（summary_page.detect_mode）、跳过不写的字段（skip_fields）、队友同规则、统计分组、
页面筛选都会自动用上，不用再改记录程序、面板或测试工具的代码。

属性（`MODES` 的每一项）：
- label：显示的名字（战绩页、总览页、分享卡、设置页都用它）
- br：算不算大逃杀（有大逃次名次、能算胜率和前五率）
- ranked：要不要用排位那套（RP、段位、校准、排位走势）
- stats：在"全体玩家"汇总里有没有自己一份（False 的并进匹配，如 other）
- choices：战绩页的修改表单、"我的"筛选里能不能单独选
- summary：config/summary.json 里的段落名（结算页最后两行对得上就认成这个模式）；None = 没有结算页标签

统计口径：
- 全部模式：所有保存的局（含历史未知模式），每局计数一次
- 大逃杀：只算 br 的模式，胜率以有有效名次的局为分母
- 单一模式：可以单独筛；RP、段位、排位走势只在 ranked 下用
- 未知模式：mode 是 None 的局，单独一份统计，不自动归进匹配（以前"不是排位就算匹配"的口径改掉），历史数据保留原值

面板、统计、匿名上报、服务端（server/stats/worker.js 的 modeGroup）都要用这里的名字，别再各写一份。
只用标准库：面板会导入它，不能带上识别引擎（见 tests/test_light.py）。
"""

RANKED, PUBS, WILDCARD, OTHER = "ranked", "pubs", "wildcard", "other"
UNKNOWN = None  # 没认出模式
ALL = "all"  # 全部模式（"全体玩家"的筛选键，不是数据库里的值）

# 模式注册表（顺序就是页面上的顺序）。以后加团队死斗、控制、子弹时间等，在这里加一条：
#   "tdm": {"label": "团队死斗", "br": False, "ranked": False, "stats": True, "choices": True, "summary": "tdm"},
# 再把横幅、参照图、结算页标签写进配置（见文件开头的说明）。
MODES = {
    RANKED: {"label": "排位", "br": True, "ranked": True, "stats": True, "choices": True, "summary": None},
    PUBS: {"label": "匹配", "br": True, "ranked": False, "stats": True, "choices": True, "summary": None},
    WILDCARD: {"label": "外卡", "br": True, "ranked": False, "stats": True, "choices": True, "summary": "wildcard"},
    OTHER: {"label": "其他", "br": False, "ranked": False, "stats": False, "choices": True, "summary": None},
}

# 显示的名字（战绩页、总览页、分享卡、设置页都用它）
LABELS = {mode: spec["label"] for mode, spec in MODES.items()}

# 大逃杀：有名次、能算胜率和前五率的玩法。以后加的轮换玩法算不算大逃杀，改 MODES 里的 br
BR_MODES = tuple(mode for mode, spec in MODES.items() if spec["br"])

# 大逃杀这一组在页面上叫什么（"全体玩家"的筛选框、说明文字用）
BR_LABEL = "大逃杀"

# 战绩页修改表单里的模式（顺序就是显示的顺序）。未知模式（None）不在这里，另外显示
CHOICES = {mode: spec["label"] for mode, spec in MODES.items() if spec["choices"]}

# "全体玩家"汇总里的几份（和服务器 server/stats/worker.js 的 sliceKeys 一样）：
# all = 全部模式（含历史未知模式，每局算一次）、br = 大逃杀、有自己一份的模式、unknown 未知（没认出模式）。
STATS_MODES = {"all": "全部模式", "br": BR_LABEL}
STATS_MODES.update({mode: spec["label"] for mode, spec in MODES.items() if spec["stats"]})
STATS_MODES["unknown"] = "未知"

# 排位那套（RP、段位、校准、排位走势）只在排位下用
RANKED_ONLY = RANKED


def label(mode, unknown="—"):
    """模式的值 → 显示的名字。未知（None）或没登记的值返回 unknown / 原值。"""
    return LABELS.get(mode, mode or unknown)


def is_br(mode):
    """算不算大逃杀（有名次、能算胜率和前五率）。"""
    return bool((MODES.get(mode) or {}).get("br"))


def is_ranked(mode):
    """要不要用排位那套（RP、段位、排位走势）。"""
    return bool((MODES.get(mode) or {}).get("ranked"))


def normalize(mode):
    """面板、匿名统计里用的模式值：认识的照原样，不认识的当作其他（历史数据可能存过奇怪的值）。"""
    return mode if mode in MODES else OTHER


def stats_mode(mode):
    """这一局在"全体玩家"汇总里算哪一份（和服务器的 modeGroup 一样）：有自己一份的模式照原样，
    没认出模式的算"未知"，没有自己一份的（other）并进匹配。"""
    spec = MODES.get(mode) if mode is not None else None
    if spec and spec["stats"]:
        return mode
    return "unknown" if mode is UNKNOWN else PUBS


def summary_key(mode):
    """这个模式在 config/summary.json 里的段落名（结算页兜底认模式用）；没有返回 None。"""
    return (MODES.get(mode) or {}).get("summary")


def summary_modes():
    """有结算页标签的模式：{config/summary.json 的段落名: 模式值}。加新玩法时在 MODES 里写 summary。"""
    return {spec["summary"]: mode for mode, spec in MODES.items() if spec.get("summary")}


def is_rotational(mode):
    """是不是有自己的结算页标签的轮换玩法（外卡、以后的团队死斗……）：这类模式最后两项可能不是急救、重生。"""
    return summary_key(mode) is not None


def mode_values(key):
    """筛选框里的一个键 → 数据库里 `matches.mode` 可能是哪几个值（战绩页的 SQL 筛选、面板的 mode_rows 都用它）。
    "br" = 所有算大逃杀的值；"unknown" = 没认出模式（None）；"pubs" = 所有并进匹配的值（匹配 + 其他等）；
    其它登记过的模式是它自己；"all"（全部模式）返回 ALL（不做筛选）；认不出的键返回 None（调用方当作一个具体的值）。"""
    if key == "all":
        return ALL
    if key == "br":
        return list(BR_MODES)
    if key == "unknown":
        return [UNKNOWN]
    if key in MODES:
        if key == PUBS:  # 匹配：连同并进匹配的模式（如 other）和认不出的值，和 stats_mode 一样
            return [mode for mode in MODES if stats_mode(mode) == PUBS]
        return [key]
    return None
