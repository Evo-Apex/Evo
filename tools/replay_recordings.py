"""录像重放（开发工具）：把每段整局录像当成屏幕画面喂给真正的记录程序，看它只靠录像记下的五项对不对。

Evo 正常在一局的总结页、经验值页上读名次、击杀、助攻、伤害、RP。玩家很快跳过这两页时，Evo 用备用来源：
名次靠淘汰画面 / 冠军横幅，击杀、助攻、伤害靠对局中右上角最后的读数，RP 靠对局中右上角最后的 RP
（规则见 docs/对局全流程图.md 的"数据来源和优先级"）。这个工具专门检验这些备用来源准不准；同一局的正确答案
（当时 Evo 在总结页、经验值页上读到的数）在正式数据库里。工具只负责把问题找出来，不改识别。

注意：录像在一局结束（淘汰画面、右下角"返回大厅"按钮）以后还要再录 3 秒才停，玩家手快时这 3 秒里已经点进了
总结页、经验值页，Evo 每 0.3–1 秒一张就截到了——2026-11-25 的 12 段里 9 段是这样（8 段读到了总结页、3 段进了经验值页）。
照原样喂的话，比的其实是总结页上的数，不是"玩家跳过总结页"时的结果。所以这个工具**默认把这两页藏起来**：
从录像里第一帧认出总结页或经验值页的地方起，后面的录像都换成黑图（相当于玩家没看这两页、直接走了），
淘汰画面、冠军横幅、返回大厅按钮、对局中的画面照常给。藏的时候用的是 Evo 自己认画面的代码，藏没藏住还会再查一遍
（见下面"藏总结页 / 经验值页"）；加 --keep-summary 就不藏，原样喂，用来对照。

怎么运行：
    E:\\Evo\\.venv\\Scripts\\python.exe tools/replay_recordings.py
    只跑名字里含某段文字的一两段（可以写几次）：--only 2026-11-21_202753 --only 2026-11-21_214305
    原样喂、不藏总结页 / 经验值页（对照用）：--keep-summary
    手动指定画面比例：--aspect 16:10（或 16:9、4:3；不写就是 None，collect 自己看画面认）
    手动指定文件夹：--data-dir "E:\\Evo Tracker Data"、--recordings-dir "E:\\...\\recordings"

正式数据怎么找、怎么对上数据库：
- 正式数据文件夹 = 安装版的数据文件夹（source_run.installed_data_dir()，这台电脑上是 E:\\Evo Tracker Data）。
  正式数据库（<数据文件夹>\\data\\evo.db）只读打开（sqlite 的 file:...?mode=ro），不往正式数据文件夹里写任何东西，
  不删、不动任何录像
- 录像文件夹 = settings.json 里的 recording_dir，没有就是 <数据文件夹>\\recordings；录像按月份放在子文件夹（年-月）里
  （--recordings-dir 也可以直接指到某个月份文件夹）。只要整局录像：跳过自动剪的高光片段（旁边 .json 里有 "highlight" 键）、
  正在录的 _recording\\、上次崩了没拼完的 "_未完成"。按 .json 里的 started 排好
- 对上数据库里的局：每段录像取 ended_at 在"这段录像的 started 之后、下一段录像的 started 之前"的第一局。
  最后一段没有下一段做上限，用"这段录像的 ended 之后 30 分钟"做上限（.json 里没有 ended 就没有上限）。
  再用传奇核对（.json 的 match.legend 和数据库的 legend，.json 里没有传奇就不核对）。
  找不到局、传奇不一样的录像在报告里写原因，不跑

喂画面：假摄像头 + 假时钟 + 临时数据库，调用真正的 live_collect.collect()（和 tests/fake_game.py 一个办法：
把 dxcam.create 换成假摄像头、把 live_collect 里的 time 换成假时钟。假时钟的 sleep 不真的等，所以比真人快很多）。
- 每段录像用一个新数据库（在临时数据文件夹里；整个进程结束时临时文件夹删掉）
- 按录像里的时间戳取画面：假时钟从开始走了 t 秒，就给录像里时间戳 ≤ t 的最后一帧。时间只往前走、顺序读，
  跳过的帧只 grab 不转成图（retrieve），所以比每帧都读快。录像断掉的那几秒（.json 的 outages 记着断在哪）没有画面，
  MP4 里时间戳会往前跳，重放时这段时间就重复上一帧（和真人看着屏幕差不多）
- 假摄像头的 width、height 和这段录像的画面一样大
- 画面比例：--aspect 给了就原样交给 collect(game_aspect=...)，不给是 None，collect 自己看画面认
  （fake_game 把 Apex 的视频设置文件指到临时文件夹，那里没有这个文件，所以会走"看画面认"）
- 默认藏总结页 / 经验值页（见下）；--keep-summary 时不藏
- 不往硬盘写画面图片（Evo 自己留的截图在临时数据文件夹里，跑完就删）

藏总结页 / 经验值页（模拟玩家跳过这两页）：
- 判断"是不是总结页 / 经验值页"用的是 Evo 自己认画面的代码（code/screens.py 的 GameScreens / ScreenDetector，
  只比 config/screens.json 里的 summary、xp 两个画面，关键字、检查区域都照 config/ 里的），只在假摄像头交给 Evo 的
  那些帧上认（对局中每 0.3 秒一张，其他时候每 1 秒一张），不用每帧都认。报告里的秒数是交出去那一帧的时间，
  最多比真正的第一帧晚 1 秒
- 画面比例要和 collect 用的一致（16:10 压成 16:9 时界面位置才对）：认的时候读 collect 现在用的那个（layout 里的），
  存下来、认完恢复，不影响 collect
- 认出第一帧总结页 / 经验值页就记下录像里的秒数，这一帧和后面的都换成黑图；淘汰画面、冠军横幅、返回大厅按钮、
  对局中的画面照常给。报告里每段写：从第几秒起换成黑图（录像共几秒）、认出了哪个画面；没认出就写"没有藏"。
  正常应该在录像最后 3–10 秒里，离结尾还远就标"可能认错了"
- 藏起来以后，重放完再查临时数据库的 readings 表（source 一列记着从哪个画面读的）：这一局只要有一条来源是
  总结页或经验值页，就在报告里标"重放里还是读到了总结页 / 经验值页，不算跳过"，这样的局不算进汇总表
  （说明没藏住，不能当成"跳过总结页"的结论）
- --keep-summary：不藏，照原样喂（Evo 能看到总结页、经验值页），用来对照：这时结果应该基本和数据库一样，
  能看出录像本身清不清楚。这时上面的检查不排除任何局，只在每局下面写读到了哪些来源

录像放完以后怎么处理（没有总结页时 Evo 什么时候把一局存下）：
- 读 live_collect 的结论：回到大厅、下一局开局、停止记录时 post.finish() 才把对局中右上角的读数单独记成一局
  （post_match.PostMatch._insert_hud_only）；一局打完了、玩家一直停在别的画面上（也不回大厅、不开下一局）不会存。
  停止记录时 collect 的 finally 里也会调 post.finish()，所以停的时候会把还没存的那一局存下
- 录像结尾没有大厅和开局画面（录像一局结束以后还录 3 秒就停），所以喂黑图多久都不会自己存下。做法：录像放完以后
  接着喂和录像一样大的黑图（玩家没看结算页面、直接走了），最多喂 15 分钟（假时间）；还没存下就停——相当于玩家
  直接退出游戏、Evo 停止记录。真实情况下玩家多半是回大厅（Evo 也是存同样的东西：右上角最后的读数），两者存下的一样。
  停完再查一次临时数据库：有局就比，没有就记"没存下"
- 停的时候正好是"停止记录"这条路，不会把这一局标成"没看到结算画面"（录像里的淘汰画面已经正常结束了这一局）

比哪五项、每项怎么判（报告里每一项一格）：
- 名次、击杀、助攻、伤害、RP（RP 只算 rp_source 是"经验值页"的正确答案）
- 一样：两边相等
- 少一点（只用于击杀、助攻、伤害）：重放的比正确答案小（对局中最后一秒没截到，规则里算正常）
- 错：名次、RP 不相等；击杀、助攻、伤害比正确答案大；伤害不到正确答案的一半（不是最后一秒没截到，
  多半是读错了：2026-11-25 第 194 局 1118 读成 11）
- 没读出：重放是空的；RP 在正确答案是冠军（名次 1）时写成"没读出（冠军局规则上不用右上角）"
- 没有答案：正确答案本身是空的（RP 的来源不是经验值页时也算"没有答案"）
- 另外列出重放那一局的 needs_review（待确认）、review_note、rp_source
一局都没存下记"没存下"；存下好几局记"分成了 N 局"并把每一局都列出来。

结果：
- 屏幕上打印每段的对照（录像名、第几局、传奇；每项：正确答案 / 重放 / 判定；待确认；藏了没有、从第几秒起、
  重放读到了哪些来源）和最后的汇总表，再列出没对上的录像和原因、每段用了多久
- 汇总表只算"藏住了、存下正好一局"的局；读到了总结页 / 经验值页的不算进去，表下面写一句几段没算
- 同样的内容存到 captures/recording_replay/<年月日_时分秒>/report.txt；
  每段录像那次 collect() 的日志（log 收到的每一行）存成同一个文件夹里的 <录像名>.log
"""

import argparse
import bisect
import itertools
import json
import re
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import code_path  # noqa: F401  把 code/ 加进找模块的路径，下面才能 import 程序代码
import source_run

ROOT = Path(__file__).resolve().parent.parent  # 项目文件夹
CAPTURES = ROOT / "captures"  # 报告存在这里（不进 git）
WORK_DIR_NAME = "_recording"  # 正在录的临时文件夹（见 recorder.py），里面的分段不算整局录像
TAIL_SECONDS = 15 * 60  # 录像放完以后最多再喂这么久的黑图（假时间），还没存下就停
LATE_MINUTES = 30  # 数据库里的局最多比录像的 ended 晚这么多分钟，再晚就不算这一局的
HIDE_FAR_SECONDS = 10  # 总结页 / 经验值页正常在录像最后 3–10 秒里；离结尾比这还远就提醒"可能认错了"
PAGES = ("summary", "xp")  # 要藏的两种画面（名字照 config/screens.json 的 screens）
FIELDS = (("placement", "名次"), ("kills", "击杀"), ("assists", "助攻"), ("damage", "伤害"), ("rp_delta", "RP"))
LOWER_OK = ("kills", "assists", "damage")  # 这几项重放比正确答案少是正常的（对局中最后一秒没截到）
VERDICTS = ("一样", "少一点", "错", "没读出", "没有答案")  # 汇总表的几列
_runs = itertools.count(1)  # 第几次重放（临时数据库的文件名）


def pad(text, width):
    """补空格补到 width 个显示宽度（汉字算两个），让汇总表对齐。"""
    shown = sum(2 if ord(char) > 127 else 1 for char in text)
    return text + " " * max(0, width - shown)


def show(value):
    """一个数写成报告里的文字：None 是"空"。"""
    return "空" if value is None else str(value)


def parse_time(text):
    """数据库、.json 里的时间文字 → datetime（"2026-11-21 20:30:59" 和 "2026-11-21T20:27:53" 都认）；没有、坏了返回 None。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def load_json(path):
    """读一个 .json 文件；没有、坏了返回 None。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Recording:
    """一段整局录像：MP4 和旁边的 .json 里读到的信息。"""
    video: Path
    started: datetime  # .json 的 started（这一局开始录的时刻），没有就用文件名里的时间
    ended: datetime | None  # .json 的 ended（录像存下的时刻）
    legend: str | None  # .json 的 match.legend（这一局用的传奇）
    duration: float | None  # .json 的 duration（秒）

    @property
    def name(self):
        """录像的文件名（不含 .mp4），报告里用它当名字。"""
        return self.video.stem


@dataclass
class Answer:
    """正式数据库里的一局：正确答案（当时在总结页、经验值页上读到的数）。"""
    match_id: int
    ended_at: datetime  # 总结页存下的时间
    legend: str | None
    placement: int | None
    kills: int | None
    assists: int | None
    damage: int | None
    rp_delta: int | None
    rp_source: str | None  # 经验值页 / 对局中 / 大厅校准 / 空
    needs_review: int
    review_note: str | None


@dataclass
class Video:
    """一段录像的画面：每一帧的时间戳（秒）和画面大小。"""
    path: Path
    times: list  # 每一帧的时间戳（秒，从 MP4 开头算起），只增不减
    width: int
    height: int

    @property
    def duration(self):
        """录像有多长（秒）：最后一帧的时间戳。"""
        return self.times[-1] if self.times else 0.0

    @property
    def frames(self):
        """录像一共几帧。"""
        return len(self.times)


# ---------- 正式数据、录像、对局 ----------

def data_folder(picked=None):
    """安装版的数据文件夹（--data-dir 指定的优先）。找不到返回 None。"""
    if picked:
        return Path(picked)
    return source_run.installed_data_dir()


def recordings_folder(data_dir, picked=None):
    """录像文件夹：--recordings-dir 优先，否则 settings.json 里的 recording_dir，没有就是 <数据文件夹>\\recordings。"""
    if picked:
        return Path(picked)
    settings = load_json(Path(data_dir) / "settings.json") or {}
    return Path(settings.get("recording_dir") or Path(data_dir) / "recordings")


def read_answers(db_path):
    """正式数据库里的全部对局（只读打开，不写），按 ended_at 排好。"""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT match_id, ended_at, legend, placement, kills, assists, damage, rp_delta, rp_source, "
            "needs_review, review_note FROM matches").fetchall()
    finally:
        conn.close()
    answers = []
    for row in rows:
        ended = parse_time(row["ended_at"])
        if ended is None:  # 没有结束时间的局没法按时间对上，跳过
            continue
        answers.append(Answer(row["match_id"], ended, row["legend"], row["placement"], row["kills"], row["assists"],
                              row["damage"], row["rp_delta"], row["rp_source"], row["needs_review"],
                              row["review_note"]))
    answers.sort(key=lambda answer: answer.ended_at)
    return answers


def started_from_name(video):
    """文件名里的开始时间（"2026-11-21_202753_班加罗尔.mp4" → 2026-11-21 20:27:53）；读不出来用文件的修改时间。"""
    try:
        return datetime.strptime(video.stem[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(video.stat().st_mtime)


def find_recordings(folder):
    """录像文件夹里的整局录像（按开始时间排好）。返回 (录像列表, 跳过的 [(文件, 原因)])。
    认 <文件夹>\\年-月\\*.mp4 和 <文件夹>\\*.mp4 两种放法。跳过高光片段（.json 里有 "highlight"）、
    正在录的 _recording\\、上次崩了没拼完的 "_未完成"、旁边没有 .json 的。"""
    found, skipped = [], []
    folder = Path(folder)
    if not folder.is_dir():
        return found, skipped
    videos = list(folder.glob("*.mp4")) + list(folder.glob("*/*.mp4"))  # 传录像根文件夹、某个月份文件夹都行
    for video in sorted(videos):
        if video.parent.name == WORK_DIR_NAME:
            skipped.append((video, "正在录的临时文件夹 _recording 里的分段"))
            continue
        if "_未完成" in video.stem:
            skipped.append((video, "上次崩了没拼完的录像"))
            continue
        meta = load_json(video.with_suffix(".json"))
        if meta is None:
            skipped.append((video, "旁边没有能读的 .json（不知道什么时候开始、是哪一局）"))
            continue
        if meta.get("highlight"):
            skipped.append((video, "自动剪的高光片段，不是整局"))
            continue
        started = parse_time(meta.get("started")) or started_from_name(video)
        legend = (meta.get("match") or {}).get("legend")
        duration = meta.get("duration")
        found.append(Recording(video, started, parse_time(meta.get("ended")), legend,
                               float(duration) if isinstance(duration, (int, float)) else None))
    found.sort(key=lambda recording: (recording.started, recording.name))
    return found, skipped


def _window(recording, next_recording, late_minutes=LATE_MINUTES):
    """这段录像能对上哪一段时间里的局：(开始, 结束, 结束这边包不包含)。
    结束 = 下一段录像的 started（"之前"，不含）；最后一段没有下一段，用"这段录像的 ended 之后 30 分钟"做上限。"""
    if next_recording is not None:
        return recording.started, next_recording.started, False
    if recording.ended is not None:
        return recording.started, recording.ended + timedelta(minutes=late_minutes), True
    return recording.started, None, True


def match_recordings(recordings, answers, late_minutes=LATE_MINUTES):
    """每段录像对上数据库里的哪一局：[(录像, 局 或 None, 原因或 None)]。
    取 ended_at 在"这段录像的 started 之后、下一段录像的 started 之前"的第一局；最后一段没有下一段做上限，
    用"这段录像的 ended 之后 30 分钟"做上限。传奇对不上（.json 里有传奇时）算没对上。"""
    out, times = [], [answer.ended_at for answer in answers]
    for i, recording in enumerate(recordings):
        next_recording = recordings[i + 1] if i + 1 < len(recordings) else None
        lo, hi, closed = _window(recording, next_recording, late_minutes)
        index = bisect.bisect_right(times, lo)  # 第一个 ended_at 比录像的 started 晚的（"之后" = 不含）
        found = None
        if index < len(answers):
            answer = answers[index]
            if hi is None or answer.ended_at < hi or (closed and answer.ended_at == hi):
                found = answer
        if found is None:
            # 写清楚为什么没对上：这段时间里没有局，还是最近的那一局太晚了
            hint = ""
            if index < len(answers):
                near = answers[index]
                parts = []
                if next_recording is not None and near.ended_at >= next_recording.started:
                    parts.append(f"晚于下一段录像的开始 {next_recording.started:%Y-%m-%d %H:%M:%S}")
                elif next_recording is None and recording.ended is not None:
                    minutes = (near.ended_at - recording.ended).total_seconds() / 60
                    parts.append(f"比这段录像的 ended 晚 {minutes:.0f} 分钟，超过 {late_minutes} 分钟")
                hint = (f"（最近的一局是第 {near.match_id} 局，ended_at {near.ended_at:%Y-%m-%d %H:%M:%S}，"
                        f"{'，'.join(parts) or '对不上'}）")
            end = "（.json 里没有 ended，没有上限）" if recording.ended is None else f"、{recording.ended:%H:%M:%S} 结束"
            out.append((recording, None, f"这段时间里没有一局（录像从 {recording.started:%Y-%m-%d %H:%M:%S} 开始{end}）{hint}"))
            continue
        legend = (recording.legend or "").strip()
        if legend and legend != (found.legend or "").strip():
            out.append((recording, None,
                        f"传奇对不上：录像里是 {legend}，数据库里第 {found.match_id} 局是 {found.legend or '空'}"))
            continue
        out.append((recording, found, None))
    return out


# ---------- 录像的帧 ----------

def read_video(path):
    """读一段录像每一帧的时间戳（秒）和画面大小；读不了（打不开、一帧都没有）返回 None。
    只 grab、不转成图，所以一帧约 0.4 毫秒（1920×1080），比每帧都读（约 2 毫秒）快。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        times = []
        while cap.grab():
            times.append(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0)
        fps = cap.get(cv2.CAP_PROP_FPS)
    finally:
        cap.release()
    if not times or width <= 0 or height <= 0:
        return None
    if times[-1] <= 0 and fps > 0:  # 有的文件不报时间戳：按帧率估
        times = [i / fps for i in range(len(times))]
    for i in range(1, len(times)):  # 时间戳有抖动时不让它倒退（后面要按顺序找帧）
        times[i] = max(times[i], times[i - 1])
    return Video(Path(path), times, width, height)


class ReplayCamera:
    """假摄像头：假时钟走到的时刻 → 录像里时间戳 ≤ 它的最后一帧；录像放完以后给和录像一样大的黑图。
    黑图这一段里，存下了（saved() 大于 0）或者喂满了 tail_seconds 秒（假时间）就 set(stop)，
    记录程序这一轮就收尾（收尾时会把还没存的那一局存下）。
    classify 给了就是"藏总结页 / 经验值页"：交出某一帧之前先问 classify(frame)，它返回 (名字, 中文名) 就说明
    这一帧是这两页——这一帧和以后都换成黑图，并记下从录像第几秒起（hidden_at）。"""

    def __init__(self, video, clock, stop, saved=lambda: 0, tail_seconds=TAIL_SECONDS, classify=None):
        """video：read_video 读出来的录像；clock：假时钟（有 monotonic()）；stop：threading.Event；
        saved()：这次重放已经存下几局（看 collect 的 status）；tail_seconds：录像放完以后最多再喂多久黑图；
        classify：认总结页 / 经验值页（frame → (名字, 中文名) 或 None），None = 不藏。"""
        self.video, self.clock, self.stop, self.saved, self.classify = video, clock, stop, saved, classify
        self.tail_seconds = tail_seconds
        self.cap = cv2.VideoCapture(str(video.path))
        if not self.cap.isOpened():
            raise OSError(f"读不了录像：{video.path}")
        self.width, self.height = video.width, video.height
        self.black = np.zeros((video.height, video.width, 3), np.uint8)
        self.start = None  # 假时钟开始那一刻（第一次 grab 的时间）
        self.index = 0  # 下一帧从录像里读第几帧
        self.last = None  # 最近交出去的那张录像画面
        self.over_at = None  # 录像放完时假时钟走了多少秒
        self.current = None  # 这次交出去的画面是录像里第几秒（FakeClock 记等待时用）
        self.hidden_at = None  # 从录像第几秒起换成黑图（没藏是 None）
        self.hidden_name = None  # 认出的画面（config 里的名字，如 summary）
        self.hidden_label = None  # 认出的画面（中文名，如 总结页）
        self.classify_error = None  # 认画面出错时的 traceback（出错就不再认了，照样喂）

    def at(self, seconds):
        """录像里 seconds 秒那一刻的画面（时间戳 ≤ seconds 的最后一帧）；放完了（超过最后一帧）返回 None。"""
        times = self.video.times
        if seconds > times[-1]:
            return None
        target = max(0, bisect.bisect_right(times, seconds) - 1)
        while self.index <= target:
            if not self.cap.grab():
                return None
            if self.index == target:  # 这一帧是要的：转成图；前面的只 grab，不转换
                ok, frame = self.cap.retrieve()
                self.index += 1
                if ok:
                    self.last = frame
                return self.last
            self.index += 1
        return self.last

    def _page_of(self, frame):
        """这一帧是不是总结页 / 经验值页：(名字, 中文名) 或 None。认的时候出错就记下、以后不再认。"""
        if self.classify is None:
            return None
        try:
            return self.classify(frame)
        except Exception:  # noqa: BLE001 认画面出错不能耽误重放：当作不是，以后也不再认
            self.classify_error = traceback.format_exc()
            self.classify = None
            return None

    def grab(self, new_frame_only=False):
        """记录程序每截一张调一次：按假时钟现在走到哪一秒，给出录像里对应的那一帧（放完以后是黑图）。
        藏总结页 / 经验值页时，认出第一帧以后都给黑图。"""
        now = self.clock.monotonic()
        if self.start is None:
            self.start = now
        t = max(0.0, now - self.start)
        self.current = t
        if self.over_at is None:
            frame = self.at(t)
            if frame is None:
                self.over_at = t  # 录像放完了：以后都是黑图
            elif self.hidden_at is None:
                found = self._page_of(frame)
                if found is None:
                    return frame
                self.hidden_name, self.hidden_label = found
                self.hidden_at = t  # 这一帧和以后都换成黑图（玩家没看这一页）
        if self.over_at is not None and (t - self.over_at >= self.tail_seconds or self.saved() > 0):
            self.stop.set()
        return self.black

    def release(self):
        """放开录像文件（记录程序收尾时调）。"""
        self.cap.release()


# ---------- 藏总结页 / 经验值页 ----------

_page_catcher = None  # 整个进程共用一个：参照图和识别模型只装一次


class PageCatcher:
    """用 Evo 自己认画面的代码（code/screens.py 的 GameScreens）看一帧是不是总结页 / 经验值页。
    只比 config/screens.json 里的 summary、xp 两个画面（关键字、检查区域都照 config 里的），画面比例用 collect
    现在用的那个（layout 里的，16:10 压成 16:9 时界面位置才对），存下来、认完恢复，不影响 collect。"""

    def __init__(self):
        import layout  # 用到时才 import：这些程序代码要看临时数据文件夹，必须在 fake_game 之后
        import live_collect
        import ocr
        import screens
        self.layout = layout
        self.engine = ocr.LazyEngine(ocr.SMALL_CROP_PARAMS, live_collect.MODEL_IDLE_SECONDS,
                                     clock=time.monotonic, log=lambda text: None)
        self.detector = screens.GameScreens(self.engine, log=lambda text: None)

    def __call__(self, frame):
        """这一帧是总结页 / 经验值页 → (config 里的名字, 中文名)；都不是返回 None。"""
        before = self.layout.game_aspect()
        try:
            for detector in self.detector.detectors.values():
                for name in PAGES:
                    spec = detector.cfg["screens"].get(name)
                    if spec is not None and detector.check(frame, name):
                        return name, spec.get("label", name)
        finally:
            self.layout.set_game_aspect(before)
            self.engine.release_if_idle()  # 识别模型闲置久了就还回去（和记录程序里一样）
        return None


def page_catcher():
    """整个进程共用一个 PageCatcher（装一次参照图、识别模型；第一段藏的时候才建）。"""
    global _page_catcher
    if _page_catcher is None:
        _page_catcher = PageCatcher()
    return _page_catcher


# ---------- 重放里读到了哪些来源（有没有藏住） ----------

def read_sources(conn, match_id):
    """这一局读到了哪些来源（readings 表的 source 列，如"总结页""对局中"），按第一次读到的时间排。"""
    rows = conn.execute("SELECT source FROM readings WHERE match_id = ? GROUP BY source ORDER BY MIN(read_at)",
                        (match_id,)).fetchall()
    return [row[0] for row in rows]


def page_sources():
    """哪两个来源算"读了总结页 / 经验值页"（名字照 post_match.py 的常量）。"""
    import post_match  # 用到时才 import：它 import paths，要在临时数据文件夹设好以后（即 fake_game 之后）
    return post_match.SOURCE_SUMMARY, post_match.SOURCE_XP


def leaked_sources(sources):
    """这些来源里，哪些是总结页 / 经验值页（报告里标"不算跳过"用）。"""
    pages = page_sources()
    return [source for source in sources if source in pages]


def result_sources(result):
    """这次重放读到的全部来源（几局合起来，一个来源只算一次，按第一次出现的顺序）。"""
    found = []
    for sources in (result.get("sources") or {}).values():
        for source in sources:
            if source not in found:
                found.append(source)
    return found


def sources_text(sources):
    """来源列表写成报告里的一行。"""
    return "、".join(sources) if sources else "（没有）"


def hide_text(result):
    """报告里"藏："那一行：有没有藏、从录像第几秒起换成黑图、认出了哪个画面。"""
    if result.get("keep_summary"):
        return "原样喂（--keep-summary，没藏）"
    if result.get("classify_error"):
        return f"认画面出错了，没藏（{result['classify_error'].strip().splitlines()[-1]}）"
    at, total = result.get("hidden_at"), result["video"].duration
    if at is None:
        return f"没有藏（录像里没认出总结页 / 经验值页；录像共 {total:.1f} 秒）"
    text = (f"从录像第 {at:.1f} 秒起换成黑图（录像共 {total:.1f} 秒，最后 {total - at:.1f} 秒），"
            f"认出了 {result.get('hidden_label') or '总结页 / 经验值页'}（{result.get('hidden_name')}）")
    if total - at > HIDE_FAR_SECONDS:
        text += f"；⚠ 离结尾还有 {total - at:.1f} 秒，可能认错了"
    return text


# ---------- 重放一段录像 ----------

def aspect_from_log(lines):
    """collect 日志里"游戏画面比例：…"那一行 → 实际用的画面比例（最后一次说的）；没有就写没写。"""
    found = ""
    for line in lines:
        match = re.match(r"游戏画面比例：(.*)", line)
        if match:
            found = match.group(1)
    return found or "日志里没写"


def replay_recording(video, db_path=None, aspect=None, tail_seconds=TAIL_SECONDS, hide_pages=True, classify=None):
    """把一段录像喂给真正的记录程序（live_collect.collect），返回这次的结果（字典）：
    video（Video）、matches（临时数据库里的每一局）、sources（每局读到了哪些来源）、log（collect 的日志每一行）、
    seconds（重放用了多久，真时间）、aspect（实际用的画面比例）、hidden_at / hidden_name / hidden_label（藏的时刻和认出的画面）、
    keep_summary（是不是 --keep-summary 原样喂）、classify_error（认画面出错时的 traceback）、error（重放出错）。
    每段录像用一个新数据库；录像放完以后喂黑图，最多 tail_seconds 秒（假时间）。
    hide_pages：藏总结页 / 经验值页（从第一帧认出起换成黑图）；False = 原样喂（--keep-summary）。
    classify：认画面的函数（frame → (名字, 中文名) 或 None）；不给时用 Evo 自己的认画面代码（PageCatcher），
    测试时可以换成假的。"""
    tests_dir = Path(__file__).resolve().parent.parent / "tests"  # fake_game.py 在这里（开发工具可以 import 测试）
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    import fake_game  # 这个一 import 就把数据文件夹换成临时文件夹：必须在读过正式数据以后才能进来
    import dxcam
    import live_collect
    import storage

    db = Path(db_path) if db_path else fake_game._TEMP / f"replay{next(_runs)}.db"
    log_lines, status = [], {}
    stop = threading.Event()
    clock = fake_game.FakeClock(None)
    result = {"video": video, "matches": [], "sources": {}, "log": log_lines, "seconds": 0.0, "aspect": "",
              "error": None, "keep_summary": not hide_pages, "hidden_at": None, "hidden_name": None,
              "hidden_label": None, "classify_error": None}
    if classify is None and hide_pages:
        try:
            classify = page_catcher()
        except Exception:  # noqa: BLE001 认画面的代码起不来也不能不跑：记下来，当作没藏
            result["classify_error"] = traceback.format_exc()
    camera = None
    began = time.perf_counter()
    try:
        camera = ReplayCamera(video, clock, stop, saved=lambda: status.get("saved", 0), tail_seconds=tail_seconds,
                              classify=classify)
        clock.camera = camera  # FakeClock.sleep 记每次等了多久时要看 camera.current
        # 和 tests/fake_game.py 的 run_collect 一样：dxcam.create 换成假摄像头，live_collect 里的 time 换成假时钟
        with mock.patch.object(dxcam, "create", lambda **kw: camera), \
                mock.patch.object(live_collect, "time", clock):
            live_collect.collect(db_path=db, stop=stop, log=log_lines.append, game_aspect=aspect,
                                 status=status, keep_all=False)
    except Exception:  # noqa: BLE001 一段录像出错不能影响后面的：记下来，报告里写出来
        result["error"] = traceback.format_exc()
        log_lines.append(result["error"])
    finally:
        if camera is not None:
            camera.release()
            result["hidden_at"] = camera.hidden_at
            result["hidden_name"] = camera.hidden_name
            result["hidden_label"] = camera.hidden_label
            result["classify_error"] = result["classify_error"] or camera.classify_error
        result["seconds"] = time.perf_counter() - began
    if db.is_file():
        conn = storage.connect(db)
        try:
            result["matches"] = [dict(row) for row in conn.execute("SELECT * FROM matches ORDER BY match_id")]
            result["sources"] = {row["match_id"]: read_sources(conn, row["match_id"]) for row in result["matches"]}
        finally:
            conn.close()
    result["aspect"] = aspect_from_log(log_lines)
    return result


# ---------- 判定 ----------

def answer_text(answer, field):
    """正确答案写成文字。RP 的来源不是经验值页时说明它不算数（规则里只有经验值页的 RP 才当答案）。"""
    value = getattr(answer, field)
    if field != "rp_delta":
        return show(value)
    if value is None:
        return "空"
    if (answer.rp_source or "").strip() != "经验值页":
        return f"{value}（来源 {answer.rp_source or '空'}，不算数）"
    if answer.placement == 1:
        return f"{value}（冠军局）"
    return str(value)


def judge(field, want, got, want_placement=None, want_rp_source=None):
    """一项的判定：一样 / 少一点 / 错 / 没读出 / 没有答案（规则见文件开头）。"""
    if field == "rp_delta" and (want_rp_source or "").strip() != "经验值页":
        return "没有答案"
    if want is None:
        return "没有答案"
    if got is None:
        if field == "rp_delta" and want_placement == 1:
            return "没读出（冠军局规则上不用右上角）"
        return "没读出"
    if got == want:
        return "一样"
    if field in LOWER_OK and got < want:
        if field == "damage" and got * 2 < want:
            return "错"  # 少了一半以上：不是最后一秒没截到，多半是读错了（如 1118 读成 11）
        return "少一点"
    return "错"


def bucket(verdict):
    """判定 → 汇总表里的那一列（"没读出（冠军局…）"算"没读出"）。"""
    for name in VERDICTS:
        if verdict.startswith(name):
            return name
    return "错"


def result_line(result):
    """报告里"重放："后面那一段：记下 1 局 / 分成了 N 局 / 没存下，加上用了几秒、画面比例。"""
    seconds = f"用了 {result['seconds']:.1f} 秒；画面比例 {result['aspect']}"
    count = len(result["matches"])
    if count == 0:
        return f"没存下（只靠这段录像一局都没记下），{seconds}"
    if count == 1:
        return f"记下 1 局，{seconds}"
    return f"分成了 {count} 局，{seconds}"


def match_line(row, sources=None):
    """重放的一局写成一行：名次 17，击杀 0，助攻 0，伤害 43，RP -48；待确认 否；备注 无；RP 来源 对局中；
    读到的来源 总结页、对局中（sources 没给就不写最后那一段）。"""
    values = "，".join(f"{label} {show(row.get(field))}" for field, label in FIELDS)
    text = (f"{values}；待确认 {'是' if row.get('needs_review') else '否'}；"
            f"备注 {row.get('review_note') or '无'}；RP 来源 {row.get('rp_source') or '空'}")
    if sources is not None:
        text += f"；读到的来源 {sources_text(sources)}"
    return text


def block_lines(answer, result, verdicts):
    """一段录像的对照：报告里的那几行。verdicts：{项: 判定}，存下的不是正好一局时是 None。"""
    video, rows = result["video"], result["matches"]
    lines = [f"{video.path.stem}（录像 {video.duration:.1f} 秒，{video.width}×{video.height}）",
             f"  对上第 {answer.match_id} 局（ended_at {answer.ended_at:%Y-%m-%d %H:%M:%S}，"
             f"传奇 {answer.legend or '空'}）",
             f"  重放：{result_line(result)}",
             f"  藏：{hide_text(result)}"]
    if result.get("error"):
        lines.append(f"  ⚠ 重放出错：{result['error'].strip().splitlines()[-1]}（详细看 .log）")
    if verdicts is not None:
        row = rows[0]
        for field, label in FIELDS:
            lines.append(f"  {label}：正确答案 {answer_text(answer, field)}；重放 {show(row.get(field))}；"
                         f"{verdicts[field]}")
        lines.append(f"  重放这一局：待确认 {'是' if row.get('needs_review') else '否'}；"
                     f"备注 {row.get('review_note') or '无'}；RP 来源 {row.get('rp_source') or '空'}")
    elif len(rows) > 1:
        for i, row in enumerate(rows, 1):
            sources = (result.get("sources") or {}).get(row.get("match_id"), [])
            lines.append(f"    第 {i} 局（编号 {row.get('match_id')}）：{match_line(row, sources)}")
    else:
        lines.append("  只靠这段录像一局都没记下（停止记录时也没有可以存的数据）")
    sources = result_sources(result)
    lines.append(f"  重放读到的来源：{sources_text(sources)}")
    if leaked_sources(sources) and not result.get("keep_summary"):
        lines.append("  ⚠ 重放里还是读到了总结页 / 经验值页，不算跳过（没算进汇总）")
    return lines


def summary_lines(done, keep_summary=False):
    """最后的汇总表：每项"一样、少一点、错、没读出、没有答案"各几局。
    读了总结页 / 经验值页的局不算进汇总（说明没藏住，不能当"跳过总结页"的结论）；--keep-summary 时都算。"""
    ran = [item for item in done if item["result"] is not None]
    single = [item for item in ran if item["verdicts"] is not None]
    leaked = [item for item in single if leaked_sources(result_sources(item["result"]))]
    leaked_ids = {id(item) for item in leaked}
    rows = [item for item in single if keep_summary or id(item) not in leaked_ids]
    counts = {label: {verdict: 0 for verdict in VERDICTS} for _, label in FIELDS}
    for item in rows:
        for field, label in FIELDS:
            counts[label][bucket(item["verdicts"][field])] += 1
    counted = "都算进汇总" if keep_summary else f"{len(rows)} 段算进汇总"
    lines = [f"  跑了 {len(ran)} 段：存下正好一局 {len(single)} 段（{counted}），没存下 "
             f"{sum(1 for item in ran if not item['result']['matches'])} 段，分成多局 "
             f"{sum(1 for item in ran if len(item['result']['matches']) > 1)} 段"
             + (f"，没跑 {len(done) - len(ran)} 段" if len(done) != len(ran) else ""),
             "  " + pad("项", 8) + "".join(pad(verdict, 8) for verdict in VERDICTS)]
    for _, label in FIELDS:
        lines.append("  " + pad(label, 8) + "".join(pad(str(counts[label][verdict]), 8) for verdict in VERDICTS))
    if leaked:
        if keep_summary:
            lines.append(f"  （--keep-summary：{len(leaked)} 段读到了总结页 / 经验值页，也算进汇总了）")
        else:
            lines.append(f"  {len(leaked)} 段读到了总结页 / 经验值页，没算进汇总")
    return lines


# ---------- 主程序 ----------

class Report:
    """屏幕和 report.txt 上一样的内容：每加一行就打印（跑得久时看得到进度），最后整段存成文件。"""

    def __init__(self):
        """空的报告：每说一句就往 lines 里加一行。"""
        self.lines = []

    def say(self, text=""):
        """打印一行，同时记进报告（最后一起存成 report.txt）。"""
        print(text, flush=True)
        self.lines.append(text)

    def text(self):
        """报告的全部文字（写文件用）。"""
        return "\n".join(self.lines) + "\n"


def new_out_dir():
    """报告文件夹 captures/recording_replay/<年月日_时分秒>（已经有了就加 _2、_3，不覆盖已有的）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = CAPTURES / "recording_replay" / stamp
    tried = 1
    while path.exists():
        tried += 1
        path = CAPTURES / "recording_replay" / f"{stamp}_{tried}"
    path.mkdir(parents=True)
    return path


def parse_args(argv=None):
    """命令行参数：数据文件夹、录像文件夹、--only（可以写几次）、--aspect、--keep-summary。"""
    parser = argparse.ArgumentParser(description="把整局录像喂给记录程序，看只靠录像记下的五项对不对")
    parser.add_argument("--data-dir", type=Path, default=None, help="安装版的数据文件夹（默认自动找）")
    parser.add_argument("--recordings-dir", type=Path, default=None,
                        help="录像文件夹（放年-月子文件夹的那一层，也可以直接指月份文件夹；默认看 settings.json 的 recording_dir）")
    parser.add_argument("--only", action="append", metavar="文字", default=None,
                        help="只跑名字里含这段文字的录像（可以写几次）")
    parser.add_argument("--keep-summary", action="store_true",
                        help="不藏总结页 / 经验值页，原样喂（用来对照：Evo 能看到这两页时结果应该基本和数据库一样）")
    parser.add_argument("--aspect", choices=("16:9", "16:10", "4:3"), default=None,
                        help="手动指定游戏画面比例（默认不指定，collect 自己看画面认）")
    return parser.parse_args(argv)


def main(argv=None):
    """命令行入口：找正式数据和录像 → 对上数据库里的局 → 一段一段重放 → 打印对照、汇总，存报告和日志。"""
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    report = Report()
    report.say("Evo 录像重放：把每段整局录像当成屏幕画面喂给记录程序，看只靠录像记下的五项对不对")
    data = data_folder(args.data_dir)
    if data is None or not Path(data).is_dir():
        report.say("找不到安装版的数据文件夹。请用 --data-dir 指定（如 --data-dir \"E:\\Evo Tracker Data\"）。")
        return 2
    data = Path(data)
    db_path = data / "data" / "evo.db"
    if not db_path.is_file():
        report.say(f"找不到正式数据库：{db_path}（请用 --data-dir 指定数据文件夹）")
        return 2
    folder = recordings_folder(data, args.recordings_dir)
    report.say(f"正式数据文件夹：{data}")
    report.say(f"正式数据库：{db_path}（只读打开，不写）")
    report.say(f"录像文件夹：{folder}")
    report.say("画面比例：" + (f"手动指定 {args.aspect}（原样交给 collect）" if args.aspect
                             else "不指定（collect 自己看画面认）"))
    report.say("总结页 / 经验值页：" + ("原样喂（--keep-summary：Evo 能看到这两页，用来对照）" if args.keep_summary
                                    else "藏起来（从第一次认出起换成黑图，模拟玩家跳过这两页）"))

    answers = read_answers(db_path)
    recordings, skipped = find_recordings(folder)
    reasons = {}
    for _, why in skipped:
        reasons[why] = reasons.get(why, 0) + 1
    report.say(f"找到 {len(recordings)} 段整局录像，按开始时间排好"
               + (f"；跳过 {len(skipped)} 段："
                  + "、".join(f"{count} 段{why}" for why, count in reasons.items()) if skipped else ""))
    for video, why in skipped:  # 高光片段按设计跳过，只把别的（没有 .json 等）逐个列出来
        if "高光片段" not in why:
            report.say(f"  跳过 {video.name}：{why}")
    if not recordings:
        report.say("这个文件夹里没有整局录像：还没录过，或者路径不对。")
        return 2

    planned = match_recordings(recordings, answers)
    only = args.only or []
    chosen = [item for item in planned if not only or any(word in item[0].name for word in only)]
    if only:
        report.say(f"--only 选了 {len(chosen)} 段（共 {len(recordings)} 段）："
                   + ("、".join(item[0].name for item in chosen) or "（一段都没选上）"))
    if not chosen:
        report.say("没有要跑的录像。")
        return 2
    out_dir = new_out_dir()
    report.say()

    done = []
    for number, (recording, answer, reason) in enumerate(chosen, 1):
        if answer is None:
            report.say(f"[{number}/{len(chosen)}] {recording.name}：没对上，跳过")
            report.say(f"  原因：{reason}")
            report.say()
            done.append({"recording": recording, "answer": None, "result": None, "verdicts": None, "reason": reason})
            continue
        length = f"（.json 里记的 {recording.duration:.1f} 秒）" if recording.duration else ""
        print(f"[{number}/{len(chosen)}] 重放 {recording.name}{length}…", flush=True)
        video = read_video(recording.video)
        if video is None:
            report.say(f"[{number}/{len(chosen)}] {recording.name}：读不了这段录像（打不开或者一帧都没有），没跑")
            report.say()
            done.append({"recording": recording, "answer": answer, "result": None, "verdicts": None,
                         "reason": "读不了这段录像"})
            continue
        try:
            result = replay_recording(video, aspect=args.aspect, hide_pages=not args.keep_summary)
        except Exception:  # noqa: BLE001 一段录像出错不能影响后面的：记下来，报告里写出来
            result = {"video": video, "matches": [], "sources": {}, "log": [traceback.format_exc()], "seconds": 0.0,
                      "aspect": "日志里没写", "error": traceback.format_exc(), "keep_summary": args.keep_summary,
                      "hidden_at": None, "hidden_name": None, "hidden_label": None, "classify_error": None}
        (out_dir / f"{recording.name}.log").write_text("\n".join(result["log"]) + "\n", encoding="utf-8")
        verdicts = None
        if len(result["matches"]) == 1:
            row = result["matches"][0]
            verdicts = {field: judge(field, getattr(answer, field), row.get(field), answer.placement, answer.rp_source)
                        for field, _ in FIELDS}
        for line in block_lines(answer, result, verdicts):
            report.say(line)
        report.say()
        done.append({"recording": recording, "answer": answer, "result": result, "verdicts": verdicts, "reason": None})

    report.say("===== 汇总 =====")
    for line in summary_lines(done, keep_summary=args.keep_summary):
        report.say(line)
    report.say()
    report.say("===== 没对上的、没跑的录像 =====")
    not_run = [item for item in done if item["result"] is None]
    for item in not_run:
        label = "没对上" if item["answer"] is None else "没跑"
        report.say(f"  - {item['recording'].name}：{label}——{item['reason']}")
    if not not_run:
        report.say("  （都对上了）")
    report.say()
    report.say("===== 每段用了多久 =====")
    for item in done:
        if item["result"] is None:
            report.say(f"  - {item['recording'].name}：没跑（{item['reason']}）")
        else:
            report.say(f"  - {item['recording'].name}：录像 {item['result']['video'].duration:.1f} 秒，"
                       f"重放 {item['result']['seconds']:.1f} 秒")

    path = out_dir / "report.txt"
    path.write_text(report.text(), encoding="utf-8")
    print(f"\n报告：{path}")
    print(f"每段录像的日志：{out_dir}\\<录像名>.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
