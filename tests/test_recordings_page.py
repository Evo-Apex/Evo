"""录像页（panel_recordings.py、recordings.html、recordings_watch.html）和战绩页的"看录像"按钮。

不用 ffmpeg：录像文件用几 KB 的假数据，时长写在 .json 里，缩略图用现成的 .jpg。
- 导航：没打开录像、也没有录像时没有"录像"；打开了、或者已经有录像时有，在"战绩"后面
- 列表：从新到旧，每张卡片有缩略图、时长、传奇、模式和地图（对上的战绩里的）、名次、击杀、伤害、事件数；
  没 .json 的（上次崩了拼回来的）标"未完成"；上面写共多大、上限；没有录像时的说明
- 播放页：播放器不自动播放；时间轴上事件标在对应位置（时长 120 秒、60 秒处的倒地在 50%），每个是按钮、念得出名字和时刻；
  挨得很近的几个合成一个标记（几段颜色、个数、悬停写出全部），下面的列表照样一条一条列；
  断掉的那段画出来；下面的事件列表；对上的战绩能点过去；网址带 t= 从那一刻开始；删除要先确认
- 录像文件支持拖动（Range 请求回 206）；缩略图；网址不能指到录像文件夹以外、分段文件夹里的文件
- 删除：MP4、.json、.jpg 一起删；没有口令不让删
- 战绩页：有录像的局多一个"看录像"按钮，指到那个录像；没录像的局没有
- 设置页"录像"一栏（假的 ffmpeg 和编码器检测）：没有 ffmpeg 时整栏不显示、打不开（正式版）；只有 H.264 时不显示编码格式；
  配置里有几种编码格式时（以后加回 AV1，假装这台电脑"编不了 AV1"）：编不了的不能选、不保存；
  打开时检测编码器、记住；画质四项一起保存，选了编不了的 AV1、乱填的不保存；声音两档（默认只录 Apex）；麦克风开关（默认关）、
  音量滑块（松手保存、范围）、测试麦克风（假的麦克风：录的时候有音量、录完取得回 WAV、正在录时不能再点、麦克风用不了时说原因）；
  估一局多大；硬盘上限的范围；
  保存位置（不是完整路径、在程序文件夹里的不行，填的留在输入框里；换了以后说原来的录像还在哪；改回默认）；关掉

运行：python tests/test_recordings_page.py
"""

import json
import re
import shutil
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_settings
import panel_app
import panel_recordings
import recorder
import storage

NOW = datetime.now().replace(microsecond=0)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def nav_labels(raw):
    nav = re.findall(r'<nav class="nav".*?</nav>', raw, re.S)[0]
    return text(nav).split()


def make_recording(base, started, name, duration, events=(), outages=(), legend="班加罗尔", sidecar=True):
    month = base / started.strftime("%Y-%m")
    month.mkdir(parents=True, exist_ok=True)
    video = month / f"{started:%Y-%m-%d_%H%M%S}_{name}.mp4"
    video.write_bytes(bytes(range(256)) * 16)  # 4 KB 的假录像
    video.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    if sidecar:
        video.with_suffix(".json").write_text(json.dumps({
            "version": 1, "video": video.name, "started": started.isoformat(timespec="seconds"),
            "first_frame": 0.4, "duration": duration, "match": {"legend": legend} if legend else {},
            "events": list(events), "outages": list(outages), "hud_changes": []}, ensure_ascii=False), encoding="utf-8")
    return video


def make_clip(full, name, index, count, start, end, events=(), of_duration=600, started=None, legend="恶灵"):
    """一段假的高光片段（highlights.save 存的样子）：放在整局 full 旁边，名字是整局的名字 + "_击杀1" 这样。full 可以不存在（只录高光）。"""
    clip = full.with_name(f"{full.stem}_{name}.mp4")
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(bytes(range(256)) * 4)
    clip.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    clip.with_suffix(".json").write_text(json.dumps({
        "version": 1, "video": clip.name, "started": (started or NOW).isoformat(timespec="seconds"), "first_frame": 0.4,
        "highlight": {"of": full.name, "name": name, "kind": "kill", "index": index, "count": count, "from": start, "to": end,
                      "of_duration": of_duration, "hits": ["kill"]},
        "duration": end - start, "match": {"legend": legend}, "events": list(events), "outages": []}, ensure_ascii=False),
        encoding="utf-8")
    return clip


def show_checks(client, token, base):
    base = base / "Evo 录像,备份"
    video = make_recording(base, NOW, "排位, 高光", 20)
    rid = f"{video.parent.name}/{video.stem}"
    back = "/recordings/watch?" + urlencode({"v": rid, "t": 10})
    form = {"token": token, "v": rid, "next": back}
    shell, ole = mock.Mock(), mock.Mock()
    libraries = {"shell32": shell, "ole32": ole}
    with (mock.patch.object(panel_recordings, "folder", return_value=base),
          mock.patch("ctypes.OleDLL", side_effect=libraries.__getitem__), mock.patch("subprocess.Popen"),
          mock.patch("os.startfile") as open_folder):
        shown = client.post("/recordings/show", data=form)
        parsed = shell.SHParseDisplayName.call_args
        selected = shell.SHOpenFolderAndSelectItems.call_args
        exact = (parsed is not None and selected is not None and parsed.args[0].value == str(video.resolve())
                 and parsed.args[2]._obj is selected.args[0] and selected.args[1:] == (0, None, 0)
                 and ole.CoTaskMemFree.call_count == ole.CoUninitialize.call_count == 1 and not open_folder.called
                 and shown.status_code == 302 and shown.headers["Location"] == back)

        shell.SHOpenFolderAndSelectItems.side_effect = OSError("无法选中")
        fallback = client.post("/recordings/show", data=form)
        with client.session_transaction() as state:
            flash_messages = state.pop("_flashes", [])
        parent = (open_folder.call_args == mock.call(video.parent.resolve()) and fallback.headers["Location"] == back
                  and any("未能选中" in msg for _, msg in flash_messages)
                  and ole.CoTaskMemFree.call_count == ole.CoUninitialize.call_count == 2)

        open_folder.reset_mock()
        missing = client.post("/recordings/show", data={**form, "v": "2099-01/已删除"}, follow_redirects=True)
        reports_missing = (missing.request.path == "/recordings" and "找不到这个录像" in missing.get_data(as_text=True)
                           and not open_folder.called and shell.SHOpenFolderAndSelectItems.call_count == 2)

        root = client.post("/recordings/show", data={"token": token, "next": "/settings#recording"})
        opens_root = open_folder.call_args == mock.call(base) and root.headers["Location"] == "/settings#recording"
    shutil.rmtree(base)
    return {
        "在资源管理器中显示：完整路径含空格、逗号和中文时，选中路径对应的 MP4": exact,
        "Windows 无法选中录像时，打开实际所在目录并给出提示": parent,
        "录像已找不到时，提示并回录像列表": reports_missing,
        "打开文件夹：未指定录像时打开保存根目录，返回设置页": opens_root,
    }


def clip_checks(client, token, base, conn):
    """高光片段（highlights.py 存的）在录像页上：列表里和整局归成一张卡片、播放页下面的"高光片段"、"保存高光"按钮、删一段。"""
    import highlights
    out = {}
    start = NOW - timedelta(hours=6)
    full = make_recording(base, start, "排位_恶灵", 600, legend="恶灵",
                          events=[{"t": 100, "kind": "kill", "kills": 1, "count": 1}, {"t": 300, "kind": "knock"}])
    fid = f"{full.parent.name}/{full.stem}"
    c1 = make_clip(full, "击杀1", 1, 2, 70.0, 110.0, [{"t": 30, "kind": "kill", "kills": 1, "count": 1}], started=start)
    c2 = make_clip(full, "击倒1", 2, 2, 280.0, 310.0, [{"t": 20, "kind": "knock"}], started=start)
    ids = [f"{c.parent.name}/{c.stem}" for c in (c1, c2)]
    alone = base / full.parent.name / f"{NOW - timedelta(hours=8):%Y-%m-%d_%H%M%S}_匹配.mp4"  # 只录高光：整局删了
    a1 = make_clip(alone, "成盒1", 1, 1, 400.0, 415.0, [{"t": 10, "kind": "dead"}], started=NOW - timedelta(hours=8))
    match = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{start + timedelta(seconds=590):%Y-%m-%d %H:%M:%S}",
                                        "placement": 2, "kills": 1, "mode": "ranked", "legend": "恶灵"})
    lonely = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{NOW - timedelta(hours=8) + timedelta(seconds=420):%Y-%m-%d %H:%M:%S}",
                                         "placement": 12, "legend": "恶灵"})
    try:
        raw = client.get("/recordings").get_data(as_text=True)
        cards = [text(c) for c in re.findall(r'<article class="rec-card".*?</article>', raw, re.S)]
        mine = [c for c in cards if "排名" not in c and "#2" in c]
        out[f"列表：整局和它的高光片段是一张卡片，写着'高光 2 段'（{mine}）"] = (
            len(mine) == 1 and "高光 2 段" in mine[0] and "10:00" in mine[0] and not any("击杀1" in c for c in cards))
        only = [c for c in cards if "只有高光" in c]
        link = re.findall(r'<a class="rec-thumb" href="([^"]+)"', raw)
        out[f"只录高光（整局删了）：标'只有高光'、时长写片段加起来的（0:15），点了放第一段（{only}）"] = (
            len(only) == 1 and "0:15" in only[0] and "高光 1 段" in only[0] and "#12" in only[0]
            and any(f"{a1.stem}" in l or "%E6%88%90%E7%9B%921" in l for l in link))
        with panel_app.app.app_context():
            links = panel_recordings.match_links([{"match_id": match}, {"match_id": lonely}])
        out[f"战绩页的'看录像'：有整局去整局，只有高光去第一段（{links}）"] = (
            links.get(match) == fid and links.get(lonely) == f"{a1.parent.name}/{a1.stem}")

        raw = client.get("/recordings/watch", query_string={"v": fid}).get_data(as_text=True)
        strip = (re.findall(r'<section class="rec-clips".*?</section>', raw, re.S) or [""])[0]
        out[f"整局的播放页：下面'高光片段'列出两段（名字、在整局的哪一段、时长、事件图标），'保存高光'不能再点（{text(strip)[:90]}）"] = (
            "击杀 1" in strip and "击倒 1" in strip and "1:10–1:50" in strip and "4:40–5:10" in strip and "0:40" in strip
            and strip.count('class="rec-clip') >= 2 and "ev-kill" in strip and re.search(r"<button[^>]*disabled[^>]*>已保存高光", strip)
            and 'aria-current="page"' not in strip)
        out["删整局时说高光片段不删"] = "高光片段不删" in raw
        raw = client.get("/recordings/watch", query_string={"v": ids[1]}).get_data(as_text=True)
        strip = (re.findall(r'<section class="rec-clips".*?</section>', raw, re.S) or [""])[0]
        current = re.findall(r'<a class="rec-clip current" href="([^"]+)" aria-current="page">', strip)
        out["片段的播放页：标题写片段的名字、整局的哪一段；正在放的那段标出来；有'看整局'；没有'保存高光'"] = (
            "· 击倒 1" in raw and "整局的 <span class=\"num\">4:40–5:10</span>" in raw and len(current) == 1
            and "看整局" in strip and "保存高光" not in strip and "删除这一段" in raw and f'name="back" value="{fid}"' in raw)
        raw = client.get("/recordings/watch", query_string={"v": f"{a1.parent.name}/{a1.stem}"}).get_data(as_text=True)
        out["只有高光的片段页：没有'看整局'，删掉回列表"] = "看整局" not in raw and 'name="back"' not in raw

        # 保存高光：假的切（不用 ffmpeg）
        bare = make_recording(base, NOW - timedelta(hours=10), "匹配_恶灵", 300, legend="恶灵",
                              events=[{"t": 50, "kind": "kill", "kills": 1, "count": 1}])
        bid = f"{bare.parent.name}/{bare.stem}"
        raw = client.get("/recordings/watch", query_string={"v": bid}).get_data(as_text=True)
        out["还没存高光的整局：写着'还没有保存高光'，'保存高光'能点"] = (
            "还没有保存高光" in raw and re.search(r'<button type="submit" class="btn" >保存高光</button>', raw))
        real = highlights.save
        calls = []
        result = {"clips": None, "error": None}

        def fake_save(ffmpeg, video, picked, log=print):
            calls.append((Path(video), picked))
            if result["error"]:
                raise highlights.HighlightError(result["error"])
            return result["clips"](video) if result["clips"] else []
        highlights.save = fake_save
        try:
            r = client.post("/recordings/highlights", data={"v": bid})
            out["保存高光：没有口令不让存"] = r.status_code != 302 and calls == []
            result["error"] = "读不出录像里的关键帧"
            r = client.post("/recordings/highlights", data={"v": bid, "token": token})
            raw = client.get(r.headers["Location"]).get_data(as_text=True)
            out["切不成：写出原因（红字）"] = 'role="alert">没存成：读不出录像里的关键帧' in raw
            result["error"] = None
            r = client.post("/recordings/highlights", data={"v": bid, "token": token})
            raw = client.get(r.headers["Location"]).get_data(as_text=True)
            out["一个勾了的事件都没有：说没有存"] = "这一局没有设置页里勾选的事件" in raw
            result["clips"] = lambda video: [make_clip(Path(video), "击杀1", 1, 1, 20.0, 60.0, started=NOW - timedelta(hours=10))]
            app_settings.set_value("recording_highlights", {"kill": {"on": True, "before": 12, "after": 3}})
            r = client.post("/recordings/highlights", data={"v": bid, "token": token})
            location = r.headers.get("Location", "")
            raw = client.get(location).get_data(as_text=True)
            out[f"保存高光：按设置页现在的切（击杀前 12 后 3），存好了跳回'高光片段'，写着存了几段（{location}）"] = (
                r.status_code == 302 and location.endswith("#h-clips") and calls[-1][0] == bare
                and calls[-1][1]["kill"] == {"on": True, "before": 12, "after": 3}
                and 'role="status">存好了 1 段高光' in raw and "击杀 1" in raw and "已保存高光" in raw)
            n = len(calls)
            r = client.post("/recordings/highlights", data={"v": bid, "token": token})
            raw = client.get(r.headers["Location"]).get_data(as_text=True)
            out["已经存过高光：不再切"] = len(calls) == n and "这一局已经存过高光了" in raw
        finally:
            highlights.save = real
            app_settings.set_value("recording_highlights", None)

        r = client.post("/recordings/delete", data={"v": ids[0], "token": token, "back": fid})
        out["删一段高光：只删这一段（MP4、.json、.jpg），回到整局那一页的'高光片段'"] = (
            r.status_code == 302 and r.headers["Location"].endswith("#h-clips") and "/recordings/watch" in r.headers["Location"]
            and not c1.exists() and not c1.with_suffix(".json").exists() and c2.exists() and full.exists())
    finally:
        for video in base.glob("*/*"):
            if video.stem.startswith((f"{start:%Y-%m-%d_%H%M%S}", f"{NOW - timedelta(hours=8):%Y-%m-%d_%H%M%S}",
                                      f"{NOW - timedelta(hours=10):%Y-%m-%d_%H%M%S}")):
                video.unlink(missing_ok=True)
    return out


def main():
    print("录像页（列表、播放、删除、战绩页的看录像按钮；设置页的\"录像\"一栏）")
    try:
        return checks()
    finally:  # 全部测试在同一个临时数据文件夹里跑：留下录像、开着录像，后面几组的导航会多出"录像"
        app_settings.set_value("recording_dir", None)
        shutil.rmtree(panel_recordings.folder(), ignore_errors=True)
        for key in ("recording_enabled", panel_recordings.CHECK_KEY, "recording_size", "recording_fps",
                    "recording_quality", "recording_codec", "recording_limit_gb", "recording_audio_source", "recording_mic",
                    "recording_mic_volume", "recording_mode", "recording_highlights"):
            app_settings.set_value(key, None)


def section(raw):
    """设置页里"录像"那一栏的 HTML。"""
    found = re.findall(r'<section class="card settings-card" aria-labelledby="h-video">.*?</section>', raw, re.S)
    return found[0] if found else ""


def note(client, path, data, token):
    """提交设置，跟着跳回设置页，返回（"录像"那一栏，那一项下面写的结果）。"""
    r = client.post(path, data={**data, "token": token})
    if r.status_code != 302 or not r.headers["Location"].endswith("/settings#h-video"):
        return "", f"没跳回设置页（{r.status_code}）"
    box = section(client.get("/settings").get_data(as_text=True))
    found = re.findall(r'<div class="setting-desc rec-note-line[^"]*"\s*role="(\w+)" id="rec-note-\w+">([^<]*)</div>', box)
    return box, (f"{found[0][0]}:{found[0][1]}" if found else "")


class FakeMic(threading.Thread):
    """假的麦克风（测试麦克风用）：录的时候 level 是 0.5，stop() 时写一段 0.2 秒的 WAV；error 不为空就是用不了。"""
    error_next = None

    def __init__(self, source, wav_path, meter=False):
        super().__init__(daemon=True)
        self.source, self.wav_path, self.meter = source, Path(wav_path), meter
        self.ready, self.level, self.error = threading.Event(), 0.0, FakeMic.error_next
        self._stop = threading.Event()

    def run(self):
        self.ready.set()
        if self.error:
            return
        self.level = 0.5
        self._stop.wait(5)
        with wave.open(str(self.wav_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x10" * 2 * 9600)

    def stop(self):
        self._stop.set()


def mic_test_checks(client, token):
    """设置页的"测试麦克风"（假的麦克风，测试只录 0.3 秒）。"""
    out = {}
    real = panel_recordings.MIC_FACTORY, panel_recordings.MIC_TEST_SECONDS
    panel_recordings.MIC_FACTORY, panel_recordings.MIC_TEST_SECONDS = FakeMic, 0.3
    try:
        started = client.post("/settings/recording/mic-test", data={"token": token}).get_json()
        during = client.get("/settings/recording/mic-test").get_json()
        again = client.post("/settings/recording/mic-test", data={"token": token}).get_json()
        time.sleep(0.6)
        after = client.get("/settings/recording/mic-test").get_json()
        audio = client.get("/settings/recording/mic-test.wav")
        out[f"测试麦克风：开始（{started}）、录的时候有音量和剩几秒（{during}）、正在录时再点不行（{again}）"] = (
            started.get("ok") is True and during.get("state") == "recording" and during.get("level") == 0.5
            and 0 <= during.get("left", -1) <= 0.3 and again.get("ok") is False and "正在录" in again.get("error", ""))
        out[f"测试麦克风：录完是 done（{after}），取得回那段 WAV（{audio.status_code}，{audio.mimetype}）"] = (
            after.get("state") == "done" and audio.status_code == 200 and audio.mimetype == "audio/wav"
            and audio.data[:4] == b"RIFF")
        # 上一段还被占着（像页面正在放、这个回答还没关）时再测一次：每次一个新文件，照样能测
        second = client.post("/settings/recording/mic-test", data={"token": token}).get_json()
        time.sleep(0.6)
        second_done = client.get("/settings/recording/mic-test").get_json()
        audio.close()
        out[f"上一段录音还被占着时再测一次也行（{second}，{second_done}）"] = (
            second.get("ok") is True and second_done.get("state") == "done")
        FakeMic.error_next = "找不到麦克风"
        broken = client.post("/settings/recording/mic-test", data={"token": token}).get_json()
        none_now = client.get("/settings/recording/mic-test.wav").status_code
        out[f"麦克风用不了：说原因（{broken}），也取不到录音（{none_now}）"] = (
            broken.get("ok") is False and "找不到麦克风" in broken.get("error", "") and none_now == 404)
        nobody = client.post("/settings/recording/mic-test").status_code
        out["没有口令不让开始测试"] = nobody != 200
    finally:
        FakeMic.error_next = None
        panel_recordings.MIC_FACTORY, panel_recordings.MIC_TEST_SECONDS = real
        panel_recordings.mic_test.error = None
    return out


def highlight_form(box):
    """设置页"剪高光"那一组现在的样子：(录制模式, {事件: 勾没勾}, {事件: (前, 后)})。"""
    form = (re.findall(r'<form[^>]*id="rec-highlights".*?</form>', box, re.S) or [""])[0]
    mode = re.findall(r'<option value="(\w+)" selected>', form)
    on = {k: "checked" in rest for k, rest in re.findall(r'name="on_(\w+)" value="1" data-hl-on="\w+" ?(checked)?', form)}
    seconds = {}
    for side, kind, value in re.findall(r'name="(before|after)_(\w+)" min="0" max="\d+" step="1"\s*value="([\d.]+)"', form):
        seconds.setdefault(kind, [None, None])[side == "after"] = float(value)
    return (mode[0] if mode else None), on, {k: tuple(v) for k, v in seconds.items()}, form


def highlight_settings_checks(client, token):
    """设置页的剪高光：录制模式、高光包括哪些事件、每种事件前后录几秒（滑块）。"""
    out = {}
    kinds = ["kill", "knock", "assist_knock", "assist", "champion", "knocked", "revived", "respawned", "dead"]
    box = section(client.get("/settings").get_data(as_text=True))
    mode, on, seconds, form = highlight_form(box)
    out[f"录制模式默认整局录像；高光包括 9 种事件（带图标），默认勾 6 种（{[k for k, v in on.items() if v]}）"] = (
        mode == "full" and list(on) == kinds and [k for k, v in on.items() if v] == ["kill", "knock", "assist_knock", "champion",
                                                                                     "knocked", "dead"]
        and form.count('class="ev-icon"') == 18 and "自己倒地" in form and "只录高光" in form)
    out[f"高级选项折叠着，每种事件两个滑块（0–60 秒），默认照 Outplayed（击杀 {seconds.get('kill')}、被复活 {seconds.get('respawned')}）"] = (
        '<details class="move-data rec-hl-more" id="rec-hl-more">' in form and form.count('type="range"') == 18
        and seconds.get("kill") == (30, 10) and seconds.get("respawned") == (9, 20) and seconds.get("assist_knock") == (20, 10)
        and 'aria-label="击杀：事件前录几秒"' in form and "前 30 秒" in form and "后 10 秒" in form)
    out["写着击倒、助攻击倒还在适配多语种，反馈群能点（用系统浏览器开）"] = (
        "还在适配多语种" in form and re.search(r'<a href="https://qm\.qq\.com/[^"]+" target="_blank"[^>]*>反馈群</a>', form))

    fields = {"mode": "highlights", **{f"on_{k}": "1" for k in ("kill", "knock", "champion")},
              **{f"before_{k}": "20" for k in kinds}, **{f"after_{k}": "8" for k in kinds}}
    fields["before_kill"], fields["after_kill"] = "45", "5"
    box, said = note(client, "/settings/recording/highlights", fields, token)
    mode, on, seconds, _ = highlight_form(box)
    saved = app_settings.get("recording_highlights") or {}
    out[f"保存：录制模式、勾的事件、秒数都存下，页面上照着显示（{said}）"] = (
        said == "status:保存了：从下一局开始只录高光" and app_settings.get("recording_mode") == "highlights" and mode == "highlights"
        and [k for k, v in on.items() if v] == ["kill", "knock", "champion"] and seconds["kill"] == (45, 5)
        and seconds["dead"] == (20, 8) and saved.get("kill") == {"on": True, "before": 45, "after": 5}
        and saved.get("dead") == {"on": False, "before": 20, "after": 8})
    for wrong, why in ((dict(fields, before_kill="61"), "超过 60 秒"), (dict(fields, after_dead="-1"), "负数"),
                       (dict(fields, after_dead="abc"), "不是数"), (dict(fields, mode="其他"), "录制模式不认得")):
        _, said = note(client, "/settings/recording/highlights", wrong, token)
        out[f"{why}：不保存（{said}）"] = said.startswith("alert:没保存") and (app_settings.get("recording_highlights") or {}) == saved
    nothing = {k: v for k, v in fields.items() if not k.startswith("on_")}
    _, said = note(client, "/settings/recording/highlights", nothing, token)
    out[f"只录高光时一种都没勾：不保存（{said}）"] = said == "alert:没保存：只录高光时至少要勾一种事件"
    _, said = note(client, "/settings/recording/highlights", dict(nothing, mode="full"), token)
    out[f"整局录像时可以一种都不勾（{said}）"] = said == "status:保存了" and app_settings.get("recording_mode") == "full"
    box, said = note(client, "/settings/recording/highlights", dict(fields, reset="1"), token)
    mode, on, seconds, _ = highlight_form(box)
    out[f"恢复默认的秒数：秒数改回默认，勾的照表单上的（{said}）"] = (
        "改回默认" in said and seconds["kill"] == (30, 10) and seconds["dead"] == (10, 5)
        and [k for k, v in on.items() if v] == ["kill", "knock", "champion"])
    r = client.post("/settings/recording/highlights", data=fields)
    out["剪高光的设置：没有口令不让改"] = r.status_code != 302
    for key in ("recording_mode", "recording_highlights"):
        app_settings.set_value(key, None)
    return out


def settings_checks(client, token, base):
    """设置页的"录像"一栏。base：现在的录像文件夹（里面有一个录像）。"""
    out = {}
    real_find, real_pick = recorder.find_ffmpeg, recorder.pick_encoder
    ffmpeg = {"path": None}
    recorder.find_ffmpeg = lambda *_, **__: ffmpeg["path"]
    tried = []

    def fake_pick(path, options, rules, hwnd=None):  # 这台电脑：NVIDIA，编得了 H.264、编不了 AV1
        tried.append(options.codec)
        return {"h264": "h264_nvenc"}.get(options.codec)
    recorder.pick_encoder = fake_pick
    try:
        app_settings.set_value("recording_enabled", False)
        app_settings.set_value(panel_recordings.CHECK_KEY, None)
        raw = client.get("/settings").get_data(as_text=True)
        out["没有 ffmpeg（正式版）、录像也没开：设置页整栏\"录像\"都不显示"] = 'id="h-video"' not in raw and "设置" in raw
        r = client.post("/settings/recording", data={"on": "1", "token": token})
        out["没有 ffmpeg 时直接提交\"打开\"也打不开"] = (
            r.status_code == 302 and app_settings.get("recording_enabled") is not True)
        app_settings.set_value("recording_enabled", True)
        box = section(client.get("/settings").get_data(as_text=True))
        out["开着、ffmpeg 却不见了：照样显示这一栏（能关掉），写着找不到 ffmpeg"] = (
            'aria-checked="true"' in box and "找不到录像要用的组件" in box)
        app_settings.set_value("recording_enabled", False)
        r = client.post("/settings/recording", data={"on": "1"})
        out["没有口令不让改"] = r.status_code != 302 and app_settings.get("recording_enabled") is not True

        ffmpeg["path"] = "ffmpeg.exe"
        box, said = note(client, "/settings/recording", {"on": "1"}, token)
        check = app_settings.get(panel_recordings.CHECK_KEY) or {}
        out[f"有 ffmpeg 时显示这一栏，标着\"实验\"；打开：检测了编码器、记住了；写着用 NVIDIA 显卡；显示画质、上限、位置（{said}）"] = (
            app_settings.get("recording_enabled") is True and said.startswith("status:打开了") and ">实验<" in box
            and check == {"ffmpeg": "ffmpeg.exe", "h264": "h264_nvenc"}
            and "NVIDIA 显卡" in box and 'aria-checked="true"' in box and 'id="rec-size"' in box
            and "硬盘上限" in box and "保存位置" in box)
        tried.clear()
        client.get("/settings")
        out["记住了就不再检测（打开设置页不跑 ffmpeg）"] = tried == []
        out["只有 H.264（AV1 先拿掉了）：不显示编码格式那一项，提交时带着 h264"] = (
            'id="rec-codec"' not in box and '<input type="hidden" name="codec" value="h264">' in box)
        out["默认：1080p、60 帧、标准、H.264；画质四档按很高、高、标准、省空间排"] = all(re.search(p, box) for p in (
            r'<option value="1080p" selected>1080p（推荐）', r'<option value="60" selected>',
            r'<option value="standard" selected>标准（推荐）')) and (
            re.findall(r'<option value="(best|high|standard|small)"', box) == ["best", "high", "standard", "small"])

        rules = recorder.load_rules()
        line = panel_recordings.estimate(rules, (2560, 1440), "1080p", 60, "best", "h264", 30)
        out[f"估一局多大：1080p 60 帧很高约 12 Mbps、一局 1.7 GB、30 GB 存 17 局（{line}）"] = (
            "1920 × 1080" in line and "约 12.4 Mbps" in line and "约 1.7 GB" in line and "约 17 局" in line)
        line = panel_recordings.estimate(rules, (2560, 1440), "1080p", 60, "standard", "h264", 30)
        out[f"默认那一档（1080p 60 帧标准）约 5.6 Mbps、一局约 800 MB、30 GB 存 38 局（{line}）"] = (
            "约 5.6 Mbps" in line and "约 801 MB" in line and "约 38 局" in line)
        table = json.loads(re.findall(r"data-estimates='([^']*)'", box)[0].replace("&#34;", '"')) if "data-estimates" in box else {}
        out[f"每种组合都先算好了（{len(table)} 种），页面上改选项时换那一行"] = (
            len(table) == len(rules["sizes"]) * len(rules["fps"]) * len(rules["bits_per_pixel"]) * len(rules["encoders"])
            and 'id="rec-estimate"' in box)

        good = {"size": "720p", "fps": "30", "quality": "small", "codec": "h264"}
        box, said = note(client, "/settings/recording/quality", good, token)
        out[f"画质四项一起保存（{said}），页面上选着刚保存的"] = (
            said.startswith("status:保存了") and app_settings.get("recording_size") == "720p"
            and app_settings.get("recording_fps") == 30 and app_settings.get("recording_quality") == "small"
            and '<option value="720p" selected>' in box and '<option value="30" selected>' in box)
        mic_switch = re.findall(r'<button type="submit" class="switch"[^>]*aria-labelledby="rec-mic-name"', box)
        out["声音默认只录 Apex；麦克风单独一个开关，默认关；音量滑块默认 100%、0–200%，有测试按钮"] = (
            '<option value="game" selected>只录 Apex 的声音' in box and app_settings.get("recording_audio_source") == "game"
            and len(mic_switch) == 1 and 'aria-checked="false"' in mic_switch[0]
            and 'id="rec-mic-volume" name="volume" min="0" max="200"' in box and 'value="100"' in box
            and 'id="rec-mic-test"' in box)
        box, said = note(client, "/settings/recording/quality", {**good, "audio_source": "system"}, token)
        out[f"选电脑上所有的声音：存下，页面上选着（{said}）"] = (
            said.startswith("status:保存了") and app_settings.get("recording_audio_source") == "system"
            and '<option value="system" selected>电脑上所有的声音' in box)
        _, said = note(client, "/settings/recording/quality", {**good, "audio_source": "everything"}, token)
        out["声音乱填的不保存"] = said.startswith("alert:没保存") and app_settings.get("recording_audio_source") == "system"
        box, said = note(client, "/settings/recording/mic", {"on": "1"}, token)
        mic_switch = re.findall(r'<button type="submit" class="switch"[^>]*aria-labelledby="rec-mic-name"', box)
        out[f"打开录麦克风：开关是开着的，下面不另写结果（{said or '没写'}）"] = (
            app_settings.get("recording_mic") is True and said == "" and 'aria-checked="true"' in mic_switch[0])
        fetch = {"X-Evo-Fetch": "1"}
        saved = client.post("/settings/recording/mic-volume", data={"volume": "150", "token": token}, headers=fetch)
        wrong = [client.post("/settings/recording/mic-volume", data={"volume": v, "token": token}, headers=fetch).status_code
                 for v in ("250", "-5", "abc")]
        box = section(client.get("/settings").get_data(as_text=True))
        out[f"麦克风音量：脚本提交 150 存下（{saved.get_json()}），超出范围、不是数的不存（{wrong}），页面上是 150%"] = (
            saved.get_json() == {"ok": True, "volume": 150} and wrong == [400, 400, 400]
            and app_settings.get("recording_mic_volume") == 150 and ">150%</output>" in box)
        out.update(mic_test_checks(client, token))
        note(client, "/settings/recording/mic", {"on": "0"}, token)
        note(client, "/settings/recording/quality", good, token)  # 改回默认，后面的检查照旧
        _, said = note(client, "/settings/recording/quality", {**good, "codec": "av1"}, token)
        out[f"配置里没有的 AV1：不保存（{said}）"] = (
            said.startswith("alert:没保存：选项不对") and app_settings.get("recording_codec") == "h264")
        real_rules = recorder.load_rules
        two = {**real_rules(), "encoders": {**real_rules()["encoders"], "av1": ["av1_nvenc"]}}
        recorder.load_rules = lambda: two  # 以后加回 AV1 时：这台电脑编不了 AV1
        try:
            box = section(client.get("/settings").get_data(as_text=True))
            codec = re.findall(r'<option value="av1"[^>]*>[^<]*</option>', box)
            _, said = note(client, "/settings/recording/quality", {**good, "codec": "av1"}, token)
        finally:
            recorder.load_rules = real_rules
        out[f"配置里有几种编码格式时显示那一项：显卡编不了的 AV1 不能选、写着原因，提交了也不保存（{codec}，{said}）"] = (
            'id="rec-codec"' in box and len(codec) == 1 and "disabled" in codec[0] and "编不了" in codec[0]
            and said.startswith("alert:没保存：这台电脑的显卡编不了 AV1") and app_settings.get("recording_codec") == "h264")
        bad = [{**good, "size": "4k"}, {**good, "fps": "144"}, {**good, "quality": "ultra"}, {**good, "fps": "abc"}]
        results = [note(client, "/settings/recording/quality", b, token)[1] for b in bad]
        out["乱填的（4k、144 帧、ultra、abc）都不保存"] = (
            all(r.startswith("alert:没保存") for r in results) and app_settings.get("recording_size") == "720p")

        results = {gb: note(client, "/settings/recording/limit", {"gb": gb}, token)[1] for gb in ("0.5", "abc", "20000")}
        out[f"硬盘上限：不到 1、不是数、太大都不保存（{results['0.5']}）"] = (
            all(r.startswith("alert:没保存") for r in results.values()) and app_settings.get("recording_limit_gb") is None)
        box, said = note(client, "/settings/recording/limit", {"gb": "50"}, token)
        out[f"硬盘上限填 50：存成 50（{said}），框里是 50，写着现在几个录像、多大"] = (
            app_settings.get("recording_limit_gb") == 50 and said == "status:保存了" and 'value="50"' in box
            and "2</span> 个录像" in box)

        box, said = note(client, "/settings/recording/folder", {"folder": "录像"}, token)
        out[f"保存位置不是完整路径：不保存，\"更改保存位置\"展开着，填的留在框里，原因写在框下面（{said}）"] = (
            said.startswith("alert:请写完整的路径") and app_settings.get("recording_dir") is None
            and re.search(r'<details class="move-data" id="rec-folder"\s+open>', box) is not None
            and 'name="folder" value="录像"' in box and 'aria-describedby="rec-note-folder"' in box)
        _, said = note(client, "/settings/recording/folder", {"folder": str(panel_recordings.EXE_DIR / "录像")}, token)
        out[f"保存位置在程序文件夹里：不保存（{said}）"] = (
            "程序文件夹" in said and app_settings.get("recording_dir") is None)
        elsewhere = Path(tempfile.mkdtemp(prefix="evo-rec-elsewhere-"))
        box, said = note(client, "/settings/recording/folder", {"folder": f'"{elsewhere}"'}, token)
        out[f"换到别的文件夹：存下（带引号也行），说原来的 2 个录像没搬、还在哪（{said[:60]}…）"] = (
            app_settings.get("recording_dir") == str(elsewhere) and said.startswith("status:保存了")
            and "原来的 2 个录像没有搬" in said and str(base) in said and str(elsewhere) in box
            and "改回默认位置" in box)
        box, said = note(client, "/settings/recording/folder", {"reset": "1"}, token)
        out[f"改回默认位置（{said}）"] = (
            app_settings.get("recording_dir") is None and said.startswith("status:改回默认位置了")
            and "改回默认位置（" not in box)
        shutil.rmtree(elsewhere, ignore_errors=True)

        out.update(highlight_settings_checks(client, token))

        box, said = note(client, "/settings/recording", {"on": "0"}, token)
        out[f"关掉：只剩开关，说录好的还在（{said}）"] = (
            app_settings.get("recording_enabled") is False and "录好的还在" in said and "rec-size" not in box)
    finally:
        recorder.find_ffmpeg, recorder.pick_encoder = real_find, real_pick
    return out


def checks():
    out = {}
    folder = Path(tempfile.mkdtemp(prefix="evo-recordings-"))
    db = folder / "evo.db"
    conn = storage.connect(db)
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    base = panel_recordings.folder()

    # 没打开录像、也没有录像：导航里没有
    app_settings.set_value("recording_enabled", False)
    raw = client.get("/matches").get_data(as_text=True)
    out["没打开录像、也没有录像：导航里没有\"录像\"，战绩页不扫录像文件夹"] = (
        "录像" not in nav_labels(raw) and panel_recordings.match_links([]) == {})
    app_settings.set_value("recording_enabled", True)
    raw = client.get("/recordings").get_data(as_text=True)
    out["打开了录像：导航里\"录像\"在\"战绩\"后面，选中；还没有录像时写说明"] = (
        nav_labels(raw)[:3] == ["总览", "战绩", "录像"] and 'aria-current="page"' in re.findall(
            r'<a href="/recordings"[^>]*>', raw)[0] and "还没有录像" in raw)

    # 两局录像：新的那局对得上战绩，旧的那局是上次崩了拼回来的（没有 .json）
    first = NOW - timedelta(hours=3)
    second = NOW - timedelta(minutes=30)
    old = make_recording(base, first, "未完成", 0, sidecar=False)
    new = make_recording(base, second, "班加罗尔", 120,
                         events=[{"t": 60, "kind": "knocked"}, {"t": 90, "kind": "dead"}, {"t": 100, "kind": "unknown"},
                                 {"t": 30, "kind": "kill", "kills": 2, "count": 2},
                                 {"t": 45, "kind": "assist", "assists": 1, "count": 1}],
                         outages=[[30, 36]])
    linked = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{second + timedelta(seconds=118):%Y-%m-%d %H:%M:%S}",
                                         "placement": 3, "kills": 4, "damage": 1234, "mode": "pubs", "map": "残月",
                                         "legend": "班加罗尔"})
    other = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{NOW - timedelta(days=2):%Y-%m-%d %H:%M:%S}",
                                        "placement": 9, "legend": "恶灵"})
    app_settings.set_value("recording_enabled", False)
    raw = client.get("/recordings").get_data(as_text=True)
    cards = re.findall(r'<article class="rec-card".*?</article>', raw, re.S)
    out["没打开录像但已经有录像：导航里照样有\"录像\""] = "录像" in nav_labels(raw)
    newest = text(cards[0]) if cards else ""
    out[f"列表：从新到旧两张卡片；新的这张有时长 2:00、传奇、对上的战绩（匹配 · 残月、#3、击杀 4、伤害 1,234）、4 个事件（{newest}）"] = (
        len(cards) == 2 and "2:00" in newest and "班加罗尔" in newest and "匹配 · 残月" in newest and "#3" in newest
        and "击杀 4" in newest and "伤害 1,234" in newest and "4 个事件" in newest)
    oldest = text(cards[1]) if len(cards) > 1 else ""
    out["列表：没有 .json 的（上次崩了拼回来的）标\"未完成\"、写\"没对上战绩\""] = "未完成" in oldest and "没对上战绩" in oldest
    out["列表：缩略图按需加载、先占好位置；上面写共多大、上限 30 GB"] = (
        'loading="lazy"' in cards[0] and 'width="480" height="270"' in cards[0] and "上限" in raw and "30 GB" in raw)

    rid = f"{new.parent.name}/{new.stem}"
    raw = client.get("/recordings/watch", query_string={"v": rid, "t": "61.5"}).get_data(as_text=True)
    marks = re.findall(r'<button type="button" class="rec-mark [^"]*"[^>]*>', raw)
    out["播放页：播放器不自动播放、先只读时长；网址带 t= 时从那一刻开始"] = (
        "<video" in raw and "autoplay" not in raw and 'preload="metadata"' in raw and 'data-start="61.5"' in raw)
    out[f"时间轴：认得的事件才标（4 个，按时间），位置按时长算（60 秒在 50%），按钮念得出名字和时刻；一下子两个击杀写'×2'（{marks[:2]}）"] = (
        len(marks) == 4 and "left: 25.000%" in marks[0] and "击杀 ×2，0:30" in marks[0] and "ev-kill" in marks[0]
        and "left: 37.500%" in marks[1] and "助攻，0:45" in marks[1] and "ev-assist" in marks[1]
        and "left: 50.000%" in marks[2] and "倒地，1:00" in marks[2] and "ev-knocked" in marks[2]
        and "left: 75.000%" in marks[3] and "成盒，1:30" in marks[3])
    out["时间轴：断掉的那段（30–36 秒）画出来"] = 'class="rec-gap" style="left: 25.000%; width: 5.000%"' in raw
    events = text("".join(re.findall(r'<ol class="rec-events".*?</ol>', raw, re.S)))
    out[f"事件列表：按时间（{events}）"] = events == "0:30 击杀 ×2 0:45 助攻 1:00 倒地 1:30 成盒"
    out["对上的战绩：名次、击杀、伤害，点了去战绩页展开那一局"] = (
        f'href="/matches?open={linked}"' in raw and "在战绩里看" in raw)
    out["删除要先确认（写着文件名、多大、不能恢复）"] = "data-confirm" in raw and new.name in raw and "不能恢复" in raw

    # 挨得很近的事件合成一个标记（11-21 那局：6:21.8 倒地、6:22.1 助攻击倒；2:50.3 击杀、2:50.6 助攻）
    close = make_recording(base, NOW - timedelta(minutes=50), "挨得近", 617, events=[
        {"t": 160.4, "kind": "knock"}, {"t": 170.3, "kind": "kill", "kills": 1, "count": 1},
        {"t": 170.6, "kind": "assist", "assists": 1, "count": 1}, {"t": 381.8, "kind": "knocked"},
        {"t": 382.1, "kind": "assist_knock"}, {"t": 500, "kind": "dead"}])
    raw = client.get("/recordings/watch", query_string={"v": f"{close.parent.name}/{close.stem}"}).get_data(as_text=True)
    marks = re.findall(r'<button type="button" class="rec-mark [^"]*"[^>]*>.*?</button>', raw, re.S)
    groups = [m for m in marks if "rec-mark-group" in m]
    listed = text("".join(re.findall(r'<ol class="rec-events".*?</ol>', raw, re.S)))
    first, second = (groups + ["", ""])[:2]
    opening = second.split(">")[0]  # 第二个标记的 <button ...>：class 里写着画哪个事件
    out[f"挨得很近的（相隔不到时长的 2.4%，这一局约 15 秒）合成一个标记：6 个事件 3 个标记；画最要紧的那个的图标"
        f"（击杀优先于击倒、助攻；倒地优先于助攻击倒），角上写个数、悬停写出全部、点了从第一个开始（{len(marks)} 个）"] = (
        len(marks) == 3 and len(groups) == 2
        and "ev-kill" in first.split(">")[0] and '>3</span>' in first and 'data-t="160.4"' in first
        and "ev-knocked" in opening and "ev-assist-knock" not in opening and '>2</span>' in second
        and 'data-label="6:21 倒地 · 6:22 助攻击倒"' in second and 'data-t="381.8"' in second
        and "2 个事件：倒地 6:21，助攻击倒 6:22" in second)
    out["每个标记、下面列表的每一项都有图标（不只靠颜色分）"] = (
        all(m.count('class="ev-icon"') == 1 for m in marks)
        and raw.count('class="ev-icon"') == len(marks) + 6)
    out[f"下面的事件列表照样一条一条列，助攻击倒有自己的名字（{listed}）"] = (
        listed.count("助攻击倒") == 1 and listed.count(":") == 6 and "6:22 助攻击倒" in listed)
    for extra in (close, close.with_suffix(".json"), close.with_suffix(".jpg")):
        extra.unlink(missing_ok=True)

    r = client.get("/recordings/video", query_string={"v": rid}, headers={"Range": "bytes=0-9"})
    out[f"录像文件支持拖动：Range 请求回 206 和那一段（{r.status_code}，{len(r.data)} 字节）"] = (
        r.status_code == 206 and r.data == bytes(range(10)) and r.headers.get("Content-Type", "").startswith("video/mp4"))
    r = client.get("/recordings/thumb", query_string={"v": rid})
    out["缩略图：录像旁边的 .jpg"] = r.status_code == 200 and r.data.startswith(b"\xff\xd8")
    work = base / "_recording" / "x"
    work.mkdir(parents=True, exist_ok=True)
    (work / "part.mp4").write_bytes(b"0")
    outside = folder / "secret.mp4"
    outside.write_bytes(b"0")
    bad = ["../secret", "..\\..\\secret", "_recording/part", "2026-11/../../secret", rid + "/x", ""]
    codes = [client.get("/recordings/video", query_string={"v": b}).status_code for b in bad]
    watch_bad = client.get("/recordings/watch", query_string={"v": "../secret"})
    out[f"网址不能指到录像文件夹以外、分段文件夹里的文件（{codes}）"] = (
        all(c == 404 for c in codes) and watch_bad.status_code == 302)

    raw = client.get("/matches").get_data(as_text=True)
    rows = {int(mid): row for mid, row in re.findall(r'<tr id="m(\d+)"(.*?)</tr>', raw, re.S)}  # 每一局那一行
    link = re.findall(r'class="rec-link" href="([^"]+)"', rows.get(linked, ""))
    out["战绩页：有录像的局多一个\"看录像\"按钮，指到那个录像；没录像的局没有"] = (
        len(link) == 1 and link[0].startswith("/recordings/watch?v=") and "rec-link" not in rows.get(other, "-")
        and "看第" in rows.get(linked, ""))

    out.update(settings_checks(client, token, base))
    out.update(clip_checks(client, token, base, conn))
    out.update(show_checks(client, token, base))

    r = client.post("/recordings/delete", data={"v": rid})
    still = new.exists()
    r = client.post("/recordings/delete", data={"v": rid, "token": token})
    out["删除：没有口令不让删；有口令时 MP4、.json、.jpg 一起删，回到列表"] = (
        still and r.status_code == 302 and r.headers["Location"].endswith("/recordings") and not new.exists()
        and not new.with_suffix(".json").exists() and not new.with_suffix(".jpg").exists() and old.exists())
    conn.close()
    return report(out)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
