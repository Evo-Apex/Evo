"""真实 WebView2 DOM 点击验证：外部链接走 open_external、派圈助手弹窗、支持我三步。
会短暂打开一个 Evo 的 WebView2 窗口，不需要人工点击。
运行：python tests/test_settings_webview2.py

注意：这验证的是 DOM 点击和脚本事件，不是物理鼠标命中、透明遮罩或窗口层级。
"""
import json
import shutil
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
    tmp_parent = ROOT / ".tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="evo-webview2-", dir=tmp_parent))
    db = tmp / "settings.db"
    storage.connect(db).close()
    panel_app.app.config.update(DB=db, TESTING=True)

    server = None
    window = None
    timer = None
    real_shell = app_window._shell_execute
    real_cmd = app_window._cmd_start
    opened = []
    fail_next = [False]
    result = {}

    def fake_cmd(target):
        if fail_next[0]:
            raise OSError("cmd 起不来（测试）")
        opened.append(target)

    def fake_shell(target):
        opened.append(target)
        return 5 if fail_next[0] else 33

    def js(code):
        return window.evaluate_js(code)

    def wait_js(expr, expected=True, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if window.evaluate_js(expr) == expected:
                    return True
            except Exception:  # noqa: BLE001 窗口还在初始化/正在关闭
                pass
            time.sleep(0.05)
        return False

    def wait_py(check, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return False

    def run():
        try:
            wait_js("document.readyState", "complete")
            # 1) 查看源代码：系统浏览器被调用，页面仍是设置页
            js("document.querySelector('a[data-external-link]').click()")
            result["external_called"] = wait_py(lambda: bool(opened) and opened[0].startswith("https://"))
            result["page_stayed"] = js("location.pathname") == "/settings"

            # 1b) 模拟 ShellExecuteW 失败：页面必须出现可见提示
            fail_next[0] = True
            js("document.querySelector('a[data-external-link]').click()")
            result["external_failure_prompt"] = wait_js(
                "(function(){var e=document.getElementById('external-link-error');"
                "return !!e && !e.hidden && e.textContent.indexOf('没有成功打开系统浏览器')>=0;})()")
            fail_next[0] = False

            # 2) 顶栏派圈助手
            js("document.getElementById('partner-open').click()")
            result["partner_top_open"] = wait_js("document.getElementById('partner-dialog').open")
            js("document.querySelector('#partner-dialog [data-partner-close]').click()")
            result["partner_close"] = wait_js("document.getElementById('partner-dialog').open", False)

            # 3) 鸣谢卡片派圈助手
            js("document.querySelector('[data-partner-open]').click()")
            result["partner_card_open"] = wait_js("document.getElementById('partner-dialog').open")
            js("document.getElementById('partner-dialog').dispatchEvent("
               "new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            result["partner_esc"] = wait_js("document.getElementById('partner-dialog').open", False)
            js("document.querySelector('[data-partner-open]').click()")
            wait_js("document.getElementById('partner-dialog').open")
            js("document.getElementById('partner-dialog').click()")
            result["partner_backdrop"] = wait_js("document.getElementById('partner-dialog').open", False)

            # 4) 支持我三步仍在
            js("document.getElementById('support-open').click()")
            result["support_1"] = wait_js("document.getElementById('support-1').open")
            js("document.querySelector('[data-support-next=\"2\"]').click()")
            result["support_2"] = wait_js("document.getElementById('support-2').open")
            js("document.querySelector('[data-support-next=\"3\"]').click()")
            result["support_3"] = wait_js("document.getElementById('support-3').open")
            js("document.querySelector('#support-3 [data-support-close]').click()")
            result["support_closed"] = wait_js("document.getElementById('support-3').open", False)
        except Exception as exc:  # noqa: BLE001
            result["error"] = repr(exc)
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    try:
        app_window._cmd_start = fake_cmd
        app_window._shell_execute = fake_shell
        server = make_server("127.0.0.1", 0, panel_app.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/settings"
        window = webview.create_window("Evo R10 test", url, js_api=WindowApi(), width=1200, height=900)
        window.events.loaded += lambda: threading.Thread(target=run, daemon=True).start()

        # 兜底：WebView2 初始化失败时不让测试永远挂着；成功跑完会在 finally 里取消。
        def force_close():
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 窗口已经销毁
                pass

        timer = threading.Timer(25, force_close)
        timer.daemon = True
        timer.start()
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(tmp / "webview"))
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    finally:
        if timer is not None:
            timer.cancel()
        if server is not None:
            server.shutdown()
            server.server_close()
        app_window._cmd_start = real_cmd
        app_window._shell_execute = real_shell
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + 5  # WebView2 的子进程可能晚一点放开文件，重试几次
        while tmp.exists() and time.monotonic() < deadline:
            shutil.rmtree(tmp, ignore_errors=True)
            if tmp.exists():
                time.sleep(0.1)

    expected = ["external_called", "page_stayed", "external_failure_prompt", "partner_top_open", "partner_close",
                "partner_card_open", "partner_esc", "partner_backdrop", "support_1", "support_2", "support_3",
                "support_closed"]
    result["browser_restored"] = app_window._shell_execute is real_shell
    result["cmd_restored"] = app_window._cmd_start is real_cmd
    result["temp_cleaned"] = not tmp.exists()
    result["timer_cancelled"] = timer is None or timer.finished.is_set()
    ok = all(result.get(k) for k in expected) and result["browser_restored"] and result["cmd_restored"] \
        and result["temp_cleaned"] and result["timer_cancelled"] and "error" not in result
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"临时目录已清理：{not tmp.exists()}")
    print("通过" if ok else "失败")
    return 0 if ok and not tmp.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
