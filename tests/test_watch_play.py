"""在 Evo 窗口里播斗鱼、抖音、Twitch（flv_relay.py、watch_douyu.py、watch_douyin.py、watch_twitch.py、hls_relay.py、
panel_esports.py 的 /esports/play 和 /esports/stream）。不联网：视频是这里拼出来的假 FLV，斗鱼、抖音、Twitch 的接口换成假的。

- FLV 拆标签：不管数据怎么分块，拆出来的都一样；不是 FLV 报错；认得出 H.264、H.265（老式和新式两种写法）
- 中转：第一段原样转；不是 H.264 一开始就报错、什么都不发；
  换地址（快到期时预先连好、连上就换）和断了重连：接上以后画面、声音的时间戳连着走，不重复、不缺帧，
  说明信息和一样的初始化包不再发；标签只收到一半就断了也接得上；新的一段时间戳归零了：从关键帧接、时间戳平移；
  主播换了设置（初始化包不一样）要转；重连几次都不行就结束，不会没完没了；外面叫停就停、连接关掉
- 斗鱼取地址：签名（MD5 连算几遍）、表单里写明不要 H.265、签名材料 5 分钟内不重要、斗鱼不认时重要一次再试；
  清晰度按斗鱼给的顺序、线路现在用的在前；地址怎么拼、限 300 秒的补上不限时、算得出多久到期；
  实际给的是哪一档；刚取过的地址紧接着用一次；默认的线路连不上换别的线路
- 抖音取地址：各档从好到差、叫什么（抖音菜单上的名字加分辨率或码率）、只有声音的不算；没选过时要码率不超过上限的
  最好一档；选了按选的；地址换成 https；只要 H.264，一档都没有、没开播说明原因；记着这一场的内部房间号就只问一个请求，
  没记着、记着的那一场结束了就开直播间网页
- Twitch 取地址（照 PureLive）：先要播放令牌（固定的查询编号、登录名转小写），再问 usher（只要 H.264）；
  多档目录从好到差、名字用 NAME（原画写"1080p60（原画）"）、不要 audio_only；默认给原画、选了按选的、选的那档没有给最好的；
  令牌是 null、usher 404 / 403（地区限制、订阅专属）、PersistedQueryNotFound 的报错；连不上带 unreachable；
  remember / reuse；中转第一次用刚取到的地址、地址重取时还是同一档
- 面板的接口：问清晰度和播放地址（不是能播的选手回 404、平台开关关了回 404、取不到说明原因）；
  中转要带口令；一次只播一路，开新的一路上一路就停；不是 H.264 回 502；
  抖音：给页面的是抖音自己的视频地址（页面直接连），不中转；Twitch：回清晰度和中转地址，连不上回 reason "unreachable"
  （播放地址和中转的 502 都算）

运行：python tests/test_watch_play.py
"""

import hashlib
import json
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import esports_data
import flv_relay
import hls_relay
import panel_app
import panel_esports
import live_status
import storage
import watch_douyin
import watch_douyu
import watch_twitch
from flv_relay import AUDIO, SCRIPT, VIDEO

HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


def tag(kind, ts, payload):
    return (bytes([kind]) + len(payload).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([(ts >> 24) & 255])
            + b"\x00\x00\x00" + payload + (11 + len(payload)).to_bytes(4, "big"))


def video(ts, key=False, codec=7):
    return tag(VIDEO, ts, bytes([(0x10 if key else 0x20) | codec, 1, 0, 0, 0]) + b"frame" + ts.to_bytes(8, "big"))


def audio(ts):
    return tag(AUDIO, ts, b"\xaf\x01" + b"sound" + ts.to_bytes(8, "big"))


def head(config=b"avc-config", codec=7):
    """一段流的开头：文件头、说明信息、视频和音频的初始化包。"""
    return (HEADER + tag(SCRIPT, 0, b"onMetaData") + tag(VIDEO, 0, bytes([0x10 | codec, 0, 0, 0, 0]) + config)
            + tag(AUDIO, 0, b"\xaf\x00" + b"aac-config"))


def frames(start, end, base=0, gop=50):
    """从第 start 帧到第 end 帧（不含）的画面和声音：画面每 40 毫秒一帧、每 gop 帧一个关键帧，声音每 23 毫秒一个。
    base：这一段的时间戳从多少算起（斗鱼是推流的绝对时间，两段的 base 一样）。"""
    out = []
    for n in range(start, end):
        out.append((base + n * 40, 0, video(base + n * 40, key=n % gop == 0)))
    for n in range(start * 40 // 23, end * 40 // 23):
        out.append((base + n * 23, 1, audio(base + n * 23)))
    return b"".join(raw for _, _, raw in sorted(out))


def segment(start, end, base=1_000_000, **more):
    """平台给的一段：从 start 所在的那一组的关键帧开始（start 要是 50 的倍数）。"""
    return head(**more) + frames(start, end, base)


class Body:
    """假的视频连接：每次 read 给一小块（故意把标签切开），读完返回空。"""

    def __init__(self, data, piece=777, clock=None, step=0.0, fail=False):
        self.data, self.piece, self.clock, self.step, self.fail = data, piece, clock, step, fail
        self.at, self.closed = 0, False

    def read(self, _size):
        if self.closed:
            return b""
        if self.clock is not None:
            self.clock[0] += self.step
        chunk = self.data[self.at:self.at + self.piece]
        self.at += len(chunk)
        if not chunk and self.fail:
            raise OSError("连接被重置")
        return chunk

    def close(self):
        self.closed = True


def run(bodies, ttl=None, clock=None, **more):
    """用这些连接依次当作每一段，跑完一路中转。返回 (发给播放器的全部字节, relay, 开过几次连接, 睡过几次)。"""
    queue, opened, naps = list(bodies), [], []
    clock = clock or [0.0]

    def open_segment():
        opened.append(1)
        if not queue:
            raise OSError("连不上")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, ttl

    relay = flv_relay.Relay(open_segment, clock=lambda: clock[0], sleep=naps.append, spawn=lambda fn: fn(), **more)
    relay.start()
    return b"".join(relay.stream()), relay, len(opened), naps


def parse(data):
    demux = flv_relay.Demuxer()
    return demux.feed(data), demux.header


def timeline(data):
    """发给播放器的东西里：画面的时间戳、声音的时间戳、说明信息几个、初始化包几个。"""
    tags, _ = parse(data)
    media = [t for t in tags if t.kind != SCRIPT and not t.seq]
    return ([t.ts for t in media if t.kind == VIDEO], [t.ts for t in media if t.kind == AUDIO],
            sum(1 for t in tags if t.kind == SCRIPT), sum(1 for t in tags if t.seq))


def steady(stamps, step):
    return all(b - a == step for a, b in zip(stamps, stamps[1:]))


def demux_checks(out):
    data = segment(0, 120)
    whole, header = parse(data)
    pieces = flv_relay.Demuxer()
    split = [t for start in range(0, len(data), 37) for t in pieces.feed(data[start:start + 37])]
    out["FLV 拆标签：不管数据怎么分块，拆出来的都一样，文件头单独留着"] = (
        split == whole and header == HEADER and pieces.header == HEADER and len(whole) > 300
        and whole[0].kind == SCRIPT and whole[1].seq and whole[2].seq and whole[3].key and not whole[5].seq)
    errors = []
    for bad in (b"<html>502 Bad Gateway</html>", HEADER + b"\x07" + b"\x00" * 30):
        try:
            flv_relay.Demuxer().feed(bad)
        except flv_relay.RelayError as e:
            errors.append(e.code)
    out["不是 FLV、读不懂的数据：报错，不乱转"] = errors == ["not_flv", "not_flv"]
    kinds = [flv_relay.video_codec(parse(HEADER + raw)[0][0]) for raw in (
        video(0, True), video(0, True, codec=12), tag(VIDEO, 0, b"\x90hvc1" + b"x" * 8), tag(VIDEO, 0, b"\x90av01" + b"x" * 8))]
    out["认得出 H.264、H.265（老式写法编号 12、新式写法 hvc1），别的编码算\"其他\""] = kinds == ["h264", "h265", "h265", "other"]
    moved = parse(HEADER + flv_relay.retime(video(123456, True), 0x12345678))[0][0]
    out["改时间戳只改时间戳，别的字节不动（超过 24 位的也写得对）"] = (
        moved.ts == 0x12345678 and moved.key and moved.raw[11:] == video(123456, True)[11:])


def relay_checks(out):
    first = segment(0, 150)
    sent, relay, opened, naps = run([Body(first)], retries=3)
    out["只有一段：原样转给播放器，一个字节不差；断了重连 3 次都连不上就结束"] = (
        sent == first and opened == 4 and len(naps) == 3 and relay.closed)

    body = Body(segment(0, 150, codec=12))
    refused = None
    try:
        flv_relay.Relay(lambda: (body, None)).start()
    except flv_relay.RelayError as e:
        refused = e.code
    silent = None
    try:
        flv_relay.Relay(lambda: (Body(HEADER + tag(SCRIPT, 0, b"onMetaData")), None)).start()
    except flv_relay.RelayError as e:
        silent = e.code
    out["不是 H.264（或者根本没有画面数据）：一开始就报错，什么都没发给播放器，连接关掉"] = (
        refused == "not_h264" and body.closed and silent == "upstream")

    # 地址 300 秒到期：每读一块算过了 10 秒，到第 295 秒预先连好下一段，连上就换（上一段不读完）
    clock = [0.0]
    old = Body(segment(0, 1500), piece=2000, clock=clock, step=10.0)
    new = Body(segment(500, 1000))  # 新的一段从更早的关键帧开始重放（换的时候旧的一段大约转到第 700 帧）
    sent, relay, opened, _ = run([old, new], ttl=300, clock=clock, retries=1)
    pictures, sounds, scripts, inits = timeline(sent)
    switched_at = old.at
    out["地址快到期：提前 5 秒预先连好新的，连上就换，不等旧的被断开"] = (
        opened >= 2 and old.closed and 0 < switched_at < len(old.data) and relay.segments >= 2)
    out["换了以后画面、声音接着走：一帧不缺、一帧不重复（直接接下一帧，不等关键帧）"] = (
        pictures[0] == 1_000_000 and pictures[-1] == 1_000_000 + 999 * 40 and steady(pictures, 40) and steady(sounds, 23))
    out["新的一段的文件头、说明信息、一样的初始化包不再发给播放器"] = (
        scripts == 1 and inits == 2 and sent.count(b"FLV\x01") == 1)

    cut = segment(0, 137)
    sent, _, opened, _ = run([Body(cut[:-9]), Body(segment(100, 260))], retries=1)
    pictures, sounds, _, _ = timeline(sent)
    out["连接突然断了（最后一个标签只收到一半）：重连，从断的地方接上"] = (
        opened >= 2 and steady(pictures, 40) and steady(sounds, 23) and pictures[-1] == 1_000_000 + 259 * 40
        and len(pictures) == 260)
    sent, _, _, _ = run([Body(cut, fail=True), Body(segment(100, 200))], retries=1)
    out["读的时候出错（连接被重置）也一样重连"] = steady(timeline(sent)[0], 40) and len(timeline(sent)[0]) == 200

    # 新的一段时间戳从 0 开始（对不上）：从它的关键帧接，后面的时间戳都平移到接着已经转到的地方
    sent, _, _, _ = run([Body(segment(0, 120)), Body(head() + frames(30, 180, base=0))], retries=1)
    pictures, sounds, _, _ = timeline(sent)
    tags = [t for t in parse(sent)[0] if t.kind == VIDEO and not t.seq]
    out["新的一段时间戳归零了：从它的第一个关键帧接上，时间戳平移过来，一直往前走不倒退"] = (
        len(pictures) == 120 + 130 and all(b > a for a, b in zip(pictures, pictures[1:]))
        and tags[120].key and 40 <= pictures[120] - pictures[119] <= 80 and steady(pictures[120:], 40)
        and all(b > a for a, b in zip(sounds, sounds[1:])))

    sent, _, _, _ = run([Body(segment(0, 100)), Body(segment(50, 200, config=b"new-avc-config"))], retries=1)
    _, _, _, inits = timeline(sent)
    out["主播换了设置（新的一段的初始化包不一样）：要转给播放器"] = inits == 3 and steady(timeline(sent)[0], 40)

    sent, relay, opened, naps = run([Body(segment(0, 60)), Body(b""), Body(b""), Body(b""), Body(segment(0, 60))], retries=3)
    out["平台那边一连上就断，连着 3 次：结束，不会没完没了地重连"] = opened == 4 and relay.closed and len(timeline(sent)[0]) == 60

    clock = [0.0]
    old = Body(segment(0, 700), piece=2000, clock=clock, step=10.0)
    sent, relay, opened, _ = run([old, OSError("取地址超时"), Body(segment(550, 800))], ttl=300, clock=clock, retries=2)
    pictures = timeline(sent)[0]
    out["预先连下一段没连上：先接着播旧的，过一会儿再试；旧的断了以后照样接得上"] = (
        opened >= 3 and steady(pictures, 40) and pictures[-1] == 1_000_000 + 799 * 40)

    body = Body(segment(0, 5000), piece=500)
    relay = flv_relay.Relay(lambda: (body, None)).start()
    stream = relay.stream()
    got = next(stream) + next(stream)
    relay.close()
    rest = list(stream)
    out["外面叫停（换了选手、关了直播）：马上停下，连接关掉"] = bool(got) and rest == [] and body.closed and body.at < len(body.data)


def douyu_checks(out):
    enc = {"key": "k3y", "rand_str": "rnd", "enc_time": 3, "enc_data": "ENC", "is_special": 0, "expire_at": 2_000_000_000}
    secret = "rnd"
    for _ in range(3):
        secret = hashlib.md5((secret + "k3y").encode()).hexdigest()
    expected = hashlib.md5((secret + "k3y" + "520013" + "1794900000").encode()).hexdigest()
    special = hashlib.md5((secret + "k3y").encode()).hexdigest()
    out["签名：随机串和钥匙拼起来连算 3 遍 MD5，再和钥匙、房间号、时间算一遍（特殊的那种不带房间号和时间）"] = (
        watch_douyu.sign(enc, "520013", 1794900000) == expected
        and watch_douyu.sign({**enc, "is_special": 1}, "520013", 1794900000) == special)

    play = {"error": 0, "data": {
        "rtmp_url": "https://ws.douyucdn.cn/live", "rtmp_live": "520013rABC_4000.flv?wsAuth=x&amp;expire=0&fcdn=ws",
        "rtmp_cdn": "ws-h5", "rate": 4,
        "cdnsWithName": [{"name": "线路5", "cdn": "scdn-a"}, {"name": "线路1", "cdn": "ws-h5"}, {"name": "线路7", "cdn": "hw-h5"}],
        "multirates": [{"name": "原画1080P60", "rate": 0}, {"name": "蓝光4M", "rate": 4}, {"name": "超清", "rate": 3},
                       {"name": "蓝光4M", "rate": 4}, {"name": "高清", "rate": 2}]}}
    calls = []

    def get(url, room="", form=None):
        calls.append((url, room, form))
        if "getEncryption" in url:
            return {"error": 0, "data": {**enc, "expire_at": 1794900000 + 600}}
        return play

    watch_douyu._encryption.clear()
    found = watch_douyu.resolve("520013", None, get=get, now=1794900000)
    form = calls[1][2]
    out["取地址：先要签名材料，再带着签名问；表单里写明不要 H.265，带上设备号和时间"] = (
        "getEncryption?did=" + watch_douyu.DEVICE_ID in calls[0][0] and calls[1][0].endswith("/getH5PlayV1/520013")
        and form["hevc"] == "0" and form["ver"] == "Douyu_new" and form["rate"] == "0" and form["tt"] == "1794900000"
        and form["did"] == watch_douyu.DEVICE_ID and form["enc_data"] == "ENC" and len(form["auth"]) == 32)
    out["清晰度按斗鱼给的顺序（不排序、不重复）；线路现在用的在前、scdn 排最后；实际给的是回包里的那一档"] = (
        found["qualities"] == [{"id": "0", "name": "原画1080P60"}, {"id": "4", "name": "蓝光4M"},
                               {"id": "3", "name": "超清"}, {"id": "2", "name": "高清"}]
        and found["lines"] == ["ws-h5", "hw-h5", "scdn-a"] and found["rate"] == "4")
    out["地址：两截拼起来，&amp; 还原；不限时的没有期限"] = (
        found["url"] == "https://ws.douyucdn.cn/live/520013rABC_4000.flv?wsAuth=x&expire=0&fcdn=ws" and found["ttl"] is None)
    count = len(calls)
    watch_douyu.resolve("520013", 3, get=get, now=1794900000 + 200)
    reused = len(calls) - count
    watch_douyu.resolve("520013", 3, get=get, now=1794900000 + 580)
    out["签名材料 5 分钟内、离过期还远就接着用；快过期了重新要"] = reused == 1 and len(calls) - count == 3

    limited = {"rtmp_url": "https://ws.douyucdn.cn/live/", "rtmp_live": "/a.flv?expire=300&fcdn=ws&x=1"}
    other = {"rtmp_url": "https://tc.douyucdn.cn/live", "rtmp_live": "a.flv?expire=300&fcdn=tct"}
    full = {"rtmp_live": "https://hw.douyucdn.cn/live/a.flv?expire=0"}
    missing = []
    for broken in ({}, {"rtmp_url": "https://x/live", "rtmp_live": ""}, {"rtmp_live": "rtmp://x/a"}):
        try:
            watch_douyu.stream_url(broken)
        except watch_douyu.DouyuError:
            missing.append(1)
    out["限 300 秒的地址补上不限时（照 DTV 的做法）；补不了的算得出 300 秒到期；没有地址报错"] = (
        watch_douyu.stream_url(limited) == "https://ws.douyucdn.cn/live/a.flv?expire=300&fcdn=ws&x=1&expire=0"
        and watch_douyu.lifetime(watch_douyu.stream_url(limited)) is None
        and watch_douyu.lifetime(watch_douyu.stream_url(other)) == 300
        and watch_douyu.stream_url(full) == "https://hw.douyucdn.cn/live/a.flv?expire=0" and len(missing) == 3)

    replies = [{"error": 0, "data": {**enc, "expire_at": 1794900000 + 6000}}, {"error": -9, "msg": "鉴权失败"},
               {"error": 0, "data": {**enc, "expire_at": 1794900000 + 6000}}, play]
    watch_douyu._encryption.clear()
    asked = []
    watch_douyu.play_data("520013", 0, get=lambda url, room="", form=None: (asked.append(url), replies.pop(0))[1], now=1794900000)
    offline = None
    watch_douyu._encryption.clear()
    try:
        watch_douyu.resolve("520013", 0, now=1794900000, get=lambda url, room="", form=None: (
            {"error": 0, "data": {**enc, "expire_at": 1794900000 + 6000}} if "getEncryption" in url
            else {"error": -5, "msg": "房间未开播"}))
    except watch_douyu.DouyuError as e:
        offline = str(e)
    out["斗鱼不认签名：重新要一次签名材料再试；还不行（比如没开播）说明原因"] = (
        ["getEncryption" in url for url in asked] == [True, False, True, False] and offline is not None and "房间未开播" in offline)

    watch_douyu._encryption.clear()
    watch_douyu.remember("520013", "4", found, now=1000.0)
    count = len(calls)
    kept = watch_douyu.resolve("520013", "4", get=get, now=1010.0, reuse=True)
    again = len(calls) - count
    watch_douyu.resolve("520013", "4", get=get, now=1794900000, reuse=True)
    out["页面刚问过清晰度，紧接着开播：直接用刚取的地址（只用一次），不再问一遍"] = (
        kept is found and again == 0 and len(calls) - count >= 1)

    real_resolve, real_open = watch_douyu.resolve, watch_douyu.open_stream
    tried = []

    def fake_resolve(room, rate=None, cdn="", get=None, now=None, reuse=False):
        return {"url": f"https://{cdn or 'default'}.example/a.flv", "rate": "4", "qualities": [], "lines": ["ws-h5", "hw-h5"],
                "cdn": cdn, "ttl": None}

    def fake_open(url, room):
        tried.append(url)
        if "hw-h5" not in url:
            raise watch_douyu.DouyuError("连不上斗鱼的视频服务器（握手失败）")
        return "连接"

    watch_douyu.resolve, watch_douyu.open_stream = fake_resolve, fake_open
    try:
        body, info = watch_douyu.open_segment("520013", "4")
        watch_douyu.open_stream = lambda url, room: (_ for _ in ()).throw(watch_douyu.DouyuError("第一条线路的原因"))
        reason = None
        try:
            watch_douyu.open_segment("520013", "4")
        except watch_douyu.DouyuError as e:
            reason = str(e)
    finally:
        watch_douyu.resolve, watch_douyu.open_stream = real_resolve, real_open
    out["默认的线路连不上：换别的线路试；都连不上说的是第一条的原因"] = (
        body == "连接" and info["cdn"] == "hw-h5" and tried == ["https://default.example/a.flv", "https://ws-h5.example/a.flv",
                                                               "https://hw-h5.example/a.flv"] and reason == "第一条线路的原因")
    out["打印地址时不带口令：只留主机、路径开头和参数的名字"] = (
        "secret-token" not in watch_douyu.mask("https://ws.douyucdn.cn/live/520013rABC.flv?wsAuth=secret-token&expire=0")
        and "wsAuth" in watch_douyu.mask("https://ws.douyucdn.cn/live/520013rABC.flv?wsAuth=secret-token&expire=0"))


DOUYIN_TIERS = {  # 档名: (编码, 分辨率, 帧率, 码率)；顺序故意是乱的，ao 只有声音
    "sd": ("h264", "1280x720", 30, 2_000_000), "uhd": ("h264", "1920x1080", 60, 8_000_000),
    "md": ("h264", "640x360", 15, 800_000), "ao": ("h264", "", 0, 0), "hd": ("h264", "1280x720", 59, 6_000_000),
    "ld": ("h264", "960x540", 25, 1_000_000), "origin": ("h264", "", 60, 14_080_000)}
DOUYIN_MENU = {"ld": "标清", "sd": "高清", "hd": "超清", "uhd": "蓝光"}
DOUYIN_PAGE = '<html>…{\\"roomInfo\\":{\\"roomId\\":\\"%s\\",\\"web_rid\\":\\"800\\"}}…</html>'


def douyin_body(tiers=None, status=2, menu=None, flv=None):
    """假的抖音房间接口回包。flv：{档名: 地址}，没写的用 http 的假地址。"""
    tiers = DOUYIN_TIERS if tiers is None else tiers
    data = {key: {"main": {"flv": (flv or {}).get(key, f"http://pull-q5.douyincdn.com/thirdgame/stream-1_{key}.flv?expire=1&sign=secret-{key}"),
                           "hls": f"http://pull-q5.douyincdn.com/thirdgame/stream-1_{key}.m3u8",
                           "sdk_params": json.dumps({"VCodec": codec, "resolution": size, "fps": fps, "vbitrate": bitrate})}}
            for key, (codec, size, fps, bitrate) in tiers.items()}
    menu = DOUYIN_MENU if menu is None else menu
    pull = {"stream_data": json.dumps({"common": {}, "data": data}),
            "options": {"qualities": [{"sdk_key": key, "name": name} for key, name in menu.items()]}}
    return json.dumps({"status_code": 0, "data": {"room": {"status": status, "stream_url": {"live_core_sdk_data": {"pull_data": pull}}}}}).encode("utf-8")


def douyin_checks(out):
    found = watch_douyin.parse(douyin_body())
    out["抖音：各档从好到差，叫抖音菜单上的名字加分辨率（原画写码率）；只有声音的那一档不算"] = (
        [(q["id"], q["name"]) for q in found["qualities"]] == [
            ("origin", "原画 14M"), ("uhd", "蓝光 1080P60"), ("hd", "超清 720P60"), ("sd", "高清 720P"),
            ("ld", "标清 540P"), ("md", "流畅 360P")])
    small = {**{k: v for k, v in DOUYIN_TIERS.items() if k != "uhd"}, "origin": ("h264", "", 59, 5_885_952)}
    low = watch_douyin.parse(douyin_body(small, menu={"ld": "标清", "sd": "高清", "hd": "超清", "origin": "蓝光"}))
    out["抖音：没选过清晰度时要码率不超过上限（9 Mbps）的最好一档：原画 14 Mbps 的房间给 1080p60，原画不到 9 Mbps 的给原画"] = (
        found["rate"] == "uhd" and low["rate"] == "origin" and low["qualities"][0] == {"id": "origin", "name": "蓝光 6M"})
    heavy = {"origin": ("h264", "", 60, 20_000_000), "uhd": ("h264", "1920x1080", 60, 12_000_000)}
    out["抖音：玩家选了就按选的；选的那一档这个房间没有，按没选过的办；每一档都超过上限就要码率最低的"] = (
        watch_douyin.parse(douyin_body(), "origin")["rate"] == "origin" and watch_douyin.parse(douyin_body(), "ld")["rate"] == "ld"
        and watch_douyin.parse(douyin_body(small), "uhd")["rate"] == "origin"
        and watch_douyin.parse(douyin_body(heavy, menu={}))["rate"] == "uhd")
    odd = watch_douyin.parse(douyin_body(flv={"uhd": "javascript:alert(1)", "hd": "https://pull-flv-q5.douyincdn.com/a/b_hd.flv?sign=x"}))
    out["抖音：给页面的地址是 https 的（抖音给 http 的换掉）；不像地址的那一档不要"] = (
        found["url"] == "https://pull-q5.douyincdn.com/thirdgame/stream-1_uhd.flv?expire=1&sign=secret-uhd"
        and [q["id"] for q in odd["qualities"]] == ["origin", "hd", "sd", "ld", "md"] and odd["rate"] == "hd"
        and odd["url"] == "https://pull-flv-q5.douyincdn.com/a/b_hd.flv?sign=x" and found["ttl"] is None)
    mixed = {"origin": ("h265", "", 60, 8_000_000), "hd": ("h264", "1280x720", 60, 6_000_000), "sd": ("bytevc1", "1280x720", 30, 2_000_000)}
    only = watch_douyin.parse(douyin_body(mixed))
    problems = []
    for body in (douyin_body({k: ("h265", *v[1:]) for k, v in DOUYIN_TIERS.items()}), douyin_body(status=4),
                 douyin_body({}), b'{"status_code": 4001038, "data": {}}', b"<html>"):
        try:
            watch_douyin.parse(body)
            problems.append(None)
        except watch_douyin.DouyinError as e:
            problems.append((type(e).__name__, str(e)))
    out["抖音：只要 H.264 的档；一档都没有、没开播、没给地址、回的不是房间数据：说明原因（页面显示退路）"] = (
        [q["id"] for q in only["qualities"]] == ["hd"] and only["rate"] == "hd"
        and problems[0][0] == "DouyinError" and "H.264" in problems[0][1] and problems[1][0] == "NotLive"
        and problems[2] == ("DouyinError", "抖音没给视频地址") and "4001038" in problems[3][1] and problems[4] is not None)

    asked, replies, page = [], {}, {"inner": "7001000000"}

    def get(url, headers, limit=0, until=None, cookies=False):
        if url.startswith("https://live.douyin.com/"):
            asked.append("page")
            if page["inner"] == "blocked":
                raise live_status.StatusError("HTTP 403", 403)
            return (DOUYIN_PAGE % page["inner"] if page["inner"] else "<html>没有</html>").encode("utf-8")
        inner = url.split("room_id=")[1].split("&")[0]
        asked.append(inner)
        return replies[inner]

    def run(rate=None):
        asked.clear()
        try:
            return watch_douyin.resolve("800", rate, get)["rate"], list(asked)
        except watch_douyin.DouyinError as e:
            return type(e).__name__, list(asked)

    live_status._douyin_inner.clear()
    try:
        replies.update({"7001000000": douyin_body(), "7002000000": douyin_body(small), "7000000000": douyin_body(status=4)})
        fresh = run("hd")
        live_status._douyin_inner["800"] = "7001000000"
        known = run()
        out["抖音：查开播状态时记着这一场的内部房间号就只问接口一次；没记着先开直播间网页"] = (
            fresh == ("hd", ["page", "7001000000"]) and known == ("uhd", ["7001000000"]))
        live_status._douyin_inner["800"] = "7000000000"
        page["inner"] = "7002000000"
        restarted = run()
        page["inner"] = "7000000000"
        ended = run()
        out["抖音：记着的那一场结束了，开网页看有没有新的一场：有就播新的，没有说没开播"] = (
            restarted == ("origin", ["7000000000", "page", "7002000000"]) and ended == ("NotLive", ["7000000000", "page"]))
        live_status._douyin_inner.clear()
        page["inner"] = ""
        missing = run()
        page["inner"] = "blocked"
        blocked = run()
        out["抖音：网页里读不出房间、被拦住：说明原因，不出错"] = (
            missing == ("DouyinError", ["page"]) and blocked == ("DouyinError", ["page"]))
    finally:
        live_status._douyin_inner.clear()
    out["抖音：打印地址时不带口令"] = "secret" not in watch_douyin.mask(found["url"]) and "douyincdn.com" in watch_douyin.mask(found["url"])


TWITCH_TOKEN = {"data": {"streamPlaybackAccessToken": {"value": "VALUE+token=", "signature": "SIG/sig="}}}
TWITCH_URLS = {"chunked": "https://video-weaver.arn02.hls.ttvnw.net/v1/playlist/source.m3u8",
               "720p60": "https://video-weaver.arn02.hls.ttvnw.net/v1/playlist/720.m3u8",
               "480p30": "https://video-weaver.arn02.hls.ttvnw.net/v1/playlist/480.m3u8",
               "160p30": "https://video-weaver.arn02.hls.ttvnw.net/v1/playlist/160.m3u8"}
# 多档目录（照 2026-11-25 实测的样子）：故意不按清晰度排，最后一档只有声音
TWITCH_MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=8269706,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
{chunked}
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="480p30",NAME="480p"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=852x480,CODECS="avc1.4d401e,mp4a.40.2",VIDEO="480p30",FRAME-RATE=30.000
{480p30}
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="160p30",NAME="160p"
#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160,CODECS="avc1.42c00b,mp4a.40.2",VIDEO="160p30",FRAME-RATE=30.000
{160p30}
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",VIDEO="720p60",FRAME-RATE=60.000
{720p60}
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="audio only"
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://video-weaver.arn02.hls.ttvnw.net/v1/playlist/audio.m3u8
""".format(**TWITCH_URLS)


def twitch_ask(calls=None, token=None, playlist=None):
    """假的 Twitch 联网：要令牌回 TWITCH_TOKEN，问 usher 回多档目录。token / playlist 换成回包或要抛的错。"""
    def ask(url, headers, limit=None, until=None, cookies=False, data=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        if url == live_status.PLATFORMS["twitch"]["gql_url"]:
            if isinstance(token, Exception):
                raise token
            return token if token is not None else json.dumps(TWITCH_TOKEN).encode("utf-8")
        if isinstance(playlist, Exception):
            raise playlist
        return playlist if playlist is not None else TWITCH_MASTER.encode("utf-8")
    return ask


def twitch_checks(out):
    watch_twitch._recent.clear()
    calls = []
    ask = twitch_ask(calls)
    found = watch_twitch.resolve("ImperialHal__", None, ask=ask)
    out["Twitch：各档从好到差（原画最前），名字用目录里的 NAME（原画去掉 (source)、加\"原画\"），不要 audio_only"] = (
        [(q["id"], q["name"]) for q in found["qualities"]] == [
            ("chunked", "1080p60（原画）"), ("720p60", "720p60"), ("480p30", "480p"), ("160p30", "160p")]
        and found["rate"] == "chunked" and found["kind"] == "mpegts" and found["ttl"] is None
        and found["hls"] == [TWITCH_URLS["chunked"]])
    token_call, playlist_call = calls[0], calls[1]
    sent = json.loads(token_call["data"].decode("utf-8"))
    parts = urllib.parse.urlsplit(playlist_call["url"])
    params = urllib.parse.parse_qs(parts.query)
    out["Twitch 取地址：先要播放令牌（PlaybackAccessToken，固定的查询编号，登录名转小写），再问 usher（只要 H.264、带令牌）"] = (
        token_call["url"] == live_status.PLATFORMS["twitch"]["gql_url"]
        and sent["operationName"] == "PlaybackAccessToken" and sent["variables"]["login"] == "imperialhal__"
        and sent["variables"]["isLive"] is True
        and sent["extensions"]["persistedQuery"]["sha256Hash"] == live_status.PLATFORMS["twitch"]["token_hash"]
        and parts.path.endswith("/imperialhal__.m3u8") and params["sig"] == ["SIG/sig="]
        and params["token"] == ["VALUE+token="] and params["supported_codecs"] == ["h264"]
        and params["allow_source"] == ["true"] and token_call["timeout"] == 5)

    problems = []
    for ask_one in (
        twitch_ask(token=json.dumps({"data": {"streamPlaybackAccessToken": None}}).encode("utf-8")),
        twitch_ask(token=json.dumps({"errors": [{"message": "PersistedQueryNotFound"}]}).encode("utf-8")),
        twitch_ask(playlist=live_status.StatusError("HTTP 404", 404)),
        twitch_ask(playlist=live_status.StatusError("HTTP 403", 403, b'{"error":"geo","error_code":"GEOBLOCK"}')),
        twitch_ask(playlist=live_status.StatusError("HTTP 403", 403, b'{"error":"subscription required"}')),
        twitch_ask(token=live_status.StatusError("连不上（超时）")),
        twitch_ask(token=OSError("timed out")),
    ):
        try:
            watch_twitch.resolve("imperialhal__", None, ask=ask_one)
            problems.append(None)
        except watch_twitch.TwitchPlayError as e:
            problems.append((str(e), e.unreachable))
    out["Twitch：令牌是 null（没这个频道）、PersistedQueryNotFound（改了接口）、usher 404（没开播）、403（地区限制、订阅专属）"
        "都说明白；连不上带 unreachable"] = (
        "没有这个直播间" in problems[0][0] and "改了接口" in problems[1][0] and "没开播" in problems[2][0]
        and "地区" in problems[3][0] and "订阅" in problems[4][0]
        and "连不上" in problems[5][0] and problems[5][1] is True and problems[6][1] is True)
    out["Twitch：没选过给原画（chunked）；选了按选的；选的那一档这次没有就给最好的一档"] = (
        found["rate"] == "chunked"
        and watch_twitch.resolve("imperialhal__", "720p60", ask=ask)["hls"] == [TWITCH_URLS["720p60"]]
        and watch_twitch.resolve("imperialhal__", "720p60", ask=ask)["rate"] == "720p60"
        and watch_twitch.resolve("imperialhal__", "1080p60", ask=ask)["rate"] == "chunked")

    watch_twitch._recent.clear()
    fresh = watch_twitch.resolve("ImperialHal__", "720p60", ask=ask)
    count = len(calls)
    watch_twitch.remember("ImperialHal__", "720p60", fresh, now=1000.0)
    kept = watch_twitch.resolve("imperialhal__", "720p60", ask=ask, now=1010.0, reuse=True)
    quiet = len(calls) - count
    watch_twitch.resolve("imperialhal__", "720p60", ask=ask, now=1794900000, reuse=True)
    out["Twitch：页面刚问过清晰度，紧接着开播：直接用刚取的地址（只用一次），不再问一遍"] = (
        kept is fresh and quiet == 0 and len(calls) - count >= 2)

    real_fetch = live_status.fetch
    live_status.fetch = ask
    try:
        watch_twitch._recent.clear()
        relay = watch_twitch.open_relay("ImperialHal__", "720p60")
        first = relay.open_playlist()
        again = relay.open_playlist()
    finally:
        live_status.fetch = real_fetch
    out["Twitch 的中转：给 HlsRelay 的是选的那一档；地址要重取时还是同一档（按 id 对）"] = (
        isinstance(relay, hls_relay.HlsRelay) and first == [TWITCH_URLS["720p60"]]
        and again == [TWITCH_URLS["720p60"]] and relay.fetch is watch_twitch.fetch)
    out["Twitch：打印地址时不带令牌（只留主机和路径开头）"] = (
        "VALUE+token=" not in watch_twitch.mask(TWITCH_URLS["chunked"]) and "ttvnw.net" in watch_twitch.mask(TWITCH_URLS["chunked"]))


class FakeDouyin:
    """假的"抖音取地址"：页面直接连平台的那一种（DIRECT）。"""
    DIRECT = True

    def __init__(self):
        self.asked = []

    def resolve(self, room, rate=None):
        self.asked.append((room, rate))
        return {"url": "https://pull-q5.douyincdn.com/thirdgame/stream-1_x.flv?sign=s", "rate": rate or "uhd", "ttl": None,
                "qualities": [{"id": "uhd", "name": "蓝光 1080P60"}, {"id": "hd", "name": "超清 720P60"}]}


class FakeDouyu:
    """假的"斗鱼取地址"：记下被问了什么，视频连接是这里给的。"""

    def __init__(self):
        self.asked, self.bodies, self.fail = [], [], None

    def resolve(self, room, rate=None):
        self.asked.append(("resolve", room, rate))
        if self.fail:
            raise watch_douyu.DouyuError(self.fail)
        return {"url": "https://x/a.flv", "rate": "4" if rate in (None, "0") else str(rate), "ttl": None, "lines": [], "cdn": "",
                "qualities": [{"id": "0", "name": "原画1080P60"}, {"id": "4", "name": "蓝光4M"}, {"id": "3", "name": "超清"}]}

    def remember(self, room, rate, found):
        self.asked.append(("remember", room, rate))

    def open_segment(self, room, rate=None, reuse=False):
        self.asked.append(("open", room, rate, reuse))
        if not self.bodies:
            raise watch_douyu.DouyuError("连不上斗鱼的视频服务器")
        return self.bodies.pop(0), {"ttl": None}


def panel_checks(out):
    folder = tempfile.mkdtemp(prefix="evo-play-")
    now = datetime.now().astimezone()
    players = [{"id": "douyu-520013", "name": "Kite", "team": "甲队", "platform": "douyu", "room": "520013"},
               {"id": "douyu-520014", "name": "Hex", "team": "甲队", "platform": "douyu", "room": "520014"},
               {"id": "bilibili-6", "name": "Sol", "team": "甲队", "platform": "bilibili", "room": "6"},
               {"id": "douyin-800", "name": "Rei", "team": "甲队", "platform": "douyin", "room": "800"}]
    event = {"id": "live", "name": "联赛", "type": "联赛", "stage": "", "start": (now - timedelta(hours=1)).isoformat(),
             "end": (now + timedelta(hours=3)).isoformat(), "teams": ["甲队"],
             "main": [{"id": "douyu-777", "name": "官方直播", "platform": "douyu", "room": "777"}]}  # 主舞台的一路

    def serve(platforms=None):
        path = Path(folder) / "esports.json"
        path.write_text(json.dumps({"version": 1, "updated": now.isoformat(), "players": players, "events": [event],
                                    "platforms": platforms or {}}), encoding="utf-8")
        esports_data.URL = str(path)
        esports_data.CACHE_PATH.unlink(missing_ok=True)
        esports_data.get(force=True)

    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    fake, fake_douyin = FakeDouyu(), FakeDouyin()
    real = panel_esports.PLAYERS
    panel_esports.PLAYERS = {"douyu": fake, "douyin": fake_douyin}
    try:
        serve()
        page = client.get("/esports/live/watch").get_data(as_text=True)
        data = json.loads(page.split('id="watch-data">')[1].split("</script>")[0])
        plays = {p["id"]: p["play"] for p in data["players"]}
        out["看直播页：斗鱼、抖音选手带着问清晰度的接口，B站的没有（用官方播放器）；播放器从面板自己的文件加载，不联网"] = (
            plays == {"douyu-777": "/esports/play/douyu-777", "douyu-520013": "/esports/play/douyu-520013",
                      "douyu-520014": "/esports/play/douyu-520014", "bilibili-6": None, "douyin-800": "/esports/play/douyin-800"}
            and data["mpegts_url"] == "/static/vendor/mpegts.js")
        script = client.get("/static/vendor/mpegts.js")
        out["mpegts.js 和它的许可证随面板一起发，类型是脚本"] = (
            script.status_code == 200 and script.mimetype == "text/javascript" and len(script.data) > 200_000
            and (Path(panel_app.app.static_folder) / "vendor" / "mpegts.LICENSE.txt").read_text(encoding="utf-8").lstrip()
            .startswith("Apache License"))

        info = client.get("/esports/play/douyu-520013").get_json()
        out["问清晰度：回有哪些档、实际给的是哪一档（没指定时要最好的），和带口令、带实际那一档的播放地址"] = (
            info["ok"] and [q["name"] for q in info["qualities"]] == ["原画1080P60", "蓝光4M", "超清"] and info["applied"] == "4"
            and info["stream"] == f"/esports/stream/douyu-520013?q=4&s=0&t={token}"
            and fake.asked == [("resolve", "520013", None), ("remember", "520013", "4")])
        chosen = client.get("/esports/play/douyu-520013?q=3").get_json()
        junk = client.get("/esports/play/douyu-520013?q=3;drop").get_json()
        out["玩家选了清晰度就按选的要；写得不对的清晰度当作没选"] = (
            chosen["applied"] == "3" and ("resolve", "520013", "3") in fake.asked and junk["applied"] == "4"
            and fake.asked[-2] == ("resolve", "520013", None))
        fake.fail = "斗鱼的取流接口回了 error=-5（房间未开播）"
        failed = client.get("/esports/play/douyu-520013").get_json()
        fake.fail = None
        codes = [client.get(url).status_code for url in ("/esports/play/bilibili-6", "/esports/play/nobody")]
        out["取不到地址：说明原因（页面显示退路）；不是用 Evo 的播放器播的选手、名单里没有的人：404"] = (
            failed == {"ok": False, "reason": "upstream", "detail": "斗鱼的取流接口回了 error=-5（房间未开播）"} and codes == [404, 404])

        data_one, data_two = segment(0, 120), segment(0, 80)
        fake.bodies = [Body(data_one)]
        fake.asked.clear()
        no_token = client.get("/esports/stream/douyu-520013?q=4")
        bad_token = client.get("/esports/stream/douyu-520013?q=4&t=wrong")
        out["中转要带面板的口令：没带、带错都拒绝，也不去连平台"] = (
            no_token.status_code == 403 and bad_token.status_code == 403 and fake.asked == [])
        one = client.get(f"/esports/stream/douyu-520013?q=4&t={token}", buffered=False)
        opened = list(fake.asked)
        out["中转：回的是 FLV，第一次连用刚取过的地址"] = (
            one.status_code == 200 and one.mimetype == "video/x-flv" and opened == [("open", "520013", "4", True)])
        relay_one = panel_esports._playing["0"]
        fake.bodies = [Body(data_two)]
        side = client.get(f"/esports/stream/douyu-520014?q=4&s=1&t={token}", buffered=False)  # 多屏同看的第二格
        relay_side = panel_esports._playing["1"]
        out["多屏同看：别的格子开一路，不停第一格的；格子写得不对当作第一格；问播放地址时带着格子"] = (
            side.status_code == 200 and relay_side is not relay_one and not relay_one.closed
            and "s=1" in client.get("/esports/play/douyu-520013?s=1").get_json()["stream"]
            and "s=0" in client.get("/esports/play/douyu-520013?s=9").get_json()["stream"])
        side.close()
        fake.bodies = [Body(data_two)]
        two = client.get(f"/esports/stream/douyu-520014?q=4&t={token}", buffered=False)
        relay_two = panel_esports._playing["0"]
        got_two = b"".join(two.response)
        out["每一格一次只播一路：这一格开新的一路，它上一路就停"] = (
            relay_one is not relay_two and relay_one.closed and got_two == data_two)
        one.close()

        fake.bodies = [Body(segment(0, 50, codec=12))]
        hevc = client.get(f"/esports/stream/douyu-520013?q=4&t={token}")
        dead = client.get(f"/esports/stream/douyu-520013?q=4&t={token}")
        out["不是 H.264、连不上视频服务器：回 502 和原因，不发视频"] = (
            hevc.status_code == 502 and hevc.get_json()["reason"] == "not_h264"
            and dead.status_code == 502 and dead.get_json()["reason"] == "upstream")

        stage = client.get("/esports/play/douyu-777").get_json()
        out["主舞台的直播间（不在选手名单里）也能问到播放地址"] = (
            stage["ok"] and stage["stream"] == f"/esports/stream/douyu-777?q=4&s=0&t={token}" and ("resolve", "777", None) in fake.asked)
        direct = client.get("/esports/play/douyin-800").get_json()
        named = client.get("/esports/play/douyin-800?q=hd").get_json()
        shouted = client.get("/esports/play/douyin-800?q=HD!").get_json()
        out["抖音：给页面的是抖音自己的视频地址（页面直接连），清晰度用档名选；写得不对的当作没选"] = (
            direct["ok"] and direct["stream"] == "https://pull-q5.douyincdn.com/thirdgame/stream-1_x.flv?sign=s"
            and direct["applied"] == "uhd" and [q["id"] for q in direct["qualities"]] == ["uhd", "hd"]
            and named["applied"] == "hd" and shouted["applied"] == "uhd"
            and fake_douyin.asked == [("800", None), ("800", "hd"), ("800", None)])
        fake.bodies = [Body(data_one)]
        out["抖音不经过本机中转：中转的接口不认它"] = (
            client.get(f"/esports/stream/douyin-800?q=hd&t={token}").status_code == 404 and len(fake.bodies) == 1)
        serve({"douyin": False})
        out["抖音的平台开关关了：不给地址"] = (
            client.get("/esports/play/douyin-800").status_code == 404 and len(fake_douyin.asked) == 3)

        serve({"douyu": False})
        fake.bodies = [Body(data_one)]
        out["平台开关关了：不给地址、不中转（页面改成去平台看）"] = (
            client.get("/esports/play/douyu-520013").status_code == 404
            and client.get(f"/esports/stream/douyu-520013?q=4&t={token}").status_code == 404 and len(fake.bodies) == 1)
    finally:
        panel_esports.PLAYERS = real
        panel_esports._playing.clear()


class FakeTwitch:
    """假的"Twitch 取地址"：经本机中转（有 open_relay）的那一种。"""

    def __init__(self):
        self.asked, self.fail, self.relay_fail = [], None, None

    def resolve(self, room, rate=None):
        self.asked.append(("resolve", room, rate))
        if self.fail:
            raise watch_twitch.TwitchPlayError(self.fail[0], unreachable=self.fail[1])
        return {"qualities": [{"id": "chunked", "name": "1080p60（原画）"}, {"id": "720p60", "name": "720p60"}],
                "rate": rate or "chunked", "kind": "mpegts", "hls": [TWITCH_URLS["chunked"]], "ttl": None}

    def remember(self, room, rate, found):
        self.asked.append(("remember", room, rate))

    def open_relay(self, room, rate=None, on_event=None):
        self.asked.append(("open_relay", room, rate))
        if self.relay_fail:
            raise watch_twitch.TwitchPlayError(self.relay_fail[0], unreachable=self.relay_fail[1])
        raise watch_twitch.TwitchPlayError("测试里不真的连视频服务器")


def twitch_panel_checks(out):
    """面板的 /esports/play 对 Twitch 选手：清晰度、中转地址、连不上、平台开关。"""
    folder = tempfile.mkdtemp(prefix="evo-twitch-play-")
    now = datetime.now().astimezone()
    players = [{"id": "twitch-imperialhal__", "name": "ImperialHal", "team": "甲队", "platform": "twitch",
                "room": "imperialhal__"},
               {"id": "douyu-101", "name": "Kite", "team": "甲队", "platform": "douyu", "room": "101"}]
    event = {"id": "live", "name": "联赛", "type": "联赛", "stage": "", "start": (now - timedelta(hours=1)).isoformat(),
             "end": (now + timedelta(hours=3)).isoformat(), "teams": ["甲队"]}

    def serve(platforms=None):
        path = Path(folder) / "esports.json"
        path.write_text(json.dumps({"version": 1, "updated": now.isoformat(), "players": players, "events": [event],
                                    "platforms": platforms or {}}), encoding="utf-8")
        esports_data.URL = str(path)
        esports_data.CACHE_PATH.unlink(missing_ok=True)
        esports_data.get(force=True)

    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    fake, real = FakeTwitch(), panel_esports.PLAYERS
    panel_esports.PLAYERS = {**real, "twitch": fake}
    try:
        serve()
        page = client.get("/esports/live/watch").get_data(as_text=True)
        data = json.loads(page.split('id="watch-data">')[1].split("</script>")[0])
        member = {p["id"]: p for p in data["players"]}["twitch-imperialhal__"]
        info = client.get("/esports/play/twitch-imperialhal__").get_json()
        chosen = client.get("/esports/play/twitch-imperialhal__?q=720p60").get_json()
        out["面板：Twitch 选手带着问清晰度的接口和弹幕地址（没有嵌入播放器）；回清晰度、实际那一档和中转地址"] = (
            member["play"] == "/esports/play/twitch-imperialhal__" and member["embed"] is None
            and member["danmaku"] == "/esports/danmaku/twitch-imperialhal__"
            and member["url"] == "https://www.twitch.tv/imperialhal__"
            and info["ok"] and info["kind"] == "mpegts" and info["applied"] == "chunked"
            and [q["id"] for q in info["qualities"]] == ["chunked", "720p60"]
            and info["stream"] == f"/esports/stream/twitch-imperialhal__?q=chunked&s=0&t={token}"
            and chosen["applied"] == "720p60" and ("resolve", "imperialhal__", "720p60") in fake.asked)
        fake.fail = ("连不上 Twitch（在国内要能访问国外网站）", True)
        down = client.get("/esports/play/twitch-imperialhal__").get_json()
        fake.fail = None
        fake.relay_fail = ("连不上 Twitch（在国内要能访问国外网站）", True)
        relay_down = client.get(f"/esports/stream/twitch-imperialhal__?q=chunked&t={token}")
        fake.relay_fail = None
        serve({"twitch": False})
        off = client.get("/esports/play/twitch-imperialhal__").status_code
        out["面板：Twitch 连不上回 reason \"unreachable\"（播放地址和中转都算）；平台开关关了回 404"] = (
            down == {"ok": False, "reason": "unreachable", "detail": "连不上 Twitch（在国内要能访问国外网站）"}
            and relay_down.status_code == 502 and relay_down.get_json()["reason"] == "unreachable"
            and off == 404)
    finally:
        panel_esports.PLAYERS = real
        panel_esports._playing.clear()
        panel_esports._status.update(at=0.0, found={})


def main():
    print("在 Evo 窗口里播斗鱼、抖音、Twitch（FLV 拆标签和接续、斗鱼 / 抖音 / Twitch 取地址、面板的两个接口）")
    out = {}
    demux_checks(out)
    relay_checks(out)
    douyu_checks(out)
    douyin_checks(out)
    twitch_checks(out)
    panel_checks(out)
    twitch_panel_checks(out)
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
