"""Evo 窗口左边的侧边栏（_side.html、base.html，2026-11-19 用户要求，代替原来顶部那一行）。不用截图，不开浏览器（Flask 自带的测试客户端）。

- 每一页都有侧边栏：EVO 标志、导航（总览、战绩、数据、赛季、赛事、设置，"设置"推到最下面）、「Evo 占用」、展开 / 收起按钮
- 导航每一项有图标和文字；这一页的那一项写着 aria-current，垫着底色（nav-pill）
- 从别的页面点过来：底色带着"从第几项过来"，脚本按实际位置量出滑多远；同一页里的操作不滑
- 主题按钮两个（展开时在标志右边、收起时在最下面），都能切换；展开 / 收起按钮控制侧边栏
- 收没收起：画页面之前就按 localStorage 的 evo-side 加上 side-collapsed（换页不闪）
- 收起时只有图标、不写文字（2026-11-19 用户要求）：名字留给读屏软件，每个只有图标的地方都有 data-tip（指上去在右边显示）
- 有两个账号时：切换账号的下拉框和收起时的人像按钮都在侧边栏里；只有一个账号时都没有
- 看直播页没有侧边栏
- 样式：内容区"变成一列"按内容区自己的宽度算（@container page），不按整个窗口算

运行：python tests/test_side.py
"""

import re
import tempfile
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import panel_app
import app_settings
import storage

ROOT = Path(__file__).resolve().parent.parent
FOLDER = Path(tempfile.mkdtemp(prefix="evo-side-", dir=storage.DB_PATH.parent.parent))
PAGES = {"/": "总览", "/matches": "战绩", "/stats": "数据", "/seasons": "赛季", "/esports": "电竞", "/settings": "设置"}


def side_of(html):
    found = re.findall(r'<header class="side" id="side">.*?</header>', html, re.S)
    return found[0] if found else ""


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main():
    print("侧边栏（导航、展开 / 收起、主题、切换账号）")
    out = {}
    db = FOLDER / "evo.db"
    conn = storage.connect(db)
    storage.insert_match(conn, {"player_name": "ImperialHzd", "ended_at": "2026-11-15 20:00:00", "mode": "ranked"})
    conn.close()
    panel_app.app.config["DB"] = db
    client = panel_app.app.test_client()

    pages = {url: client.get(url).get_data(as_text=True) for url in PAGES}
    sides = {url: side_of(html) for url, html in pages.items()}
    out["每一页都有侧边栏：标志、导航、Evo 占用、展开 / 收起按钮"] = all(
        'class="brand"' in s and '<nav class="nav"' in s and 'id="usage-top"' in s and 'id="side-toggle"' in s
        for s in sides.values())
    nav = re.findall(r'<nav class="nav".*?</nav>', sides["/"], re.S)[0]
    links = re.findall(r"<a .*?</a>", nav, re.S)
    out["导航：总览、战绩、数据、赛季、赛事、设置，每一项一个图标"] = (
        text(nav).split() == list(PAGES.values()) and all('class="nav-icon"' in a for a in links))
    out["\"设置\"推到最下面（nav-end），只有它"] = (
        [text(a) for a in links if "nav-end" in a] == ["设置"])
    current_ok = True
    for url, label in PAGES.items():
        here = re.findall(r'<a [^>]*aria-current="page"[^>]*>.*?</a>', sides[url], re.S)
        current_ok &= len(here) == 1 and text(here[0]) == label and 'class="active' in here[0] and "nav-pill" in here[0]
    out["这一页的那一项：aria-current、active、垫着底色（每一页只有一项）"] = current_ok

    came = client.get("/", headers={"Referer": "http://localhost/settings"}).get_data(as_text=True)
    same = client.get("/", headers={"Referer": "http://localhost/"}).get_data(as_text=True)
    out["从设置页点过来：底色从下面第 5 项滑过来（脚本量距离）；同一页里不滑"] = (
        re.search(r'class="nav-pill slide" data-shift="5"', came) is not None
        and "nav-pill slide" not in same and "offsetTop" in came)

    home = pages["/"]
    head = home.split("</head>")[0]
    out["画页面之前就按 evo-side 收起（在样式表之前）"] = (
        'localStorage.getItem("evo-side") === "collapsed"' in head and "side-collapsed" in head
        and head.find("evo-side") < head.find("style.css"))
    themes = re.findall(r"<button [^>]*data-theme-toggle[^>]*>", sides["/"])
    out["主题按钮两个（标志右边、最下面），脚本给每个都绑上"] = (
        len(themes) == 2 and "side-theme-top" in themes[0] and "side-theme-bottom" in themes[1]
        and sum('id="theme-toggle"' in t for t in themes) == 1
        and 'querySelectorAll("[data-theme-toggle]")' in home)
    out["展开 / 收起按钮控制侧边栏，状态记在 evo-side、写进 aria-expanded"] = (
        re.search(r'id="side-toggle" aria-controls="side"', sides["/"]) is not None
        and 'localStorage.setItem("evo-side", "collapsed")' in home and '"aria-expanded"' in home)
    tipped = re.findall(r'<(?:a|button) [^>]*>', sides["/"])
    out["收起时不写文字：导航文字只留给读屏软件；导航、主题、展开 / 收起、Evo 占用都有提示，有放提示的那一块"] = (
        all(f'data-tip="{label}"' in sides["/"] for label in PAGES.values())
        and all("data-tip=" in t for t in tipped if "data-theme-toggle" in t or 'id="usage-open"' in t)
        and 'id="side-tip"' in sides["/"] and "toggle.dataset.tip = label" in home
        and re.search(r"\.side-collapsed \.nav-label, \.side-collapsed \.pill-text \{\s*position: absolute; width: 1px",
                      (ROOT / "panel" / "static" / "style.css").read_text(encoding="utf-8")) is not None)
    out["只有一个账号：没有切换账号、没有人像按钮"] = (
        'id="account-select"' not in home and 'id="side-account"' not in home)

    conn = storage.connect(db)
    storage.insert_match(conn, {"player_name": "AltAccount", "ended_at": "2026-11-16 20:00:00", "mode": "ranked"})
    storage.answer_name(conn, "AltAccount")
    conn.close()
    two = side_of(client.get("/matches").get_data(as_text=True))
    out["两个账号：下拉框和人像按钮都在侧边栏里，人像按钮写着现在看的是哪个"] = (
        'id="account-select"' in two and 'id="side-account"' in two
        and re.search(r'id="side-account" aria-label="切换账号（现在看的是 AltAccount）" data-tip="账号：AltAccount"', two) is not None
        and two.find('id="account-select"') < two.find('<nav class="nav"'))

    css = (ROOT / "panel" / "static" / "style.css").read_text(encoding="utf-8")
    media = re.findall(r"@media \(max-width: (\d+)px\)", css)
    out["内容区按自己的宽度变成一列（@container page）；按窗口宽度的只剩外框留白和看直播页"] = (
        "container: page / inline-size" in css and css.count("@container page") >= 10 and sorted(media) == ["560", "900"])
    endpoint = "/settings/sidebar"
    token = panel_app.app.config["TOKEN"]
    out["自动收起默认关闭，提交必须带正确 token"] = (
        panel_app.sidebar_auto_seconds() == 0 and client.post(endpoint, data={"seconds": "5"}).status_code == 403)
    saved = client.post(endpoint, data={"token": token, "seconds": "5"})
    out["等待秒数保存在本地设置，并在其他页面和设置页恢复"] = (
        saved.status_code == 302 and app_settings.get("sidebar_auto_seconds") == 5
        and 'var autoSeconds = 5' in client.get("/matches").get_data(as_text=True)
        and 'value="5" aria-describedby="sidebar-auto-desc"' in client.get("/settings").get_data(as_text=True))
    out["拒绝非法秒数，不覆盖已保存的设置"] = all(
        client.post(endpoint, data={"token": token, "seconds": value}).status_code == 400
        and app_settings.get("sidebar_auto_seconds") == 5
        for value in ("-1", "601", "1.5", "abc", ""))
    client.post(endpoint, data={"token": token, "seconds": "0"})
    out["0 秒关闭自动收起；旧设置损坏时安全回到关闭"] = panel_app.sidebar_auto_seconds() == 0
    for invalid in (None, "5", True, -1, 601, {}):
        app_settings.set_value("sidebar_auto_seconds", invalid)
        out["0 秒关闭自动收起；旧设置损坏时安全回到关闭"] &= panel_app.sidebar_auto_seconds() == 0
    app_settings.set_value("sidebar_auto_seconds", 0)
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
