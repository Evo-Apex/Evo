"""Evo 窗口的看直播页（panel_esports.py、esports_watch.html）。不联网：赛事文件是临时文件夹里的一个文件，
查开播状态、Apex 在不在运行都换成假的。页面上的脚本（点亮在播的、切换选手、放播放器）在浏览器里看，这里测服务器给的东西。

- 页面：占满窗口、没有侧边栏，有"← 赛事"；赛事名、阶段；正在进行的写"进行中"，没开始的写什么时候开始；
  三个去平台的按钮、同队视角、延迟的说明、Apex 提示条（先藏着）；没有"上一位 / 下一位"
- 右边：每支队一栏（按赛程里的顺序，队标的位置是队名第一个字），每位选手一行，写着平台，先都不能点；
  "全部 N 支 / 有人在播的"；最下面写每 60 秒刷新
- 给脚本的数据：每位选手的直播间地址只按配置拼；只有 B站有嵌入播放器的地址；网址里的 p 是名单里的人才选中；
  名字里的尖括号不会变成网页的一部分
- 开播状态接口：只查这场赛事名单里的人；50 秒内再问不重查；带上平台开关；头像只要 https 的；
  没有这场赛事回 404（页面据此回赛事页）
- Twitch（2026-11-25）：名单里的那一行照样能点、带着问播放地址和弹幕的接口；页面有它的弹幕（IRC over WebSocket，
  匿名只读）和"连不上"那一行说明（role=status）；开播状态带着 unreachable；连不上时那一格的话（用户定的）和"重试"：retry=1 马上重查
- 页面播放器 mpegts.js 带着 Evo 的补丁（tools/mpegts_patch.py：Twitch 的视频 PES 没对齐到帧开头时把开头那截接回上一帧），补丁工具找不到地方就报错
- Apex 在不在运行的接口
- 别的页面照旧有侧边栏（导航和主题按钮）

运行：python tests/test_esports_watch.py
"""

import html
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import esports_data
import game_detect
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)
TEAMS = ("甲队", "<b>乙队</b>", "丙队")


STREAMERS = [{"id": "douyu-901", "name": "大主播", "platform": "douyu", "room": "901"},
             {"id": "huya-902", "name": "<i>二主播</i>", "platform": "huya", "room": "902"},
             {"id": "bilibili-903", "name": "三主播", "platform": "bilibili", "room": "903"}]


def serve(folder, platforms=None, teams=None, main=None, off=False, streamers=None, observers=None, nobody=False):
    """observers：老文件里"观战的人"那一项（2026-11-21 去掉了，Evo 不再读；测试用它看是不是真的不读）。"""
    players = []
    for team, platform in zip(TEAMS, ("douyu", "bilibili", "douyin")):
        players += [{"id": f"{platform}-{n}", "name": f"{team[:1]}{n}号" if n > 1 else "kite</script>", "team": team,
                     "platform": platform if n < 3 else "huya", "room": str(100 + n)} for n in (1, 2, 3)]
    events = [{"id": "live", "name": "职业联赛 第 3 周", "type": "联赛", "stage": "B 组对 C 组",
               "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=3)).isoformat(),
               "teams": ["丙队", "甲队", "<b>乙队</b>", "没有人的队"]},
              {"id": "soon", "name": "明天的训练赛", "type": "训练赛", "stage": "",
               "start": (NOW + timedelta(days=1)).isoformat(), "end": (NOW + timedelta(days=1, hours=3)).isoformat(),
               "teams": ["甲队"]}]
    if main:
        events[0]["main"] = main
    if observers:
        events[0]["observers"] = observers
    if nobody:  # 选手库是空的（只看另外加的主播时用）
        players = []
    if off:  # 甲队的第三个人不开直播
        players[2].update(id="none-0123456789ab", platform="none", room="")
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": events,
                                "platforms": platforms or {}, **({"teams": teams} if teams else {}),
                                **({"streamers": streamers} if streamers else {})}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})


def text(page):
    """网页上看得到的字（去掉标签和脚本，&lt; 这类还原成原来的字）。"""
    return html.unescape(" ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", page, flags=re.S)).split()))


def page_data(page):
    found = re.search(r'<script type="application/json" id="watch-data">(.*?)</script>', page, re.S)
    return json.loads(found.group(1)) if found else {}


def main():
    print("看直播页（页面、名单、给脚本的数据、开播状态接口、Apex 接口）")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-watch-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    serve(folder, {"douyin": False})

    raw = client.get("/esports/live/watch").get_data(as_text=True)
    words = text(raw)
    head = re.findall(r'<div class="watch-head">.*?</div>', raw, re.S)[0]
    out["占满窗口、没有侧边栏（导航、主题按钮），有\"← 赛事\"能回去；写着赛事名、阶段和\"进行中\""] = (
        '<nav class="nav"' not in raw and 'id="theme-toggle"' not in raw and 'id="side"' not in raw
        and 'class="wrap watch-wrap"' in raw
        and 'href="/esports"' in head and text(head).split() == ["电竞", "职业联赛", "第", "3", "周", "B", "组对", "C", "组", "进行中", "单屏", "双屏", "三屏", "四屏", "四宫格"]
        and "es-live-tag" in head)
    out["多屏同看：顶上有\"单屏 / 双屏 / 三屏 / 四屏 / 四宫格\"（默认单屏）；播放器那一块的格子由脚本画，每格问播放地址时带着是第几格"] = (
        [(m, on) for m, on in re.findall(r'aria-pressed="(true|false)" data-layout="(\d)"', head)]
        == [("true", "1"), ("false", "2"), ("false", "3"), ("false", "4"), ("false", "4")]
        and head.count('data-grid="1"') == 1 and 'data-layout="4" data-grid="1" title="四格一样大（2×2）">四宫格</button>' in head
        and '<div class="watch-stage-box n1">' in raw and 'id="watch-screen"></div>' in raw and "function makeCell(index)" in raw
        and 'p.play + "?s=" + cell.index' in raw and 'data-filter="all"' in raw and '"[data-filter]"' in raw)
    css = (Path(panel_app.__file__).parent.parent / "panel" / "static" / "style.css").read_text(encoding="utf-8")
    out["三屏（一大两小）、四屏（一大三小）：选中的那一格是大屏（只换样式里的位置，格子不动）；点小格子控制条上的按钮不换"] = (
        ".watch-stage-box.n3 .watch-cell.focus { grid-column: 1; grid-row: 1 / span 2; }" in css
        and ".watch-stage-box.n3 { aspect-ratio: 8 / 3; }" in css
        and ".watch-stage-box.n4:not(.grid) .watch-cell.focus { grid-column: 1; grid-row: 1 / span 3; }" in css
        and ".watch-stage-box.n4:not(.grid) { aspect-ratio: 64 / 27; }" in css
        and ".watch-stage-box:is(.n3, .n4:not(.grid)) .watch-cell:not(.focus) { grid-column: 2; }" in css
        and "function big() { return st.layout === 3 || (st.layout === 4 && !st.grid); }" in raw
        and 'if (big() && e.target.closest && e.target.closest(".watch-controls")) return;' in raw
        and 'big() ? "换到大屏" : "选这一格"' in raw)
    out["四宫格（2×2，四格一样大：原来的四屏）还留着，和四屏（一大三小）是两个按钮"] = (
        ".watch-stage-box.n2 .watch-screen, .watch-stage-box.n4.grid .watch-screen { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in css
        and 'st.grid = count === 4 && !!grid;' in raw and '(st.grid ? " grid" : "")' in raw
        and 'setLayout(parseInt(btn.dataset.layout, 10), !!btn.dataset.grid);' in raw)
    out["控制条平时藏着：鼠标在画面上动一下才出来，4 秒没动、移出画面藏起来；暂停、菜单开着、鼠标停在上面、键盘选到时不藏"] = (
        'box.addEventListener("mousemove", showBar);' in raw and 'box.addEventListener("mouseleave", hideBar);' in raw
        and "idle = setTimeout(hideBar, 4000);" in raw
        and 'return video.paused || !wait.hidden || !menu.hidden || (!!look && !look.hidden)' in raw
        and '|| bar.matches(":hover") || !!bar.querySelector(":focus-visible");' in raw
        and ".watch-player.ui-on .watch-controls, .watch-controls:has(:focus-visible) { opacity: 1; pointer-events: auto; }" in css
        and ".watch-player:not(.ui-on) { cursor: none; }" in css)
    out["控制条上播放 / 暂停旁边有\"刷新\"：这一格重新连（四屏的小格子收起来）"] = (
        'bar.append(playBtn, reloadBtn, muteBtn, volume, el("span", "grow"));' in raw
        and 'reloadBtn.addEventListener("click", function () { cell.key = null; render(); });' in raw
        and ".watch-stage-box.n4:not(.grid) .watch-cell:not(.focus) :is(.watch-mute-btn, .watch-reload-btn) { display: none; }" in css)
    out["多屏时点哪一格的全屏，就只放大那一格（并选中它，声音跟过去）；单屏照旧是播放器那一块"] = (
        "var target = multi() ? cell.node : stageBox;" in raw
        and "if (multi() && st.focus !== cell.index) focusCell(cell.index);" in raw
        and ".watch-cell:fullscreen { width: 100vw; height: 100vh; background: #050506; }" in css
        and ".watch-cell:fullscreen .watch-cell-tools, .watch-stage-box.multi .watch-cell:fullscreen::after { display: none; }" in css
        and ".watch-stage-box:fullscreen" in css and "stageBox.requestFullscreen();  // 多屏时几格一起全屏" not in raw)
    out["顶上那一行最右边能收起 / 展开右边那一栏，记在这台电脑上，画出来之前就收好；收起了播放器变大但不高过窗口"] = (
        '<button type="button" class="watch-side-toggle" id="watch-side-toggle" aria-controls="watch-side" aria-expanded="true" title="收起右边的队伍栏"' in raw
        and '<aside class="watch-side" id="watch-side"' in raw
        and 'if (localStorage.getItem("evo-watch-side") === "0") document.currentScript.parentNode.classList.add("side-off");' in raw
        and 'save("evo-watch-side", on ? "1" : "0");' in raw
        and ".watch.side-off .watch-side { display: none; }" in css
        and ".watch.side-off .watch-stage-box:not(:fullscreen) { align-self: center; --stage-h: max(270px, calc(100vh - 110px)); }" in css)
    out["多屏只有选中的那一格有声音（四屏的大屏也是）"] = (
        "cell.video.muted = off || (multi() && cell.index !== st.focus);" in raw)
    soon = client.get("/esports/soon/watch").get_data(as_text=True)
    start = NOW + timedelta(days=1)
    soon_head = re.findall(r'<div class="watch-head">.*?</div>', soon, re.S)[0]
    out["没开始的赛事也能进来：右上角写什么时候开始，不写\"进行中\""] = (
        f"{panel_esports.day_text(start, NOW)} {start:%H:%M} 开始" in text(soon_head) and "进行中" not in soon_head
        and "es-live-tag" not in soon_head and len(page_data(soon)["players"]) == 3)
    out["三个去平台的按钮（用浏览器打开）、同队视角；没有延迟的说明（用户删了）、没有\"上一位 / 下一位\""] = (
        all(word in words for word in ("发送弹幕", "给主播送礼物", "同队视角")) and "延迟" not in words
        and raw.count('target="_blank" rel="noopener noreferrer"') == 4  # 三个去平台的按钮，加最下面的官网
        and "上一位" not in raw and "下一位" not in raw)
    brand = re.findall(r'<footer class="watch-brand">.*?</footer>', raw, re.S)
    out["最下面一行：Evo 的标志、标语\"APEX 爱好者\"和官网的网址（用浏览器打开）"] = (
        len(brand) == 1 and text(brand[0]) == "EVO evotracker.cn APEX 爱好者" and 'fill="#3B9EFF"' in brand[0]  # 原来的图标（蓝色的一横），网址紧挨着
        and 'href="https://evotracker.cn/" target="_blank" rel="noopener noreferrer"' in brand[0])
    apex = re.findall(r'<div class="watch-apex"[^>]*>.*?</div>', raw, re.S)[0]
    out["Apex 提示条先藏着，写着会占用显卡，有\"继续看\"和\"关闭直播\""] = (
        " hidden" in apex.split(">")[0] and "Apex 正在运行。看直播会占用显卡，可能让游戏掉帧。" in text(apex)
        and text(apex).endswith("继续看 关闭直播"))

    boxes = re.findall(r'<details class="watch-team-box".*?</details>', raw, re.S)
    names = [text(re.findall(r'<span class="watch-team-name[^"]*">(.*?)</span>', box, re.S)[0]) for box in boxes]
    logos = [text(re.findall(r'<span class="watch-team-logo"[^>]*>(.*?)</span>', box, re.S)[0]) for box in boxes]
    buttons = re.findall(r'<button type="button" class="watch-p"[^>]*>', raw)
    side = re.findall(r'<aside class="watch-side".*?</aside>', raw, re.S)[0]
    out["右边每支队一栏，按赛程里的顺序，队标的位置是队名第一个字；没有选手的队也在"] = (
        names == ["丙队", "甲队", "<b>乙队</b>", "没有人的队"] and logos == ["丙", "甲", "<", "没"]
        and "名单里还没有这支队的选手" in text(boxes[3]))
    out["每位选手一行，写着平台，查到在播之前都不能点"] = (
        len(buttons) == 9 and all(" disabled" in b and 'aria-pressed="false"' in b for b in buttons)
        and text(boxes[1]).count("斗鱼") == 2 and text(boxes[1]).count("虎牙") == 1 and "kite</script>" in text(boxes[1]))
    out["\"全部 N 支 / 有人在播的\"、在播人数的位置、最下面写每 60 秒刷新"] = (
        "全部 4 支" in text(side) and "有人在播的" in text(side) and 'id="watch-count"' in side
        and "开着这一页时每 60 秒刷新一次谁在播。" in text(side))

    data = page_data(raw)
    by_id = {p["id"]: p for p in data.get("players", [])}
    out["给脚本的数据：直播间地址按配置拼；只有 B站有嵌入播放器的地址；带上平台开关和两个接口的地址"] = (
        list(by_id) == [f"{p}-{n}" for p in ("douyin", "douyu", "bilibili") for n in (1, 2, 3)]
        and by_id["douyu-1"]["url"] == "https://www.douyu.com/101" and by_id["douyu-3"]["url"] == "https://www.huya.com/103"
        and by_id["bilibili-2"]["embed"] == "https://www.bilibili.com/blackboard/live/live-activity-player.html?cid=102&quality=1"
        and [p["id"] for p in by_id.values() if p["embed"]] == ["bilibili-1", "bilibili-2"]
        and data["platforms"] == {"douyu": True, "huya": True, "bilibili": True, "douyin": False, "twitch": True}
        and data["status_url"] == "/esports/live/status" and data["game_url"] == "/esports/game")
    picked = page_data(client.get("/esports/live/watch?p=bilibili-2").get_data(as_text=True))
    wrong = page_data(client.get("/esports/live/watch?p=nobody").get_data(as_text=True))
    out["网址里的 p 是名单里的选手才选中；不是的话不选"] = (
        data["selected"] is None and picked["selected"] == "bilibili-2" and wrong["selected"] is None)
    out["名字里的尖括号不会变成网页的一部分"] = (
        "kite</script>" not in raw and "<b>乙队</b>" not in raw and by_id["douyu-1"]["name"] == "kite</script>"
        and by_id["bilibili-1"]["team"] == "<b>乙队</b>")

    asked = []

    def fake_query(players):
        asked.append(sorted(p["id"] for p in players))
        return {p["id"]: live_status.result(
            "live" if p["id"].endswith("-1") else "error" if p["id"] == "douyu-2" else "offline", name="平台上的名字",
            title="比赛日", since=1794900000 if p["id"].endswith("-1") else None,
            avatar={"douyu-1": "http://apic.douyucdn.cn/a.jpg", "bilibili-1": "javascript:alert(1)",
                    "douyin-1": "https://p3.douyinpic.com/a.jpeg"}.get(p["id"], ""),
            error="连不上" if p["id"] == "douyu-2" else "") for p in players}

    real_query, live_status.query = live_status.query, fake_query
    real_game = game_detect.game_running
    try:
        got = client.get("/esports/live/status").get_json()
        again = client.get("/esports/live/status").get_json()
        players = got["players"]
        out["开播状态接口：只查这场赛事名单里的人，回每个人在不在播、标题、开播时刻；不把没查到的原因发给页面"] = (
            asked == [sorted(by_id)] and players["douyu-1"] == {"state": "live", "loop": False, "title": "比赛日",
                                                              "since": 1794900000, "avatar": "https://apic.douyucdn.cn/a.jpg",
                                                              "unreachable": False}
            and players["douyu-2"]["state"] == "error" and players["douyu-3"]["state"] == "offline"
            and "连不上" not in json.dumps(got, ensure_ascii=False))
        out["头像只要 https 的（http 的换成 https），别的不要"] = (
            players["douyu-1"]["avatar"] == "https://apic.douyucdn.cn/a.jpg" and players["bilibili-1"]["avatar"] == ""
            and players["douyin-1"]["avatar"] == "https://p3.douyinpic.com/a.jpeg")
        out["带上平台开关（esports.json 里关掉的平台，页面改成去平台看）"] = got["platforms"]["douyin"] is False
        out["50 秒内再问不重查"] = len(asked) == 1 and again["players"] == players
        panel_esports._status["at"] -= 51
        client.get("/esports/live/status")
        out["页面每 60 秒问一次，那时会重查（开播、下播后 2 分钟内页面跟着变）"] = len(asked) == 2
        missing = client.get("/esports/no-such/status")
        out["没有这场赛事：回 404（页面据此回赛事页），不去查直播间"] = missing.status_code == 404 and len(asked) == 2
        game_detect.game_running = lambda: True
        on = client.get("/esports/game").get_json()
        game_detect.game_running = lambda: False
        off = client.get("/esports/game").get_json()
        out["Apex 在不在运行的接口（只看进程名）"] = on == {"running": True} and off == {"running": False}
    finally:
        live_status.query, game_detect.game_running = real_query, real_game

    serve(folder, teams={"甲队": {"logo": "3fa9c1d2e4b5a6f7.webp"}, "<b>乙队</b>": {"logo": "https://example.invalid/x.png"}})
    with_logo = client.get("/esports/live/watch").get_data(as_text=True)
    marks = [m for m in re.findall(r'<span class="watch-team-logo[^"]*"[^>]*>.*?</span>', with_logo, re.S)
             if "is-fav" not in m]  # 名单最上面"我关注的"一组的星星不是队徽
    out["有队徽的队显示队徽（图片只从 dl.evotracker.cn 的 esports/logos/ 加载，加载不出来时页面换回第一个字）；没有的、写得不对的照旧是队名第一个字"] = (
        len(marks) == 4 and sum("<img " in m for m in marks) == 1
        and 'class="watch-team-logo has-img"' in marks[1] and 'data-initial="甲"' in marks[1]
        and 'src="https://dl.evotracker.cn/esports/logos/3fa9c1d2e4b5a6f7.webp"' in marks[1]
        and 'referrerpolicy="no-referrer"' in marks[1] and 'alt=""' in marks[1]
        and [text(m) for m in (marks[0], marks[2], marks[3])] == ["丙", "<", "没"] and "example.invalid" not in with_logo)
    serve(folder, main=[{"id": "bilibili-900", "name": "官方直播", "platform": "bilibili", "room": "900"},
                        {"id": "douyin-xxfps", "name": "<i>解说</i>", "platform": "douyin", "room": "xxfps"},
                        {"id": "douyu-1", "name": "和选手同一个 id", "platform": "douyu", "room": "101"}])
    staged = client.get("/esports/live/watch").get_data(as_text=True)
    stage_boxes = [box for box in re.findall(r'<details class="watch-team-box[^>]*>.*?</details>', staged, re.S)
                   if "data-fav" not in box.split(">", 1)[0]]  # "我关注的"一组不算
    stage_data = page_data(staged)
    out["赛程里写了主舞台：右边最上面多一栏\"主舞台\"（默认展开、图标不是队名的字），下面照旧是队伍；\"全部 N 支\"不数它"] = (
        len(stage_boxes) == 5 and 'data-main open' in stage_boxes[0] and 'class="watch-team-logo is-main"' in stage_boxes[0]
        and "主舞台" in text(stage_boxes[0]) and "官方直播" in text(stage_boxes[0]) and "<i>解说</i>" in text(stage_boxes[0])
        and "<i>解说</i>" not in staged and len(re.findall(r'<button type="button" class="watch-p"', stage_boxes[0])) == 2
        and all("data-main" not in box for box in stage_boxes[1:]) and "全部 4 支" in text(staged)
        and len(re.findall(r'<details class="watch-team-box"[^>]*>', raw)) == 4 and "data-main" not in raw.split("<script>")[0])
    out["给脚本的数据：主舞台的两路排在最前面、标着是主舞台；和选手同一个房间的那一路不算；网址里的 p 也能选主舞台"] = (
        [(p["id"], p["main"], p["team"]) for p in stage_data["players"][:3]] == [
            ("bilibili-900", True, "主舞台"), ("douyin-xxfps", True, "主舞台"), ("douyin-1", False, "丙队")]
        and len(stage_data["players"]) == 11 and stage_data["players"][0]["url"] == "https://live.bilibili.com/900"
        and stage_data["players"][1]["url"] == "https://live.douyin.com/xxfps"
        and page_data(client.get("/esports/live/watch?p=douyin-xxfps").get_data(as_text=True))["selected"] == "douyin-xxfps")
    asked_ids = []
    real_query = live_status.query
    live_status.query = lambda players, get=None: (asked_ids.extend(p["id"] for p in players) or
                                                   {p["id"]: live_status.result("offline") for p in players})
    try:
        panel_esports._status.update(at=0.0, found={})
        got = client.get("/esports/live/status").get_json()
        count = client.get("/esports/live").get_json()
    finally:
        live_status.query = real_query
        panel_esports._status.update(at=0.0, found={})
    out["查开播状态时主舞台的直播间也查；赛事页\"几位选手在播\"不数主舞台"] = (
        asked_ids[:2] == ["bilibili-900", "douyin-xxfps"] and len(got["players"]) == 11
        and count["events"]["live"]["total"] == 9)
    serve(folder, off=True)
    quiet = client.get("/esports/live/watch").get_data(as_text=True)
    quiet_data = page_data(quiet)
    row = [b for b in re.findall(r'<button type="button" class="watch-p".*?</button>', quiet, re.S) if "none-0123456789ab" in b]
    silent = {p["id"]: p for p in quiet_data["players"]}.get("none-0123456789ab", {})
    out["不开直播的选手照样列在队里：一行、不能点，右边写\"不直播\"，没有平台的色点；不能在窗口里播、没有直播间地址"] = (
        len(row) == 1 and " disabled" in row[0] and "不直播" in text(row[0]) and "es-dot" not in row[0]
        and len(re.findall(r'<button type="button" class="watch-p"', quiet)) == 9
        and silent.get("off") is True and silent.get("platform_name") == "不直播" and silent.get("url") is None
        and silent.get("play") is None and silent.get("embed") is None
        and sum(p["off"] for p in quiet_data["players"]) == 1
        and page_data(client.get("/esports/live/watch?p=none-0123456789ab").get_data(as_text=True))["selected"] is None)
    asked_ids = []
    real_query = live_status.query
    live_status.query = lambda players, get=None: (asked_ids.extend(p["id"] for p in players) or
                                                   {p["id"]: live_status.result("offline") for p in players})
    try:
        panel_esports._status.update(at=0.0, found={})
        got = client.get("/esports/live/status").get_json()
        count = client.get("/esports/live").get_json()
        gone = client.get("/esports/play/none-0123456789ab").status_code
    finally:
        live_status.query = real_query
        panel_esports._status.update(at=0.0, found={})
    out["不开直播的选手不去查开播状态，\"几位选手在播\"不数他，也问不到播放地址"] = (
        "none-0123456789ab" not in asked_ids and len(asked_ids) == 8 and len(got["players"]) == 8
        and count["events"]["live"]["total"] == 8 and gone == 404)
    # ---- 主播的看直播页（2026-11-19）----
    serve(folder, observers=[{"id": "bilibili-1", "team": "丙队"}, {"id": "douyin-2", "team": "甲队"}])
    seen = client.get("/esports/live/watch?p=bilibili-1").get_data(as_text=True)
    seen_data = page_data(seen)
    seen_side = seen[seen.find('<aside class="watch-side"'):seen.find("</aside>")]
    boxes = {re.search(r'class="watch-team-name es-ell">(.*?)</span>', box, re.S).group(1): box
             for box in re.findall(r"<details.*?</details>", seen_side, re.S)}
    order = [(p["id"], p["team"]) for p in seen_data["players"]]
    out["\"观战\"去掉了（2026-11-21）：老赛程里写了观战的人也不读，每位都在自己的队里，页面上没有\"观战\"的标签"] = (
        order[:6] == [("douyin-1", "丙队"), ("douyin-2", "丙队"), ("douyin-3", "丙队"), ("douyu-1", "甲队"), ("douyu-2", "甲队"), ("douyu-3", "甲队")]
        and len(order) == 9 and all("ob" not in p for p in seen_data["players"]) and seen_data["selected"] == "bilibili-1"
        and "watch-ob" not in seen and "观战" not in text(seen_side)
        and boxes["丙队"].count('class="watch-p"') == 3 and boxes["&lt;b&gt;乙队&lt;/b&gt;"].count('class="watch-p"') == 3)
    # ---- 主舞台最多 24 路（2026-11-21）----
    serve(folder, main=[{"id": f"bilibili-9{n:02d}", "name": f"第 {n} 路", "platform": "bilibili", "room": f"9{n:02d}"} for n in range(1, 31)])
    wide = client.get("/esports/live/watch").get_data(as_text=True)
    stage = re.search(r'<details class="watch-team-box watch-main-box".*?</details>', wide, re.S).group(0)
    out["主舞台最多 24 路：多出来的不要；这一栏最下面有一行\"没开播的 N 路\"（脚本填字，在播的排前面、没开播的收起来，点了才列出来）"] = (
        stage.count('class="watch-p"') == 24 and stage.count("data-main-more") == 1 and 'aria-expanded="false" hidden' in stage
        and sum(1 for p in page_data(wide)["players"] if p["main"]) == 24
        and "var MAIN_FOLD = 4;" in wide and "st.mainOpen = !st.mainOpen; renderList();" in wide
        and wide.count("data-main-more aria") == 1)
    serve(folder)
    out["没有主舞台的赛事没有\"没开播的 N 路\"那一行"] = (
        "data-main-more aria" not in client.get("/esports/live/watch").get_data(as_text=True))
    serve(folder, nobody=True)
    none = client.get("/esports/streamers")
    out["名单里一位开直播的都没有：主播的看直播页回电竞页并说明，状态接口回 404"] = (
        none.status_code == 302 and none.headers["Location"].endswith("/esports")
        and "现在没有主播名单" in client.get("/esports").get_data(as_text=True)
        and client.get("/esports/streamers/status").status_code == 404)
    serve(folder, streamers=STREAMERS)
    everyone = page_data(client.get("/esports/streamers?p=douyu-1").get_data(as_text=True))
    out["主播的看直播页列的是名单里所有开直播的选手（带着队名），后面接另外加的主播；选手也能选来看"] = (
        [p["id"] for p in everyone["players"]][:3] == ["douyu-1", "douyu-2", "douyu-3"] and len(everyone["players"]) == 12
        and everyone["players"][0]["team"] == "甲队" and everyone["players"][-1]["id"] == "bilibili-903"
        and everyone["players"][-1]["team"] == "" and everyone["selected"] == "douyu-1")
    serve(folder, streamers=STREAMERS, nobody=True)
    solo = client.get("/esports/streamers?p=huya-902").get_data(as_text=True)
    side = solo[solo.find('<aside class="watch-side"'):solo.find("</aside>")]
    rows = re.findall(r'<button type="button" class="watch-p".*?</button>', side, re.S)
    info = page_data(solo)
    out["主播的看直播页：和赛事的是同一个页面，标题写\"主播\"、能回电竞页；右边不分队伍，一位一行、写着平台，先都不能点"] = (
        "<title>主播 · Evo</title>" in solo and 'href="/esports"' in solo and "watch-mark" not in solo[:solo.find("<aside")]
        and side.count("<details") == 1 and 'id="watch-fav" data-fav' in side  # 只有"我关注的"那一组能折叠
        and 'data-solo' in side and len(rows) == 3 and all(" disabled" in row for row in rows)
        and [text(row).split()[-2] for row in rows] == ["斗鱼", "虎牙", "B站"] and "<i>二主播</i>" not in side
        and text(side).startswith("主播 正在看谁在播… 全部 3 位 在播的") and "现在没有主播在播。" in side)
    out["给脚本的数据：是主播名单（solo）、页面上称呼\"主播\"、问状态的地址是主播的；网址里的 p 是名单里的主播才选中；能播的平台带着问播放地址的接口"] = (
        info.get("solo") is True and info.get("noun") == "主播" and info.get("status_url") == "/esports/streamers/status"
        and info.get("selected") == "huya-902" and [p["id"] for p in info["players"]] == [s["id"] for s in STREAMERS]
        and all(p["team"] == "" and not p["main"] and not p["off"] for p in info["players"])
        and info["players"][0]["play"] == "/esports/play/douyu-901" and info["players"][1]["play"] is None
        and info["players"][1]["url"] == "https://www.huya.com/902"
        and page_data(client.get("/esports/streamers?p=douyu-101").get_data(as_text=True)).get("selected") is None)
    event_page = page_data(client.get("/esports/live/watch").get_data(as_text=True))
    out["赛事的看直播页照旧：不是 solo，称呼\"选手\"，右边还是队伍"] = (
        event_page.get("solo") is False and event_page.get("noun") == "选手" and event_page.get("status_url") == "/esports/live/status")
    asked_ids = []

    def streamer_query(players):
        asked_ids.extend(p["id"] for p in players)
        return {p["id"]: live_status.result("live" if p["id"] == "douyu-901" else "offline", title="今晚上分") for p in players}

    real_query, live_status.query = live_status.query, streamer_query
    try:
        got = client.get("/esports/streamers/status").get_json()
    finally:
        live_status.query = real_query
        panel_esports._status.update(at=0.0, found={})
    out["主播的状态接口：只查主播名单里的房间，格式和赛事的一样"] = (
        sorted(asked_ids) == sorted(s["id"] for s in STREAMERS) and set(got) == {"checked", "platforms", "players"}
        and got["players"]["douyu-901"]["state"] == "live" and got["players"]["douyu-901"]["title"] == "今晚上分"
        and got["players"]["huya-902"]["state"] == "offline")
    out["主播的直播间也能问播放地址（按 id 找得到）：能在窗口里播的平台不回 404，虎牙、名单外的回 404"] = (
        panel_esports._player("douyu-901") == STREAMERS[0] and client.get("/esports/play/huya-902").status_code == 404
        and client.get("/esports/play/douyu-999").status_code == 404)
    serve(folder)
    home = client.get("/esports").get_data(as_text=True)
    out["别的页面照旧有侧边栏（导航和主题按钮）"] = '<nav class="nav"' in home and 'id="theme-toggle"' in home and 'id="side"' in home
    # ---- Twitch（2026-11-25）----
    now = datetime.now().astimezone()
    twitch_players = [{"id": "twitch-imperialhal__", "name": "ImperialHal", "team": "甲队", "platform": "twitch",
                       "room": "imperialhal__"},
                      {"id": "douyu-101", "name": "Kite", "team": "甲队", "platform": "douyu", "room": "101"}]
    twitch_event = {"id": "live", "name": "示例联赛", "type": "联赛", "stage": "",
                    "start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=2)).isoformat(),
                    "teams": ["甲队"]}
    (Path(folder) / "esports.json").write_text(
        json.dumps({"version": 1, "updated": now.isoformat(), "players": twitch_players, "events": [twitch_event]}),
        encoding="utf-8")
    esports_data.URL = str(Path(folder) / "esports.json")
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})
    twitch_page = client.get("/esports/live/watch").get_data(as_text=True)
    twitch_data = page_data(twitch_page)
    twitch = next(p for p in twitch_data["players"] if p["platform"] == "twitch")
    out["Twitch：名单里的那一行能在页面里点（脚本按在播点亮），带着问播放地址和弹幕的接口；直播间地址按配置拼"] = (
        twitch["play"] == "/esports/play/twitch-imperialhal__"
        and twitch["danmaku"] == "/esports/danmaku/twitch-imperialhal__"
        and twitch["url"] == "https://www.twitch.tv/imperialhal__" and twitch["embed"] is None
        and 'data-player="twitch-imperialhal__"' in twitch_page and '<span class="es-dot pf-twitch"' in twitch_page)
    out["页面上的 Twitch 弹幕（IRC over WebSocket，匿名只读）和\"连不上\"那一行说明、退路里的那句话"] = (
        "function twitchRead(cell, chat, socket, text)" in twitch_page
        and 'socket.send("CAP REQ :twitch.tv/tags");' in twitch_page
        and 'socket.send("PASS SCHMOOPIIE");' in twitch_page
        and 'socket.send("NICK justinfan" + (1000 + Math.floor(Math.random() * 99000)));' in twitch_page
        and 'socket.send("JOIN #" + found.room);' in twitch_page
        and 'found.platform === "twitch"' in twitch_page
        and 'id="watch-unreachable" role="status" hidden>连不上 Twitch：在国内要能访问国外网站' in twitch_page
        and 's && s.unreachable ? "连不上"' in twitch_page
        and "Twitch 在国内要能访问国外网站" in twitch_page
        and 'message("啊哦，看起来你不具备看Twitch的“环境”，重试一下吧", "你知道怎么做的，对吧")' in twitch_page
        and "return twitchDown(" in twitch_page and 'el("button", "watch-primary", "重试")' in twitch_page
        and '"retry=1"' in twitch_page)
    asked = []

    def twitch_query(players):
        asked.extend(p["id"] for p in players)
        return {p["id"]: live_status.result("error" if p["platform"] == "twitch" else "live",
                                            unreachable=p["platform"] == "twitch") for p in players}

    def twitch_live(players):
        return {p["id"]: live_status.result("live", since=1794900000) for p in players}

    real_query = live_status.query
    try:
        live_status.query = twitch_live
        panel_esports._status.update(at=0.0, found={})
        on_air = client.get("/esports/live/status").get_json()
        live_status.query = twitch_query
        panel_esports._status.update(at=0.0, found={})
        status = client.get("/esports/live/status").get_json()
        # 玩家点了"重试"（retry=1）：忘掉 Twitch 的"连不上"、不用 50 秒内查过的，马上重查
        asked.clear()
        client.get("/esports/live/status")  # 刚查过：用上次的，不重查
        cached_asked = list(asked)
        live_status._twitch_down[0] = time.time() + 999
        client.get("/esports/live/status?retry=1")
        retried = (sorted(asked) == ["douyu-101", "twitch-imperialhal__"] and live_status._twitch_down[0] == 0.0)
    finally:
        live_status.query = real_query
        live_status._twitch_down[0] = 0.0
        panel_esports._status.update(at=0.0, found={})
    out["Twitch 连不上时点\"重试\"（retry=1）：忘掉刚才的连不上，不用 50 秒内查过的、马上重查；不带 retry 照旧用上次的"] = (
        cached_asked == [] and retried)
    out["Twitch 连不上时开播状态带着 unreachable（别的平台照旧 False），页面据此写\"连不上\"和那一行说明"] = (
        status["players"]["twitch-imperialhal__"]["unreachable"] is True
        and status["players"]["douyu-101"]["unreachable"] is False
        and sorted(asked) == ["douyu-101", "twitch-imperialhal__"])
    out["Twitch 在播时和别的平台一样（状态是 live、带开播时刻），页面据此把那一行点亮"] = (
        on_air["players"]["twitch-imperialhal__"]["state"] == "live"
        and on_air["players"]["twitch-imperialhal__"]["since"] == 1794900000)

    # 2026-11-26：页面播放器 mpegts.js 打了 Evo 的补丁（Twitch 的视频 PES 约两成没对齐到帧开头，不打会拖影、重影）
    root = Path(__file__).resolve().parent.parent
    vendor = (root / "panel" / "static" / "vendor" / "mpegts.js").read_text(encoding="utf-8")
    sys.path.insert(0, str(root / "tools"))
    import mpegts_patch  # tools/mpegts_patch.py
    try:
        mpegts_patch.patch(vendor)
        again = None
    except mpegts_patch.PatchError as e:
        again = str(e)
    fake = "var x=1;" + mpegts_patch.OLD + "BODY" + mpegts_patch.OLD2 + "=function(){}"
    patched = mpegts_patch.patch(fake)
    try:
        mpegts_patch.patch("var x=1;")
        unknown = None
    except mpegts_patch.PatchError as e:
        unknown = str(e)
    out["播放器 mpegts.js 带着 Evo 的补丁（没对齐的 H.264 PES 把开头那截接回上一帧），开头写明改过；再打一次不会重复打"] = (
        vendor.startswith("/* mpegts.js 1.8.0") and "EVO-PATCH" in vendor[:200] and vendor.count("evo_h264_held_") == 3
        and mpegts_patch.NEW in vendor and mpegts_patch.NEW2 in vendor and mpegts_patch.OLD not in vendor and again == "已经打过补丁了")
    out["补丁工具：原版的两处都换掉、加上说明；mpegts.js 版本变了找不到要改的地方 → 报错、一处都不改"] = (
        patched.startswith(mpegts_patch.HEADER) and mpegts_patch.NEW in patched and mpegts_patch.NEW2 in patched
        and mpegts_patch.OLD not in patched and unknown is not None and "版本变了" in unknown)
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
