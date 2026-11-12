"""测试工具窗口的预览（开发工具）：用假数据打开 Evo 测试工具的真窗口，可以直接跳到某一步，改界面时边看边改。

python tools/tester_preview.py                      打开窗口，从"选项目"开始，点"开始""结束"都能走（截屏是假的）
python tools/tester_preview.py --page welcome       每次打开先看到的欢迎页
python tools/tester_preview.py --page reopen        再次打开时的档案页：问"要直接用上次的档案吗？"
python tools/tester_preview.py --page finish        直接到"收尾"（正常对局：3 局假战绩等测试员确认）
    --page welcome / profile（第一次打开的档案页）/ reopen（再次打开的档案页）/ projects / recording / finish / done
    --project match / weapons / seasons / diag       哪个测试项目（默认 match = 正常对局）
    --no-evo                                         假装 Evo 没开着（看"选项目"的提醒）
    --shot 文件.png                                  打开、截下窗口（带标题栏）存成图片就关掉（给 Claude 看）

不截屏、不读 Evo 的数据库、不写测试员档案；点"打包"时把测试员确认的答案打印出来（看 collect() 交出去的是什么），
不真的打包。战绩是编的。
"""

import argparse
import ctypes
import json
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

import code_path  # noqa: F401  先把 code/ 加进搜索路径，下面才能 import 程序代码（见 code_path.py）
import tester_projects
import tester_window

PROFILE = {"nickname": "预览", "game_language": "简体中文", "launch_options": "-w 2560 -h 1440"}
PAGES = ("welcome", "profile", "reopen", "projects", "recording", "finish", "done")


def sample_shot(out):
    """收尾页每局旁边的"总结页截图"用哪张：项目 captures/live/ 里最新的一张总结页截图（只在本机）；没有就画一张灰的。"""
    found = sorted(Path(code_path.ROOT, "captures", "live").glob("*/summary/*.png"))
    if found:
        return found[-1]
    from PIL import Image, ImageDraw

    path = out / "summary_preview.png"
    img = Image.new("RGB", (1920, 1080), (30, 34, 44))
    ImageDraw.Draw(img).text((80, 80), "SUMMARY (preview)", fill=(200, 200, 200))
    img.save(path)
    return path


def fake_matches(now, shot):
    """3 局编的战绩：一局普通的、一局 Evo 标了待确认的、一局有几项没读出来的（这一局没有总结页截图）。"""
    base = {"mode": "ranked", "rp_source": "经验值页", "edited": None, "app_version": "0.5.6", "readings": {},
            "evidence_path": str(shot)}
    return [
        {**base, "match_id": 101, "last_read": now - timedelta(minutes=48), "map": "风暴点", "legend": "班加罗尔",
         "needs_review": 0, "review_note": None, "placement": 7, "kills": 2, "assists": 1, "knockdowns": 3,
         "damage": 812, "rp_delta": 42},
        {**base, "match_id": 102, "last_read": now - timedelta(minutes=27), "map": "残月", "legend": "亡灵",
         "needs_review": 1, "review_note": "名次和淘汰画面对不上", "placement": 2, "kills": 4, "assists": 2,
         "knockdowns": 5, "damage": 1530, "rp_delta": 118},
        {**base, "match_id": 103, "last_read": now - timedelta(minutes=6), "map": "世界尽头", "legend": "地平线",
         "needs_review": 0, "review_note": None, "rp_source": None, "evidence_path": None, "placement": 15, "kills": 0, "assists": None,
         "knockdowns": 0, "damage": 96, "rp_delta": None},
    ]


class FakeRecorder:
    """录制页要看的几个数：存了几张、多少 MB（跟着时间涨）、游戏开没开。"""

    def __init__(self, project):
        self.began = time.monotonic()
        self.game_on = True
        if project["capture"] == "still":
            self.pages = 0
        if project["capture"] == "weapons":
            self.done = []

    @property
    def saved(self):
        return int(time.monotonic() - self.began)

    @property
    def mb(self):
        return self.saved * 0.4


def session_factory(out):
    now = datetime.now()
    shot = sample_shot(out)

    class FakeSession:
        def __init__(self, project, profile, log, notice):
            self.project, self.profile = project, profile
            self.folder = out / "Evo测试_预览"
            self.recorder = self.info = None
            log("（预览：截屏是假的）")

        def start(self):
            self.recorder, self.info = FakeRecorder(self.project), {}

        def run(self, stop):
            stop.wait()
            return "点了结束"

        def evo_record(self):
            lobby = {"read_at": f"{now - timedelta(minutes=2):%Y-%m-%d %H:%M:%S}", "rank_tier": "白银",
                     "rank_division": "II", "rank_progress": 437, "score": 2437}
            seasons = [{"status": "saved"}] * 5 + [{"status": "pending"}] * 2
            return {"数据库": "预览", "局": fake_matches(now, shot), "大厅": [lobby], "赛季": seasons}

        def finish(self, note, answers=None):
            print("备注：", note or "（没写）")
            print("确认的答案：", json.dumps(answers, ensure_ascii=False, indent=2, default=str))
            zip_path = out / f"{tester_projects.package_name(self.project, '预览', now)}.zip"
            zip_path.write_bytes(b"0" * 2 ** 20 * 3)
            return zip_path

    return FakeSession


def window_rect(root):
    """整个窗口（带标题栏）在屏幕上的位置：DWM 给的外框，不含看不见的阴影边。"""
    hwnd = ctypes.WinDLL("user32").GetParent(root.winfo_id())
    rect = (ctypes.c_long * 4)()
    if ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return tuple(rect)
    return (root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + root.winfo_width(),
            root.winfo_rooty() + root.winfo_height())


def main():
    parser = argparse.ArgumentParser(description="用假数据打开测试工具的窗口")
    parser.add_argument("--page", choices=PAGES, default="projects")
    parser.add_argument("--project", default="match")
    parser.add_argument("--no-evo", action="store_true")
    parser.add_argument("--shot", type=Path, default=None)
    args = parser.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")

    projects = tester_projects.load_projects()
    project = projects[args.project]
    out = Path(tempfile.mkdtemp(prefix="evo_tester_preview_"))
    new_session = session_factory(out)

    tester_window.make_dpi_aware()
    root = tk.Tk()
    app = tester_window.TesterApp(
        root, projects, {} if args.page == "profile" else PROFILE, tester_projects.languages(),
        lambda profile: print("档案（预览，不保存）：", profile), new_session,
        lambda: not args.no_evo, "预览", tester_window.icon_path(), open_folder=lambda path: print("打开文件夹：", path))

    if args.page in ("recording", "finish", "done"):
        app.project = project
        app.start(project)  # 录制页；截屏在另一个线程里"跑"（假的），等点结束
        if args.page in ("finish", "done"):
            app.stop.set()
            while app.state != "finishing":  # 等假截屏停下、窗口到收尾
                root.update()
                time.sleep(0.02)
        if args.page == "done":
            app.show_done(app.session.finish("", None))
    elif args.page == "projects":
        app.project = project
        app.show_projects()
    elif args.page in ("profile", "reopen"):
        app.show_profile()  # 打开时先是欢迎页（welcome 就停在那里）

    if args.shot is not None:
        root.attributes("-topmost", True)

        def shoot():
            from PIL import ImageGrab

            root.update()
            ImageGrab.grab(bbox=window_rect(root), all_screens=True).save(args.shot)
            print("截好了：", args.shot)
            root.destroy()

        root.after(1200, shoot)
    root.mainloop()


if __name__ == "__main__":
    main()
