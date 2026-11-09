"""总览页"关注我"：开发者的 B 站、小黑盒主页和 Evo 官网（panel_app.FOLLOW_LINKS、overview.html、panel/static/brands/）。
不用截图，不开浏览器（用 Flask 自带的测试客户端）。

- 总览页有"关注我"和三个按钮，地址是正式地址（不带分享追踪参数），交给系统浏览器打开（target="_blank"、rel="noopener noreferrer"）
- 有对局时在左边一列"最近对局"下面（2026-10-28 用户要求）；没有对局时也显示
- 三个图标是写对了的 SVG、单色（颜色跟随文字）、不引用别的文件，面板能取到
- 设置页"关于 Evo"最下面的"支持我"（2026-10-28）：连着三个弹窗（文字是用户写的原文），最后一个是开发者的微信收款码
- 右上角"Evo 官方合作小程序：派圈助手"（2026-10-28，每一页都有）：前面是派圈助手的图标，点了弹出小程序码（弹窗、码、扫码说明、关闭）；
  每一页都有；没有小程序码图片时不显示；有图片时两张都是真的 PNG、面板能取到，文件夹里有许可说明
- 设置页"关于 Evo"下面的鸣谢卡片（2026-11-07）：开发者（链到 GitHub）→ 官方合作（派圈助手，弹同一个小程序码）→ 特别鸣谢
- 可选：装了 Node 时，再用 tests/js_settings_actions.mjs 跑一遍假 DOM 脚本测试；没装就明确跳过。
  Evo 运行不依赖 Node，真实 WebView2 行为由 tests/test_settings_webview2.py 单独验证。

运行：python tests/test_follow.py
"""

import json
import re
import shutil
import subprocess
import tempfile
from unittest import mock
import xml.etree.ElementTree as ET
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_window
import panel_app
import storage
from paths import PANEL_DIR

FOLDER = Path(tempfile.mkdtemp(prefix="evo-follow-", dir=storage.DB_PATH.parent.parent))  # 在测试的临时数据文件夹里
PNG_HEADER = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])  # PNG 文件开头的 8 个字节
EXPECTED = {"哔哩哔哩": "https://space.bilibili.com/26649278",
            "小黑盒": "https://www.xiaoheihe.cn/app/user/profile/765d6910c805",
            "Evo 官网": "https://evotracker.cn/"}


def follow_links(page):
    """总览页"关注我"那一块里的按钮：[(名字, 地址, 整个 <a> 标签)]。"""
    found = re.search(r'<section class="card follow-card".*?</section>', page, re.S)
    if not found:
        return []
    return [(" ".join(re.sub(r"<[^>]+>", " ", body).split()).removesuffix(" ↗"), href, tag)
            for tag, href, body in re.findall(r'(<a [^>]*href="([^"]+)"[^>]*>)(.*?)</a>', found.group(0), re.S)]


def main():
    print("总览页\"关注我\"：B 站、小黑盒、Evo 官网；右上角派圈助手；设置页支持我、鸣谢卡片")
    path = FOLDER / "follow.db"
    conn = storage.connect(path)
    storage.insert_match(conn, {"ended_at": "2026-10-22 20:00:00", "placement": 3, "legend": "恶灵"})
    conn.close()
    empty = FOLDER / "empty.db"
    storage.connect(empty).close()

    panel_app.app.config.update(DB=path, TESTING=True)
    client = panel_app.app.test_client()
    page = client.get("/").get_data(as_text=True)
    links = follow_links(page)
    panel_app.app.config["DB"] = empty
    empty_links = follow_links(panel_app.app.test_client().get("/").get_data(as_text=True))
    panel_app.app.config["DB"] = path

    icons = {}
    for f in panel_app.FOLLOW_LINKS:
        text = (PANEL_DIR / "static" / f["icon"]).read_text(encoding="utf-8")
        try:
            root = ET.fromstring(text)
            icons[f["name"]] = {"svg": root.tag.endswith("svg"), "color": root.get("fill") == "currentColor",
                                "paths": len(root.findall("{http://www.w3.org/2000/svg}path")),
                                "external": bool(re.search(r'href=|url\(|<image', text)),
                                "served": client.get("/static/" + f["icon"]).status_code}
        except ET.ParseError:
            icons[f["name"]] = {}

    checks = {
        "总览页有\"关注我\"，三个按钮：哔哩哔哩、小黑盒、Evo 官网": [name for name, _, _ in links] == list(EXPECTED),
        "地址是正式地址（不带分享追踪参数）": {name: href for name, href, _ in links} == EXPECTED,
        "交给系统浏览器打开（target=_blank、rel=noopener noreferrer）":
            bool(links) and all('target="_blank"' in tag and 'rel="noopener noreferrer"' in tag for _, _, tag in links),
        "没有对局时也显示": [name for name, _, _ in empty_links] == list(EXPECTED),
        "有对局时在左边一列\"最近对局\"下面（不在最下面占满一行）":
            -1 < page.find('class="card list-card"') < page.find('class="card follow-card"') < page.find('class="ov-side"'),
        "三个图标都是写对了的 SVG": all(i.get("svg") and i.get("paths") for i in icons.values()) and len(icons) == 3,
        "图标单色、颜色跟随文字（fill=currentColor）": all(i.get("color") for i in icons.values()),
        "图标不引用别的文件（不联网）": all(i.get("external") is False for i in icons.values()),
        "面板能取到图标": all(i.get("served") == 200 for i in icons.values()),
    }
    checks.update(partner_checks(client))
    checks.update(support_checks(client))
    checks.update(credits_checks(client))
    checks.update(external_link_checks(client))
    checks.update(window_api_checks())
    js_checks, js_skip = script_behaviour_checks(client)
    checks.update(js_checks)
    if js_skip:
        print(f"  ⏭ {js_skip}")
    return report(checks)


def credits_checks(client):
    """设置页"关于 Evo"下面的鸣谢卡片（2026-11-07）：开发者 → 官方合作 → 特别鸣谢。"""
    def card(**patch):
        with mock.patch.multiple(panel_app, **patch):
            page = client.get("/settings").get_data(as_text=True)
        found = re.search(r'<section class="card credits-card".*?</section>', page, re.S)
        return page, found.group(0) if found else ""

    page, credits = card(partner_qr=lambda: panel_app.PARTNER_QR)
    _, no_partner = card(partner_qr=lambda: None)
    _, no_thanks = card(partner_qr=lambda: panel_app.PARTNER_QR, THANKS=())
    devs = re.findall(r'<a class="source-link" href="([^"]+)" target="_blank" rel="noopener noreferrer"[^>]*>([^<]+) ↗</a>', credits)
    thanks = re.search(r'<ul class="thanks-list">(.*?)</ul>', credits, re.S)
    thanks = re.findall(r"<li>([^<]+)</li>", thanks.group(1)) if thanks else []
    return {
        "设置页右边一列：\"关于 Evo\"下面是鸣谢卡片": -1 < page.find('class="settings-side"') < page.find("about-card")
            < page.find("credits-card"),
        "顺序：开发者 → 官方合作 → 特别鸣谢（放最下面）":
            -1 < credits.find(">开发者<") < credits.find(">官方合作<") < credits.find(">特别鸣谢<"),
        "开发者：显示的名字是各自取的，点了用浏览器打开 GitHub 主页": [(n, u) for u, n in devs]
            == [(d["name"], d["url"]) for d in panel_app.DEVELOPERS] and all("github.com/" in u for u, _ in devs),
        "官方合作：派圈助手按钮，点了弹出右上角那个小程序码": "data-partner-open" in credits and ">派圈助手<" in credits,
        "没有小程序码图片时不显示\"官方合作\"": ">官方合作<" not in no_partner and ">开发者<" in no_partner,
        "特别鸣谢：名单里的名字都在、按顺序，一样大（不带字号档位）": thanks == list(panel_app.THANKS) and bool(thanks),
        "名单空了不显示\"特别鸣谢\"": ">特别鸣谢<" not in no_thanks and ">开发者<" in no_thanks,
    }


def partner_checks(client):
    """右上角"Evo 官方合作小程序：派圈助手"。小程序码图片在不在都要能测：用 mock 换掉"有没有这张图"。"""
    with mock.patch.object(panel_app, "partner_qr", return_value=panel_app.PARTNER_QR):
        overview = client.get("/").get_data(as_text=True)
        others = [client.get(url).get_data(as_text=True) for url in ("/matches", "/stats", "/stats?view=all", "/settings")]
    with mock.patch.object(panel_app, "partner_qr", return_value=None):
        without = client.get("/").get_data(as_text=True)
    header = overview.split("</header>")[0]
    dialog = re.search(r'<dialog class="dialog partner-dialog".*?</dialog>', overview, re.S)
    dialog = dialog.group(0) if dialog else ""
    out = {
        "总览页右上角有按钮\"Evo 官方合作小程序：派圈助手\"（在顶部那一行里）":
            'id="partner-open"' in header and "Evo 官方合作小程序：</span><span class=\"partner-name\">派圈助手" in header,
        "点了弹出小程序码：弹窗里有码（读屏写着是什么）、扫码说明、关闭按钮":
            f'src="/static/{panel_app.PARTNER_QR}"' in dialog and 'alt="派圈助手的小程序码"' in dialog and "扫一扫" in dialog
            and "data-partner-close" in dialog and 'id="partner-open"' in overview,
        "每一页都有（战绩、数据、全体玩家、设置页也有按钮和弹窗）": all(
            'id="partner-open"' in page.split("</header>")[0] and 'id="partner-dialog"' in page for page in others),
        "没有小程序码图片时不显示按钮和弹窗": "partner-open" not in without and "partner-dialog" not in without,
    }
    image = PANEL_DIR / "static" / panel_app.PARTNER_QR
    if image.is_file():
        reply = client.get(f"/static/{panel_app.PARTNER_QR}")
        icon = PANEL_DIR / "static" / panel_app.PARTNER_ICON
        out["按钮前面是派圈助手的图标（PNG、面板能取到，读屏软件跳过）"] = (
            icon.read_bytes()[:8] == PNG_HEADER and client.get(f"/static/{panel_app.PARTNER_ICON}").status_code == 200
            and f'<img class="partner-icon" src="/static/{panel_app.PARTNER_ICON}" alt=""' in header)
        out["两张图都写明了不适用 AGPL（同一个文件夹里的 LICENSE.txt）"] = (image.parent / "LICENSE.txt").is_file()
        out["小程序码图片是 PNG、面板能取到"] = (image.read_bytes()[:8] == PNG_HEADER and reply.status_code == 200
                                       and panel_app.partner_qr() == panel_app.PARTNER_QR)
    else:
        print(f"  （还没有小程序码图片 {image}：真的图片那一项跳过）")
    return out


def external_link_checks(client):
    """设置页/总览页所有 http(s) 外链都标了 data-external-link，普通浏览器不拦截。"""
    settings = client.get("/settings").get_data(as_text=True)
    overview = client.get("/").get_data(as_text=True)
    tags = re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", settings + overview)
    external = [tag for tag in tags if re.search(r'href="https?://', tag)]
    return {
        "设置页外部链接都标了 data-external-link": bool(external) and all("data-external-link" in tag for tag in external),
        "设置页覆盖查看源代码/意见反馈/开发者/Mirror酱": all(
            marker in settings for marker in ("查看源代码 ↗", "意见反馈 ↗", "黄指导", "M0MoNa", "了解 Mirror酱")),
        "开发者改名 M0MoNa，GitHub 地址不变": "M0MoNa" in settings
            and "https://github.com/wzxlll33" in settings and ">wzx" not in settings,
        "base.html 外部链接失败会显示提示，不静默吞掉": "没有成功打开系统浏览器" in settings
            and "showOpenError" in settings and "catch(showOpenError)" in settings,
        "总览页关注我也标了 data-external-link":
            all("data-external-link" in tag for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", overview)
                if "https://" in tag),
        "base.html 有外部链接事件委托，且不是拦截所有链接":
            'closest("a[data-external-link]")' in settings and "open_external" in settings,
        "派圈助手改成事件委托（不再逐个 addEventListener）":
            "closest(\"#partner-open, [data-partner-open]\")" in settings
            and 'querySelectorAll("#partner-open, [data-partner-open]")' not in settings,
    }


def window_api_checks():
    """WindowApi.open_external：只放行 http/https，走 Windows ShellExecuteW，异常返回失败而不是抛给页面。"""
    api = app_window.WindowApi()
    with mock.patch.object(app_window, "_shell_execute", return_value=33) as shell, \
            mock.patch.object(app_window, "_external_log") as logged:
        good = api.open_external("https://github.com/Evo-Apex/Evo")
    with mock.patch.object(app_window, "_shell_execute", return_value=33) as shell_http:
        good_http = api.open_external("http://example.com/a?b=1")
    with mock.patch.object(app_window, "_shell_execute", return_value=33) as shell_bad:
        bad = [api.open_external(url) for url in
               ("javascript:alert(1)", "file:///C:/Windows/win.ini", "data:text/html,x",
                "C:\\Windows\\win.ini", "ftp://x/")]
    with mock.patch.object(app_window, "_shell_execute", return_value=5):
        denied = api.open_external("https://example.com/")
    with mock.patch.object(app_window, "_shell_execute", side_effect=RuntimeError("没有浏览器")):
        broken = api.open_external("https://example.com/")
    with mock.patch.object(app_window._shell32, "ShellExecuteW", return_value=42) as native, \
            mock.patch.object(app_window._user32, "AllowSetForegroundWindow", return_value=True) as allow:
        code = app_window._shell_execute("https://example.com/native")
    log_text = " ".join(str(c) for c in logged.call_args_list)
    return {
        "open_external：https 走 ShellExecuteW 一次": good["ok"] and good["code"] == 33
            and shell.call_count == 1,
        "open_external：http 也放行": good_http["ok"] and shell_http.call_count == 1,
        "open_external：javascript/file/data/本地路径/ftp 全拒绝且不调系统": all(not r["ok"] for r in bad)
            and all(r["error"] for r in bad) and shell_bad.call_count == 0,
        "open_external：ShellExecuteW <=32 返回失败和错误码": denied["ok"] is False and denied["code"] == 5
            and "错误码 5" in denied["error"],
        "open_external：异常返回失败、不抛出": broken["ok"] is False and "没有浏览器" in broken["error"],
        "open_external：日志写 URL、调用和返回码": "https://github.com/Evo-Apex/Evo" in log_text
            and "ShellExecuteW" in log_text and "33" in log_text,
        "ShellExecuteW：先 AllowSetForegroundWindow，再传 URL": code == 42 and allow.called
            and native.call_args[0][2] == "https://example.com/native",
    }


def script_behaviour_checks(client):
    """可选的前端脚本测试（Node + 假 DOM）；没装 Node 时返回空检查 + 跳过原因。

    这不是 Evo 的运行依赖：普通 Python 测试、tests/run_all.py 都不要求 Node。
    真实 WebView2 行为由 tests/test_settings_webview2.py 单独验证。
    """
    node = shutil.which("node")
    if not node:
        return {}, "可选 Node 假 DOM 测试未运行（找不到 node；Evo 运行不依赖 Node）"
    page = client.get("/settings").get_data(as_text=True)
    html = FOLDER / "settings-rendered.html"
    html.write_text(page, encoding="utf-8")
    script = Path(__file__).with_name("js_settings_actions.mjs")
    try:
        done = subprocess.run([node, str(script), str(html)], capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
        result = json.loads(done.stdout) if done.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result = {}
    return {
        "可执行 JS：pywebview 里点外链会调 open_external 且页面不跳": result.get("external_calls_api")
            and result.get("external_keeps_page"),
        "可执行 JS：打开成功不弹错误提示": result.get("external_success_no_prompt"),
        "可执行 JS：打开失败显示可见错误提示": result.get("external_failure_prompt"),
        "可执行 JS：普通浏览器不阻止原生链接": result.get("external_browser_keeps_default"),
        "可执行 JS：顶栏派圈助手能打开弹窗": result.get("partner_top_opens"),
        "可执行 JS：鸣谢卡片派圈助手能打开同一个弹窗": result.get("partner_card_opens"),
        "可执行 JS：关闭按钮/Esc/点背景都能关": result.get("partner_close_button")
            and result.get("partner_esc_closes") and result.get("partner_backdrop_closes"),
    }, None


SUPPORT_TEXTS = (  # 用户写的原文（"Evo" 后面照面板的习惯加了空格）
    "谢谢你对我的支持！Evo 承诺给大家一个免费的使用体验，除非你真的喜欢这个产品，并且有余力，否则我强烈不建议你破费！",
    "是的，我就是想真金白银支持你",
    "看样子你真的很喜欢 Evo，我再一次感谢你的支持！不过，任何消费之前还请三思！你还有机会退出去",
    "我真的想好了！发码！",
)
JPEG_HEADER = bytes([0xFF, 0xD8, 0xFF])


def support_checks(client):
    """设置页"关于 Evo"下面的"支持我"：连着三个弹窗，最后一个是收款码。"""
    settings = client.get("/settings").get_data(as_text=True)
    about = re.search(r'<aside class="card about-card".*?</aside>', settings, re.S)
    about = about.group(0) if about else ""
    dialogs = {n: re.search(rf'<dialog [^>]*id="support-{n}".*?</dialog>', settings, re.S) for n in (1, 2, 3)}
    dialogs = {n: d.group(0) if d else "" for n, d in dialogs.items()}
    image = PANEL_DIR / "static" / "support" / "wechat-pay.jpg"
    return {
        "设置页\"关于 Evo\"最下面（意见反馈下面）有\"支持我\"按钮":
            -1 < about.find("feedback-btn") < about.find('id="support-open"')
            and re.search(r'id="support-open".*?</svg>\s*支持我\s*</button>', about, re.S) is not None,
        "第一个弹窗：原文 + \"是的，我就是想真金白银支持你\"（下一步）+ 关闭":
            SUPPORT_TEXTS[0] in dialogs[1] and f'data-support-next="2">{SUPPORT_TEXTS[1]}<' in dialogs[1]
            and "data-support-close" in dialogs[1],
        "第二个弹窗：原文 + \"我真的想好了！发码！\"（下一步）+ 关闭":
            SUPPORT_TEXTS[2] in dialogs[2] and f'data-support-next="3">{SUPPORT_TEXTS[3]}<' in dialogs[2]
            and "data-support-close" in dialogs[2],
        "第三个弹窗：收款码（读屏写着是什么）+ 关闭":
            'src="/static/support/wechat-pay.jpg"' in dialogs[3] and 'alt="开发者的微信收款码"' in dialogs[3]
            and "data-support-close" in dialogs[3],
        "收款码是 JPG、面板能取到，写明了不适用 AGPL":
            image.read_bytes()[:3] == JPEG_HEADER and client.get("/static/support/wechat-pay.jpg").status_code == 200
            and (image.parent / "LICENSE.txt").is_file(),
        "只在设置页（总览页没有）": "support-open" not in client.get("/").get_data(as_text=True),
    }


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
