"""Evo 窗口的"电竞"页面（赛事列表；0.6.0、0.6.1 里导航上叫"赛事"，2026-11-19 用户改的名）和看直播页（2026-11-17 用户定的：导航在"赛季"和"设置"之间）：赛事列表，点进去看选手的第一视角直播。

赛事页 /esports（esports.html）
- 打开这一页时下载一次 esports.json（esports_data.get，60 秒内不重下）；没读到时用上次存下的，并在顶部说明
- 三块：正在进行（每场一张卡片：进行中、类型、开始时间、名称、阶段和队伍数、几位选手在播、"看直播"按钮）、
  即将开始（一行一场，右边"看名单和直播"按钮：2026-11-17 用户要求没开始也能点进去，选手赛前练习、打排位也值得看）、
  最近结束（最近 7 天，灰色，右边"已结束"，不能点）。
  进行中 / 即将开始 / 已结束按电脑上的时间算，日期和钟点也按电脑的时区显示
- 几位选手在播：页面先画出来，再问 GET /esports/live（查这几场赛事名单里的直播间，live_status.query），
  60 秒内再问不重查。只在这一页开着时查，不在后台查

看直播页 /esports/<赛事>/watch（esports_watch.html，占满整个窗口，没有顶部导航）
- 左边：返回、赛事名、播放器、选手和三个去平台的按钮、同队视角；右边：队伍和选手，在播的可以点。
  赛程里这场赛事写了主舞台（播导播视角的直播间）时，右边最上面多一栏"主舞台"，和队伍并列（2026-11-18 用户要的）
- 页面上先把名单画好，谁在播由脚本问 GET /esports/<赛事>/status：页面开着、看得见时每 60 秒问一次
  （这里 50 秒内不重查，开播、下播后 2 分钟内页面跟着变）。顺便带上平台开关（esports.json 5 分钟内下载过就不重下）
- 能在窗口里播的平台：配置里有 embed_url 的用平台官方的嵌入播放器（现在是 B站）；配置里 player 是 flv（斗鱼，经过
  本机中转）、direct（抖音、登录了的 B站，页面直接连平台的视频服务器）、hls（Twitch，经本机中转接成一条 TS 流）的
  用 Evo 自己的播放器（mpegts.js）。别的平台（虎牙：用户 2026-11-18 定了不做）、平台开关关了的显示退路"去 X 看"
- 抖音：不登录，取地址在 watch_douyin.py（只要 H.264 的档；没选过清晰度时要码率不超过配置里那个数的最好一档）
- Twitch（2026-11-25，照 PureLive）：不登录，走 Evo 自己的播放器（和登录以后的 B站一样，不用 player.twitch.tv 的嵌入
  播放器）。取地址在 watch_twitch.py：先要播放令牌，再要各档的目录（只要 H.264，清晰度 id 用 GROUP-ID：chunked 是原画，
  还有 720p60、480p30…），视频经本机中转（hls_relay.py）。广告照放、不过滤。**Twitch 在中国大陆上不去**：连不上时
  开播状态带着 unreachable（页面写"连不上"、名单上方一行说明），/esports/play 回 reason "unreachable"
- B站：没登录的用官方嵌入播放器（最高 720p）；玩家扫码登录了 B站（bili_login.py，登录信息加密存在本机）以后改用
  Evo 自己的播放器播原画，视频经过本机中转（watch_bilibili.py：优先 HLS，hls_relay.py；2026-11-19 改的，原来是页面直接连，
  国内的玩家播不成）。
  登录：POST /esports/login/bilibili/start 要二维码（回画好的 SVG）、…/poll 问扫了没有、…/logout 退出；都要带面板的口令
- Evo 自己的播放器：页面先问 GET /esports/play/<选手>?q=清晰度（有哪些清晰度、实际给的是哪一档、播放地址），
  再让播放器连 GET /esports/stream/<选手>（本机中转，flv_relay.py：只听 127.0.0.1、要带面板的口令、一次只播一路，
  开新的一路就停掉上一路；播放器断开就停，不在后台留着）。取地址、连视频服务器都在玩家自己的电脑上
- Apex 在运行：GET /esports/game（game_detect，只看进程名），页面每 10 秒问一次，正在播时显示黄色提示条
- 关注（2026-11-20 用户要的；follows.py，名单存在 settings.json）：POST /esports/follow/<选手> 关注 / 取消，
  POST /esports/follow-notify 开 / 关开播通知（托盘里的 Evo 在后台查，follow_alerts.py）；电竞页最上面"我关注的"一块、
  两页上的星星按钮都用它们（_esports_follow.html）
这个文件不加载识别用的库。规则见 docs/面板.md"赛事页""看直播页"。
"""

import json
import logging
import re
import threading
import time
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

import bili_login
import danmaku
import douyu_login
import douyin_chat
import esports_data
import flv_relay
import follow_alerts
import follows
import game_detect
import live_status
import qr
import watch_bilibili
import watch_douyin
import watch_douyu
import watch_twitch

bp = Blueprint("esports", __name__)
WEEKDAYS = "一二三四五六日"
STATUS_SECONDS = 60        # 赛事页：这么久以内查过开播状态就不重查
WATCH_STATUS_SECONDS = 50  # 看直播页：页面每 60 秒问一次，每次都查新的（留 10 秒给同时开着的另一页）
WATCH_DATA_SECONDS = 300   # 看直播页开着时，这么久重新下载一次 esports.json（赛程改了、平台开关关了以后最慢这么久生效；
                           # 原来是 10 分钟，2026-11-18 用户要改成 5 分钟）
EMBED = {key: platform["embed_url"] for key, platform in live_status.PLATFORMS.items() if platform.get("embed_url")}
# 用 Evo 自己的播放器播的平台 → 取地址的模块。模块里 DIRECT 是 True 的（抖音）：页面直接连平台的视频服务器；
# 别的（斗鱼、B站、Twitch）：经过本机中转（/esports/stream）。模块里有 open_relay 的（B站、Twitch）自己决定怎么接
PLAYERS = {"douyu": watch_douyu, "bilibili": watch_bilibili, "douyin": watch_douyin, "twitch": watch_twitch}
PLAYERS = {key: module for key, module in PLAYERS.items() if live_status.PLATFORMS[key].get("player") in ("flv", "direct", "hls")}
# 要玩家登录了才用 Evo 自己的播放器的平台 → 管登录的模块（没登录的用平台官方的嵌入播放器）
LOGINS = {"bilibili": bili_login}
# 登录是可选的平台 → 管登录的模块（斗鱼，2026-11-21）：不登录也用 Evo 自己的播放器播，登录了才有原画。
# 登录不是扫码，是在 Evo 窗口另开的小窗口里打开平台官方的登录页（login_window.py）
WINDOW_LOGINS = {"douyu": douyu_login}
RATE_RE = re.compile(r"-?\d{1,5}|[a-z0-9_]{1,16}")  # 清晰度：斗鱼、B站是编号，抖音是档名（origin、uhd、hd……），
# Twitch 是 GROUP-ID（chunked、720p60、480p30……）
SLOTS = ("0", "1", "2", "3")  # 多屏同看：页面上最多 4 格，每格一路中转
_playing = {}  # 格子 → 正在中转的那一路（每格一次只播一路：这一格开新的一路就停掉它上一路）
_chats = {}    # 格子 → 正在转的那一路抖音弹幕（同上，每格一次只转一路）
_playing_lock = threading.Lock()
log = logging.getLogger("evo")
_status = {"at": 0.0, "found": {}}  # 上次查到的：{选手 id: live_status 的结果}
_status_lock = threading.Lock()  # 同时打开两次页面时只查一次


def day_text(moment, now):
    """"今天" / "明天" / "昨天"，别的写 "11-19"（按电脑的时区）。"""
    days = (moment.date() - now.date()).days
    return {0: "今天", 1: "明天", -1: "昨天"}.get(days, f"{moment:%m-%d}")


def view(event, now):
    """一场赛事在页面上要显示的。"""
    start = esports_data.parse_time(event["start"]).astimezone()
    teams = len(event["teams"])
    state = esports_data.status(event, now)
    return {"id": event["id"], "name": event["name"], "type": event["type"], "stage": event["stage"],
            "teams": teams, "sub": f"{event['stage']}，{teams} 支队伍" if event["stage"] else f"{teams} 支队伍",
            "day": day_text(start, now), "weekday": f"周{WEEKDAYS[start.weekday()]}", "time": f"{start:%H:%M}",
            "start": event["start"], "state": state,
            # 看直播页右上角的标记：没开始的写什么时候开始，不写"进行中"
            "mark": {esports_data.LIVE: "进行中", esports_data.ENDED: "已结束"}.get(
                state, f"{day_text(start, now)} {start:%H:%M} 开始")}


def players_status(players, max_age=STATUS_SECONDS):
    """这些选手现在在不在播：{选手 id: 结果}。max_age 秒内查过、而且这些人都在里面，就用上次的。"""
    with _status_lock:
        fresh = 0 <= time.time() - _status["at"] < max_age
        if not fresh or any(p["id"] not in _status["found"] for p in players):
            found = live_status.query(players)
            _status["found"] = {**(_status["found"] if fresh else {}), **found}
            _status["at"] = time.time()
        return {p["id"]: _status["found"][p["id"]] for p in players}


def safe_image(url):
    """平台给的头像地址：只要 https 的（http 的换成 https），别的不要。"""
    url = str(url or "")
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") and not any(c in url for c in '"<> \\') else ""


def member(player, team, main=False):
    """看直播页上的一位（选手、主舞台的一路、主播）：id、名字、队名、平台、房间号、直播间地址、main、off（不开直播的选手）；
    embed 是平台官方嵌入播放器的地址，play 是用 Evo 自己的播放器时问清晰度和播放地址的接口（都没有就是不能在窗口里播）。"""
    embed = EMBED.get(player["platform"])
    off = player["platform"] == esports_data.NO_STREAM  # 不开直播的选手：照样列在队里，标"不直播"，点不开
    return {"id": player["id"], "name": player["name"], "initial": player["name"][:1].upper(),
            "team": team, "main": main, "off": off, "platform": player["platform"],
            "platform_name": esports_data.NO_STREAM_NAME if off else live_status.NAMES[player["platform"]],
            "room": player["room"],
            "url": live_status.room_url(player["platform"], player["room"]),
            "embed": embed.format(room=player["room"]) if embed else None,
            "play": url_for("esports.play", player_id=player["id"]) if player["platform"] in PLAYERS else None,
            # 弹幕（只有用 Evo 自己的播放器时才有；平台官方的嵌入播放器里本来就有它自己的弹幕）
            "danmaku": url_for("esports.danmaku_info", player_id=player["id"])
            if player["platform"] in PLAYERS and danmaku.supported(player["platform"]) else None}


def roster(data, event):
    """看直播页要的名单：[{"name": 队名, "initial": 第一个字, "logo": 队徽地址或 None, "main": 是不是主舞台,
    "players": [选手, ...]}, ...]。这场赛事有主舞台（播导播视角的直播间）时它排在最前面，"选手"是那几路直播间。
    选手是 member() 那样的。"""
    teams = []
    main = esports_data.event_main(data, event)
    if main:
        teams.append({"key": "main", "name": esports_data.MAIN_TEAM, "initial": esports_data.MAIN_TEAM[:1], "logo": None,
                      "main": True, "players": [member(stream, esports_data.MAIN_TEAM, True) for stream in main]})
    for index, (team, players) in enumerate(esports_data.event_teams(data, event)):
        teams.append({"key": f"t{index}", "name": team, "initial": team[:1].upper(), "main": False,
                      "players": [member(player, team) for player in players],
                      "logo": esports_data.logo_url(data, team)})  # 队徽的地址，没有是 None（页面用队名第一个字）
    return teams


def follow_view(data, now=None):
    """关注的事页面要的（电竞页"我关注的"一块、两页上的星星）：
    {"ids": 关注的 id, "notify": 开播通知开没开（None 还没问过）,
     "events": 正在进行、即将开始的赛事里有关注的人的那几场（view() 的几项 + names 关注的是谁 + watch 看直播页的地址）,
     "idle": 关注了、但不开直播的选手（"主播"一块里没有他们的卡片）}。"""
    now = now or datetime.now().astimezone()
    events = []
    for event, mine in follows.events(data, now):
        shown = view(event, now)
        first = next((p["id"] for p in mine if p["platform"] != esports_data.NO_STREAM), None)  # 点进去先看关注的那一位
        events.append({**{key: shown[key] for key in ("id", "name", "type", "stage", "day", "weekday", "time", "start", "state")},
                       "names": [p["name"] for p in mine],
                       "watch": url_for("esports.watch", event_id=event["id"], **({"p": first} if first else {}))})
    return {"ids": follows.ids(), "notify": follows.notify(), "events": events,
            "idle": [{"id": p["id"], "name": p["name"], "team": p.get("team", "")}
                     for p in follows.players(data) if p["platform"] == esports_data.NO_STREAM]}


def follow_config(data):
    """给页面里的脚本（window.evoFollow）的：follow_view() 加上两个接口的地址、面板的口令、最多关注几位。"""
    return {**follow_view(data), "url": url_for("esports.follow", player_id="_").rsplit("/", 1)[0] + "/",
            "notify_url": url_for("esports.follow_notify"), "token": current_app.config["TOKEN"], "max": follows.MAX}


@bp.app_context_processor
def _follow_context():
    """设置页的"开播通知"开关要的（每个页面都能用）：开没开、关注了几位。"""
    return {"esports_follow": {"notify": follows.notify() is True, "count": len(follows.ids())}}


@bp.post("/esports/follow/<player_id>")
def follow(player_id):
    """关注 / 取消关注一位（表单里 on=1 / 0）。只能关注名单里有的选手、主播；取消随时可以。
    回 follow_view() 那几项，加 ok、full（满了，没加上）。"""
    saved = esports_data.cached()
    data = saved["data"] if saved else None
    on = request.form.get("on") == "1"
    known = {item["id"] for item in [*(data.get("players") or []), *(data.get("streamers") or [])]} if data else set()
    if on and player_id not in known:
        return jsonify(ok=False, full=False, **follow_view(data))
    before = follows.ids()
    now = follows.set_following(player_id, on)
    if now != before:
        log.info("关注：%s %s（现在关注 %d 位）", "关注了" if on else "取消了", player_id, len(now))
        follow_alerts.wake.set()
    full = on and player_id not in now
    return jsonify(ok=not full, full=full, **follow_view(data))


@bp.post("/esports/follow-notify")
def follow_notify():
    """开 / 关"关注的主播开播时通知"（表单里 on=1 / 0）。设置页的开关带着 next：改完回设置页；页面里的脚本不带，回 JSON。"""
    on = follows.set_notify(request.form.get("on") == "1")
    log.info("关注：开播通知%s", "打开了（托盘里的 Evo 每 5 分钟查一次关注的主播，Apex 开着时不查）" if on else "关掉了（不在后台查了）")
    follow_alerts.wake.set()
    if request.form.get("next"):
        flash("已开启：关注的主播开播时用 Windows 通知提醒你。Evo 开着时每 5 分钟问一次直播平台，只问你关注的主播；Apex 开着时不问。"
              if on else "已关闭开播通知，Evo 不再在后台查关注的主播。", "ok")
        return redirect(url_for("settings"))
    saved = esports_data.cached()
    return jsonify(ok=True, **follow_view(saved["data"] if saved else None))


@bp.route("/esports")
def page():
    got = esports_data.get()
    now = datetime.now().astimezone()
    data = got["data"]
    found = esports_data.groups(data, now) if data else {"live": [], "upcoming": [], "recent": []}
    read_at = datetime.fromtimestamp(got["fetched_at"]).astimezone() if got["fetched_at"] else None
    return render_template(
        "esports.html", loaded=data is not None, stale=data is not None and not got["ok"],
        read_at=f"{day_text(read_at, now)} {read_at:%H:%M}" if read_at else None,
        live=[view(e, now) for e in found["live"]], upcoming=[view(e, now) for e in found["upcoming"]],
        recent=[view(e, now) for e in found["recent"]],
        # 主播名单（赛程文件里有才有这一块）：先照名单的顺序画出来，谁在播由页面里的脚本问 /esports/live
        streamers=[{**member(s, s.get("team", "")), "watch": url_for("esports.streamers", p=s["id"])}
                   for s in esports_data.streamers(data)],
        follow=follow_config(data))


@bp.get("/esports/live")
def live():
    """正在进行的每场赛事有几位选手在播，和主播名单里每一位在不在播：
    {"events": {赛事 id: {"live": 在播, "total": 名单里几位, "unknown": 没查到的}},
     "streamers": {主播 id: {"state", "loop", "title", "avatar", "since"}}, "checked": 查的时间}。"""
    data = esports_data.cached()
    data = data["data"] if data else None
    events = esports_data.groups(data)["live"] if data else []
    rosters = {event["id"]: esports_data.streaming(esports_data.event_players(data, event)) for event in events}  # 不开直播的不数
    streamers = esports_data.streamers(data)
    everyone = list({p["id"]: p for players in [*rosters.values(), streamers] for p in players}.values())
    found = players_status(everyone) if everyone else {}
    out = {}
    for event_id, players in rosters.items():
        states = [found[p["id"]]["state"] for p in players]
        out[event_id] = {"live": states.count(live_status.LIVE), "total": len(players),
                         "unknown": states.count(live_status.ERROR)}
    # checked：这些状态是什么时候查的（60 秒内再问用的是上次的，时间也是上次的）
    return jsonify(events=out, checked=_status["at"] if everyone else time.time(), streamers={s["id"]: status_view(found[s["id"]]) for s in streamers})


def status_view(got):
    """一个直播间的开播状态里给页面的那几项（头像只要 https 的）。unreachable：连不上这个平台（现在只有 Twitch），
    页面据此写"连不上"、名单上方说一句"在国内要能访问国外网站"。"""
    return {"state": got["state"], "loop": got["loop"], "title": got["title"], "since": got["since"],
            "avatar": safe_image(got["avatar"]), "unreachable": bool(got.get("unreachable"))}


def watch_page_data(data, members, selected, status_url):
    """看直播页里的脚本要的东西（赛事的和主播的一样）。"""
    return {"players": members, "platforms": data["platforms"], "selected": selected,
            "status_url": status_url, "game_url": url_for("esports.game"),
            "mpegts_url": url_for("static", filename="vendor/mpegts.js"), "token": current_app.config["TOKEN"],
            "failed_url": url_for("esports.play_failed", player_id="_").rsplit("/", 1)[0] + "/",  # 后面接选手 id
            "follow": follow_config(data),  # 关注（星星按钮、名单最上面"我关注的"一组）
            # 要登录才能看高画质的平台：现在登录着没有（打开页面时隔 6 小时向平台核对一次）、登录用的三个接口
            "accounts": {key: module.account(check=True) for key, module in {**LOGINS, **WINDOW_LOGINS}.items()},
            # window：登录是可选的、在另开的小窗口里登录（斗鱼）；没有这一项的是扫码登录、登录了才用 Evo 自己的播放器（B站）
            "login": {**{key: {step: url_for(f"esports.login_{step}", platform=key) for step in ("start", "poll", "logout")}
                         for key in LOGINS},
                      **{key: {"window": True, "state": url_for("esports.login_state", platform=key),
                               "logout": url_for("esports.login_logout", platform=key)} for key in WINDOW_LOGINS}}}


@bp.route("/esports/streamers")
def streamers():
    """主播的看直播页：和赛事的看直播页是同一个页面，右边不分队伍，只有一列主播（在播的排前面）。"""
    data = esports_data.get()["data"]
    members = [member(s, s.get("team", "")) for s in esports_data.streamers(data)]  # 选手带着队名，另外加的主播没有队
    if not members:
        flash("现在没有主播名单。", "error")
        return redirect(url_for("esports.page"))
    ids = {m["id"] for m in members}
    wanted = request.args.get("p")
    selected = wanted if wanted in ids else None
    return render_template(
        "esports_watch.html", solo=True, noun="主播", teams=[], team_count=len(members), total=len(members), selected=selected,
        event={"name": "主播", "stage": "", "state": None, "mark": None}, members=members,
        page_data={**watch_page_data(data, members, selected, url_for("esports.streamers_status")), "solo": True, "noun": "主播"})


@bp.get("/esports/streamers/status")
def streamers_status():
    """主播名单里每一位现在在不在播（和 /esports/<赛事>/status 一样的格式）。名单没有了回 404。"""
    data = esports_data.get(fresh=WATCH_DATA_SECONDS)["data"]
    streamers = esports_data.streamers(data)
    if not streamers:
        return jsonify(error="gone"), 404
    found = players_status(streamers, _status_age())
    return jsonify(checked=time.time(), platforms=data["platforms"],
                   players={pid: status_view(got) for pid, got in found.items()})


def _status_age():
    """看直播页问开播状态时，多久以内查过的可以直接用。网址带 retry=1（Twitch 连不上时玩家点了"重试"，2026-11-25）：
    忘掉刚才的"连不上"、不用上次的，马上重查。"""
    if request.args.get("retry") == "1":
        live_status.retry_twitch()
        return 0
    return WATCH_STATUS_SECONDS


@bp.route("/esports/<event_id>/watch")
def watch(event_id):
    data = esports_data.get()["data"]
    event = esports_data.find_event(data, event_id) if data else None
    if event is None:
        flash("没有找到这场赛事，可能已经从赛程里拿掉了。", "error")
        return redirect(url_for("esports.page"))
    teams = roster(data, event)
    ids = {player["id"] for team in teams for player in team["players"] if not player["off"]}  # 能选来看的人
    wanted = request.args.get("p")
    selected = wanted if wanted in ids else None
    return render_template(
        "esports_watch.html", solo=False, noun="选手", event=view(event, datetime.now().astimezone()), teams=teams,
        team_count=sum(not team["main"] for team in teams),  # "全部 N 支"只数队伍，不数主舞台
        total=len(ids), selected=selected,
        page_data={**watch_page_data(data, [player for team in teams for player in team["players"]], selected,
                                     url_for("esports.status", event_id=event["id"])), "solo": False, "noun": "选手"})


@bp.get("/esports/<event_id>/status")
def status(event_id):
    """这场赛事名单里的选手现在在不在播：{"players": {选手 id: {"state", "loop", "title", "avatar", "since"}},
    "platforms": 平台开关, "checked": 查的时间}。没有这场赛事回 404。"""
    data = esports_data.get(fresh=WATCH_DATA_SECONDS)["data"]
    event = esports_data.find_event(data, event_id) if data else None
    if event is None:
        return jsonify(error="gone"), 404
    # 主舞台的直播间也查；不开直播的选手不查
    players = esports_data.event_main(data, event) + esports_data.streaming(esports_data.event_players(data, event))
    found = players_status(players, _status_age()) if players else {}
    return jsonify(checked=time.time(), platforms=data["platforms"],
                   players={pid: status_view(got) for pid, got in found.items()})


@bp.get("/esports/game")
def game():
    """Apex 有没有在运行（只看进程名，game_detect.py）：看直播页据此显示"可能让游戏掉帧"的提示条。"""
    return jsonify(running=game_detect.game_running())


def _player(player_id):
    """名单里的这位选手和现在的赛事数据；不能用 Evo 自己的播放器播（不在名单里、平台不支持、平台开关关了）返回 None。"""
    saved = esports_data.cached()
    data = saved["data"] if saved else None
    player = esports_data.find_stream(data, player_id) if data else None  # 名单里的选手，或者主舞台的一路
    if player is None or player["platform"] not in PLAYERS or data["platforms"].get(player["platform"]) is False:
        return None
    login = LOGINS.get(player["platform"])
    if login is not None and not login.account()["logged_in"]:
        return None  # 没登录：Evo 不碰这个平台的视频地址，页面用官方嵌入播放器
    return player


def _slot():
    """网址里的 s（多屏同看的第几格，"0"–"3"）；没写、写得不对当作第一格。"""
    wanted = request.args.get("s", "0")
    return wanted if wanted in SLOTS else SLOTS[0]


def _rate():
    """网址里的 q（清晰度的编号或档名）；没写、写得不对是 None（没选过：由取地址的模块定要哪一档）。"""
    wanted = request.args.get("q", "")
    return wanted if RATE_RE.fullmatch(wanted) else None


@bp.get("/esports/play/<player_id>")
def play(player_id):
    """看直播页开播前问的：{"ok": True, "qualities": [{"id", "name"}], "applied": 实际给的那一档, "stream": 播放地址,
    "kind": 播放器按哪种放（"flv" / "mpegts"）, "logged": 这次是不是带着玩家在这个平台的登录要到的（斗鱼）}；
    取不到是 {"ok": False, "reason": "upstream", "detail": 原因}；Twitch 连不上（在国内要能访问国外网站）是
    {"ok": False, "reason": "unreachable", "detail": 原因}。不能在窗口里播的选手回 404。"""
    player = _player(player_id)
    if player is None:
        return jsonify(ok=False, reason="unsupported"), 404
    module = PLAYERS[player["platform"]]
    try:
        found = module.resolve(player["room"], _rate())
    except Exception as e:  # noqa: BLE001  平台那边的各种错：页面显示退路
        log.info("看直播：%s 房间 %s 没取到地址：%s", player["platform"], player["room"], e)
        reason = "unreachable" if getattr(e, "unreachable", False) else "upstream"
        return jsonify(ok=False, reason=reason, detail=str(e))
    if getattr(module, "DIRECT", False):  # 页面直接连平台的视频服务器
        return jsonify(ok=True, qualities=found["qualities"], applied=found["rate"], stream=found["url"], kind="flv")
    module.remember(player["room"], found["rate"], found)  # 紧接着播放器来连时直接用，不再问一遍
    return jsonify(ok=True, qualities=found["qualities"], applied=found["rate"], kind=found.get("kind", "flv"),
                   logged=bool(found.get("logged")),
                   stream=url_for("esports.stream", player_id=player_id, q=found["rate"], s=_slot(), t=current_app.config["TOKEN"]))


@bp.get("/esports/danmaku/<player_id>")
def danmaku_info(player_id):
    """页面连弹幕要的东西（danmaku.py；连接是页面自己直接连平台，只看不发）：{"ok": True, "platform", "url", "room"}。
    这个平台的弹幕没做、不能在窗口里播的选手回 404；平台那边没问到是 {"ok": False, "detail": 原因}。"""
    player = _player(player_id)
    if player is None or not danmaku.supported(player["platform"]):
        return jsonify(ok=False, reason="unsupported"), 404
    try:
        found = danmaku.info(player["platform"], player["room"])
    except Exception as e:  # noqa: BLE001  平台那边的各种错：页面就不显示弹幕
        log.info("看直播：%s 房间 %s 的弹幕没问到：%s", player["platform"], player["room"], e)
        return jsonify(ok=False, reason="upstream", detail=str(e))
    if found is None:
        return jsonify(ok=False, reason="unsupported"), 404
    if found["platform"] == "douyin":  # 抖音：页面连不了，连面板的这个地址，由 Evo 的后台去连抖音再转过来
        found = {**found, "feed": url_for("esports.danmaku_feed", player_id=player_id, s=_slot(), t=current_app.config["TOKEN"])}
    return jsonify(ok=True, **found)


@bp.get("/esports/danmaku-feed/<player_id>")
def danmaku_feed(player_id):
    """抖音的弹幕（douyin_chat.py）：Evo 的后台连抖音的弹幕服务器，把弹幕一条条转给页面（text/event-stream，每条是 {"text": 内容}）。
    要带面板的口令（别的网页不能让 Evo 去连平台）；每一格一次只转一路。页面断开就跟着断开抖音。连不上回 502 和原因。"""
    if request.args.get("t") != current_app.config["TOKEN"]:
        abort(403)
    player = _player(player_id)
    if player is None or player["platform"] != "douyin" or not danmaku.supported("douyin"):
        abort(404)
    try:
        feed = douyin_chat.Feed(player["room"])
    except Exception as e:  # noqa: BLE001  没开播、连不上、被拒绝：页面就不显示弹幕
        log.info("看直播：douyin 房间 %s 的弹幕没连上：%s", player["room"], e)
        return jsonify(ok=False, reason="upstream", detail=str(e)), 502
    slot = _slot()
    with _playing_lock:
        old, _chats[slot] = _chats.get(slot), feed
    if old is not None:
        old.close()
    room = player["room"]

    def events():
        yield ": ok\n\n"
        quiet = time.monotonic()
        try:
            for batch in feed.texts():
                for text in batch[:20]:  # 一次最多转 20 条：再多页面也显示不下
                    yield "data: " + json.dumps({"text": text}, ensure_ascii=False) + "\n\n"
                if batch:
                    quiet = time.monotonic()
                elif time.monotonic() - quiet > 15:  # 没有弹幕时隔一会儿发一行空的：页面走了这里才发现得了
                    quiet = time.monotonic()
                    yield ": keep\n\n"
        except douyin_chat.ChatError as e:
            log.info("看直播：douyin 房间 %s 的弹幕断了：%s", room, e)

    log.info("看直播：开始转 douyin 房间 %s 的弹幕", room)
    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-store"})


@bp.post("/esports/play-failed/<player_id>")
def play_failed(player_id):
    """页面里的播放器没播成时告诉后台一声，只为了写进运行记录（玩家反馈"播不了"时好查；不联网）。"""
    player = _player(player_id)
    if player is not None:
        reason = " ".join(str(request.form.get("reason") or "").split())[:200]
        log.info("看直播：页面里没播成 %s 房间 %s：%s", player["platform"], player["room"], reason)
    return jsonify(ok=True)


@bp.get("/esports/stream/<player_id>")
def stream(player_id):
    """本机中转：把平台的视频转给页面里的播放器（斗鱼的 FLV：flv_relay.py；B站：HLS 接成一条 TS 流，hls_relay.py，
    没有 HLS 才是 FLV）。要带面板的口令（别的网页不能让 Evo 去连平台）。
    每一格（网址里的 s，多屏同看最多 4 格）一次只播一路：这一格开新的一路就停掉它上一路，别的格子不动。
    取不到地址、连不上、不是 H.264 回 502 和原因，页面显示退路。"""
    if request.args.get("t") != current_app.config["TOKEN"]:
        abort(403)
    player = _player(player_id)
    if player is None or getattr(PLAYERS[player["platform"]], "DIRECT", False):
        abort(404)
    module, rate, first = PLAYERS[player["platform"]], _rate(), [True]

    def open_segment():
        body, found = module.open_segment(player["room"], rate, reuse=first[0])
        first[0] = False
        return body, found["ttl"]

    def on_event(text):
        log.info("看直播：%s 房间 %s %s", player["platform"], player["room"], text)

    if hasattr(module, "open_relay"):  # 这个平台自己决定怎么接（B站：有 HLS 接 HLS；Twitch：接 HLS）
        try:
            relay = module.open_relay(player["room"], rate, on_event=on_event)
        except Exception as e:  # noqa: BLE001  取地址出的各种错
            log.info("看直播：%s 房间 %s 没取到地址：%s", player["platform"], player["room"], e)
            reason = "unreachable" if getattr(e, "unreachable", False) else "upstream"
            return jsonify(ok=False, reason=reason, detail=str(e)), 502
    else:
        relay = flv_relay.Relay(open_segment, lead=live_status.PLATFORMS[player["platform"]].get("renew_lead", 5), on_event=on_event)
    slot = _slot()
    with _playing_lock:
        old, _playing[slot] = _playing.get(slot), relay
    if old is not None:
        old.close()
    try:
        relay.start()
    except flv_relay.RelayError as e:
        log.info("看直播：%s 房间 %s 播不了：%s", player["platform"], player["room"], e)
        return jsonify(ok=False, reason=e.code, detail=str(e)), 502
    mimetype = getattr(relay, "mimetype", "video/x-flv")
    mimetype = getattr(relay, "mimetype", mimetype)  # B站：接上以后才知道是 FLV 还是 TS
    log.info("看直播：开始中转 %s 房间 %s（清晰度 %s，%s%s）", player["platform"], player["room"], rate, mimetype,
             "" if slot == SLOTS[0] else f"，第 {int(slot) + 1} 格")
    return Response(relay.stream(), mimetype=mimetype, headers={"Cache-Control": "no-store"})


@bp.post("/esports/login/<platform>/start")
def login_start(platform):
    """要一个登录二维码：{"ok": True, "key": 问状态用的, "svg": 画好的二维码}；没要到是 {"ok": False, "detail": 原因}。"""
    module = LOGINS.get(platform)
    if module is None:
        abort(404)
    try:
        found = module.start()
        return jsonify(ok=True, key=found["key"], svg=qr.svg(found["url"]))
    except Exception as e:  # noqa: BLE001  平台那边的各种错、二维码的内容太长画不下：页面说没要到
        log.info("看直播：%s 没要到登录二维码：%s", platform, e)
        return jsonify(ok=False, detail=str(e))


@bp.post("/esports/login/<platform>/poll")
def login_poll(platform):
    """扫了没有：{"state": "waiting" / "scanned" / "expired" / "ok" / "error", "name": 登录好了时的昵称}。
    登录好了由管登录的模块存下来（加密）；登录信息不回给页面，也不写进日志。"""
    module = LOGINS.get(platform)
    if module is None:
        abort(404)
    try:
        found = module.poll(request.form.get("key", ""))
    except Exception as e:  # noqa: BLE001
        return jsonify(state="error", name="", detail=str(e))
    if found["state"] == "ok":
        log.info("看直播：%s 登录好了", platform)
    return jsonify(state=found["state"], name=found["name"])


@bp.post("/esports/login/<platform>/save")
def login_save(platform):
    """登录窗口（login_window.py，在 Evo 窗口的进程里）登录好了，把平台给的 Cookie 交到这里存起来（表单里的 cookie）。
    要带面板的口令。回 {"ok": 存下了没有}；登录凭证不写进日志、不回给请求的人。"""
    module = WINDOW_LOGINS.get(platform)
    if module is None:
        abort(404)
    found = module.save(request.form.get("cookie", "")[:200_000])
    log.info("看直播：%s %s", platform, "登录好了，登录凭证存在这台电脑上（加密）" if found["ok"] else "登录窗口交来的东西里没有登录凭证，没存")
    return jsonify(ok=found["ok"])


@bp.get("/esports/login/<platform>/state")
def login_state(platform):
    """这个平台现在登录着没有：{"logged_in", "name"}（页面等登录窗口时隔两秒问一次）。"""
    module = WINDOW_LOGINS.get(platform) or LOGINS.get(platform)
    if module is None:
        abort(404)
    return jsonify(module.account())


@bp.post("/esports/login/<platform>/logout")
def login_logout(platform):
    """退出登录：删掉本机存的；B站还会告诉平台让这次登录失效（斗鱼只删本机的）。"""
    module = LOGINS.get(platform) or WINDOW_LOGINS.get(platform)
    if module is None:
        abort(404)
    told = module.logout()
    log.info("看直播：%s 退出登录（平台那边%s）", platform, "也退出了" if told else "没告诉成，本机的已经删掉")
    return jsonify(ok=True)

