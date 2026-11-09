"""真实 WebView2 点击验证：外部链接走 open_external、派圈助手弹窗、支持我三步。

会短暂打开一个 Evo 的 WebView2 窗口（约 3 秒），不需要人工点击。
运行：python tests/test_settings_webview2.py
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import webview
from werkzeug.serving import make_server

import app_window
import panel_app
import storage
from app_window import WindowApi


def main():
    tmp = Path(tempfile.mkdtemp(prefix="evo-webview2-", dir=ROOT / ".tmp"))
    db = tmp / "settings.db"
    storage.connect(db).close()
    panel_app.app.config.update(DB=db, TESTING=True)
    server = make_server("127.0.0.1", 0, panel_app.app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/settings"

    opened = []
    real_open = app_window.webbrowser.open

    def fake_open(target, new=0):
        opened.append(target)
        return True

    app_window.webbrowser.open = fake_open
    api = WindowApi()
    window = webview.create_window("Evo R08 test", url, js_api=api, width=1200, height=900)
    api._window = window
    result = {}

    def js(code):
        return window.evaluate_js(code)

    def run():
        try:
            time.sleep(0.8)
            # 1) 查看源代码：系统浏览器被调用，页面仍是设置页
            js("document.querySelector('a[data-external-link]').click()")
            time.sleep(0.4)
            result["external_called"] = len(opened) == 1 and opened[0].startswith("https://")
            result["page_stayed"] = js("location.pathname") == "/settings"

            # 2) 顶栏派圈助手
            js("document.getElementById('partner-open').click()")
            time.sleep(0.2)
            result["partner_top_open"] = js("document.getElementById('partner-dialog').open") is True
            js("document.querySelector('#partner-dialog [data-partner-close]').click()")
            time.sleep(0.2)
            result["partner_close"] = js("document.getElementById('partner-dialog').open") is False

            # 3) 鸣谢卡片派圈助手
            js("document.querySelector('[data-partner-open]').click()")
            time.sleep(0.2)
            result["partner_card_open"] = js("document.getElementById('partner-dialog').open") is True
            js("document.getElementById('partner-dialog').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            time.sleep(0.2)
            result["partner_esc"] = js("document.getElementById('partner-dialog').open") is False
            js("document.querySelector('[data-partner-open]').click()")
            time.sleep(0.2)
            js("document.getElementById('partner-dialog').click()")
            time.sleep(0.2)
            result["partner_backdrop"] = js("document.getElementById('partner-dialog').open") is False

            # 4) 支持我三步仍在
            js("document.getElementById('support-open').click()")
            result["support_1"] = js("document.getElementById('support-1').open") is True
            js("document.querySelector('[data-support-next=\"2\"]').click()")
            result["support_2"] = js("document.getElementById('support-2').open") is True
            js("document.querySelector('[data-support-next=\"3\"]').click()")
            result["support_3"] = js("document.getElementById('support-3').open") is True
            js("document.querySelector('#support-3 [data-support-close]').click()")
            result["support_closed"] = js("document.getElementById('support-3').open") is False
        except Exception as exc:  # noqa: BLE001
            result["error"] = repr(exc)
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    window.events.loaded += lambda: threading.Thread(target=run, daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(tmp / "webview"))
    server.shutdown()
    app_window.webbrowser.open = real_open

    expected = ["external_called", "page_stayed", "partner_top_open", "partner_close", "partner_card_open",
                "partner_esc", "partner_backdrop", "support_1", "support_2", "support_3", "support_closed"]
    ok = all(result.get(k) for k in expected) and "error" not in result
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
