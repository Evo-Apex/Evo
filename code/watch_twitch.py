"""Twitch：取一个直播间的视频地址（HLS），并经本机中转播放（看直播页用 Evo 自己的播放器播 Twitch 时用）。

2026-11-25 加的，照开源的 PureLive（https://github.com/wzgrx/PureLive ，AGPL-3.0，和 Evo 一样）读来的接口和做法，
代码是自己写的。用户定的：走 Evo 自己的播放器（和登录以后的 B站一样：后台取 HLS、经本机中转 hls_relay.py 接成一条
TS 流给页面里的 mpegts.js），**不用** Twitch 官方的嵌入播放器（player.twitch.tv）；不登录（匿名就有 1080p60 原画）。

两步（接口地址、client id、查询编号都在 config/watch.json 的 twitch 里）：
1. 向 Twitch 网页版的 GraphQL 要播放令牌（PlaybackAccessToken，固定的查询编号）。回 data.streamPlaybackAccessToken
   = {value, signature}；是 null = 没有这个频道；回包 errors 里有 PersistedQueryNotFound = Twitch 改了接口
2. 拿令牌问 usher（各档的目录，只要 H.264：supported_codecs=h264）。目录里每一档有 GROUP-ID（chunked = 原画，
   还有 720p60、480p30、360p30、160p30，有时有 audio_only 不要）和 NAME；每档的播放列表是 2 秒一段的 TS。
   没开播回 404；403 的回包里写着原因（地区限制、订阅专属）
- **视频经过本机中转**：段和播放列表都在 ttvnw.net 下，服务器虽然回了 Access-Control-Allow-Origin: *，但照 B站的做法
  一律经本机中转（hls_relay.py），不让页面直连：取流只在玩家自己的电脑上、不存盘、不转推
- **广告照放，不过滤**：Twitch 有时把广告插进视频（播放列表里的 #EXT-X-DATERANGE CLASS="twitch-stitched-ad"、
  #EXT-X-DISCONTINUITY）。Evo 原样转，不识别、不跳过、不去掉（PureLive 也不去）
- **中国大陆上不去 Twitch**：玩家要自己开着能访问国外网站的工具（系统代理或 TUN），Evo 不加任何代理设置
  （Python 的 urllib 和 WebView2 在 Windows 上本来就跟着系统代理走）。连不上时抛的 TwitchPlayError 带着
  unreachable=True，面板据此告诉玩家"连不上 Twitch：在国内要能访问国外网站"
- 令牌、签名、段地址都不写进日志（日志里只有平台、房间号、清晰度和"接不接得上"）

用法：python code/watch_twitch.py 登录名 [清晰度]   → 打印清晰度和实际给的那一档（地址只打印主机和路径开头）
"""

import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import hls_relay
import live_status

CONFIG = live_status.PLATFORMS["twitch"]
TIMEOUT = CONFIG.get("timeout") or live_status.CONFIG["timeout"]  # Twitch 单独用 5 秒：国内连不上时少等
SOURCE = "chunked"           # 原画的 GROUP-ID
REUSE_SECONDS = 20           # 刚取过的地址这么久以内直接用（页面先问有哪些清晰度，紧接着就要开播）
MAX_PLAYLIST = 1_000_000     # 多档目录最多读这么多（它只有几十 KB）
MAX_SEGMENT = 64 * 1024 * 1024  # 一段 TS 最多读这么多（2 秒一段，原画约 2 MB）
ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_recent = {}
_lock = threading.Lock()


class TwitchPlayError(Exception):
    """没取到地址。unreachable：连不上 Twitch（网络层失败，不是 HTTP 4xx/5xx），页面据此说"在国内要能访问国外网站"。"""

    def __init__(self, message, unreachable=False):
        super().__init__(message)
        self.unreachable = unreachable


def _attributes(text):
    """HLS 的一行属性 → {名字: 值}（引号里的逗号不算分隔）。"""
    return {name: (value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else value)
            for name, value in ATTRIBUTE.findall(text)}


def _quality_name(name, group, height, fps, source):
    """一档叫什么：目录里的 NAME；原画去掉英文的 (source)、加"原画"（如 1080p60（原画））。"""
    text = " ".join(str(name or "").split())
    text = re.sub(r"\(\s*source\s*\)", "", text, flags=re.I).strip()
    if not text:
        text = f"{height}p{round(fps)}" if height else group
    return f"{text}（原画）" if source else text


def parse_master(text, base=""):
    """usher 的多档目录 → [{"id", "name", "urls", "height", "fps", "bandwidth", "source"}, ...]，从好到差。
    id 是 GROUP-ID（chunked、720p60、480p30…），name 是 NAME；不要只有声音的档（audio_only）。"""
    if not text.lstrip().startswith("#EXTM3U"):
        raise TwitchPlayError("Twitch 回的不是播放列表")
    names, found, pending = {}, {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = _attributes(line[len("#EXT-X-MEDIA:"):])
            group = attributes.get("GROUP-ID", "")
            if group and attributes.get("TYPE", "").upper() == "VIDEO":
                names.setdefault(group, attributes.get("NAME", ""))
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attributes(line[len("#EXT-X-STREAM-INF:"):])
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        attributes, pending = pending, None
        group = attributes.get("VIDEO", "")
        if not group or group.lower() == "audio_only":  # 只有声音的那一档不要
            continue
        url = urllib.parse.urljoin(base, line)
        if not url.startswith("https://"):
            continue
        entry = found.setdefault(group, {"id": group, "urls": [], "height": 0, "fps": 0.0, "bandwidth": 0})
        if url not in entry["urls"]:
            entry["urls"].append(url)
        size = re.fullmatch(r"(\d+)x(\d+)", attributes.get("RESOLUTION", ""))
        entry["height"] = max(entry["height"], int(size.group(2)) if size else 0)
        try:
            entry["fps"] = max(entry["fps"], float(attributes.get("FRAME-RATE") or 0))
            entry["bandwidth"] = max(entry["bandwidth"], int(attributes.get("BANDWIDTH") or 0))
        except ValueError:
            pass
    qualities = [entry for entry in found.values() if entry["height"] > 0 and entry["urls"]]
    for entry in qualities:
        entry["source"] = entry["id"] == SOURCE
        entry["name"] = _quality_name(names.get(entry["id"]), entry["id"], entry["height"], entry["fps"], entry["source"])
    # 从好到差：原画在最前，然后分辨率高的、帧率高的、码率大的
    qualities.sort(key=lambda q: (not q["source"], -q["height"], -q["fps"], -q["bandwidth"]))
    if not qualities:
        raise TwitchPlayError("Twitch 没给清晰度（可能没开播）")
    return qualities


def pick(qualities, rate=None):
    """要哪一档：玩家选的（这次没有就给最好的一档）；没选过给原画（chunked），没有原画给最好的一档。"""
    for quality in qualities:
        if rate and quality["id"] == str(rate):
            return quality
    if not rate:
        return next((quality for quality in qualities if quality["source"]), qualities[0])
    return qualities[0]


def _headers():
    """带 Client-ID 和只在内存里的 Device-ID（live_status._headers）。"""
    return live_status._headers("twitch")


def _gql_headers():
    return {**_headers(), "Content-Type": "text/plain;charset=UTF-8"}


def _media_headers():
    """段和播放列表：User-Agent、Origin、Referer（不带登录凭证，也没有 Cookie）。"""
    return {"User-Agent": CONFIG["user_agent"], "Accept": "*/*", "Origin": CONFIG["origin"],
            "Referer": CONFIG["referer"]}


def _problem(error):
    """联网出的错 → 给玩家看的 TwitchPlayError。网络层失败（没有 HTTP 状态码）带着 unreachable。"""
    if isinstance(error, live_status.StatusError):
        if error.status is None:
            return TwitchPlayError("连不上 Twitch（在国内要能访问国外网站）", unreachable=True)
        return TwitchPlayError(f"Twitch 回了 HTTP {error.status}")
    if isinstance(error, OSError):
        return TwitchPlayError("连不上 Twitch（在国内要能访问国外网站）", unreachable=True)
    return TwitchPlayError(str(error))


def token_body(login):
    """要播放令牌的 GraphQL 请求正文（固定的查询编号在 config/watch.json 的 token_hash）。"""
    return json.dumps({
        "operationName": "PlaybackAccessToken",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": CONFIG["token_hash"]}},
        "variables": {"isLive": True, "login": str(login).lower(), "isVod": False, "vodID": "", "playerType": "site",
                      "isClip": False, "clipID": "", "platform": "site"}}).encode("utf-8")


def access_token(login, ask=None):
    """要播放令牌 → {"value", "signature"}。没有这个频道、Twitch 改了接口、连不上都抛 TwitchPlayError。"""
    ask = ask or live_status.fetch
    try:
        body = ask(CONFIG["gql_url"], _gql_headers(), data=token_body(login), timeout=TIMEOUT)
    except (live_status.StatusError, OSError) as e:
        raise _problem(e) from e
    try:
        data = json.loads(body.decode("utf-8"))
    except (AttributeError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise TwitchPlayError("Twitch 回的不是播放令牌")
    messages = [str((error or {}).get("message") or "") for error in data.get("errors") or [] if isinstance(error, dict)]
    if any("PersistedQueryNotFound" in message for message in messages):
        raise TwitchPlayError("Twitch 改了接口（播放令牌的查询编号对不上了），要等 Evo 更新")
    if messages:
        raise TwitchPlayError("Twitch 没给播放令牌（" + "；".join(messages)[:120] + "）")
    found = (data.get("data") or {}).get("streamPlaybackAccessToken") if isinstance(data.get("data"), dict) else None
    if not isinstance(found, dict):
        raise TwitchPlayError("Twitch 上没有这个直播间")
    value, signature = found.get("value"), found.get("signature")
    if not isinstance(value, str) or not isinstance(signature, str) or not value or not signature:
        raise TwitchPlayError("Twitch 没给播放令牌")
    return {"value": value, "signature": signature}


def usher_url(login, token):
    """各档目录的地址（只问 H.264）：ps 里的 p 是随机整数，sig、token 来自播放令牌。"""
    params = [("allow_source", "true"), ("platform", "web"), ("player_backend", "mediaplayer"),
              ("playlist_include_framerate", "true"), ("fast_bread", "true"), ("supported_codecs", "h264"),
              ("p", str(secrets.randbelow(10_000_000))), ("sig", token["signature"]), ("token", token["value"])]
    return (CONFIG["usher_url"].format(login=urllib.parse.quote(str(login).lower())) + "?"
            + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))


def refused(error):
    """usher 的 403：回包里写着原因（地区限制、订阅专属），翻成给玩家看的中文。"""
    text = (getattr(error, "body", b"") or b"").decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    reason = " ".join(str((data or {}).get(key) or "") for key in ("error_code", "error")) if isinstance(data, dict) else ""
    words = (reason + " " + text).lower()
    if "geo" in words:
        return "Twitch 说这个直播间在你所在的地区看不了（在国内要能访问国外网站）"
    if "sub" in words:  # subscriber only
        return "Twitch 说这场直播只给订阅者看"
    return "Twitch 不让看这场直播" + (f"（{reason.strip()}）" if reason.strip() else "（HTTP 403）")


def master(login, token, ask=None):
    """问 usher 要多档目录 → parse_master() 的结果。没开播、地区限制、订阅专属、连不上都抛 TwitchPlayError。"""
    ask = ask or live_status.fetch
    url = usher_url(login, token)
    try:
        body = ask(url, _media_headers(), limit=MAX_PLAYLIST, timeout=TIMEOUT)
    except live_status.StatusError as e:
        if e.status is None:
            raise _problem(e) from e
        if e.status == 404:
            raise TwitchPlayError("这个直播间现在没开播") from e
        if e.status == 403:
            raise TwitchPlayError(refused(e)) from e
        raise _problem(e) from e
    except OSError as e:
        raise _problem(e) from e
    return parse_master(body.decode("utf-8", "replace"), url)


def resolve(room, rate=None, ask=None, now=None, reuse=False):
    """取地址。room：登录名（大小写都行，查的时候转小写）。rate：清晰度 id（None = 原画）。
    返回 {"qualities": [{"id", "name"}], "rate": 实际给的那一档的 id, "kind": "mpegts",
          "hls": [这一档的播放列表地址], "ttl": None}。
    reuse：REUSE_SECONDS 秒以内 remember() 过同一个（房间, 清晰度）就直接用（只用一次）。"""
    login = str(room).lower()
    wanted = str(rate) if rate else SOURCE
    moment = time.time() if now is None else now
    if reuse:
        with _lock:
            saved = _recent.pop((login, wanted), None)
        if saved and 0 <= moment - saved[0] < REUSE_SECONDS:
            return saved[1]
    token = access_token(login, ask)
    qualities = master(login, token, ask)
    chosen = pick(qualities, rate)
    return {"qualities": [{"id": quality["id"], "name": quality["name"]} for quality in qualities],
            "rate": chosen["id"], "kind": "mpegts", "hls": chosen["urls"], "ttl": None}


def remember(room, rate, found, now=None):
    """页面刚问过清晰度：把取到的地址留着，紧接着开播时直接用（一次只播一路，只留最近的一个）。"""
    with _lock:
        _recent.clear()
        _recent[(str(room).lower(), str(rate) if rate else SOURCE)] = (time.time() if now is None else now, found)


def fetch(url):
    """取播放列表或一段 TS，返回字节；连不上、不是 200 抛 OSError（hls_relay 用）。"""
    request = urllib.request.Request(url, headers=_media_headers())
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read(MAX_SEGMENT)


def open_relay(room, rate=None, on_event=None):
    """这个房间的一路中转（面板的 /esports/stream 用）：open_playlist 第一次用刚取到的地址，
    之后（地址过期、断了）重新要令牌和目录、取同一档（按 id 对）。"""
    first = resolve(room, rate, reuse=True)
    pending = [first]

    def open_playlist():
        found = pending.pop() if pending else resolve(room, first["rate"])
        return found["hls"]

    return hls_relay.HlsRelay(open_playlist, fetch, on_event=on_event)


def mask(url):
    """地址里的令牌不打印：只留主机和路径的开头。"""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path[:24]}…"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or not live_status.room_ok("twitch", sys.argv[1]):
        raise SystemExit(__doc__)
    try:
        result = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except TwitchPlayError as error:
        print(f"没取到：{error}")
        raise SystemExit(1) from None
    print("清晰度：", "、".join(f"{q['name']}（{q['id']}）" for q in result["qualities"]))
    print("实际给的：", result["rate"])
    for url in result["hls"]:
        print("  播放列表：", mask(url))
