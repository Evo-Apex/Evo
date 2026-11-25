"""斗鱼登录和斗鱼原画（douyu_login.py、watch_douyu.py 带着登录要地址、login_window.py 里不用窗口的那几步、
panel_esports.py 的登录接口、看直播页）。不联网：斗鱼的接口换成假的；登录信息存在临时数据文件夹里。
下面的 Cookie 都是编的示例。

- 读 Cookie：名字不对、带控制字符的不要，同名的留后面的；有 acf_jwt_token / acf_auth / dy_auth 之一（不是空的）才算登录着
- 多久过期：JWT 看里面的 exp，别的从存下起 7 天
- 存：没有登录令牌的不存；存下的文件里看不到原文；昵称从 acf_nickname 来
- 要地址时的身份：设备号用登录 Cookie 里的 dy_did；请求的 Cookie 里不带 LTP0
- 续期：离过期不到一天才续；带 dy_did 和 LTP0 问通行证；回的 Set-Cookie 合并进来、LTP0 留着；没续成旧的照旧
- 取地址：登录着时签名、表单、Cookie 都用登录的设备号；被拒绝了续一次期再试，还不行当游客再要；没登录照旧
- 登录窗口（不开真的窗口）：只开配置里的 https 登录页、只把凭证交给本机的面板
- 面板：存登录凭证要带口令；问登录着没有；退出登录；看直播页带着斗鱼的登录状态；不登录斗鱼照样能播

运行：python tests/test_douyu_login.py
"""

import base64
import json
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_window
import douyu_login
import esports_data
import login_window
import panel_app
import panel_esports
import secret_store
import storage
import watch_douyu

NOW = datetime.now().astimezone().replace(microsecond=0)
DID = "0123456789abcdef0123456789abcdef"
COOKIE = (f"dy_did={DID}; acf_did={DID}; acf_auth=AUTH-TOKEN-EXAMPLE; acf_uid=4321; acf_nickname=%E7%A4%BA%E4%BE%8B%20%E7%94%A8%E6%88%B7; "
          "LTP0=LONG-TERM-KEY-EXAMPLE; acf_ltkid=77")


def jwt(exp):
    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"aaa.{part}.zzz"


def cookie_checks(out):
    found = douyu_login.fields(" a=1; bad name=2; b = x=y ;a=3; =4; c=\x07bell; d;e=" + "v" * 5000)
    out["读 Cookie：一项一项拆开，同名的留后面的，值里可以有等号；名字不对、带控制字符、太长的不要"] = found == {"a": "3", "b": "x=y"}
    out["算不算登录着：有 acf_jwt_token / acf_auth / dy_auth 之一，空的不算；几个都有时用前面的"] = (
        douyu_login.token(douyu_login.fields("dy_did=1; acf_did=1")) is None
        and douyu_login.token(douyu_login.fields("dy_auth=; acf_auth=")) is None
        and douyu_login.token(douyu_login.fields("dy_auth=D; acf_auth=A")) == "A"
        and douyu_login.token(douyu_login.fields("DY_AUTH=D")) == "D")
    out["多久过期：令牌是 JWT 的看里面的 exp；不是的从存下的那一刻起 7 天；没登录没有"] = (
        douyu_login.expiry(douyu_login.fields("acf_jwt_token=" + jwt(1800000000)), 1000.0) == 1800000000.0
        and douyu_login.expiry(douyu_login.fields("acf_auth=opaque"), 1000.0) == 1000.0 + 7 * 86400
        and douyu_login.expiry(douyu_login.fields("acf_auth=not.a.jwt"), 1000.0) == 1000.0 + 7 * 86400
        and douyu_login.expiry(douyu_login.fields("dy_did=1"), 1000.0) is None)


def store_checks(out):
    douyu_login.forget()
    nothing = douyu_login.save(f"dy_did={DID}; acf_did={DID}", now=1000.0)
    out["登录窗口交来的东西里没有登录令牌：不存，照旧是没登录"] = (
        nothing == {"ok": False, "name": ""} and not douyu_login.STORE.exists()
        and douyu_login.account() == {"logged_in": False, "name": ""} and douyu_login.identity() is None)
    saved = douyu_login.save(COOKIE, now=1000.0)
    raw = douyu_login.STORE.read_bytes()
    out["有登录令牌：加密存在数据库旁边的 douyu_login.bin，文件里看不到原文；昵称从 Cookie 里来"] = (
        saved == {"ok": True, "name": "示例 用户"} and douyu_login.STORE.parent == storage.DB_PATH.parent
        and b"AUTH-TOKEN-EXAMPLE" not in raw and b"LONG-TERM-KEY" not in raw
        and douyu_login.account() == {"logged_in": True, "name": "示例 用户"})
    who = douyu_login.identity()
    out["要地址时的身份：设备号是登录 Cookie 里的 dy_did；请求的 Cookie 先写这个设备号，再接别的项，LTP0 不带"] = (
        who["did"] == DID and who["cookie"].startswith(f"dy_did={DID}; acf_did={DID}; acf_auth=AUTH-TOKEN-EXAMPLE")
        and "LTP0" not in who["cookie"] and who["cookie"].count("dy_did=") == 1 and "acf_ltkid=77" in who["cookie"])
    douyu_login.save("acf_auth=TOKEN-ONLY", now=1000.0)
    out["登录 Cookie 里没有设备号：不带着它去要地址（斗鱼只会当成游客）；昵称没有就写\"斗鱼用户\""] = (
        douyu_login.identity() is None and douyu_login.account()["name"] == "斗鱼用户")
    douyu_login.logout()
    out["退出登录：删掉本机存的"] = not douyu_login.STORE.exists() and douyu_login.account()["logged_in"] is False


def renew_checks(out):
    calls = []
    reply = {"lines": ["acf_auth=NEW-TOKEN; Path=/; Domain=.douyu.com; HttpOnly", "acf_ltkid=78; Path=/", "acf_uid=; Max-Age=0"]}

    def ask(url, cookie):
        calls.append((url, cookie))
        if reply["lines"] is None:
            raise douyu_login.DouyuLoginError("连不上斗鱼的通行证（超时）")
        return reply["lines"]

    def fresh(saved_at=1000.0):
        douyu_login.forget()
        douyu_login._renew_tried[0] = 0.0
        douyu_login.save(COOKIE, now=saved_at)
        calls.clear()

    fresh()
    early = douyu_login.renew(ask=ask, now=1000.0 + 5 * 86400)
    out["离过期还有一天以上：不续，不联网"] = early is False and calls == []
    done = douyu_login.renew(ask=ask, now=1000.0 + 6.5 * 86400)
    kept = secret_store.load(douyu_login.STORE)
    out["离过期不到一天：带着 dy_did 和 LTP0（只有这两项）问通行证的 safeAuth"] = (
        done is True and len(calls) == 1 and calls[0][1] == f"dy_did={DID};LTP0=LONG-TERM-KEY-EXAMPLE"
        and calls[0][0].startswith("https://passport.douyu.com/lapi/passport/iframe/safeAuth?client_id=1&"))
    merged = douyu_login.fields(kept["cookie"])
    out["续上了：回的 Set-Cookie 合并进来（没提到的留着，LTP0 也留着；给了空值的去掉），从这一刻起再算 7 天"] = (
        merged["acf_auth"] == "NEW-TOKEN" and merged["LTP0"] == "LONG-TERM-KEY-EXAMPLE" and merged["acf_ltkid"] == "78"
        and "acf_uid" not in merged and merged["dy_did"] == DID and "Path" not in merged
        and kept["saved"] == 1000.0 + 6.5 * 86400 and kept["name"] == "示例 用户")

    fresh()
    reply["lines"] = []
    before = secret_store.load(douyu_login.STORE)
    refused = douyu_login.renew(force=True, ask=ask, now=2000.0)
    reply["lines"] = ["acf_auth=; Max-Age=0"]
    emptied = douyu_login.renew(force=True, ask=ask, now=2000.0)
    reply["lines"] = None
    offline = douyu_login.renew(force=True, ask=ask, now=2000.0)
    out["没续成（斗鱼没回 Set-Cookie、合并以后没有令牌了、连不上）：存着的不动"] = (
        (refused, emptied, offline) == (False, False, False) and secret_store.load(douyu_login.STORE) == before and len(calls) == 3)
    fresh()
    reply["lines"] = []
    near = 1000.0 + 6.5 * 86400  # 离过期不到一天
    tried = [douyu_login.renew(ask=ask, now=near), douyu_login.renew(ask=ask, now=near + 60)]
    after_first = len(calls)
    forced = douyu_login.renew(force=True, ask=ask, now=near + 120)
    out["刚试过没成：10 分钟以内不再试（要地址被拒绝时强制的那一次不受限）"] = (
        tried == [False, False] and after_first == 1 and forced is False and len(calls) == 2)

    douyu_login.forget()
    douyu_login._renew_tried[0] = 0.0
    douyu_login.save(COOKIE.replace("LTP0=LONG-TERM-KEY-EXAMPLE; ", ""), now=1000.0)
    calls.clear()
    out["没有 LTP0：续不了，不联网"] = douyu_login.renew(force=True, ask=ask, now=2000.0) is False and calls == []

    reply["lines"] = ["acf_auth=NEW-TOKEN"]
    fresh()
    alive = douyu_login.account(check=True, ask=ask, now=1000.0 + 8 * 86400)
    fresh()
    reply["lines"] = []
    dead = douyu_login.account(check=True, ask=ask, now=1000.0 + 8 * 86400)
    gone = not douyu_login.STORE.exists()
    fresh()
    reply["lines"] = None
    unsure = douyu_login.account(check=True, ask=ask, now=1000.0 + 8 * 86400)
    out["打开看直播页时登录已经过期：续得上照旧登录着；斗鱼不给续就删掉、回到没登录；只是连不上照旧算登录着"] = (
        alive["logged_in"] is True and dead == {"logged_in": False, "name": ""} and gone
        and unsure["logged_in"] is True and douyu_login.STORE.exists())
    douyu_login.forget()


def play_checks(out):
    calls, state = [], {"refuse": 0}
    # 用现在的时间当基准：斗鱼登录"从存下起 7 天"，写死的时间过几天就过期了，_play_as_user 会先去续期（2026-11-25 修）
    stamp = time.time()
    enc = {"error": 0, "data": {"key": "KEY", "rand_str": "RAND", "enc_time": 2, "enc_data": "ENC", "is_special": 0,
                                "expire_at": stamp + 600}}

    def play(rate):
        return {"error": 0, "data": {"rate": rate, "rtmp_url": "https://hw.douyucdn.cn/live", "rtmp_live": "a.flv?expire=0",
                                     "rtmp_cdn": "hw-h5", "multirates": [{"rate": 0, "name": "原画1080P60"}, {"rate": 4, "name": "蓝光4M"}],
                                     "cdnsWithName": [{"cdn": "hw-h5"}]}}

    def get(url, room="", form=None):
        cookie = watch_douyu._headers(room)["cookie"]  # 这次请求会带的 Cookie
        calls.append((url, form, cookie))
        if form is None:
            return enc
        logged = "acf_auth=" in cookie
        if logged and state["refuse"] > 0:
            state["refuse"] -= 1
            return {"error": 102, "msg": "示例：登录不认"}
        return play(0 if logged else 4)

    renewals = []
    real_request = douyu_login.request
    douyu_login.request = lambda url, cookie: (renewals.append(cookie), ["acf_auth=RENEWED"])[1]
    try:
        douyu_login.forget()
        watch_douyu._encryption.clear()
        guest = watch_douyu.resolve("520013", None, get=get, now=stamp)
        out["没登录：照旧用这次运行的设备号当游客要，斗鱼给蓝光4M"] = (
            guest["rate"] == "4" and guest["logged"] is False and f"did={watch_douyu.DEVICE_ID}" in calls[0][0]
            and calls[1][1]["did"] == watch_douyu.DEVICE_ID and "acf_auth" not in calls[1][2] and renewals == [])

        douyu_login.save(COOKIE, now=stamp)
        calls.clear()
        logged = watch_douyu.resolve("520013", None, get=get, now=stamp)
        out["登录着：换了设备号所以重新要签名材料；签名材料、表单、Cookie 都用登录 Cookie 里的 dy_did，Cookie 里带着登录、不带 LTP0；给原画"] = (
            logged["rate"] == "0" and logged["logged"] is True and len(calls) == 2 and f"getEncryption?did={DID}" in calls[0][0]
            and calls[1][1]["did"] == DID and calls[1][2].startswith(f"dy_did={DID}; acf_did={DID}; acf_auth=AUTH-TOKEN-EXAMPLE")
            and "LTP0" not in calls[1][2] and "LTP0" not in calls[0][2] and renewals == [])
        out["连视频服务器时不带登录凭证（只在要地址的请求里带）"] = "acf_auth" not in watch_douyu._headers("520013")["cookie"]

        calls.clear()
        state["refuse"] = 2  # 带着登录要了两次（一次里面换签名材料再试）都被拒绝
        again = watch_douyu.resolve("520013", None, get=get, now=stamp)
        out["带着登录被拒绝了：强制续一次期，用新的登录再要，要到原画"] = (
            again["rate"] == "0" and again["logged"] is True and renewals == [f"dy_did={DID};LTP0=LONG-TERM-KEY-EXAMPLE"]
            and "acf_auth=RENEWED" in calls[-1][2])

        calls.clear()
        renewals.clear()
        state["refuse"] = 99
        fallback = watch_douyu.resolve("520013", None, get=get, now=stamp)
        out["续了期还是被拒绝：当游客再要一次，至少能看蓝光4M（登录出了问题不能连直播都看不了）"] = (
            fallback["rate"] == "4" and fallback["logged"] is False and len(renewals) == 1
            and "acf_auth" not in calls[-1][2] and calls[-1][1]["did"] == watch_douyu.DEVICE_ID)
    finally:
        douyu_login.request = real_request
        douyu_login.forget()
        watch_douyu._encryption.clear()


def window_checks(out):
    cfg = login_window.config("douyu")
    out["登录窗口开哪个网页由配置定：斗鱼官方的 https 登录页，读这几个地址下的 Cookie；没有官方登录页的平台不开"] = (
        cfg["page"] == "https://passport.douyu.com/h5/loginActivity" and cfg["name"] == "斗鱼"
        and cfg["domains"] == ["https://passport.douyu.com/", "https://www.douyu.com/", "https://m.douyu.com/"]
        and {"acf_auth", "dy_auth"} <= cfg["signals"] and "EvoApp" not in cfg["user_agent"]
        and login_window.config("huya") is None and login_window.config("nope") is None)
    real = login_window.PLATFORMS
    try:
        login_window.PLATFORMS = {"x": {"name": "X", "login_page": "http://example.invalid/login", "login_domains": ["https://a/"], "login_signals": ["s"]},
                                  "y": {"name": "Y", "login_page": "https://example.invalid/login", "login_domains": ["http://a/"], "login_signals": ["s"]}}
        out["配置里的登录页、读 Cookie 的地址不是 https 的：不开"] = login_window.config("x") is None and login_window.config("y") is None
    finally:
        login_window.PLATFORMS = real
    out["登录凭证只交给本机的面板：主窗口的地址不是 http://127.0.0.1:端口 的不交"] = (
        login_window.save_url("http://127.0.0.1:5123/esports/live/watch?p=a", "douyu") == "http://127.0.0.1:5123/esports/login/douyu/save"
        and login_window.save_url("https://127.0.0.1:5123/", "douyu") is None
        and login_window.save_url("http://example.invalid:5123/", "douyu") is None
        and login_window.save_url("http://127.0.0.1/", "douyu") is None and login_window.save_url("", "douyu") is None
        and login_window.save_url("http://127.0.0.1:5123/", "a/../b") == "http://127.0.0.1:5123/esports/login/a%2F..%2Fb/save")
    cookies = [("dy_did", DID, None), ("acf_auth", "", None), ("dy_did", "second", None), ("x", "1", None)]
    out["登录好了没有：表示登录的那几个 Cookie 之一出现、而且不是空的；交上去的是\"名字=值\"连起来（同名留后面的、空的不要）"] = (
        login_window.signed_in(cookies, cfg["signals"]) is False
        and login_window.signed_in([*cookies, ("dy_auth", "T", None)], cfg["signals"]) is True
        and login_window.cookie_text(cookies) == "dy_did=second; x=1")
    out["Evo 窗口给页面多了一个\"开登录窗口\"的接口"] = callable(getattr(app_window.WindowApi, "platform_login", None))


def route_checks(out, folder, client):
    path = Path(folder) / "esports.json"
    players = [{"id": "douyu-1", "name": "甲队1号", "team": "甲队", "platform": "douyu", "room": "1"},
               {"id": "bilibili-2", "name": "甲队2号", "team": "甲队", "platform": "bilibili", "room": "2"}]
    event = {"id": "live", "name": "示例联赛", "type": "联赛", "stage": "", "start": (NOW - timedelta(hours=1)).isoformat(),
             "end": (NOW + timedelta(hours=3)).isoformat(), "teams": ["甲队"]}
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": [event]}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    token = panel_app.app.config["TOKEN"]
    douyu_login.forget()

    def page_data():
        page = client.get("/esports/live/watch").get_data(as_text=True)
        return page, json.loads(re.search(r'<script type="application/json" id="watch-data">(.*?)</script>', page, re.S).group(1))

    page, data = page_data()
    out["看直播页给脚本的数据：斗鱼的登录是可选的（window）、在另开的窗口里登录，带着登录状态和问状态、退出的接口；B站照旧是扫码"] = (
        data["accounts"]["douyu"] == {"logged_in": False, "name": ""}
        and data["login"]["douyu"] == {"window": True, "state": "/esports/login/douyu/state", "logout": "/esports/login/douyu/logout"}
        and set(data["login"]["bilibili"]) == {"start", "poll", "logout"})
    out["页面：不登录斗鱼照样用 Evo 自己的播放器；登录按钮叫 Evo 窗口开斗鱼官方的登录页，登录凭证不经过页面"] = (
        "function optionalLogin(platform)" in page and "optionalLogin(p.platform) || " in page
        and "api.platform_login(login.platform, data.token)" in page and "要在 Evo 的窗口里才能登录" in page
        and "不登录不给「" in page and "AUTH-TOKEN" not in page)
    out["没登录斗鱼：照样给播放地址的接口（不回 404）"] = panel_esports._player("douyu-1") is not None

    no_token = client.post("/esports/login/douyu/save", data={"cookie": COOKIE})
    empty = client.post("/esports/login/douyu/save", data={"token": token, "cookie": f"dy_did={DID}"}).get_json()
    wrong = client.post("/esports/login/bilibili/save", data={"token": token, "cookie": COOKIE})
    out["存登录凭证的接口：要带面板的口令；没有登录令牌的不存；只有斗鱼有这个接口"] = (
        no_token.status_code != 200 and empty == {"ok": False} and wrong.status_code == 404 and not douyu_login.STORE.exists())
    saved = client.post("/esports/login/douyu/save", data={"token": token, "cookie": COOKIE})
    state = client.get("/esports/login/douyu/state").get_json()
    out["登录窗口交来登录凭证：存下来，只回存没存下（不把凭证回给请求的人）；问状态回登录着和昵称"] = (
        saved.get_json() == {"ok": True} and "AUTH-TOKEN" not in saved.get_data(as_text=True)
        and state == {"logged_in": True, "name": "示例 用户"})
    page, data = page_data()
    out["登录以后看直播页带着\"登录着\"和昵称，不带登录凭证"] = (
        data["accounts"]["douyu"] == {"logged_in": True, "name": "示例 用户"}
        and "AUTH-TOKEN" not in page and "LONG-TERM-KEY" not in page)

    real_resolve, real_remember = watch_douyu.resolve, watch_douyu.remember
    watch_douyu.resolve = lambda room, rate=None, **more: {"url": "https://hw.douyucdn.cn/live/a.flv", "rate": "0", "logged": True,
                                                           "qualities": [{"id": "0", "name": "原画1080P60"}], "lines": [], "cdn": "", "ttl": None}
    watch_douyu.remember = lambda *args, **more: None
    try:
        info = client.get("/esports/play/douyu-1").get_json()
    finally:
        watch_douyu.resolve, watch_douyu.remember = real_resolve, real_remember
    out["问播放地址：告诉页面这次是不是带着登录要到的（页面据此写\"不登录不给原画\"还是\"这次没给\"）"] = (
        info["ok"] is True and info["applied"] == "0" and info["logged"] is True)

    out_post = client.post("/esports/login/douyu/logout", data={"token": token}).get_json()
    out["面板的退出登录接口：删掉本机存的，再问就是没登录；没有登录功能的平台 404"] = (
        out_post == {"ok": True} and not douyu_login.STORE.exists()
        and client.get("/esports/login/douyu/state").get_json() == {"logged_in": False, "name": ""}
        and client.get("/esports/login/huya/state").status_code == 404)
    log_text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in Path(storage.DB_PATH.parent.parent / "logs").glob("*.log")
                       ) if (storage.DB_PATH.parent.parent / "logs").is_dir() else ""
    out["运行记录里没有登录凭证"] = "AUTH-TOKEN-EXAMPLE" not in log_text and "LONG-TERM-KEY-EXAMPLE" not in log_text


def main():
    print("斗鱼登录和斗鱼原画")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-douyu-login-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    try:
        cookie_checks(out)
        store_checks(out)
        renew_checks(out)
        play_checks(out)
        window_checks(out)
        route_checks(out, folder, client)
    finally:
        douyu_login.forget()
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
