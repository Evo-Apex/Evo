"""录像重放工具（tools/replay_recordings.py）的测试：找录像、对上数据库、按时间取帧、藏总结页 / 经验值页、查读到的来源、每项判定，再用小视频走一遍真正的 collect()。

不用真录像、不用正式数据库：临时文件夹里现造假录像、假 .json、假数据库，在别人电脑上也能跑。
运行：python tests/test_replay_recordings.py
"""

import atexit
import json
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import fake_game  # 要在项目代码之前导入（fake_game 一 import 就把数据文件夹换成临时文件夹）
from fake_game import report

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))  # 开发工具在 tools/ 里
import replay_recordings as rr  # noqa: E402
import storage  # noqa: E402

TEMP = Path(tempfile.mkdtemp(prefix="evo-replay-test-"))  # 这次测试的临时文件夹，结束自动删掉
atexit.register(shutil.rmtree, TEMP, ignore_errors=True)


def add_recording(folder, name, started, ended, legend=None, highlight=False):
    """在 folder 里放一段假录像：.mp4 里随便写点东西就够了（找录像只看旁边的 .json），旁边写 .json。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{name}.mp4"
    video.write_bytes(b"0")
    meta = {"version": 1, "video": video.name, "started": started, "ended": ended}
    if legend:
        meta["match"] = {"legend": legend}
    if highlight:  # 自动剪的高光片段：旁边 .json 里有 "highlight" 键
        meta["highlight"] = {"of": f"{name}.mp4", "name": "击杀1", "kind": "kill"}
    video.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return video


def recording(name, started, ended, legend=None):
    """直接造一段录像（不用文件夹）：name 是文件名（不带 .mp4）。"""
    return rr.Recording(Path(f"{name}.mp4"), datetime.fromisoformat(started), datetime.fromisoformat(ended), legend, None)


def make_gray_video(path, values, width=96, height=64, fps=10):
    """按 values 里每一帧的灰度现做一段小视频。写不了返回 None。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        return None
    for value in values:
        writer.write(np.full((height, width, 3), value, np.uint8))
    writer.release()
    return Path(path)


def make_video(path, width=96, height=64, fps=10, count=30, gray=None):
    """现做一段小视频：默认每一帧涂成不同的灰度（第 i 帧是 (i+1)*8），能认出是第几帧；
    gray 给了就每一帧都涂这个灰度（0 = 全黑）。写不了返回 None。"""
    values = [gray if gray is not None else (i + 1) * 8 for i in range(count)]
    return make_gray_video(path, values, width, height, fps)


def decoded_means(path):
    """把这一段小视频逐帧读出来，每一帧的平均灰度（第 i 帧涂的是 (i+1)*8，压缩后会偏一点，所以记下来比对）。"""
    cap = cv2.VideoCapture(str(path))
    means = []
    while cap.grab():
        ok, frame = cap.retrieve()
        if ok:
            means.append(float(frame.mean()))
    cap.release()
    return means


def frame_index(frame, means):
    """这一帧最像第几帧（和逐帧读出来的平均灰度比）。"""
    got = float(frame.mean())
    return min(range(len(means)), key=lambda i: abs(means[i] - got))


def find_checks():
    """找录像：只找整局的，跳过高光片段和 _recording，按开始时间排好。"""
    folder = TEMP / "recordings"
    add_recording(folder, "2026-11-21_090000_恶灵", "2026-11-21T09:00:00", "2026-11-21T09:05:00", "恶灵")  # 直接放在根文件夹
    add_recording(folder / "2026-11", "2026-11-21_101500_恶灵", "2026-11-21T10:15:00", "2026-11-21T10:20:00", "恶灵")
    add_recording(folder / "2026-11", "2026-11-21_100000_恶灵", "2026-11-21T10:00:00", "2026-11-21T10:05:00", "恶灵")
    add_recording(folder / "2026-11", "2026-11-21_100000_恶灵_击杀1", "2026-11-21T10:00:00",
                  "2026-11-21T10:05:00", "恶灵", highlight=True)
    (folder / "_recording" / "20261121_110000").mkdir(parents=True)  # 正在录的临时文件夹
    (folder / "_recording" / "20261121_110000" / "part_00000.ts").write_bytes(b"0")
    (folder / "_recording" / "part_00000.mp4").write_bytes(b"0")  # 临时文件夹里的 .mp4 也不当成整局
    (folder / "2026-11" / "2026-11-21_120000_恶灵.mp4").write_bytes(b"0")  # 旁边没有 .json
    (folder / "2026-11" / "2026-11-21_130000_恶灵_未完成.mp4").write_bytes(b"0")  # 上次崩了没拼完的
    add_recording(folder / "2026-12", "2026-12-01_090000_班加罗尔", "2026-12-01T09:00:00", "2026-12-01T09:05:00", "班加罗尔")
    found, skipped = rr.find_recordings(folder)
    names = [item.name for item in found]
    skipped_names = {video.name for video, _ in skipped}
    reasons = "；".join(f"{video.name}：{why}" for video, why in skipped)
    return {
        f"只找出整局录像（根文件夹、年-月子文件夹都认），按开始时间排好（{names}）":
            names == ["2026-11-21_090000_恶灵", "2026-11-21_100000_恶灵", "2026-11-21_101500_恶灵",
                      "2026-12-01_090000_班加罗尔"],
        f"高光片段、_recording 里的、没有 .json 的、没拼完的都跳过了，并写了原因（{reasons}）":
            skipped_names == {"2026-11-21_100000_恶灵_击杀1.mp4", "2026-11-21_120000_恶灵.mp4", "part_00000.mp4",
                             "2026-11-21_130000_恶灵_未完成.mp4"}
            and not any("part_" in name or "未完成" in name for name in names),
        "旁边的 .json 读到了开始时间、传奇":
            any(item.legend == "恶灵" and item.started == datetime(2026, 11, 21, 10, 0) for item in found),
    }


def match_checks():
    """对上数据库里的局：取第一局；找不到局、传奇不一样、晚了 30 分钟以上都报出来。"""
    db = TEMP / "answers.db"
    conn = storage.connect(db)
    # 故意不按时间顺序插入：第 1 局 10:06 和第 5 局 10:07 都在 A 的时间里，应该取最早的 10:06
    for ended, legend in (("2026-11-21 10:06:00", "恶灵"), ("2026-11-21 10:17:00", "探路者"),
                          ("2026-11-21 10:35:00", "恶灵"), ("2026-11-21 11:40:00", "恶灵"),
                          ("2026-11-21 10:07:00", "恶灵")):
        storage.insert_match(conn, {"ended_at": ended, "legend": legend, "placement": 5, "kills": 0,
                                   "assists": 0, "damage": 100})
    conn.close()
    before = db.stat().st_mtime_ns
    answers = rr.read_answers(db)  # 只读打开
    unchanged = db.stat().st_mtime_ns == before
    got = rr.match_recordings([
        recording("A", "2026-11-21T10:00:00", "2026-11-21T10:05:00", "恶灵"),
        recording("B", "2026-11-21T10:10:00", "2026-11-21T10:16:00", "探路者"),
        recording("C", "2026-11-21T10:20:00", "2026-11-21T10:25:00", "恶灵"),
        recording("D", "2026-11-21T10:30:00", "2026-11-21T10:34:00", "班加罗尔"),
        recording("E", "2026-11-21T11:00:00", "2026-11-21T11:05:00", "恶灵"),
    ], answers)
    by_name = {item[0].name: item for item in got}
    first_a = by_name["A"][1].match_id if by_name["A"][1] else "空"
    first_b = by_name["B"][1].match_id if by_name["B"][1] else "空"
    return {
        f"这段时间里有几局：取 ended_at 最早的那一局（A 对上第 {first_a} 局，不是 10:07 的第 5 局）":
            by_name["A"][1] is not None and by_name["A"][1].match_id == 1,
        f"传奇一样就对上（B 对上第 {first_b} 局）":
            by_name["B"][1] is not None and by_name["B"][1].match_id == 2,
        f"C 这段时间里没有局：报出来、不跑（{by_name['C'][2]}）":
            by_name["C"][1] is None and "没有一局" in (by_name["C"][2] or ""),
        f"D 传奇不一样：报出来、不跑（{by_name['D'][2]}）":
            by_name["D"][1] is None and "传奇" in (by_name["D"][2] or ""),
        f"E 是最后一段：ended 之后 30 分钟里没有局，报出来（{by_name['E'][2]}）":
            by_name["E"][1] is None and "30 分钟" in (by_name["E"][2] or ""),
        f"读数据库不改文件（只读打开）": unchanged,
        f"读到的局按 ended_at 排好（{len(answers)} 局）":
            len(answers) == 5 and [answer.ended_at for answer in answers] == sorted(a.ended_at for a in answers),
    }


def frames_checks():
    """按时间取帧：假时钟走了 t 秒，给录像里时间戳 ≤ t 的最后一帧；放完以后告诉调用的人。"""
    path = make_video(TEMP / "grey.mp4")
    if path is None:
        return {"⏭ 跳过按时间取帧：这台电脑的 OpenCV 写不了 MP4": True}
    video = rr.read_video(path)
    means = decoded_means(path)
    if video is None or not means:
        return {"⏭ 跳过按时间取帧：读不出来刚做的小视频": True}
    check = {"读到每一帧的时间戳和画面大小（%d 帧，%.2f–%.2f 秒，%d×%d）"
             % (len(video.times), video.times[0], video.times[-1], video.width, video.height):
             len(video.times) == 30 and abs(video.times[0]) < 0.01 and abs(video.times[-1] - 2.9) < 0.1
             and (video.width, video.height) == (96, 64)}
    stop = threading.Event()
    clock = fake_game.FakeClock(None)
    camera = rr.ReplayCamera(video, clock, stop)
    clock.camera = camera
    for label, seconds, index in (("0", 0.0, 0), ("0.35", 0.35, 3), ("1.0", 1.0, 10), ("2.2", 2.2, 22)):
        got = frame_index(camera.at(seconds), means)
        check[f"假时钟走了 {label} 秒 → 第 {index} 帧（实际第 {got} 帧，允许差一帧）"] = abs(got - index) <= 1
    same = frame_index(camera.at(2.2), means) == frame_index(camera.at(2.2), means)
    check["同一秒问两次，还是同一帧（时间往前走，不回头）"] = same
    camera.release()
    clock2 = fake_game.FakeClock(None)
    camera2 = rr.ReplayCamera(video, clock2, threading.Event(), tail_seconds=3)
    clock2.camera = camera2
    beginning = frame_index(camera2.grab(), means)  # 假时钟刚开始：第 0 帧
    clock2.now += 3.5  # 录像只有 2.9 秒：放完了
    black = camera2.grab()
    check[f"第一次要给第 0 帧（实际第 {beginning} 帧）"] = beginning == 0
    check[f"录像放完了，告诉调用的人（放完在第 {camera2.over_at:.1f} 秒），以后给一样大的黑图（{black.shape}）"] = (
        camera2.at(3.5) is None and camera2.over_at is not None and black.shape == (64, 96, 3)
        and int(black.max()) == 0)
    camera2.release()
    return check


def hide_checks():
    """藏总结页 / 经验值页：从第一帧认出起（连这一帧）换成黑图；不藏（--keep-summary）和一直认不出时不藏。"""
    values = [20 if not 10 <= i < 20 else 200 for i in range(30)]  # 第 10–19 帧像"总结页"
    path = make_gray_video(TEMP / "pages.mp4", values)
    if path is None:
        return {"⏭ 跳过藏总结页：这台电脑的 OpenCV 写不了 MP4": True}
    video = rr.read_video(path)
    if video is None:
        return {"⏭ 跳过藏总结页：读不出来刚做的小视频": True}

    def looks_like_page(frame):
        """灰度大于 120 的帧算总结页（假的"认画面"，不用真的识别）。"""
        return ("summary", "总结页") if float(frame.mean()) > 120 else None

    def grab_all(classify):
        clock = fake_game.FakeClock(None)
        camera = rr.ReplayCamera(video, clock, threading.Event(), tail_seconds=3, classify=classify)
        clock.camera = camera
        frames = []
        for step in range(30):  # 每 0.1 秒一张
            clock.now = 1000.0 + step * 0.1
            frames.append(camera.grab())
        camera.release()
        return camera, [float(frame.mean()) for frame in frames]

    camera, means = grab_all(looks_like_page)
    camera2, means2 = grab_all(None)  # 不藏（--keep-summary 就是这样）
    camera3, means3 = grab_all(lambda frame: None)  # 一帧都没认出
    return {
        f"从第一帧总结页起（含这一帧）换成黑图：认出的第 {camera.hidden_at:.1f} 秒，之前是录像"
        f"（{[round(m) for m in means[:10]]}），之后全黑（{max(means[10:])}）":
            camera.hidden_at is not None and abs(camera.hidden_at - 1.0) < 0.11
            and max(means[:10]) < 60 and max(means[10:]) == 0,
        f"记下了认出的是哪个画面（{camera.hidden_name}、{camera.hidden_label}）":
            camera.hidden_name == "summary" and camera.hidden_label == "总结页",
        f"不藏的时候照常给录像（第 11 帧还是灰 {means2[10]:.0f} 的\"总结页\"，没记藏的时刻）":
            camera2.hidden_at is None and means2[10] > 120,
        f"一帧都没认出就不藏（最亮的一帧灰 {max(means3):.0f}，没记藏的时刻）":
            camera3.hidden_at is None and max(means3) > 120,
    }


def leak_checks():
    """重放里还是读到总结页 / 经验值页：报告标出来、不算进汇总；没读到的照常算。"""
    db = TEMP / "leak.db"
    conn = storage.connect(db)
    leak_id = storage.insert_match(conn, {"ended_at": "2026-11-21 10:00:00", "legend": "恶灵", "placement": 5,
                                          "kills": 1, "assists": 0, "damage": 100})
    storage.save_readings(conn, leak_id, "总结页", {"placement": 5}, "2026-11-21 10:00:01")
    storage.save_readings(conn, leak_id, "对局中", {"kills": 1}, "2026-11-21 10:00:02")
    clean_id = storage.insert_match(conn, {"ended_at": "2026-11-21 10:10:00", "legend": "恶灵", "placement": 6,
                                           "kills": 0, "assists": 0, "damage": 50})
    storage.save_readings(conn, clean_id, "对局中", {"kills": 0}, "2026-11-21 10:10:01")
    leak_sources = rr.read_sources(conn, leak_id)  # 只读查 readings 表
    clean_sources = rr.read_sources(conn, clean_id)
    conn.close()
    answer = rr.Answer(1, datetime(2026, 11, 21, 10, 0), "恶灵", 5, 1, 0, 100, None, None, 0, None)
    video = rr.Video(Path("a.mp4"), [0.0, 10.0], 1920, 1080)
    base = {"video": video, "log": [], "seconds": 1.0, "aspect": "16:10", "error": None, "keep_summary": False}

    def result_of(match_id, sources):
        row = {"match_id": match_id, "placement": 5 if match_id == leak_id else 6,
               "kills": 1 if match_id == leak_id else 0, "assists": 0,
               "damage": 100 if match_id == leak_id else 50, "rp_delta": None,
               "needs_review": 0, "review_note": None, "rp_source": None}
        return {**base, "matches": [row], "sources": {match_id: sources}}

    leaked, clean = result_of(leak_id, leak_sources), result_of(clean_id, clean_sources)
    verdicts = {field: rr.judge(field, getattr(answer, field), leaked["matches"][0].get(field),
                                answer.placement, answer.rp_source) for field, _ in rr.FIELDS}
    clean_verdicts = {field: rr.judge(field, getattr(answer, field), clean["matches"][0].get(field),
                                      answer.placement, answer.rp_source) for field, _ in rr.FIELDS}
    lines = rr.block_lines(answer, leaked, verdicts)
    done = [{"result": leaked, "verdicts": verdicts}, {"result": clean, "verdicts": clean_verdicts}]
    excluded = rr.summary_lines(done, keep_summary=False)
    included = rr.summary_lines(done, keep_summary=True)

    def row_counts(summary):
        """汇总表里"名次"那一行的数（一样、少一点、错、没读出、没有答案）。"""
        row = next(line for line in summary if line.strip().startswith("名次"))
        return [int(value) for value in row.split()[1:]]

    return {
        f"从 readings 表读来源，按读到的先后排（第 {leak_id} 局：{leak_sources}；第 {clean_id} 局：{clean_sources}）":
            leak_sources == ["总结页", "对局中"] and clean_sources == ["对局中"],
        f"有总结页来源就算没藏住（{rr.leaked_sources(leak_sources)}），没有就不算":
            rr.leaked_sources(leak_sources) == ["总结页"] and rr.leaked_sources(clean_sources) == [],
        f"报告里标出来了（{'；'.join(line.strip() for line in lines if '⚠' in line or '来源' in line)}）":
            any("重放读到的来源：总结页、对局中" in line for line in lines)
            and any("重放里还是读到了总结页 / 经验值页，不算跳过" in line for line in lines),
        f"汇总表不算它、下面写一句（{excluded[-1].strip()}；名次那一行 {row_counts(excluded)}）":
            any("1 段读到了总结页 / 经验值页，没算进汇总" in line for line in excluded)
            and row_counts(excluded) == [0, 0, 1, 0, 0],  # 读到了总结页的那局（名次一样）没算；另一局名次 6≠5，算"错"
        f"--keep-summary 时都算进汇总（{included[-1].strip()}；名次那一行 {row_counts(included)}）":
            any("也算进汇总了" in line for line in included)
            and not any("没算进汇总" in line for line in included)
            and row_counts(included) == [1, 0, 1, 0, 0],
    }


def judge_checks():
    """每种判定至少一个例子。"""
    return {
        "一样（名次 17 → 17）": rr.judge("placement", 17, 17) == "一样",
        "少一点（击杀 5 → 4）": rr.judge("kills", 5, 4) == "少一点",
        "错（名次 17 → 16）": rr.judge("placement", 17, 16) == "错",
        "错（伤害 500 → 501，比正确答案多）": rr.judge("damage", 500, 501) == "错",
        "错（伤害 1118 → 11，不到一半：多半是读错了）": rr.judge("damage", 1118, 11) == "错",
        "少一点（伤害 1118 → 988，最后一秒没截到）": rr.judge("damage", 1118, 988) == "少一点",
        "没读出（击杀 5 → 空）": rr.judge("kills", 5, None) == "没读出",
        "没读出（RP -48 → 空）": rr.judge("rp_delta", -48, None, 7, "经验值页") == "没读出",
        "冠军局的没读出（RP 259 → 空）":
            rr.judge("rp_delta", 259, None, 1, "经验值页") == "没读出（冠军局规则上不用右上角）",
        "没有答案（伤害 空 → 500）": rr.judge("damage", None, 500) == "没有答案",
        "没有答案（RP 的正确答案不是经验值页）": rr.judge("rp_delta", -28, -28, 7, "对局中") == "没有答案",
        "汇总表把冠军局的没读出算进\"没读出\"": rr.bucket("没读出（冠军局规则上不用右上角）") == "没读出",
    }


def end_to_end_checks():
    """从头到尾跑一次：几秒钟的全黑小视频走一遍真正的 collect()，报告里这一局是"没存下"；
    再走一段"有总结页"的假录像（假的认画面），看藏的时刻对不对。"""
    path = make_video(TEMP / "black.mp4", width=1280, height=720, fps=10, count=20, gray=0)
    if path is None:
        return {"⏭ 跳过整段重放：这台电脑的 OpenCV 写不了 MP4": True}
    video = rr.read_video(path)
    if video is None:
        return {"⏭ 跳过整段重放：读不出来刚做的小视频": True}
    result = rr.replay_recording(video, TEMP / "replay.db", tail_seconds=3)
    line = rr.result_line(result)
    log = result["log"]
    # --aspect 给了就原样交给 collect(game_aspect=...)：日志里会写"测试时指定的 16:10"
    fixed = rr.replay_recording(video, TEMP / "replay_aspect.db", aspect="16:10", tail_seconds=3)
    # 前 10 帧灰 20、后 10 帧灰 200（当作总结页）：藏起来以后 collect 只看到黑图
    page_path = make_gray_video(TEMP / "page.mp4", [20] * 10 + [200] * 10, width=640, height=360)
    page = keep = None
    if page_path is not None:
        page_video = rr.read_video(page_path)
        if page_video is not None:
            def looks_like_page(frame):
                """灰度大于 120 的帧算总结页（假的"认画面"，不用真的识别）。"""
                return ("summary", "总结页") if float(frame.mean()) > 120 else None

            page = rr.replay_recording(page_video, TEMP / "replay_page.db", tail_seconds=2, classify=looks_like_page)
            keep = rr.replay_recording(page_video, TEMP / "replay_keep.db", tail_seconds=2, hide_pages=False)
    return {
        f"全黑小视频走一遍真正的 collect() 不出错（{len(log)} 行日志，用了 {result['seconds']:.1f} 秒）":
            result["error"] is None,
        f"报告里这一局是\"没存下\"（{line}）": "没存下" in line,
        f"只靠这段录像一局都没记下（{len(result['matches'])} 局）": result["matches"] == [],
        "collect 的日志里有开始记录、本次记录（能存成 .log 查问题）":
            any("开始记录" in row for row in log) and any("本次记录" in row for row in log),
        f"日志里写了画面上认出什么（{next((row for row in log if row.startswith('游戏画面比例：')), '没有这一行')}）":
            any(row.startswith("游戏画面比例：") for row in log),
        f"指定了画面比例就原样交给 collect（--aspect 16:10 → {fixed['aspect']}）":
            fixed["error"] is None and "测试时指定的 16:10" in fixed["aspect"],
        f"真实的认画面代码没把黑图认成总结页 / 经验值页（藏的时刻 {result['hidden_at']}）":
            result["hidden_at"] is None and result["keep_summary"] is False,
        f"有总结页的假录像：重放里从第 {page['hidden_at'] if page else '?'} 秒起换成黑图"
        f"（认出了 {page['hidden_label'] if page else '?'}）":
            page is not None and page["error"] is None and page["hidden_at"] is not None
            and abs(page["hidden_at"] - 1.0) < 0.2 and page["hidden_label"] == "总结页",
        f"--keep-summary（hide_pages=False）：不藏（藏的时刻 {keep['hidden_at'] if keep else '?'}，原样喂 {keep['keep_summary'] if keep else '?'}）":
            keep is not None and keep["error"] is None and keep["keep_summary"] is True and keep["hidden_at"] is None,
    }


def report_checks():
    """报告里那几行：藏了没有、存下好几局时每一局都列出来、读到的来源、汇总表按判定数、录像文件夹看 settings.json。"""
    data = TEMP / "data_dir"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"recording_dir": str(TEMP / "elsewhere")}), encoding="utf-8")
    folder_ok = rr.recordings_folder(data) == TEMP / "elsewhere"
    default_ok = rr.recordings_folder(TEMP / "no_settings") == TEMP / "no_settings" / "recordings"
    answer = rr.Answer(189, datetime(2026, 11, 21, 20, 30, 59), "班加罗尔", 17, 0, 0, 43, -48, "经验值页", 0, None)
    video = rr.Video(Path("2026-11-21_202753_班加罗尔.mp4"), [0.0, 174.7], 1920, 1080)
    base = {"video": video, "log": [], "seconds": 8.0, "aspect": "16:10", "error": None, "keep_summary": False,
            "hidden_at": None, "hidden_name": None, "hidden_label": None, "classify_error": None}
    one = {**base, "matches": [{"match_id": 1, "placement": 17, "kills": 0, "assists": 0, "damage": 43,
                                 "rp_delta": -48, "needs_review": 0, "review_note": None, "rp_source": "对局中"}],
           "sources": {1: ["对局中"]}}
    verdicts = {field: rr.judge(field, getattr(answer, field), one["matches"][0].get(field),
                                answer.placement, answer.rp_source) for field, _ in rr.FIELDS}
    lines = rr.block_lines(answer, one, verdicts)
    multi = {**base, "matches": [
        {"match_id": 1, "placement": 17, "kills": 0, "assists": 0, "damage": 43, "rp_delta": None,
         "needs_review": 1, "review_note": "测试", "rp_source": None},
        {"match_id": 2, "placement": 17, "kills": 0, "assists": 0, "damage": 43, "rp_delta": None,
         "needs_review": 0, "review_note": None, "rp_source": None}],
        "sources": {1: ["对局中"], 2: []}}
    multi_lines = rr.block_lines(answer, multi, None)
    summary = rr.summary_lines([{"result": one, "verdicts": verdicts}, {"result": multi, "verdicts": None},
                                {"result": {**base, "matches": [], "sources": {}}, "verdicts": None}])
    hidden = {**base, "hidden_at": 170.0, "hidden_name": "summary", "hidden_label": "总结页"}
    far = {**base, "hidden_at": 100.0, "hidden_name": "xp", "hidden_label": "经验值页"}
    return {
        f"录像文件夹看 settings.json 的 recording_dir（{rr.recordings_folder(data)} 而不是 {data / 'recordings'}）；"
        f"没有 settings.json 就是 <数据文件夹>\\recordings":
            folder_ok and default_ok,
        f"存下正好一局：每项一行，再写待确认、备注、RP 来源和读到的来源（{len(lines)} 行）":
            any(line.strip().startswith("重放这一局：待确认") for line in lines)
            and any(line.strip() == "重放读到的来源：对局中" for line in lines),
        f"藏的那一行：没认出时写\"没有藏\"（{rr.hide_text(one)}）":
            "没有藏（录像里没认出总结页 / 经验值页" in rr.hide_text(one)
            and "录像共 174.7 秒" in rr.hide_text(one),
        f"藏的那一行：写了从第几秒起、最后几秒、认出了哪个画面（{rr.hide_text(hidden)}）":
            "从录像第 170.0 秒起换成黑图" in rr.hide_text(hidden) and "最后 4.7 秒" in rr.hide_text(hidden)
            and "总结页（summary）" in rr.hide_text(hidden) and "可能认错了" not in rr.hide_text(hidden),
        f"藏的那一行：离结尾还远就提醒可能认错了（{rr.hide_text(far)}）":
            "离结尾还有 74.7 秒，可能认错了" in rr.hide_text(far),
        f"藏的那一行：--keep-summary 时写原样喂（{rr.hide_text({**base, 'keep_summary': True})}）":
            "原样喂（--keep-summary，没藏）" in rr.hide_text({**base, "keep_summary": True}),
        f"存下好几局：记\"分成了 2 局\"并列出每一局（带读到的来源）":
            any("分成了 2 局" in line for line in multi_lines)
            and sum(line.strip().startswith("第 ") for line in multi_lines) == 2
            and any("第 1 局（编号 1）" in line and "读到的来源 对局中" in line for line in multi_lines),
        f"汇总表按判定数（{summary[0].strip()}）":
            "存下正好一局 1 段" in summary[0] and "没存下 1 段" in summary[0] and "分成多局 1 段" in summary[0],
        f"命令行 --keep-summary 解析得出（{rr.parse_args(['--keep-summary']).keep_summary}、"
        f"{rr.parse_args([]).keep_summary}）":
            rr.parse_args(["--keep-summary"]).keep_summary is True
            and rr.parse_args([]).keep_summary is False,
    }


def main():
    """跑完全部检查：找录像、对上数据库、按时间取帧、藏两页、查来源、判定、报告那几行、整段重放。"""
    print("录像重放工具（找录像、对上数据库、按时间取帧、藏总结页 / 经验值页、查来源、判定、整段重放）")
    checks = {}
    checks.update(find_checks())
    checks.update(match_checks())
    checks.update(frames_checks())
    checks.update(hide_checks())
    checks.update(leak_checks())
    checks.update(judge_checks())
    checks.update(report_checks())
    checks.update(end_to_end_checks())
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
