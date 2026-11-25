"""弹幕（看直播页用，只看不发）：告诉页面一个直播间的弹幕要连哪里、带什么。连接本身由页面里的脚本直接连平台的弹幕服务器
（浏览器自带的 WebSocket），不经过这里、也不经过开发者的服务器；这里只做页面做不了的那一步。

2026-11-19 用户要的（看了开源的 PureLive https://github.com/wzgrx/PureLive 以后提的，协议是照它读来的，代码是自己写的）。
- 斗鱼：弹幕服务器的地址写在 config/watch.json（douyu 的 danmaku_url），不用登录，只要真实房间号。选手库里写的可能是短号、
  靓号，所以先问一次房间信息（和查开播状态是同一个地址）拿真实房间号
- B站：弹幕服务器要一个口令。后台用**游客身份**向 B站要（getDanmuInfo，要 WBI 签名和游客的设备号 buvid）：
  不带玩家的 B站登录（弹幕只显示内容，用不着名字；登录凭证照旧只用来要视频地址），所以 B站那边看到的是一位没登录的观众。
  签名用的两个 key（nav 接口给）和 buvid（spi 接口给）在内存里记 6 小时。选手库里写的可能是短号，先换成真实房间号
- 抖音：页面连不了（连接时要带网页脚本带不了的 Cookie 和来源页），由托盘里的 Evo 去连、再转给页面（douyin_chat.py，
  面板的 /esports/danmaku-feed/<选手>）。这里只回 {"platform": "douyin"}，转弹幕的地址由面板填
- Twitch（2026-11-25）：官方的聊天服务器（IRC over WebSocket，地址在 config/watch.json 的 twitch 的 danmaku_url），
  匿名只读：页面直接连，发 PASS SCHMOOPIIE、NICK justinfan…、JOIN #频道（不登录，只看不发）。这里回平台、地址和小写的登录名，
  不用联网、不用口令；表情就显示成文字（如 KEKW），不加载表情图
- 虎牙还没做（Evo 里本来就不播虎牙）：返回 None，页面就不显示弹幕的开关

用法：python code/danmaku.py 房间号 [douyu / bilibili / twitch]   → 打印页面会拿到的东西（B站的口令不打印）
"""

import hashlib
import json
import sys
import threading
import time
import urllib.parse

import bili_login
import live_status

KEEP_SECONDS = 6 * 3600  # B站签名用的 key、游客的设备号记这么久
# B站 WBI 签名：把两个 key 接起来，照这张表挑字符，取前 32 个
MIXIN = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
         37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)
_bili = {"at": 0.0, "key": "", "cookie": "", "buvid": ""}
_lock = threading.Lock()


def douyu_room(body):
    """斗鱼房间信息的回包 → 真实房间号（字符串）；没有返回 None。外面包的层数不一样，和 live_status.parse_douyu 一样找。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    outer = data if isinstance(data, dict) else {}
    inner = outer.get("data") if isinstance(outer.get("data"), dict) else {}
    room = inner.get("room") or outer.get("data") or outer.get("room") or outer
    rid = str(room.get("room_id") or "") if isinstance(room, dict) else ""
    return rid if rid.isdigit() and len(rid) <= 24 else None


def douyu(room, get=None):
    cfg = live_status.PLATFORMS["douyu"]
    url = cfg.get("danmaku_url")
    if not isinstance(url, str) or not url.startswith("wss://"):
        return None
    body = (get or live_status.fetch)(cfg["info_url"].format(room=room), live_status._headers("douyu", room))
    rid = douyu_room(body)
    if rid is None:
        raise live_status.StatusError("斗鱼回的数据里没有房间号")
    return {"platform": "douyu", "url": url, "room": rid}


def _stem(url):
    """https://…/wbi/7cd084941338484aae1ad9425b84077c.png → 7cd084941338484aae1ad9425b84077c"""
    return str(url or "").rsplit("/", 1)[-1].split(".", 1)[0]


def wbi_key(img_url, sub_url):
    """nav 接口给的两个图片地址 → 签名用的 32 个字符；不够长返回 ""。"""
    joined = _stem(img_url) + _stem(sub_url)
    return "".join(joined[i] for i in MIXIN)[:32] if len(joined) >= 64 else ""


def wbi_sign(params, key, now=None):
    """给一组参数签名，返回可以直接接在地址后面的字符串（带 wts 和 w_rid）。"""
    items = {**params, "wts": int(time.time() if now is None else now)}
    clean = {name: "".join(c for c in str(items[name]) if c not in "!'()*") for name in sorted(items)}
    query = urllib.parse.urlencode(clean, quote_via=urllib.parse.quote)
    return query + "&w_rid=" + hashlib.md5((query + key).encode("utf-8")).hexdigest()  # noqa: S324  B站规定用 MD5，不是拿来保密的


def _bili_guest(ask, now):
    """游客身份要用的东西：(签名的 key, 游客 Cookie, buvid3)。KEEP_SECONDS 以内用记着的。"""
    cfg = live_status.PLATFORMS["bilibili"]
    with _lock:
        if _bili["key"] and 0 <= now - _bili["at"] < KEEP_SECONDS:
            return _bili["key"], _bili["cookie"], _bili["buvid"]
    spi = (ask(cfg["spi_url"])[0].get("data") or {})
    buvid3, buvid4 = str(spi.get("b_3") or ""), str(spi.get("b_4") or "")
    cookie = f"buvid3={buvid3}; buvid4={buvid4}" if buvid3 else ""
    images = (ask(cfg["nav_url"], cookie)[0].get("data") or {}).get("wbi_img") or {}  # 没登录回 code -101，这两个地址照样给
    key = wbi_key(images.get("img_url"), images.get("sub_url"))
    if not key:
        raise live_status.StatusError("B站没给签名用的 key")
    with _lock:
        _bili.update(at=now, key=key, cookie=cookie, buvid=buvid3)
    return key, cookie, buvid3


def bilibili(room, ask=None, now=None):
    """B站：{"platform", "urls": [弹幕服务器, ...], "room": 真实房间号, "token": 口令, "buvid"}。ask：测试时换掉联网的函数。"""
    cfg = live_status.PLATFORMS["bilibili"]
    first = cfg.get("danmaku_url")
    if not isinstance(first, str) or not first.startswith("wss://"):
        return None
    ask = ask or bili_login.request
    moment = time.time() if now is None else now
    try:
        key, cookie, buvid = _bili_guest(ask, moment)
        init = ask(cfg["room_init_url"].format(room=room), cookie)[0]
        long_id = int((init.get("data") or {}).get("room_id") or 0)
        if init.get("code") != 0 or long_id <= 0:
            raise live_status.StatusError(f"B站没给真实房间号（code={init.get('code')}）")
        body = ask(cfg["danmaku_info_url"] + "?" + wbi_sign({"id": long_id, "type": 0}, key, moment), cookie)[0]
    except bili_login.BiliError as e:
        raise live_status.StatusError(str(e)) from e
    data = body.get("data") or {}
    token = data.get("token")
    if body.get("code") != 0 or not isinstance(token, str) or not token:
        if body.get("code") == -352:  # 风控：下次重新要 key 和设备号
            with _lock:
                _bili["at"] = 0.0
        raise live_status.StatusError(f"B站没给弹幕的口令（code={body.get('code')}）")
    urls = [first]
    for host in data.get("host_list") or []:
        name, port = str(host.get("host") or ""), host.get("wss_port")
        if name.endswith(".bilibili.com") and all(c.isalnum() or c in ".-" for c in name) and isinstance(port, int) and 0 < port < 65536:
            url = f"wss://{name}{'' if port == 443 else f':{port}'}/sub"
            if url not in urls:
                urls.append(url)
    return {"platform": "bilibili", "urls": urls[:4], "room": long_id, "token": token, "buvid": buvid}


def douyin(room, get=None):
    """抖音：没有页面能直接连的地址，只告诉页面"有"（转弹幕的地址由面板填）。"""
    return {"platform": "douyin"}


def twitch(room, get=None):
    """Twitch：官方的聊天服务器，匿名只读，页面直接连（不联网、不用口令）。房间号是登录名，转小写。"""
    url = live_status.PLATFORMS["twitch"].get("danmaku_url")
    if not isinstance(url, str) or not url.startswith("wss://"):
        return None
    return {"platform": "twitch", "url": url, "room": str(room).lower()}


SOURCES = {"douyu": douyu, "bilibili": bilibili, "douyin": douyin, "twitch": twitch}


def supported(platform):
    """这个平台的弹幕做了没有、配置里开着没有。"""
    return platform in SOURCES and bool(live_status.PLATFORMS.get(platform, {}).get("danmaku_url"))


def info(platform, room, get=None):
    """页面连弹幕要的东西：{"platform", "url", "room", ...}；这个平台没做返回 None。平台那边出错抛 live_status.StatusError。"""
    if not supported(platform) or not live_status.room_ok(platform, room):
        return None
    return SOURCES[platform](room, get)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    found = info(sys.argv[2] if len(sys.argv) > 2 else "douyu", sys.argv[1])
    if found and "token" in found:
        found = {**found, "token": f"（{len(found['token'])} 个字符，不打印）"}
    print(found)
