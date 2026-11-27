"""运行测试：python tests/run_all.py 跑全部（约 4 分钟）；改了模型时 python tests/run_all.py --model 只跑用到的几组

什么时候跑哪一档、什么时候只跑相关的一组（python tests/test_xxx.py）、什么时候不用跑：见 docs/测试SOP.md。
全部 ✅ 才说明没改坏。结果表里每一组后面写着用了几秒。

几组同时跑（2026-11-21 用户要的：以前一组接一组，全部要 15 分钟）：每组单独一个进程（各自的临时数据文件夹，互不干扰），
默认同时跑 CPU 线程数的一半（-j N 改；-j 1 就是一组接一组）。上次用时久的先跑（记在 build/test_times.json，不进 git），
总时间最短；每组跑完就把它的输出整段打出来，最后的结果表照 TESTS 的顺序。一组 20 分钟没跑完当作卡住了，算没通过。

--model（2026-11-21 用户要的："只是改了录像，为什么还要测直播"）：代码地图（tools/code_map.py 的 SECTIONS）里"模型""引擎""录像"
几节的程序算模型。看 git 里还没提交的改动（含新文件），跑两种组：
① 测试文件里直接 import 了改到的程序的组；② 用 fake_game 喂截图、整局重放的组（FAKE_GAME_MODEL 里那几个；它们把整个模型
从头跑一遍）。
- 改了 config/ 里的 xxx.json（或 config/lang/<语言>/xxx.json）：算作改了读它的程序（code/ 里写着 "xxx.json" 的那几个）；
  参照图（config/templates/）这些认不出是谁读的，当作模型全改了
- 只改了"录像"一节的程序（含它们读的 recording.json）：整局重放只跑一组（SMOKE_REPLAY）。重放时录像是关着的，测不到录像；
  录像关着时什么都不做，由 test_recorder 查
- 没有还没提交的改动、读不到 git：当作模型全改了
新加的测试不用登记，自动按 import 判断。

测试用的截图不提交到仓库（太大，而且有其他玩家的名字和 ID），只在开发者电脑上项目文件夹的 captures/ 里；
找不到截图的测试会显示"跳过"。测试文件只 import 项目代码，项目代码不 import 测试，所以测试不会被打进安装包。
"""

import ast
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

TESTS = {"总结页停留": "test_summary_page", "轻量（按需加载模型）": "test_light", "整晚重放": "test_replay",
         "录像重放（把整局录像喂给记录程序，看只靠录像记下的五项对不对）": "test_replay_recordings",
         "传奇识别": "test_legends", "玩家名（区分账号）": "test_player_name", "多账号（对照表和弹窗）": "test_accounts",
         "队友（读卡片、队友表、战绩页）": "test_teammates", "截图留不留": "test_screenshots", "段位": "test_ranks",
         "源码运行（战绩记进安装版）": "test_source_run", "淘汰画面（名次入库、和总结页/经验值页核对）": "test_eliminated",
         "0.3.0 新流程（对局中、冠军、大厅校准、各来源优先级）": "test_in_match",
         "一键更新（下载、核对、安装、恢复、面板）": "test_updater",
         "录像的录制核心（录成 MP4、断了重开、空档保留、声音对齐、拼回没拼完的、ffmpeg 跟着退；没有 ffmpeg 就只跑一部分）": "test_recorder",
         "录像页（导航什么时候有、列表、播放页的时间轴和事件、拖动、删除、战绩页的看录像按钮）": "test_recordings_page",
         "国内渠道 Mirror酱（问版本、检查更新、一键更新、增量包、设置页、发版的文件清单）": "test_mirrorchyan",
         "WebView2 检查（没有时问要不要装、不装用浏览器、不用 IE 内核、去掉 DLL 的下载标记）": "test_webview2",
         "窗口的启动画面（网页出来之前不是一块黑、颜色跟着主题、那行字的字体）": "test_splash",
         "游戏画面比例（16:10、4:3：换算、自动检测、设置页、整局重放）": "test_aspect",
         "第 31 赛季的新界面（简体、16:10：大厅、经验值页改写成'RP'，一局排位整段重放）": "test_season31",
         "游戏语言（规则按语言分开、认出游戏语言、设置页选游戏语言、匹配局不补段位）": "test_game_lang",
         "英文游戏画面（排位、匹配两局整段重放）": "test_english",
         "繁体中文游戏画面（匹配两局、排位一局整段重放）": "test_traditional",
         "外卡（Wildcard）：简中和繁中整局回放、模式口径": "test_wildcard",
         "统计对账（排位、匹配、外卡、未知各一局：本机和全体玩家口径一致，要 Node.js）": "test_mode_stats",
         "简体中文的匹配局（'三人赛'横幅、'未上榜'大厅，一局整段重放）": "test_simplified_pubs",
         "数据文件夹（放在哪里、搬家、指路文件、老玩家自动搬）": "test_data_move",
         "传奇头像（图标、许可文件、战绩页和总览页）": "test_legend_icons",
         "开机自动启动（启动项、任务管理器里关掉、换了位置、被拦住、面板）": "test_autostart",
         "提示音（声音文件、开关、什么时候响、记录程序、设置页）": "test_sounds",
         "分享战绩（三种卡片、不写队友名字、对话框、保存图片）": "test_share",
         "数据页（常一起玩的队友、传奇表现）": "test_stats_page",
         "数据页\"全体玩家\"（开了匿名统计才能看、下载汇总、和自己比、筛选、连不上）": "test_global_stats",
         "六维图（和同分段的玩家比、没开匿名统计时锁住、后台下载）": "test_radar",
         "总览页\"关注我\"（B 站、小黑盒、Evo 官网）、右上角派圈助手、设置页支持我和鸣谢卡片": "test_follow",
         "总览页的地图轮换（下载、中文名、倒计时、过了点、连不上、设置页的开关）": "test_map_rotation",
         "开发者公告（和检查新版本共用开关、读公告、显示规则、托盘提醒、提示条）": "test_notice",
         "赛事数据（赛事文件的格式和下载、查开播状态、抖音签名、发赛事数据的脚本）": "test_esports",
         "协作网页生成的赛事文件和 tools/esports.py 的一模一样（要 Node.js，没有就跳过）": "test_team_publish",
         "赛事页（导航、三块、没读到、几位选手在播）": "test_esports_page",
         "看直播页（页面、名单、给脚本的数据、开播状态接口、Apex 接口）": "test_esports_watch",
         "在 Evo 窗口里播斗鱼（FLV 拆标签和接续、斗鱼取地址、面板的两个接口）": "test_watch_play",
         "B站扫码登录和 B站原画（加密保存、二维码、登录、取地址、面板的登录接口）": "test_bili_login",
         "斗鱼登录和斗鱼原画（读 Cookie、续期、带着登录取地址、登录窗口、面板的登录接口）": "test_douyu_login",
         "HLS 中转（看直播页播 B站：播放列表和 TS 接成一条流）": "test_hls_relay",
         "弹幕（斗鱼、B站：要连哪里、面板的接口、页面上的开关）": "test_danmaku",
         "抖音的弹幕（签名、protobuf、WebSocket、转给页面）": "test_douyin_chat",
         "电竞页的关注和开播通知（谁刚开播、名单、接口、页面、托盘里的线程）": "test_follow_alerts",
         "最常用武器（认武器、计时、记进数据库、数据页）": "test_weapons",
         "匿名统计（同意了才发、只发说好的几项、日活、重发改过的局、Evo 的占用、连不上、面板）": "test_telemetry",
         "Evo 占用（读进程、现在的、对局中最近 10 局、侧边栏）": "test_usage",
         "侧边栏（导航、展开 / 收起、主题、切换账号、内容区按自己的宽度排）": "test_side",
         "测试工具（截屏、找 Evo、诊断信息、打包、看压缩包）": "test_tester",
         "截屏（dxcam 建不起来时：双显卡的笔记本设成节能、改用 mss）": "test_capture",
         "按赛季看（本赛季 / 上赛季：总览页、数据页、战绩页）": "test_season_ranges",
         "游戏里的数据页（认出、读两栏、核对，录入历史赛季数据用）": "test_season_stats",
         "录入历史赛季数据（新表、录入模式、记录程序读数据页）": "test_season_import",
         "\"赛季\"页面（录入按钮、待核对、保存、历史赛季表、设置页那一行）": "test_seasons_page",
         "代码地图（docs/代码地图.md 是最新的、每个文件都分了组）": "test_code_map",
         "联网出错的原因、国内备用线路的入口（GitHub 上没有备用线路时照旧；测试地址不走）": "test_net_route",
         "测试员 11-04 的截图（第 31 赛季 16:9、4:3：新地图、大厅分数、队友名字、经验值页段位）": "test_testers_1104",
         "网友 11-26 征集的截图（16:9 简体、繁体，16:10：外卡横幅、繁体'擊倒'、繁体剩余小队数，每张认对画面）": "test_adapt_1126",
         "训练小模型的素材和网页（分堆、考卷、训练用哪些答案、小模型读数、核对和成绩网页）": "test_train_data"}

# 云同步（微信小程序）的测试只在开发者电脑上，不上传 GitHub（2026-10-24 用户决定）：有就一起跑
if (HERE / "test_cloud_sync.py").is_file():
    TESTS["云同步（二维码、绑定、上传、重传、删除、连不上、手机上解绑、设置页）"] = "test_cloud_sync"
# 国内备用线路（2026-11-19）：和云同步一样只在开发者电脑上
if (HERE / "test_cloud_relay.py").is_file():
    TESTS["国内备用线路（经派圈助手的云环境取公开文件、记住连不上、两边都不行时说两边的原因）"] = "test_cloud_relay"


MODEL_SECTIONS = ("模型", "引擎", "录像")  # 代码地图里这几节的程序算"模型"
RECORDING_SECTION = "录像"  # 只改了这一节的程序：整局重放只跑 SMOKE_REPLAY 一组
SMOKE_REPLAY = "test_simplified_pubs"  # 最短的一组整局重放（约 17 秒）：看记录程序带着改过的录像代码照样跑得通
FAKE_GAME_MODEL = {"run_collect", "FakeCamera", "FakeClock", "shot", "load", "SHOTS", "screen_ready", "MissingShots",
                   "BLACK"}  # fake_game 里喂截图、整局重放用的（ROOT、CODE、_TEMP、report 这些只是路径和打分，不算）


TIMES = ROOT / "build" / "test_times.json"  # 上次每组用了几秒：久的先跑
TIMEOUT = 20 * 60  # 一组超过这么久当作卡住了
SKIP_CODE = 3  # 子进程的退出码：0 通过、1 没通过（出错也是 1）、3 跳过


def section_modules(wanted):
    """代码地图（tools/code_map.py 的 SECTIONS，只读文件、不导入）里这几节的程序 → 模块名。"""
    tree = ast.parse((ROOT / "tools" / "code_map.py").read_text(encoding="utf-8"))
    sections = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "SECTIONS" for t in node.targets))
    names = set()
    for group, _, items in sections:
        if group in wanted:
            for item in items:
                path = item[0] if isinstance(item, tuple) else item
                if path.startswith("code/") and path.endswith(".py"):
                    names.add(Path(path).stem)
    return names


def config_readers(path):
    """config/ 里改了的一个文件 → 读它的程序（模块名的集合）；认不出是谁读的返回 None。
    认得出的只有 config/xxx.json 和 config/lang/<语言>/xxx.json：code/ 里写着 "xxx.json" 的程序
    （load_config("xxx.json")、CONFIG_NAME = "xxx.json" 都是这样写的）。"""
    parts = Path(path).parts
    if not parts[-1].endswith(".json") or not (len(parts) == 2 or (len(parts) == 4 and parts[1] == "lang")):
        return None
    quoted = f'"{parts[-1]}"'
    readers = {code.stem for code in (ROOT / "code").glob("*.py") if quoted in code.read_text(encoding="utf-8")}
    return readers or None


def changed_modules(model):
    """git 里还没提交的改动（含新文件）改到的程序 → (模块名的集合, 说明)。
    改到的 code/ 文件只算模型的；改了 config/ 算读它的程序（不管是不是模型）。当作模型全改了的情况见开头的说明。"""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "code", "config"],
                             cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set(model), "读不到 git，当作模型全改了"
    if out.returncode != 0:
        return set(model), "读不到 git，当作模型全改了"
    paths = [line[3:].strip().strip('"').split(" -> ")[-1] for line in out.stdout.splitlines() if line.strip()]
    if not paths:
        return set(model), "没有还没提交的改动，当作模型全改了"
    changed = set()
    for path in paths:
        if path.startswith("config/"):
            readers = config_readers(path)
            if readers is None:
                return set(model), f"改了 {path}（认不出是哪个程序读的），当作模型全改了"
            changed |= readers
        elif path.startswith("code/") and path.endswith(".py") and Path(path).stem in model:
            changed.add(Path(path).stem)
    return changed, "改到的：" + ("、".join(sorted(changed)) or "没有模型文件")


def test_uses(module):
    """这一组的测试文件 → (直接 import 的模块名, 是不是用 fake_game 喂截图、整局重放)。"""
    tree = ast.parse((HERE / f"{module}.py").read_text(encoding="utf-8"))
    imports, replay = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            base = node.module.split(".")[0]
            imports.add(base)
            replay = replay or (base == "fake_game" and any(a.name in FAKE_GAME_MODEL for a in node.names))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "fake_game" \
                and node.attr in FAKE_GAME_MODEL:
            replay = True
    return imports, replay


def model_tests(tests):
    """--model：要跑的组和一行说明。"""
    model = model_modules()
    changed, note = changed_modules(model)
    replays = bool(changed & (model - section_modules((RECORDING_SECTION,))))
    smoke = bool(changed) and not replays
    picked = {}
    for name, module in tests.items():
        imports, replay = test_uses(module)
        if imports & changed or (replay and (replays or (smoke and module == SMOKE_REPLAY))):
            picked[name] = module
    how = "整局重放都跑" if replays else ("只改了录像：整局重放只跑一组" if smoke else "不用跑整局重放")
    return picked, f"只跑用到模型的 {len(picked)} 组（共 {len(tests)} 组；{note}；{how}；跑全部去掉 --model）"


def model_modules():
    return section_modules(MODEL_SECTIONS)


def run_one(module):
    """子进程里跑一组，返回退出码。"""
    import importlib

    import fake_game  # noqa: F401  要在项目代码之前导入（设置临时数据文件夹）
    import cv2
    cv2.setNumThreads(1)  # 几组同时跑：每组只用一个核，免得 OpenCV 在每个进程里都开满线程、互相抢（读出来的结果一样）
    ok = importlib.import_module(module).main()
    return SKIP_CODE if ok is None else (0 if ok else 1)


def run_group(module):
    """另开一个进程跑一组 → (退出码, 输出, 用了几秒)。"""
    began = time.perf_counter()
    try:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--one", module], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        code, text = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as e:
        text = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        code, text = 1, text + f"\n  ❌ {TIMEOUT // 60} 分钟没跑完，当作卡住了"
    return code, text.rstrip(), time.perf_counter() - began


def load_times():
    try:
        return json.loads(TIMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_times(took):
    times = {**load_times(), **{module: round(seconds, 1) for module, seconds in took.items()}}
    try:
        TIMES.parent.mkdir(exist_ok=True)
        TIMES.write_text(json.dumps(times, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def option(argv, names, default=None):
    for i, arg in enumerate(argv):
        if arg in names and i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # 输出被管道接走时（| tail、release.py）Windows 默认是 GBK，打不出 ✅：和 fake_game 一样改成 UTF-8
    # （以前主进程 import 了 fake_game，顺带改了；现在主进程不 import 各组）
    sys.stdout.reconfigure(encoding="utf-8")
    one = option(argv, ("--one",))
    if one:
        return run_one(one)
    jobs = max(1, int(option(argv, ("-j", "--jobs"), max(1, (os.cpu_count() or 2) // 2))))
    tests = TESTS
    if "--model" in argv:
        tests, note = model_tests(TESTS)
        print(note + "\n", flush=True)
    names = {module: name for name, module in tests.items()}
    last = load_times()
    order = sorted(tests.values(), key=lambda module: -last.get(module, 10 ** 6))  # 没跑过的、上次久的先跑
    print(f"{len(order)} 组，同时跑 {min(jobs, len(order)) if order else 0} 组（-j N 改）\n", flush=True)
    start = time.perf_counter()
    codes, took = {}, {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(run_group, module): module for module in order}
        for count, future in enumerate(as_completed(futures), 1):
            module = futures[future]
            code, text, seconds = future.result()
            codes[module], took[module] = code, seconds
            mark = "⏭" if code == SKIP_CODE else ("✅" if code == 0 else "❌")
            print(f"[{count}/{len(order)}] {mark} {names[module]}（{seconds:.0f} 秒）\n{text}\n", flush=True)
    save_times(took)
    print(f"===== 结果（用时 {time.perf_counter() - start:.0f} 秒；同时跑 {jobs} 组，各组加起来 {sum(took.values()):.0f} 秒）=====")
    for name, module in tests.items():
        code = codes[module]
        state = "⏭ 跳过" if code == SKIP_CODE else ("✅ 通过" if code == 0 else "❌ 没通过")
        print(f"  {state}  {name}（{took[module]:.0f} 秒）")
    return all(code in (0, SKIP_CODE) for code in codes.values())


if __name__ == "__main__":
    if "--one" in sys.argv:
        raise SystemExit(main())
    raise SystemExit(0 if main() else 1)
