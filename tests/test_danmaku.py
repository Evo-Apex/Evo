"""弹幕（code/danmaku.py 和面板的 /esports/danmaku/<选手>）：不联网。

- 斗鱼：从房间信息里拿真实房间号（外面包几层都认）；弹幕服务器的地址照配置给，必须是 wss://
- B站：用游客身份要弹幕的口令（WBI 签名、游客的设备号，记 6 小时），不带玩家的登录；短号换成真实房间号；弹幕服务器只认 B站自己的
- Twitch（2026-11-25）：不用联网，回官方聊天服务器的地址（照配置）和小写的登录名（匿名只读，页面自己连）
- 没做的平台、房间号写得不对：不给
- 面板的接口：只给能在窗口里用 Evo 自己的播放器播、弹幕做了的选手；平台那边没问到说明原因
- 看直播页给脚本的数据里，只有这样的选手带着问弹幕的地址；页面上有弹幕的开关和连斗鱼的脚本

运行：python tests/test_danmaku.py
"""

import hashlib
import json
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import bili_login
import danmaku
import esports_data
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)


def body(room):
    return json.dumps(room).encode("utf-8")


def main():
    print("弹幕（斗鱼、B站：要连哪里、面板的接口、页面）")
    out = {}
    out["斗鱼：从房间信息里拿真实房间号，外面包几层都认；没有、不是数字、不是 JSON 就是没有"] = (
        danmaku.douyu_room(body({"room": {"room_id": 9263298}})) == "9263298"
        and danmaku.douyu_room(body({"data": {"room": {"room_id": "52"}}})) == "52"
        and danmaku.douyu_room(body({"data": {"room_id": 7}})) == "7" and danmaku.douyu_room(body({"room_id": 8})) == "8"
        and danmaku.douyu_room(body({"room": {"room_id": "abc"}})) is None and danmaku.douyu_room(body({"room": {}})) is None
        and danmaku.douyu_room(b"<html>") is None and danmaku.douyu_room(body([1, 2])) is None)
    asked = []

    def get(url, headers):
        asked.append((url, headers))
        return body({"room": {"room_id": 1234567, "show_status": 1}})

    found = danmaku.info("douyu", "88", get)
    out["斗鱼：问一次房间信息（和查开播状态同一个地址），回弹幕服务器的地址（照配置）和真实房间号"] = (
        found == {"platform": "douyu", "url": live_status.PLATFORMS["douyu"]["danmaku_url"], "room": "1234567"}
        and found["url"].startswith("wss://") and len(asked) == 1 and asked[0][0] == "https://www.douyu.com/betard/88"
        and "Cookie" not in asked[0][1])
    count = len(asked)
    try:
        danmaku.info("douyu", "88", lambda url, headers: body({"error": 1}))
        refused = False
    except live_status.StatusError:
        refused = True
    out["没做的平台、房间号写得不对：不给，也不去问；斗鱼回的数据里没有房间号：说明原因"] = (
        danmaku.info("huya", "88", get) is None and danmaku.info("douyin", "88", get) == {"platform": "douyin"}
        and danmaku.info("douyu", "8x8", get) is None and danmaku.info("bilibili", "8x8", get) is None and len(asked) == count and refused
        and danmaku.supported("douyu") and not danmaku.supported("huya") and not danmaku.supported("nope"))
    real_url = live_status.PLATFORMS["douyu"]["danmaku_url"]
    live_status.PLATFORMS["douyu"]["danmaku_url"] = "http://example.invalid"
    try:
        out["配置里的地址不是 wss://：不给"] = danmaku.douyu("88", get) is None
        live_status.PLATFORMS["douyu"]["danmaku_url"] = ""
        closed = not danmaku.supported("douyu")
    finally:
        live_status.PLATFORMS["douyu"]["danmaku_url"] = real_url
    out["配置里把地址去掉：这个平台就没有弹幕"] = closed

    # ---- B站 ----
    img, sub = "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
    key = danmaku.wbi_key(f"https://i0.hdslb.com/bfs/wbi/{img}.png", f"https://i0.hdslb.com/bfs/wbi/{sub}.png")
    signed = danmaku.wbi_sign({"id": 7734200, "type": 0, "odd": "a!b'c(d)e*f g"}, key, now=1700000000)
    query = signed.rsplit("&w_rid=", 1)[0]
    out["B站的签名：两个 key 照表挑出 32 个字符；参数按名字排、去掉 !'()* 、带上时间，再接上 MD5"] = (
        key == "ea1db124af3c7062474693fa704f4ff8" and query == "id=7734200&odd=abcdef%20g&type=0&wts=1700000000"
        and signed.endswith("&w_rid=" + hashlib.md5((query + key).encode()).hexdigest())
        and danmaku.wbi_key("https://x/short.png", "") == "")
    calls = []

    def ask(url, cookie="", form=None):
        calls.append((url, cookie))
        if "finger/spi" in url:
            return {"code": 0, "data": {"b_3": "BUVID3-infoc", "b_4": "BUVID4"}}, {}
        if "web-interface/nav" in url:
            return {"code": -101, "data": {"wbi_img": {"img_url": f"https://i0.hdslb.com/bfs/wbi/{img}.png",
                                                       "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub}.png"}}}, {}
        if "room_init" in url:
            return {"code": 0, "data": {"room_id": 7734200}}, {}
        return {"code": 0, "data": {"token": "TOKEN-123", "host_list": [
            {"host": "hw-sg-live-comet-01.chat.bilibili.com", "wss_port": 443},
            {"host": "evil.example.com", "wss_port": 443}, {"host": "a.chat.bilibili.com", "wss_port": 2245},
            {"host": "bad host.bilibili.com", "wss_port": 443}, {"host": "b.chat.bilibili.com", "wss_port": "443"}]}}, {}

    danmaku._bili.update(at=0.0, key="", cookie="", buvid="")
    real_cookie = bili_login.cookie
    bili_login.cookie = lambda: "SESSDATA=玩家的登录; bili_jct=csrf0123456789"
    try:
        found = danmaku.info("bilibili", "6", ask)
        first_calls = list(calls)
        again = danmaku.bilibili("6", ask, now=danmaku.time.time() + 60)
        later = danmaku.bilibili("6", ask, now=danmaku.time.time() + danmaku.KEEP_SECONDS + 60)
    finally:
        bili_login.cookie = real_cookie
    info_url = first_calls[3][0] if len(first_calls) == 4 else ""
    asked_query = urllib.parse.urlsplit(info_url).query.rsplit("&w_rid=", 1)[0]
    out["B站：问设备号、签名的 key、真实房间号、弹幕的口令各一次；回口令、真实房间号、设备号和弹幕服务器（只要 B站自己的主机）"] = (
        found == {"platform": "bilibili", "room": 7734200, "token": "TOKEN-123", "buvid": "BUVID3-infoc", "urls": [
            "wss://broadcastlv.chat.bilibili.com/sub", "wss://hw-sg-live-comet-01.chat.bilibili.com/sub", "wss://a.chat.bilibili.com:2245/sub"]}
        and [url.split("?")[0].rsplit("/", 1)[-1] for url, _ in first_calls] == ["spi", "nav", "room_init", "getDanmuInfo"]
        and "id=6" in first_calls[2][0] and asked_query.startswith("id=7734200&type=0&wts=")
        and info_url.endswith("&w_rid=" + hashlib.md5((asked_query + key).encode()).hexdigest()))
    out["用的是游客身份：只带游客的设备号，不带玩家的 B站登录；回给页面的东西里也没有登录凭证"] = (
        all("SESSDATA" not in cookie and "bili_jct" not in cookie for _, cookie in calls)
        and [cookie for _, cookie in first_calls[1:]] == ["buvid3=BUVID3-infoc; buvid4=BUVID4"] * 3
        and "SESSDATA" not in json.dumps(found, ensure_ascii=False) and "玩家的登录" not in json.dumps(found, ensure_ascii=False))
    out["设备号和签名的 key 记 6 小时：这段时间里再要只问房间号和口令，过了重新问"] = (
        again == found and len(calls) == 4 + 2 + 4 and later == found)

    def refused(url, cookie="", form=None):
        if "getDanmuInfo" in url:
            return {"code": -352, "message": "风控"}, {}
        return ask(url, cookie, form)

    reasons = []
    for broken in (refused, lambda url, cookie="", form=None: (_ for _ in ()).throw(bili_login.BiliError("连不上 B站"))):
        try:
            danmaku._bili.update(at=0.0) if broken is not refused else None
            danmaku.bilibili("6", broken)
        except live_status.StatusError as e:
            reasons.append(str(e))
    out["B站没给口令（风控 -352：下次重新要设备号和 key）、连不上：说明原因"] = (
        len(reasons) == 2 and "-352" in reasons[0] and reasons[1] == "连不上 B站" and danmaku._bili["at"] == 0.0)
    danmaku._bili.update(at=0.0, key="", cookie="", buvid="")
    out["Twitch：不用联网，回聊天服务器的地址（照配置）和小写的登录名；登录名写得不对不给"] = (
        danmaku.supported("twitch") and danmaku.info("twitch", "ImperialHal__") == {
            "platform": "twitch", "url": live_status.PLATFORMS["twitch"]["danmaku_url"], "room": "imperialhal__"}
        and danmaku.info("twitch", "a/b") is None)

    # ---- 面板 ----
    folder = tempfile.mkdtemp(prefix="evo-danmaku-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    players = [{"id": "douyu-88", "name": "示例选手1", "team": "示例战队", "platform": "douyu", "room": "88"},
               {"id": "huya-89", "name": "示例选手2", "team": "示例战队", "platform": "huya", "room": "89"},
               {"id": "bilibili-90", "name": "示例选手3", "team": "示例战队", "platform": "bilibili", "room": "90"},
               {"id": "twitch-91", "name": "示例选手4", "team": "示例战队", "platform": "twitch", "room": "ImperialHal__"}]
    events = [{"id": "live", "name": "示例赛", "type": "联赛", "stage": "", "start": (NOW - timedelta(hours=1)).isoformat(),
               "end": (NOW + timedelta(hours=3)).isoformat(), "teams": ["示例战队"]}]

    def serve(platforms=None):
        path = Path(folder) / "esports.json"
        path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": events,
                                    "platforms": platforms or {}}), encoding="utf-8")
        esports_data.URL = str(path)
        esports_data.CACHE_PATH.unlink(missing_ok=True)

    serve()
    page = client.get("/esports/live/watch").get_data(as_text=True)
    start = page.find('<script type="application/json" id="watch-data">') + len('<script type="application/json" id="watch-data">')
    data = json.loads(page[start:page.find("</script>", start)])
    urls = {p["id"]: p["danmaku"] for p in data["players"]}
    out["看直播页给脚本的数据：只有弹幕做了、能用 Evo 自己的播放器播的选手带着问弹幕的地址"] = (
        urls == {"douyu-88": "/esports/danmaku/douyu-88", "huya-89": None, "bilibili-90": "/esports/danmaku/bilibili-90",
                 "twitch-91": "/esports/danmaku/twitch-91"})
    twitch_info = client.get("/esports/danmaku/twitch-91").get_json()
    out["Twitch 的弹幕：面板把聊天服务器的地址和小写的登录名交给页面（不用联网，房间号存成小写）"] = (
        twitch_info == {"ok": True, "platform": "twitch", "url": live_status.PLATFORMS["twitch"]["danmaku_url"],
                        "room": "imperialhal__"})
    out["页面上有弹幕的开关和连斗鱼、B站的脚本：弹幕只当纯文字显示，那一层不挡点击、不让读屏软件念；B站用游客身份、要 zlib 不要 brotli"] = (
        "watch-danmaku-btn" in page and "function douyuPacket(text)" in page and "type@=joingroup/rid@=" in page
        and "function biliPacket(op, text)" in page and "uid: 0, roomid: found.room, protover: 2" in page
        and 'new DecompressionStream("deflate")' in page
        and 'el("span", "watch-danmaku-item", String(text).slice(0, 60))' in page and 'layer.setAttribute("aria-hidden", "true")' in page)
    out["弹幕设置：不透明度、字号、速度、显示区域四个滑杆和\"恢复默认\"，记在这台电脑上；新弹幕的字号、行数、速度照设置算"] = (
        all(f'key: "{key}", label: "{label}"' in page for key, label in
            (("opacity", "不透明度"), ("size", "字号"), ("speed", "速度"), ("area", "显示区域")))
        and 'save("evo-watch-dm-" + f.key, String(dm[f.key]))' in page and '"watch-dm-reset", "恢复默认"' in page
        and 'control("弹幕设置", "sliders")' in page and "layer.style.opacity = String(dm.opacity / 100)" in page
        and "* dm.size / 100))" in page and "DM_AREAS[dm.area - 1] / 100" in page and "duration = DM_SPEEDS[dm.speed - 1]" in page)
    real_info = danmaku.info
    danmaku.info = lambda platform, room, get=None: {"platform": platform, "url": "wss://example.invalid:1", "room": "1234567"}
    try:
        good = client.get("/esports/danmaku/douyu-88")
        missing = [client.get(f"/esports/danmaku/{pid}").status_code for pid in ("huya-89", "bilibili-90", "nobody-1")]

        def broken(platform, room, get=None):
            raise live_status.StatusError("连不上斗鱼")

        danmaku.info = broken
        failed = client.get("/esports/danmaku/douyu-88").get_json()
        serve({"douyu": False})
        client.get("/esports")
        switched = client.get("/esports/danmaku/douyu-88").status_code
    finally:
        danmaku.info = real_info
    out["面板的接口：给地址和真实房间号；没做的平台、名单外的人、没登录 B站的（用的是官方播放器，里面有它自己的弹幕）404；平台那边没问到说明原因；平台开关关了也 404"] = (
        good.get_json() == {"ok": True, "platform": "douyu", "url": "wss://example.invalid:1", "room": "1234567"}
        and missing == [404, 404, 404] and failed == {"ok": False, "reason": "upstream", "detail": "连不上斗鱼"} and switched == 404)
    panel_esports._status.update(at=0.0, found={})
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
