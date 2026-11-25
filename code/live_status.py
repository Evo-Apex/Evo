"""查直播间现在在不在播（斗鱼、虎牙、B站、抖音、Twitch 的公开网页接口，不登录）：赛事页、看直播页和 tools/esports.py --check 用。

只查交给它的房间（esports.json 名单里的），只在页面开着或开发者运行 --check 时查；同时最多 CONFIG["parallel"] 个请求
（用户定的：要克制），配置里写了 batch_size 的平台一次查多个房间（B站 30、Twitch 100），别的平台一个一个问。
请求和浏览器打开直播间时发的一样（带浏览器的 User-Agent，不带 Evo 的版本号、不发送任何战绩或电脑信息）；
抖音给的游客 Cookie（ttwid）只留在内存里，不存盘，不登录任何平台账号。
接口地址、请求头都在 config/watch.json（平台改了接口先改那里）；怎么查是照 DTV（https://github.com/chen-zeong/DTV ）
的流程写的，每个平台回什么见 watch.json 里的说明。抖音 2026-11-20 起照 PureLive（https://github.com/wzgrx/PureLive ）
先问 room/web/enter（省流量），问不到再走 DTV 那两步。Twitch（2026-11-25）也照 PureLive：GraphQL 的 users(logins:)
一次查多个，匿名，不登录。Twitch 在中国大陆上不去：连不上（网络层失败，不是 HTTP 4xx/5xx）时这一批结果都标着
"unreachable"，之后 unreachable_retry_seconds 秒以内再查 Twitch 不发请求、马上回同样的"连不上"（只记在内存里）。

每个房间的结果：{"state": "live" 在播 / "offline" 没开播 / "missing" 房间不存在 / "error" 没查到,
                "loop": 是不是在轮播（算没开播）, "name": 平台上的主播名, "title": 直播标题, "avatar": 头像地址,
                "since": 开播时刻（time.time() 那种秒数，不知道是 None）, "error": 没查到的原因,
                "unreachable": 是不是连不上这个平台（只有 Twitch 会，别的平台一直是 False）}
房间号只接受数字；抖音还可以写主播的抖音号、Twitch 写登录名（字母、数字、下划线，
config/watch.json 里的 room_pattern）：esports.json 被人改了也只能让 Evo 去问这几个平台的直播间，不能让它连别的地址（room_ok()）。

用法：python code/live_status.py 斗鱼 9999 B站 6   → 查这几个房间并打印
      python code/live_status.py Twitch imperialhal__   → 查 Twitch（在中国大陆要能访问国外网站）
"""

import gzip
import http.cookiejar
import json
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import abogus
from paths import CONFIG_DIR

CONFIG = json.loads((CONFIG_DIR / "watch.json").read_text(encoding="utf-8"))
PLATFORMS = CONFIG["platforms"]
NAMES = {key: platform["name"] for key, platform in PLATFORMS.items()}  # {"douyu": "斗鱼", ...}
LIVE, OFFLINE, MISSING, ERROR = "live", "offline", "missing", "error"
BEIJING = timezone(timedelta(hours=8))
ROOM_RE = re.compile(r"\d{1,24}")  # 房间号：只能是数字（配置里没写 room_pattern 的平台）
# 每个平台的房间号可以怎么写：抖音还收字母和下划线（主播的抖音号），Twitch 收登录名（字母、数字、下划线），
# 拼不出 / ? # 这些能指到别处的字符
ROOM_RES = {key: re.compile(platform.get("room_pattern") or ROOM_RE.pattern) for key, platform in PLATFORMS.items()}
DOUYIN_ROOM_ID = re.compile(rb'\\"roomId\\":\\"(\d{10,})\\"|"roomId":"(\d{10,})"|\\"room_id\\":\\"(\d{10,})\\"')
MAX_BYTES = 1_000_000
CHUNK = 64 * 1024
TWITCH_QUERY = ("query($l:[String!]){users(logins:$l){login displayName profileImageURL(width:70) "
                "stream{createdAt title type}}}")  # 一次查多个登录名（最多 100 个）
_device = [""]  # Twitch 的 Device-ID：32 位小写十六进制，进程里生成一次，只在内存里（不存盘、不交给页面）
_twitch_down = [0.0]  # 这个时刻以前不再问 Twitch（刚才连不上），直接回"连不上"

_cookies = http.cookiejar.CookieJar()  # 只给抖音用：游客 Cookie，只在内存里
_cookie_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))


def device_id():
    """Twitch 的 Device-ID：32 位小写十六进制随机数，进程里只生成一次，只在内存里。"""
    if not _device[0]:
        _device[0] = secrets.token_hex(16)
    return _device[0]


class StatusError(Exception):
    """这次没查到（连不上、被拦、回的格式不认识）。status：HTTP 状态码（没连上是 None）；
    body：平台回的那段（只有 HTTP 错误时有，最多两千字节；Twitch 的 403 靠它说明原因）。"""

    def __init__(self, message, status=None, body=b""):
        super().__init__(message)
        self.status = status
        self.body = body


def room_ok(platform, room):
    """这个平台的房间号写得对不对（平台不认识也算不对）。"""
    return platform in ROOM_RES and isinstance(room, str) and ROOM_RES[platform].fullmatch(room) is not None


def room_url(platform, room):
    """这个直播间的网页地址（给"去 X 看"用）；平台、房间号不对返回 None。"""
    if not room_ok(platform, str(room)):
        return None
    return PLATFORMS[platform]["room_url"].format(room=room)


def result(state, name="", title="", avatar="", since=None, loop=False, error="", unreachable=False):
    since = since if isinstance(since, (int, float)) and since > 0 else None
    return {"state": state, "loop": bool(loop), "name": str(name or ""), "title": str(title or ""),
            "avatar": str(avatar or ""), "since": since, "error": error, "unreachable": bool(unreachable)}


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _dict(value):
    return value if isinstance(value, dict) else {}


def _json(body, who):
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise StatusError(f"{who}回的不是房间数据")
    return data


# ---------- 联网 ----------

def fetch(url, headers, limit=MAX_BYTES, until=None, cookies=False, data=None, timeout=None):
    """下载一个地址（data 是字节时 POST 这些字节，不给是 GET），最多读 limit 字节；给了 until（正则）时读到它出现就停
    （抖音的直播间网页有 1 MB，要的房间号在前面）。timeout 没给时用 CONFIG["timeout"]（Twitch 自己在配置里写了 5 秒）。
    连不上、状态码不是 200 抛 StatusError。测试时整个换掉。"""
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = _cookie_opener.open if cookies else urllib.request.urlopen
    wait = CONFIG["timeout"] if timeout is None else timeout
    try:
        with opener(request, timeout=wait) as response:
            body = b""
            while len(body) < limit:
                chunk = response.read(min(CHUNK, limit - len(body)))
                if not chunk:
                    break
                tail = len(body) - 200  # 要找的字可能正好跨在两块中间
                body += chunk
                if until is not None and until.search(body, max(0, tail)):
                    break
            return body
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(2000)
        except OSError:
            detail = b""
        raise StatusError(f"HTTP {e.code}", e.code, detail) from e
    except OSError as e:
        raise StatusError(f"连不上（{e}）") from e


def _headers(platform, room=None):
    cfg = PLATFORMS[platform]
    headers = {"User-Agent": cfg["user_agent"], "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Referer": cfg.get("referer") or cfg["room_url"].format(room=room)}
    if cfg.get("origin"):
        headers["Origin"] = cfg["origin"]
    if cfg.get("client_id"):  # Twitch：网页版自己的 client id，加一个只在内存里的设备号
        headers["Client-ID"] = cfg["client_id"]
        headers["Device-ID"] = device_id()
    return headers


# ---------- 每个平台：读回包（不联网，测试直接测这些） ----------

def parse_douyu(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        if PLATFORMS["douyu"]["missing_text"] in body.decode("utf-8", "replace"):
            return result(MISSING)
        raise StatusError("斗鱼回的不是房间数据") from None
    # 不同房间外面包的层数不一样（DTV 实测）：{data: {room}} / {data} / {room} / 直接就是房间
    outer = _dict(data)
    room = _dict(outer.get("data")).get("room") or outer.get("data") or outer.get("room") or outer
    room = _dict(room)
    if not room.get("room_id"):
        raise StatusError("斗鱼回的数据里没有房间")
    loop = _int(room.get("videoLoop")) == 1 or _int(room.get("isLoop")) == 1
    live = _int(room.get("show_status")) == 1 and not loop
    return result(LIVE if live else OFFLINE, loop=loop and _int(room.get("show_status")) == 1,
                  name=room.get("nickname") or room.get("owner_name"), title=room.get("room_name"),
                  avatar=room.get("avatar_mid") or _dict(room.get("avatar")).get("middle"),
                  since=_int(room.get("show_time")) if live else None)


def parse_huya(body):
    data = _json(body, "虎牙")
    status = _int(data.get("status"))
    if status == 422 or "不存在" in str(data.get("message") or ""):
        return result(MISSING)
    info = _dict(data.get("data"))
    if status != 200 or not info:
        raise StatusError(f"虎牙回了 status={data.get('status')}")
    profile, live_data = _dict(info.get("profileInfo")), _dict(info.get("liveData"))
    state = str(info.get("liveStatus") or "").upper()
    return result(LIVE if state == "ON" else OFFLINE, loop=state == "REPLAY",
                  name=profile.get("nick") or live_data.get("nick"),
                  title=live_data.get("introduction") or live_data.get("roomName"),
                  avatar=profile.get("avatar180") or live_data.get("avatar180"),
                  since=_int(live_data.get("startTime")) if state == "ON" else None)


def parse_bilibili(body, rooms):
    """一次查多个的回包 → {房间号: 结果}。表里写短号、真实房间号都认。"""
    data = _json(body, "B站")
    if _int(data.get("code")) != 0:
        raise StatusError(f"B站回了 code={data.get('code')}（{data.get('message')}）")
    found = {}
    for key, item in _dict(_dict(data.get("data")).get("by_room_ids")).items():
        if isinstance(item, dict):
            found[str(key)] = item
            if _int(item.get("short_id")):
                found[str(_int(item["short_id"]))] = item
    out = {}
    for room in rooms:
        item = found.get(str(room))
        if item is None:
            out[room] = result(MISSING)
            continue
        status = _int(item.get("live_status"))
        since = None
        if status == 1:
            try:
                since = datetime.strptime(str(item.get("live_time")), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=BEIJING).timestamp()
            except ValueError:
                since = None
        out[room] = result(LIVE if status == 1 else OFFLINE, loop=status == 2, name=item.get("uname"),
                           title=item.get("title"), since=since)
    return out


def parse_douyin_page(body):
    """抖音直播间网页 → 内部房间号（每次开播都换，查状态要用它）；页面里没有返回 None，被拦抛 StatusError。"""
    found = DOUYIN_ROOM_ID.search(body)
    if found:
        return next(group for group in found.groups() if group).decode("ascii")
    text = body.decode("utf-8", "replace")
    if any(mark in text for mark in PLATFORMS["douyin"]["blocked_text"]):
        raise StatusError("被抖音拦住了（要验证码），过一会儿再试")
    return None


def parse_douyin_enter(body):
    """抖音 room/web/enter 的回包 → (结果, 这一场的内部房间号)；没在播时内部房间号是 None。回包里没有开播时间。
    房间不存在（status_code 4001038）是 MISSING；别的不认识的抛 StatusError。"""
    data = _json(body, "抖音")
    code = _int(data.get("status_code"))
    if code == 4001038:
        return result(MISSING), None
    inner = _dict(data.get("data"))
    rooms = inner.get("data")
    if code != 0 or not isinstance(rooms, list):
        raise StatusError(f"抖音没给房间数据（status_code={data.get('status_code')}）")
    room = _dict(rooms[0]) if rooms else {}
    owner, user = _dict(room.get("owner")), _dict(inner.get("user"))

    def avatar(person):
        urls = _dict(person.get("avatar_thumb")).get("url_list")
        return urls[0] if isinstance(urls, list) and urls else ""

    live = _int(room.get("status")) == 2
    found = result(LIVE if live else OFFLINE, name=owner.get("nickname") or user.get("nickname"), title=room.get("title"),
                   avatar=avatar(owner) or avatar(user))
    return found, (str(room.get("id_str") or "") or None) if live else None


def parse_douyin_room(body):
    data = _json(body, "抖音")
    room = _dict(_dict(data.get("data")).get("room"))
    if not room:
        raise StatusError(f"抖音没给房间数据（status_code={data.get('status_code')}）")
    owner = _dict(room.get("owner"))
    avatars = _dict(owner.get("avatar_thumb")).get("url_list")
    live = _int(room.get("status")) == 2
    return result(LIVE if live else OFFLINE, name=owner.get("nickname"), title=room.get("title"),
                  avatar=avatars[0] if isinstance(avatars, list) and avatars else "",
                  since=_int(room.get("create_time")) if live else None)


def _utc(text):
    """"2026-11-25T14:20:13Z"（Twitch 的 createdAt，UTC）→ time.time() 那种秒数；写错了是 None。"""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc).timestamp()


def parse_twitch(body, rooms):
    """Twitch 的 users(logins:) 回包 → {房间号: 结果}，按交进来的房间号原样回（大小写都认，对的时候按小写的 login）。
    stream 是 null = 没开播；type 是 "live" = 在播（createdAt 是开播时刻），"rerun" = 重播（当轮播，算没开播）。
    回包里的人的 login 没有对应的房间就是"房间不存在"；回包里有 errors 当作这次没查到。"""
    data = _json(body, "Twitch")
    if data.get("errors"):
        raise StatusError("Twitch 回了错误")
    users = _dict(data.get("data")).get("users")
    if not isinstance(users, list):
        raise StatusError("Twitch 回的不是房间数据")
    found = {}
    for user in users:
        if isinstance(user, dict) and user.get("login"):
            found[str(user["login"]).lower()] = user
    out = {}
    for room in rooms:
        user = found.get(str(room).lower())
        if user is None:
            out[room] = result(MISSING)
            continue
        stream = _dict(user.get("stream"))
        kind = str(stream.get("type") or "").lower()
        live = kind == "live"
        out[room] = result(LIVE if live else OFFLINE, loop=kind == "rerun",
                           name=user.get("displayName"), title=stream.get("title"),
                           avatar=user.get("profileImageURL"), since=_utc(stream.get("createdAt")) if live else None)
    return out


# ---------- 每个平台：查（rooms → {房间号: 结果}） ----------

def check_douyu(rooms, get):
    out = {}
    for room in rooms:
        try:
            body = get(PLATFORMS["douyu"]["info_url"].format(room=room), _headers("douyu", room))
        except StatusError as e:
            if e.status != 404:
                raise
            out[room] = result(MISSING)
        else:
            out[room] = parse_douyu(body)
    return out


def check_huya(rooms, get):
    return {room: parse_huya(get(PLATFORMS["huya"]["info_url"].format(room=room), _headers("huya", room)))
            for room in rooms}


def check_bilibili(rooms, get):
    url = PLATFORMS["bilibili"]["batch_url"] + "".join(f"&room_ids={room}" for room in rooms)
    return parse_bilibili(get(url, _headers("bilibili")), rooms)


_douyin_inner = {}  # 抖音房间号 → 正在播的那一场的内部房间号（只在内存里）
_douyin_since = {}  # 抖音房间号 → (内部房间号, 这一场的开播时刻)：enter 接口不给开播时间，每一场问一次 reflow 记下
_douyin_enter_skip = [0.0]  # 这个时刻以前不问 enter 接口（刚才没问到：连不上、空回包），先走原来开网页的办法


def check_douyin(rooms, get):
    """先问 room/web/enter（2026-11-20，照 PureLive 的办法：在播约 9 KB，没开播 1–80 KB）；没问到（连不上、空回包、
    回的不认识）这个房间走原来的两步（_douyin_by_page），连不上、空回包时 enter_retry_seconds 秒以内不再试 enter。"""
    out = {}
    for room in rooms:
        if time.time() >= _douyin_enter_skip[0]:
            try:
                out[room] = _douyin_by_enter(room, get)
                continue
            except StatusError:
                pass
        out[room] = _douyin_by_page(room, get)
    return out


def _douyin_by_enter(room, get):
    try:
        body = douyin_enter(room, get)
    except StatusError:
        _douyin_enter_skip[0] = time.time() + (_int(PLATFORMS["douyin"].get("enter_retry_seconds")) or 600)
        raise
    found, inner = parse_douyin_enter(body)
    if found["state"] != LIVE or inner is None:
        _douyin_inner.pop(room, None)
        _douyin_since.pop(room, None)
        return found
    _douyin_inner[room] = inner
    if _douyin_since.get(room, (None, None))[0] != inner:  # 新的一场：问一次开播时间（问不到就先不写，下次再问）
        try:
            _douyin_since[room] = (inner, _douyin_room(inner, get)["since"])
        except StatusError:
            _douyin_since.pop(room, None)
    found["since"] = _douyin_since.get(room, (None, None))[1]
    return found


def _douyin_by_page(room, get):
    """原来的办法（enter 问不到时用）：在播的房间记着它这一场的内部房间号，下次只问接口（约 70 KB）；没在播的每次都要
    开直播间网页（约 1 MB，内部房间号在网页的最后）才知道有没有开新的一场。记着的那一场结束了：这次算没开播，下次再开网页。"""
    inner = _douyin_inner.pop(room, None)
    if inner is None:
        inner = douyin_inner(room, get)
        if inner is None:
            return result(MISSING)
    found = _douyin_room(inner, get)
    if found["state"] == LIVE:
        _douyin_inner[room] = inner
    return found


def douyin_enter(room, get=None):
    """问抖音的 room/web/enter（这个房间现在在不在播），返回回包（解开 gzip 以后的）。要先有游客 Cookie（ttwid）：
    没有就开一下首页，读到响应头就停。连不上、空回包（被拦）抛 StatusError。"""
    cfg, get = PLATFORMS["douyin"], get or fetch
    if not any(cookie.name == "ttwid" for cookie in _cookies):
        get(cfg["home_url"], {**_headers("douyin"), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            limit=1, cookies=True)
    token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(107))
    query = urllib.parse.urlencode([*map(tuple, cfg["enter_params"]), ("web_rid", room), ("msToken", token)])
    body = get(f"{cfg['enter_url']}?{query}", {**_headers("douyin"), "Accept-Encoding": "gzip"}, cookies=True)
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as e:
            raise StatusError("抖音回的东西解不开") from e
    if not body:
        raise StatusError("抖音没回东西（可能被拦住了）")
    return body


def douyin_known(room):
    """查开播状态时记着的、这个房间正在播的那一场的内部房间号；没记着返回 None（取视频地址时先用它，watch_douyin.py）。"""
    return _douyin_inner.get(str(room))


def douyin_inner(room, get=None):
    """开抖音的直播间网页，读出现在这一场的内部房间号；网页里没有返回 None，被拦抛 StatusError。"""
    cfg = PLATFORMS["douyin"]
    page_headers = {**_headers("douyin"), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    page = (get or fetch)(cfg["page_url"].format(room=room), page_headers, limit=cfg["page_max_bytes"],
                          until=DOUYIN_ROOM_ID, cookies=True)
    return parse_douyin_page(page)


def douyin_info(inner, get=None):
    """用内部房间号问抖音的房间接口，返回回包（开播状态、主播、各档清晰度的视频地址都在里面）。"""
    cfg = PLATFORMS["douyin"]
    token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(107))
    params = [*cfg["info_params"], ("room_id", inner), ("msToken", token)]
    url = abogus.signed_url(cfg["info_url"], params, cfg["user_agent"])
    return (get or fetch)(url, _headers("douyin"), cookies=True)


def _douyin_room(inner, get):
    return parse_douyin_room(douyin_info(inner, get))


def retry_twitch():
    """玩家在看直播页点了"重试"（2026-11-25 用户要的）：忘掉刚才的"连不上"，下一次查开播马上再问 Twitch。"""
    _twitch_down[0] = 0.0


def check_twitch(rooms, get=None):
    """一次查多个登录名（users(logins:)，最多 batch_size 个）。登录名转小写、结果按交进来的房间号原样回。
    连不上（StatusError 没有 HTTP 状态码：网络层失败）时还没查到的房间带 unreachable，之后 unreachable_retry_seconds 秒
    以内不再发请求、马上回同样的"连不上"（只记在内存里）。HTTP 4xx/5xx 不算连不上，照旧抛出去；
    回包里有 errors、回的不认识算"这次没查到"（state 是 error，不算连不上、不冷却）。"""
    cfg, get = PLATFORMS["twitch"], get or fetch
    if time.time() < _twitch_down[0]:
        return {room: result(ERROR, error="连不上 Twitch", unreachable=True) for room in rooms}
    size = max(1, _int(cfg.get("batch_size")) or 100)
    wait = cfg.get("timeout") or CONFIG["timeout"]
    headers = {**_headers("twitch"), "Content-Type": "text/plain;charset=UTF-8"}
    out = {}
    for start in range(0, len(rooms), size):
        batch = rooms[start:start + size]
        try:
            body = get(cfg["gql_url"], headers, data=twitch_body(batch), timeout=wait)
        except StatusError as e:
            if e.status is not None:
                raise  # 平台回了 HTTP 错误：照旧当这次没查到（不算连不上）
            _twitch_down[0] = time.time() + (_int(cfg.get("unreachable_retry_seconds")) or 120)
            return {**out, **{room: result(ERROR, error=f"连不上 Twitch（{e}）", unreachable=True)
                              for room in rooms if room not in out}}
        except OSError as e:  # 测试时换掉的联网函数可能直接抛 OSError
            _twitch_down[0] = time.time() + (_int(cfg.get("unreachable_retry_seconds")) or 120)
            return {**out, **{room: result(ERROR, error=f"连不上 Twitch（{e}）", unreachable=True)
                              for room in rooms if room not in out}}
        try:
            out.update(parse_twitch(body, batch))
        except StatusError as e:  # Twitch 回了错误、回的不认识：这一批当作没查到，不算连不上
            out.update({room: result(ERROR, error=str(e)) for room in batch})
    return out


def twitch_body(rooms):
    """Twitch 的 GraphQL 请求正文（查开播状态）：一次问这些登录名（转小写）。"""
    return json.dumps({"query": TWITCH_QUERY, "variables": {"l": [str(room).lower() for room in rooms]}}).encode("utf-8")


CHECKERS = {"douyu": check_douyu, "huya": check_huya, "bilibili": check_bilibili, "douyin": check_douyin,
            "twitch": check_twitch}


def query(players, get=None):
    """查一批选手的直播间。players：[{"id", "platform", "room"}, ...]（esports.json 里的选手就是这样）。
    返回 {选手 id: 结果}。同一个房间只查一次；某个平台出错只影响它自己的房间（state 是 "error"）。
    config 里写了 batch_size 的平台一次查多个房间（B站 30、Twitch 100），别的平台一个一个问。
    get：测试时换掉联网的那个函数。"""
    get = get or fetch
    wanted = {}  # (平台, 房间号) → 结果
    for player in players:
        key = (player.get("platform"), str(player.get("room")))
        if key[0] not in CHECKERS or not room_ok(key[0], key[1]):
            wanted[key] = result(ERROR, error="平台或房间号写得不对")
        else:
            wanted.setdefault(key, None)
    todo = sorted(key for key, value in wanted.items() if value is None)
    jobs = []  # [(平台, [房间号, ...]), ...]：todo 按平台、房间号排好序，一次查多个的平台把相邻的房间号攒成一批
    for platform, room in todo:
        size = _int(PLATFORMS[platform].get("batch_size"))
        if size > 0 and jobs and jobs[-1][0] == platform and len(jobs[-1][1]) < size:
            jobs[-1][1].append(room)
        else:
            jobs.append((platform, [room]))

    def run(job):
        platform, rooms = job
        try:
            return CHECKERS[platform](rooms, get)
        except StatusError as e:
            return {room: result(ERROR, error=str(e)) for room in rooms}
        except Exception as e:  # noqa: BLE001  平台回了没见过的东西：只当这次没查到，不让页面出错
            return {room: result(ERROR, error=f"{type(e).__name__}: {e}") for room in rooms}

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, _int(CONFIG.get("parallel")) or 4)) as pool:
            for (platform, rooms), found in zip(jobs, pool.map(run, jobs)):
                for room in rooms:
                    wanted[(platform, room)] = found.get(room) or result(ERROR, error="没有查到")
    return {player.get("id"): wanted[(player.get("platform"), str(player.get("room")))] for player in players}


def describe(found):
    """一个结果写成一句话（--check 和下面的命令行用）。"""
    if found["state"] == LIVE:
        minutes = f"，开播 {int((time.time() - found['since']) // 60)} 分钟" if found["since"] else ""
        return f"在播{minutes}：{found['name']}「{found['title']}」"
    if found["state"] == OFFLINE:
        return ("在轮播（不算在播）" if found["loop"] else "没开播") + (f"：{found['name']}" if found["name"] else "")
    if found["state"] == MISSING:
        return "房间不存在"
    if found.get("unreachable"):
        return "连不上 Twitch（在国内要能访问国外网站）"
    return f"没查到（{found['error']}）"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    keys = {name: key for key, name in NAMES.items()}
    args = sys.argv[1:]
    if not args or len(args) % 2:
        raise SystemExit(__doc__)
    asked = [{"id": str(i), "platform": keys.get(args[i], args[i]), "room": args[i + 1]} for i in range(0, len(args), 2)]
    started = time.time()
    for player, found in zip(asked, query(asked).values()):
        print(f"{NAMES.get(player['platform'], player['platform'])} {player['room']}：{describe(found)}")
    print(f"用了 {time.time() - started:.1f} 秒")
