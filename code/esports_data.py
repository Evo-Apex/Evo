"""赛事数据（赛事页、看直播页用）：下载 esports.json 这个公开的小文件，里面是开发者手动维护的赛程、队伍和每位选手的直播间。

只在打开赛事页时下载（get()，不在后台定时下载），60 秒内再打开不重下；请求里只带"Evo/<版本号>"，不发送任何战绩、
截图或电脑信息。文件放在 R2 上（https://dl.evotracker.cn/esports.json ，和 notice.json 一起），不经过统计服务器
（免费版每天有请求上限，原因同公告）。连不上、格式不对都当作"这次没读到"，用上次存下的（数据库旁边的 esports.json）。

esports.json 由 tools/esports.py 从两张 CSV 表生成、上传：
    {"version": 1, "updated": "2026-11-17T20:00:00+08:00",
     "platforms": {"douyu": true, "huya": true, "bilibili": true, "douyin": true},
     "players": [{"id": "douyu-520013", "name": "Kite", "team": "星火电竞", "platform": "douyu", "room": "520013",
                  "liquipedia": ""}],
     "events": [{"id": "20261117-1900-league", "name": "亚太北区职业联赛 第 3 周", "type": "联赛", "stage": "B 组对 C 组",
                 "start": "2026-11-17T19:00:00+08:00", "end": "2026-11-17T23:00:00+08:00", "teams": ["星火电竞", …]}],
     "teams": {"星火电竞": {"logo": "3fa9c1d2e4b5a6f7.webp"}},
     "streamers": [{"id": "douyu-123456", "name": "某主播", "platform": "douyu", "room": "123456"}]}
- platforms：远程开关。某个平台是 false，Evo 就不在窗口里播它，改成"去 X 看"。没写的平台当作 true
- players：id 只有字母、数字、- 和 _；platform 是 config/watch.json 里的五个之一；room 只能是数字，
  **不开直播的选手**（2026-11-18 用户定的：也录进名单，页面上照样列在队里、标"不直播"）：platform 写 "none"、room 是空的，
  id 是 "none-" + 选手名（UTF-8）的 sha256 前 12 位。不查他的开播状态，不算进"在播 N / M 人"（streaming()）。
  抖音的还可以是主播的抖音号（字母、数字、下划线；live_status.room_ok()），Twitch 写登录名（字母、数字、下划线，最长 25 位）。
  **Twitch 的选手多认一种写法**（2026-11-25 旧版本兼容）：0.7.2 及以前的 Evo 不认识 twitch，会把这样的选手从队伍里
  整个丢掉（platform 不认识、room_ok() 不过）。所以公布的文件里 Twitch 的选手写成
  {"platform": "none", "room": "", "stream": {"platform": "twitch", "room": "imperialhal__"}, ...}：
  旧版本照旧当"不直播"，新版本看到 stream 里的平台认识、房间号对，就当这位选手在 Twitch 播（主舞台、主播不用这样写）。
  stream 写得不对（平台不认识、房间号不对、不是对象）就忽略它，照旧"不直播"。平台**直接**写 "twitch" 也认。
  Twitch 的登录名不分大小写：读的时候存成小写（id 不变）
- events：type 是 联赛 / 训练赛 / 杯赛；start、end 带时区，end 晚于 start；teams 是队名，对应 players 里的 team。
  main（可以没有）：这场赛事的**主舞台**，就是播导播视角的直播间（官方直播、解说、看比赛的主播的直播间），照写的顺序，
  最多 24 路（2026-11-21 用户定的，原来 8 路：赛管填满了；0.6.x 和更早的 Evo 只读前 8 路，官方直播要写在前面）：
  [{"id": "bilibili-7734200", "name": "官方直播", "platform": "bilibili", "room": "7734200"}]。它们不在 players 里、不算哪支队；
  写得不对的那一路去掉，和这场赛事名单里某位选手同一个 id 的那一路也去掉（event_main()）
- teams（可以没有）：队徽。键是队名，logo 是公开存储桶 esports/logos/ 下的文件名（小写字母和数字 + .png / .webp，
  协作网页上传时起的）。**只写文件名、不写网址**：Evo 自己拼成 config/watch.json 里 logo_url 那个地址（logo_url()）。
  没有队徽的队不写；名单里没有的队、文件名写得不对的那一条去掉
  observers：2026-11-19 加过的"观战的人"（排在他正在看的那支队下面、带"观战"标签），2026-11-21 用户定了去掉：
  看比赛的主播直接写进主舞台。文件里就算有这一项也不读（0.6.2 还认它，所以发布时不再写）
- **电竞页的"主播"一块**（streamers()，2026-11-19 用户定的："默认职业选手就是主播""只放我们有上传直播链接的选手"）：
  players 里所有开直播的选手（发布时要放选手库里的全部选手，不只是这几场赛事用到的队），后面接 streamers 里另外加的
- streamers（可以没有；现在没人写它，留着以后加不是职业选手的主播）：另外加的主播，和赛事、队伍无关，
  照写的顺序（页面上在播的排前面）。每位和主舞台的一路一样写：id、name、platform（五个平台之一，不能是 "none"）、room。
  最多 40 位，其中抖音最多 10 位（抖音没开播的房间每查一次要开约 1 MB 的网页），多出来的去掉。
  主播可以同时是 players 里的选手（同一个平台和房间）；id 和某位选手、某路主舞台一样，房间却不一样的那一位去掉
  （播放时按 id 找直播间，find_stream()）。0.6.0、0.6.1 不认识这一项，会忽略
- "进行中 / 即将开始 / 已结束"按 start、end 和电脑上的时间算（status()），不用手动改
格式不对的那一条去掉，不影响别的；整个文件不对（不是 JSON、version 不认识）当作没读到。
存储桶的密钥就算泄露，别人也只能改赛程上的字和房间号（只会去问这五个平台的直播间），不能让 Evo 打开别的网站。

测试时用环境变量 EVO_ESPORTS_URL 换地址：可以是测试文件 https://dl.evotracker.cn/esports_test.json ，
也可以是本机的一个文件（如 E:\\Evo\\dist\\esports.json，tools/esports.py --out 生成的）。
用法：python code/esports_data.py   → 下载一次并打印
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import live_status
import net_route
from paths import DB_PATH
from storage import APP_VERSION

VERSION = 1
DEFAULT_URL = "https://dl.evotracker.cn/esports.json"
URL = os.environ.get("EVO_ESPORTS_URL", DEFAULT_URL)
CACHE_PATH = DB_PATH.parent / "esports.json"  # {"fetched_at": 下载的时间, "data": 清理过的内容, "via": 从哪取的}
TIMEOUT = 10
RELAY_FRESH = 15 * 60  # 秒：走国内备用线路取到的，这么久以内不重下（派圈助手的云环境每月有调用上限，2026-11-19 用户定的）
MAX_BYTES = 1_000_000
FRESH_SECONDS = 60     # 这么久以内下载过就不重下
RECENT_DAYS = 7        # "最近结束"只留这么多天
TYPES = ("联赛", "训练赛", "杯赛")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
NO_STREAM = "none"     # 选手的 platform 写这个：这位选手不开直播（room 是空的）
NO_STREAM_NAME = "不直播"
MAIN_MAX = 24          # 一场赛事的主舞台最多几路（2026-11-21 起，原来 8）
MAIN_TEAM = "主舞台"   # 主舞台的直播间在页面上"算哪支队"
STREAMERS_MAX = 40     # streamers（另外加的主播）最多几位
STREAMERS_DOUYIN_MAX = 10  # 其中抖音最多几位（没开播的抖音房间每查一次约 1 MB）
LOGO_RE = re.compile(r"[a-z0-9]{8,40}\.(png|webp)")  # 队徽的文件名：拼不出别的文件夹、别的网站
LIVE, UPCOMING, ENDED = "live", "upcoming", "ended"


class EsportsError(Exception):
    pass


def parse_time(text):
    """"2026-11-17T19:00:00+08:00" → 带时区的时间；写错了、没带时区返回 None。"""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _text(value, limit, required=True):
    """一段文字：去掉两头空格、换行，超过 limit 个字、（required 时）是空的都算不对（返回 None）。"""
    if not isinstance(value, str):
        return None if required else ""
    value = " ".join(value.split())
    if len(value) > limit or (required and not value):
        return None
    return value


def clean_player(raw):
    if not isinstance(raw, dict):
        return None
    pid, name, team = raw.get("id"), _text(raw.get("name"), 40), _text(raw.get("team"), 40)
    platform, room = raw.get("platform"), raw.get("room")
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid) or name is None or team is None:
        return None
    if platform == NO_STREAM:  # 不开直播的选手：没有房间
        if room != "":
            return None
        # 旧版本的 Evo（0.7.2 及以前）不认识 twitch，会把这样的选手从队伍里整个丢掉，所以 Twitch 的选手写成
        # platform "none"、房间号放在 stream 里（旧版本照旧当"不直播"）。stream 写得不对就忽略它、照旧不直播
        stream = raw.get("stream")
        if isinstance(stream, dict):
            candidate = stream.get("platform")
            if isinstance(candidate, str) and candidate != NO_STREAM and live_status.room_ok(candidate, stream.get("room")):
                platform, room = candidate, stream["room"]
    elif not isinstance(platform, str) or not live_status.room_ok(platform, room):
        return None
    if platform == "twitch":  # 登录名不分大小写：存成小写（id 不变）
        room = room.lower()
    return {"id": pid, "name": name, "team": team, "platform": platform, "room": room,
            "liquipedia": _text(raw.get("liquipedia"), 100, required=False) or ""}


def clean_stream(raw):
    """主舞台的一路直播间：{"id", "name", "platform", "room"}；写得不对返回 None。
    Twitch 的登录名不分大小写：存成小写（id 不变，和 clean_player 一样）。"""
    if not isinstance(raw, dict):
        return None
    pid, name, platform, room = raw.get("id"), _text(raw.get("name"), 40), raw.get("platform"), raw.get("room")
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid) or name is None:
        return None
    if not isinstance(platform, str) or not live_status.room_ok(platform, room):
        return None
    return {"id": pid, "name": name, "platform": platform, "room": room.lower() if platform == "twitch" else room}


def clean_event(raw):
    if not isinstance(raw, dict):
        return None
    eid, name, stage = raw.get("id"), _text(raw.get("name"), 80), _text(raw.get("stage"), 80, required=False)
    start, end, teams = parse_time(raw.get("start")), parse_time(raw.get("end")), raw.get("teams")
    if not isinstance(eid, str) or not ID_RE.fullmatch(eid) or name is None or stage is None:
        return None
    if raw.get("type") not in TYPES or start is None or end is None or end <= start or not isinstance(teams, list):
        return None
    names = []
    for team in teams:
        team = _text(team, 40)
        if team is not None and team not in names:
            names.append(team)
    event = {"id": eid, "name": name, "type": raw["type"], "stage": stage, "start": start.isoformat(),
             "end": end.isoformat(), "teams": names[:60]}
    main = _unique(raw["main"][:MAIN_MAX], clean_stream) if isinstance(raw.get("main"), list) else []
    if main:  # 没有主舞台的赛事不带这一项（和以前的文件一模一样）
        event["main"] = main
    return event


def clean_teams(raw, known):
    """esports.json 的 teams → {队名: {"logo": 文件名}}。known：选手、赛事里出现过的队名；别的队、写得不对的去掉。"""
    out = {}
    for name, item in (raw.items() if isinstance(raw, dict) else ()):
        name = _text(name, 40)
        logo = item.get("logo") if isinstance(item, dict) else None
        if name in known and isinstance(logo, str) and LOGO_RE.fullmatch(logo):
            out[name] = {"logo": logo}
    return out


def logo_url(data, team):
    """这支队的队徽图片地址；没有队徽返回 None。"""
    logo = ((data.get("teams") or {}).get(team) or {}).get("logo") if data else None
    return live_status.CONFIG["logo_url"].format(file=logo) if logo and LOGO_RE.fullmatch(logo) else None


def _unique(items, check):
    out, seen = [], set()
    for raw in items:
        item = check(raw)
        if item is not None and item["id"] not in seen:
            out.append(item)
            seen.add(item["id"])
    return out


def clean_streamers(raw, taken):
    """esports.json 的 streamers → [{"id", "name", "platform", "room"}, ...]，照写的顺序。
    taken：{id: (平台, 房间号)}，选手和主舞台已经用了的 id；同一个 id 房间却不一样的去掉。
    最多 STREAMERS_MAX 位，其中抖音最多 STREAMERS_DOUYIN_MAX 位。"""
    out, douyin = [], 0
    for streamer in _unique(raw[:200] if isinstance(raw, list) else [], clean_stream):
        if taken.get(streamer["id"], (streamer["platform"], streamer["room"])) != (streamer["platform"], streamer["room"]):
            continue
        if streamer["platform"] == "douyin":
            douyin += 1
            if douyin > STREAMERS_DOUYIN_MAX:
                continue
        out.append(streamer)
        if len(out) == STREAMERS_MAX:
            break
    return out


def clean(data):
    """检查整个文件，返回清理过的内容；整个不对抛 EsportsError。"""
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise EsportsError("赛事文件的格式不对")
    if not isinstance(data.get("players"), list) or not isinstance(data.get("events"), list):
        raise EsportsError("赛事文件的格式不对")
    switches = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
    players, events = _unique(data["players"][:2000], clean_player), _unique(data["events"][:200], clean_event)
    known = {player["team"] for player in players} | {team for event in events for team in event["teams"]}
    taken = {item["id"]: (item["platform"], item["room"])
             for item in players + [stream for event in events for stream in event.get("main") or []]}
    return {"version": VERSION, "updated": data["updated"] if parse_time(data.get("updated")) else None,
            "platforms": {key: switches.get(key) is not False for key in live_status.PLATFORMS},
            "players": players, "events": events, "teams": clean_teams(data.get("teams"), known),
            "streamers": clean_streamers(data.get("streamers"), taken)}


def _address(url):
    """EVO_ESPORTS_URL 写的是本机的一个文件时，换成 file:// 地址。"""
    return url if "://" in url else Path(url).resolve().as_uri()


def fetch(url=None, timeout=TIMEOUT):
    """下载并检查 esports.json，返回清理过的内容；连不上、格式不对抛 EsportsError。"""
    return _fetch_routed(url, timeout)[0]


def _fetch_routed(url=None, timeout=TIMEOUT):
    """同 fetch，返回 (清理过的内容, 从哪取的：net_route.DIRECT / RELAY)。正式地址连不上时经国内备用线路取同一个文件
    （net_route.get，2026-11-19 用户决定；cloud_relay.py 只在开发者电脑上），取回来的照样过 clean()。
    地址换成了测试文件、本机文件（EVO_ESPORTS_URL、url）时不走备用线路。"""
    address = _address(url or URL)

    def primary():
        request = urllib.request.Request(address, headers={"User-Agent": f"Evo/{APP_VERSION}", "Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(MAX_BYTES)
        except ValueError as e:
            raise EsportsError(f"连不上（{e}）") from e
        except OSError as e:
            problem = net_route.unreachable(address, e)
            if problem is None:  # 服务器回了 4xx：换条线路也一样
                raise EsportsError(f"连不上（{net_route.labeled(address, e)}）") from e
            raise problem from e

    try:
        body, via, _age = net_route.get("esports", primary, official=address == DEFAULT_URL)
    except net_route.Unreachable as e:
        raise EsportsError(f"连不上（{e}）") from e
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        raise EsportsError("赛事文件的格式不对") from None
    return clean(data), via


def cached():
    """上次存下的：{"fetched_at", "data", "via"}；没有、坏了返回 None。"""
    try:
        saved = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return {"fetched_at": float(saved["fetched_at"]), "data": clean(saved["data"]), "via": saved.get("via")}
    except (OSError, ValueError, KeyError, TypeError, EsportsError):
        return None


def get(force=False, fresh=FRESH_SECONDS):
    """打开赛事页、看直播页时调用（fresh 秒以内下载过就不重下；上次是走国内备用线路取的，至少 RELAY_FRESH 秒以内不重下）。
    返回 {"data": 赛事数据（一次都没读到过是 None）, "ok": 是不是最新的, "error": 没读到的原因, "fetched_at": data 是什么时候下载的}。"""
    saved = cached()
    if saved and saved["via"] == net_route.RELAY:
        fresh = max(fresh, RELAY_FRESH)
    if saved and not force and 0 <= time.time() - saved["fetched_at"] < fresh:
        return {"data": saved["data"], "ok": True, "error": None, "fetched_at": saved["fetched_at"]}
    try:
        data, via = _fetch_routed()
    except EsportsError as e:
        return {"data": saved["data"] if saved else None, "ok": False, "error": str(e),
                "fetched_at": saved["fetched_at"] if saved else None}
    now = time.time()
    try:  # 先写临时文件再换过去，写到一半断电也不会留下半个文件
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps({"fetched_at": now, "data": data, "via": via}, ensure_ascii=False), encoding="utf-8")
        temp.replace(CACHE_PATH)
    except OSError:
        pass
    return {"data": data, "ok": True, "error": None, "fetched_at": now}


def status(event, now=None):
    """这场赛事现在是 "live" 进行中 / "upcoming" 即将开始 / "ended" 已结束。"""
    now = now or datetime.now().astimezone()
    if now < parse_time(event["start"]):
        return UPCOMING
    return LIVE if now < parse_time(event["end"]) else ENDED


def groups(data, now=None):
    """赛事页的三块：{"live": 正在进行, "upcoming": 即将开始（先开始的在前）, "recent": 最近 7 天结束的（刚结束的在前）}。"""
    now = now or datetime.now().astimezone()
    out = {LIVE: [], UPCOMING: [], "recent": []}
    for event in sorted(data["events"], key=lambda e: parse_time(e["start"])):
        state = status(event, now)
        if state != ENDED:
            out[state].append(event)
        elif now - parse_time(event["end"]) <= timedelta(days=RECENT_DAYS):
            out["recent"].append(event)
    out["recent"].sort(key=lambda e: parse_time(e["end"]), reverse=True)
    return out


def find_event(data, event_id):
    return next((event for event in data["events"] if event["id"] == event_id), None)


def event_teams(data, event):
    """这场赛事的队伍和选手：[(队名, [选手, ...]), ...]，按赛程里写的顺序。"""
    by_team = {}
    for player in data["players"]:
        by_team.setdefault(player["team"], []).append(player)
    return [(team, by_team.get(team, [])) for team in event["teams"]]


def event_players(data, event):
    return [player for _, players in event_teams(data, event) for player in players]


def streaming(players):
    """这些选手里开直播的（去掉 platform 是 "none" 的）：查开播状态、数"几位选手"时用。"""
    return [player for player in players if player["platform"] != NO_STREAM]


def event_main(data, event):
    """这场赛事的主舞台（播导播视角的直播间），照赛程里写的顺序：[{"id", "name", "platform", "room"}, ...]。
    和名单里某位选手同一个 id 的不算（页面上按 id 认人）。"""
    taken = {player["id"] for player in event_players(data, event)}
    return [stream for stream in event.get("main") or [] if stream["id"] not in taken]


def streamers(data):
    """电竞页"主播"一块列的人：名单里所有开直播的选手（照名单的顺序），后面接 streamers 里另外加的（和选手同一个 id 的不重复）。
    没有赛事数据返回 []。"""
    if not data:
        return []
    out, seen = [], set()
    for item in streaming(data.get("players") or []) + (data.get("streamers") or []):
        if item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
    return out


def find_stream(data, stream_id):
    """按 id 找一个直播间：名单里的选手，某场赛事主舞台的一路，或者主播名单里的一位；没有返回 None。"""
    found = next((player for player in data["players"] if player["id"] == stream_id), None)
    if found is None:
        found = next((stream for event in data["events"] for stream in event.get("main") or [] if stream["id"] == stream_id), None)
    if found is None:
        found = next((streamer for streamer in streamers(data) if streamer["id"] == stream_id), None)
    return found


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"赛事文件的地址：{URL}")
    try:
        found = fetch()
    except EsportsError as error:
        raise SystemExit(f"没读到：{error}") from None
    off = [live_status.NAMES[key] for key, on in found["platforms"].items() if not on]
    print(f"{len(found['events'])} 场赛事，{len(found['players'])} 位选手，更新于 {found['updated']}"
          + (f"，关掉的平台：{'、'.join(off)}" if off else ""))
    words = {LIVE: "进行中", UPCOMING: "即将开始", ENDED: "已结束"}
    for item in found["events"]:
        print(f"  · [{words[status(item)]}] {item['start'][:16].replace('T', ' ')} {item['type']} {item['name']}"
              f"（{item['stage']}）{len(item['teams'])} 支队、{len(event_players(found, item))} 位选手")
