"""Evo 窗口的"赛事"页面（panel_esports.py、esports.html）。不联网：赛事文件是临时文件夹里的一个文件，查开播状态换成假的。

- 导航里有"电竞"（这一页的名字），在"赛季"和"设置"之间；这一页打开时"电竞"是选中的
- 三块：正在进行（卡片：进行中、类型、开始时间、名称、阶段和队伍数、"看直播"按钮）、即将开始（先开始的在前，
  每行有"看名单和直播"按钮：没开始也能点进去）、最近结束（"已结束"，不能点，超过 7 天的不显示）；最下面一行小字
- 没有正在进行的：写下一场什么时候开始；即将开始、最近结束没有就不显示那一块
- 没读到：一次都没读到过写"没有读到"；读到过、这次没连上，显示上次的并说明
- 几位选手在播（GET /esports/live）：只查正在进行的赛事名单里的选手；60 秒内再问不重查；没查到的单独数
- 赛程里的字只当纯文字显示；"看直播"能点进去（看直播页还没做，是占位的页面）、点回来；没有这场赛事时回到赛事页

运行：python tests/test_esports_page.py
"""

import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import esports_data
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)


def event(eid, start, hours=4, name="示例联赛", kind="联赛", stage="A 组对 B 组", teams=("甲队", "乙队")):
    return {"id": eid, "name": name, "type": kind, "stage": stage, "start": start.isoformat(),
            "end": (start + timedelta(hours=hours)).isoformat(), "teams": list(teams)}


def roster():
    out = []
    for team, platform in (("甲队", "douyu"), ("乙队", "bilibili"), ("丙队", "huya")):
        out += [{"id": f"{platform}-{n}", "name": f"{team}{n}号", "team": team, "platform": platform, "room": str(n)}
                for n in (1, 2, 3)]
    return out


def serve(folder, events, players=None, streamers=None):
    """把赛事文件写到临时文件夹，让 Evo 读它；清掉上次存下的和查过的开播状态。"""
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": roster() if players is None else players,
                                "events": events, **({"streamers": streamers} if streamers is not None else {})}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})
    return path


def text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", html, flags=re.S)).split())


def section(html, heading):
    found = re.findall(rf'<section class="es-section"[^>]*>\s*(?:<div class="es-section-head">\s*)?<h2[^>]*>{heading}</h2>.*?</section>', html, re.S)
    return found[0] if found else ""


def main():
    print("赛事页（导航、三块、没读到、几位选手在播）")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-esports-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()

    events = [event("live1", NOW - timedelta(hours=1), name="职业联赛 第 3 周"),
              event("live2", NOW - timedelta(minutes=5), name="晚间训练赛", kind="训练赛", stage="", teams=("乙队",)),
              event("soon2", NOW + timedelta(days=3), name="三天后的杯赛", kind="杯赛", stage="决赛"),
              event("soon1", NOW + timedelta(hours=2), name="<b>两小时后</b>的训练赛", kind="训练赛"),
              event("done", NOW - timedelta(days=1, hours=5), name="昨天的联赛"),
              event("old", NOW - timedelta(days=9), name="上上周的联赛")]
    serve(folder, events)
    raw = client.get("/esports").get_data(as_text=True)
    nav = re.findall(r'<nav class="nav".*?</nav>', raw, re.S)[0]
    out["导航里有\"赛事\"，在\"赛季\"和\"设置\"之间，这一页打开时是选中的"] = (
        text(nav).split() == ["总览", "战绩", "数据", "赛季", "电竞", "设置"]
        and 'class="active"' in re.findall(r'<a href="/esports"[^>]*>', nav)[0])
    live, upcoming, recent = (section(raw, h) for h in ("正在进行", "即将开始", "最近结束"))
    cards = re.findall(r"<article.*?</article>", live, re.S)
    first = text(cards[0]) if cards else ""
    start = NOW - timedelta(hours=1)
    out["正在进行：每场一张卡片，写着进行中、类型、开始时间、名称、阶段和队伍数，有\"看直播\"按钮"] = (
        len(cards) == 2 and all(word in first for word in ("进行中", "联赛", "职业联赛 第 3 周", "A 组对 B 组，2 支队伍", "看直播"))
        and f"{panel_esports.day_text(start, NOW)} {start:%H:%M} 开始" in first
        and 'href="/esports/live1/watch"' in cards[0] and "1 支队伍" in text(cards[1]) and "，1 支队伍" not in text(cards[1]))
    rows = [text(r) for r in re.findall(r"<li.*?</li>", upcoming, re.S)]
    out["即将开始：一行一场，先开始的在前，写日期、钟点、名称、阶段、队伍数"] = (
        len(rows) == 2 and "两小时后" in rows[0] and "三天后的杯赛" in rows[1] and "杯赛 · 决赛" in rows[1]
        and all("2 支队伍" in r for r in rows) and f"{NOW + timedelta(days=3):%m-%d} 周" in rows[1])
    links = re.findall(r'<a class="btn es-row-btn" href="([^"]+)"\s+aria-label="([^"]+)">([^<]+)</a>', upcoming)
    soon3 = NOW + timedelta(days=3)
    out["即将开始的也能点进去看名单和直播（没开始也能看选手赛前的直播）；按钮念出来带赛事名和时间；最近结束的不能点"] = (
        [(href, word) for href, _, word in links] == [("/esports/soon1/watch", "看名单和直播"), ("/esports/soon2/watch", "看名单和直播")]
        and links[1][1] == f"看名单和直播：三天后的杯赛，{soon3:%m-%d} {soon3:%H:%M} 开始"
        and "开始后可以看直播" not in raw and "<a " not in recent
        and client.get("/esports/soon1/watch").status_code == 200)
    ended = [text(r) for r in re.findall(r"<li.*?</li>", recent, re.S)]
    out["最近结束：灰色，写\"已结束\"；结束超过 7 天的不显示"] = (
        len(ended) == 1 and "昨天的联赛" in ended[0] and "已结束" in ended[0] and "es-ended" in recent
        and "上上周" not in raw)
    out["最下面一行小字、顶部写着赛事管理员；赛程里的字只当纯文字显示"] = (
        "赛事、队伍和每位选手的直播间由 Evo 统一维护。" in text(raw) and "赛事管理员：毛毛" in text(live)
        and "<b>两小时后</b>" not in raw and "&lt;b&gt;两小时后&lt;/b&gt;" in raw)

    asked = []

    def fake_query(players):
        asked.append([p["id"] for p in players])
        states = {"douyu-1": "live", "douyu-2": "live", "douyu-3": "offline", "bilibili-1": "live", "bilibili-2": "error"}
        return {p["id"]: live_status.result(states.get(p["id"], "offline")) for p in players}

    real_query, live_status.query = live_status.query, fake_query
    try:
        counts = client.get("/esports/live").get_json()
        again = client.get("/esports/live").get_json()
        out["几位选手在播：每场赛事只数它名单里的选手，没查到的单独数；名单里开直播的选手一次查完（同一个人只查一次），\"主播\"一块用"] = (
            counts["events"] == {"live1": {"live": 3, "total": 6, "unknown": 1}, "live2": {"live": 1, "total": 3, "unknown": 1}}
            and sorted(counts["streamers"]) == sorted(asked[0]) and counts["streamers"]["huya-1"]["state"] == "offline"
            and len(asked) == 1 and sorted(asked[0]) == sorted(f"{p}-{n}" for p in ("douyu", "bilibili", "huya") for n in (1, 2, 3)))
        out["60 秒内再问不重查"] = again == counts and len(asked) == 1
        panel_esports._status["at"] -= 61
        client.get("/esports/live")
        out["过了 60 秒再问会重查"] = len(asked) == 2
    finally:
        live_status.query = real_query
    out["页面上给\"几位选手在播\"留了位置（先写\"正在看谁在播…\"），由脚本问 /esports/live 填上"] = (
        raw.count('data-es-count="live') == 2 and "正在看谁在播…" in first and 'fetch("/esports/live"' in raw)

    watch = client.get("/esports/live1/watch")
    gone = client.get("/esports/no-such/watch")
    out["\"看直播\"能点进去、点回来；没有这场赛事时回到赛事页并说明"] = (
        watch.status_code == 200 and "职业联赛 第 3 周" in watch.get_data(as_text=True)
        and 'href="/esports"' in watch.get_data(as_text=True)
        and gone.status_code == 302 and gone.headers["Location"].endswith("/esports")
        and "没有找到这场赛事" in client.get("/esports").get_data(as_text=True))

    serve(folder, [event("soon", NOW + timedelta(hours=3))])
    quiet = client.get("/esports").get_data(as_text=True)
    soon = NOW + timedelta(hours=3)
    out["没有正在进行的：写下一场什么时候开始；没有最近结束的就不显示那一块，也不问几位选手在播（只有\"主播\"一块问一次）"] = (
        f"现在没有正在进行的赛事，下一场 {panel_esports.day_text(soon, NOW)} {soon:%H:%M} 开始。" in text(quiet)
        and section(quiet, "即将开始") and not section(quiet, "最近结束") and "data-es-count" not in quiet
        and quiet.count('fetch("/esports/live"') == 1)

    # ---- 主播名单（2026-11-19）----
    silent = [{"id": "none-0123456789ab", "name": "不播的", "team": "甲队", "platform": "none", "room": ""}]
    serve(folder, [event("soon", NOW + timedelta(hours=3))], players=silent)
    nobody = client.get("/esports").get_data(as_text=True)
    out["名单里没有开直播的选手：没有\"主播\"这一块，也不问谁在播"] = (
        "h-es-streamers" not in nobody and 'id="es-st-grid"' not in nobody and "/esports/live" not in nobody and section(nobody, "即将开始") != "")
    streamers = [{"id": "douyu-901", "name": "大主播", "platform": "douyu", "room": "901"},
                 {"id": "huya-902", "name": "<i>二主播</i>", "platform": "huya", "room": "902"},
                 {"id": "bilibili-1", "name": "也是选手", "platform": "bilibili", "room": "1"},
                 {"id": "douyin-abc", "name": "四主播", "platform": "douyin", "room": "abc"}]
    serve(folder, [event("live1", NOW - timedelta(hours=1), name="职业联赛 第 3 周")], players=[], streamers=streamers)
    only_extra = client.get("/esports").get_data(as_text=True)
    extra_cards = re.findall(r'<a class="card es-st".*?</a>', only_extra, re.S)
    out["另外加的主播（streamers）也列出来：照写的顺序，没有队名；名字只当纯文字"] = (
        [re.search(r'data-streamer="([^"]+)"', c).group(1) for c in extra_cards] == [s["id"] for s in streamers]
        and all('data-team=""' in c and "es-st-team" not in c for c in extra_cards)
        and "<i>二主播</i>" not in only_extra and "&lt;i&gt;二主播&lt;/i&gt;" in only_extra)
    serve(folder, [event("live1", NOW - timedelta(hours=1), name="职业联赛 第 3 周")], streamers=streamers)
    with_list = client.get("/esports").get_data(as_text=True)
    block = re.search(r'<section class="es-section" aria-labelledby="h-es-streamers">.*?</section>', with_list, re.S)
    block = block.group(0) if block else ""
    cards = re.findall(r'<a class="card es-st".*?</a>', block, re.S)
    ids = [re.search(r'data-streamer="([^"]+)"', c).group(1) for c in cards]
    out["\"正在进行\"下面有一块\"主播\"：名单里开直播的选手一位一格（写着名字、队名和平台），后面接另外加的主播；同一个人只有一格"] = (
        0 < with_list.find('id="h-es-live"') < with_list.find('id="h-es-streamers"') < with_list.find("</main>")
        and len(cards) == 12 and ids == [p["id"] for p in roster()] + ["douyu-901", "huya-902", "douyin-abc"]
        and "甲队1号 甲队 斗鱼" in text(cards[0]) and 'data-team="乙队"' in cards[3] and "大主播 斗鱼" in text(cards[9])
        and "赛事、队伍和每位选手的直播间由 Evo 统一维护。" in text(with_list))
    out["每一格带着两个去处：在 Evo 里看（主播的看直播页）和他在平台上的直播间；名单先藏着、写\"正在看谁在播…\"，有\"刷新\"和收起没开播的那一栏，由脚本问 /esports/live"] = (
        'data-watch="/esports/streamers?p=douyu-901"' in cards[9] and 'data-room="https://www.douyu.com/901"' in cards[9]
        and 'data-room="https://live.douyin.com/abc"' in cards[11] and 'id="es-st-count"' in block and "正在看谁在播…" in text(block)
        and '<ul class="es-st-grid" id="es-st-grid" hidden>' in block and '<details class="es-st-more" id="es-st-more" hidden>' in block
        and 'id="es-st-none" hidden' in block
        and 'id="es-st-refresh"' in block and with_list.count('fetch("/esports/live"') == 2) if len(cards) == 12 else False
    asked.clear()

    def streamer_query(players):
        asked.append([p["id"] for p in players])
        states = {"douyu-901": "live", "huya-902": "error", "bilibili-1": "live", "douyu-1": "live"}
        return {p["id"]: live_status.result(states.get(p["id"], "offline"), title="今晚上分" if p["id"] == "douyu-901" else "",
                                            avatar="http://example.com/a.jpg" if p["id"] == "douyu-901" else "javascript:x") for p in players}

    live_status.query = streamer_query
    try:
        got = client.get("/esports/live").get_json()
        client.get("/esports/live")
    finally:
        live_status.query = real_query
    states = {key: value["state"] for key, value in got["streamers"].items()}
    out["/esports/live 回\"主播\"一块里每一位在不在播（标题、头像只要 https 的）；和赛事的选手一起只查一次，同一个房间不重复查；60 秒内不重查"] = (
        len(states) == 12 and {key: states[key] for key in ("douyu-901", "huya-902", "bilibili-1", "douyin-abc", "douyu-1", "huya-3")}
        == {"douyu-901": "live", "huya-902": "error", "bilibili-1": "live", "douyin-abc": "offline", "douyu-1": "live", "huya-3": "offline"}
        and got["streamers"]["douyu-901"]["title"] == "今晚上分" and got["streamers"]["douyu-901"]["avatar"] == "https://example.com/a.jpg"
        and got["streamers"]["huya-902"]["avatar"] == "" and len(asked) == 1 and len(asked[0]) == len(set(asked[0])) == 12
        and got["events"]["live1"] == {"live": 2, "total": 6, "unknown": 0} and isinstance(got["checked"], float))
    serve(folder, [event("soon", NOW + timedelta(hours=3))])  # 换回没有主播的赛程，下面接着看"没连上"
    client.get("/esports")

    esports_data.URL = str(Path(folder) / "没有这个文件.json")
    stale = text(client.get("/esports").get_data(as_text=True))  # 60 秒内：还用刚才存下的，不算没连上
    cache = json.loads(esports_data.CACHE_PATH.read_text(encoding="utf-8"))
    cache["fetched_at"] -= 3600
    esports_data.CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    offline = text(client.get("/esports").get_data(as_text=True))
    out["读到过、这次没连上：显示上次的赛程，并说明可能不是最新的"] = (
        "这次没连上" not in stale and "这次没连上" in offline and "可能不是最新的" in offline and "示例联赛" in offline)
    esports_data.CACHE_PATH.unlink()
    nothing = text(client.get("/esports").get_data(as_text=True))
    out["一次都没读到过：写\"赛事信息没有读到\"和\"再试一次\"，不显示三块"] = (
        "赛事信息没有读到" in nothing and "再试一次" in nothing and "正在进行" not in nothing
        and [client.get("/esports/live").get_json().get(key) for key in ("events", "streamers")] == [{}, {}])
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
