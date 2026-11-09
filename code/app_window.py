"""Evo 的软件窗口：用 Windows 自带的 WebView2（Edge 用的同一个组件）显示面板，深色自定义标题栏。

托盘程序在单独的进程里运行它（见 evo_app.py 的 PanelServer）：关掉窗口，这个进程就退出，内存全部还给系统。
窗口去掉了 Windows 自带的标题栏和边框（frameless），所以：
- 标题栏是面板网页顶部的一条（panel/templates/base.html，只在 Evo 窗口里显示）
- 在标题栏按下鼠标时，告诉 Windows"用户按住了标题栏"，拖动交给 Windows 处理，手感和普通窗口一样；
  四边和四角的细条同理，交给 Windows 改大小
- 最小化 / 最大化 / 关闭是网页里的按钮，通过 window.pywebview.api 调用下面的 WindowApi

单独试：python code/app_window.py http://127.0.0.1:5000/   （先另外启动面板：python code/panel_app.py --no-browser）
"""

import base64
import ctypes
import hashlib
import os
import sys
import tempfile
import threading
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import webview

import paths
import storage
from paths import DATA_DIR, LOG_DIR

USER_AGENT = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"EvoApp/{storage.APP_VERSION}")  # 面板网页看到 EvoApp，就显示自定义标题栏
BACKGROUND = "#0D0F13"  # 网页加载出来之前的底色（和黑夜主题一致，避免白屏闪一下）
WM_NCLBUTTONDOWN = 0x00A1
HIT = {"caption": 2, "left": 10, "right": 11, "top": 12, "topleft": 13, "topright": 14,
       "bottom": 15, "bottomleft": 16, "bottomright": 17}

_user32 = ctypes.WinDLL("user32")
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_dwmapi = ctypes.WinDLL("dwmapi")
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
_shell32.ShellExecuteW.restype = wintypes.HINSTANCE
SHELL_EXECUTE_ERRORS = {
    0: "内存不足",
    2: "找不到文件",
    3: "找不到路径",
    5: "拒绝访问",
    8: "内存不足",
    26: "文件共享冲突",
    27: "文件关联不完整",
    28: "DDE 超时",
    29: "DDE 失败",
    30: "DDE 忙",
    31: "没有关联的程序",
    32: "找不到所需的 DLL",
}


def _external_log(message):
    """把外链调用写进 logs/evo.log；日志失败不能影响打开浏览器。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "evo.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [外链] {message}\n")
    except OSError:
        pass


def _shell_execute(url):
    """让 Windows 用默认浏览器打开 URL，返回 ShellExecuteW 的返回值。
    调用前允许浏览器到前台（用户刚点了 Evo，Evo 有前台权限）。不开 shell、不拼命令。"""
    _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    result = _shell32.ShellExecuteW(None, "open", url, None, None, 1)  # SW_SHOWNORMAL
    return int(result or 0)


def _explorer_open(url):
    """用独立的 explorer.exe 打开 URL，URL 只作为一个参数传入。
    ShellExecuteW 从 WebView2 的 JS 调用线程发出时，在部分机器上会返回 42 却没有窗口；
    explorer 是独立的系统进程，不经过 shell，也不会把 URL 拼成命令行。"""
    import subprocess

    windir = os.environ.get("WINDIR") or r"C:\Windows"
    explorer = Path(windir) / "explorer.exe"
    if not explorer.is_file():
        raise OSError(f"找不到系统文件：{explorer}")
    _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    subprocess.Popen(
        [str(explorer), url],
        shell=False,
        creationflags=0x08000000,  # CREATE_NO_WINDOW：不闪黑框
    )


class WindowApi:
    """网页里的标题栏按钮、拖动、改大小，通过 window.pywebview.api.xxx() 调用这里。
    （下划线开头的属性和方法不会暴露给网页。）"""

    def __init__(self):
        self._window = None

    def _form(self):
        return self._window.native  # WinForms 的窗口对象

    def _on_ui(self, func):
        """在窗口自己的线程里执行（WinForms 要求窗口操作都在这个线程里做）。"""
        from System import Action

        self._form().Invoke(Action(func))

    def _maximized(self):
        from System.Windows.Forms import FormWindowState

        return self._form().WindowState == FormWindowState.Maximized

    def _nc_button_down(self, where):
        """告诉 Windows：用户在标题栏 / 某条边上按下了鼠标。接下来的拖动由 Windows 接管。"""
        form = self._form()

        def go():
            _user32.ReleaseCapture()
            _user32.SendMessageW(form.Handle.ToInt64(), WM_NCLBUTTONDOWN, HIT[where], 0)

        self._on_ui(go)

    def begin_drag(self):
        if not self._maximized():  # 最大化时不能拖
            self._nc_button_down("caption")

    def begin_resize(self, edge):
        if edge in HIT and edge != "caption" and not self._maximized():
            self._nc_button_down(edge)

    def toggle_maximize(self):
        def go():
            from System.Drawing import Rectangle
            from System.Windows.Forms import FormWindowState, Screen

            form = self._form()
            if form.WindowState == FormWindowState.Maximized:
                form.WindowState = FormWindowState.Normal
            else:
                # 没有边框的窗口最大化时会盖住任务栏：限定在屏幕的工作区里（坐标相对于这块屏幕）
                screen = Screen.FromHandle(form.Handle)
                area, bounds = screen.WorkingArea, screen.Bounds
                form.MaximizedBounds = Rectangle(area.X - bounds.X, area.Y - bounds.Y, area.Width, area.Height)
                form.WindowState = FormWindowState.Maximized

        self._on_ui(go)
        return self._maximized()

    def is_maximized(self):
        return self._maximized()

    def minimize(self):
        self._window.minimize()

    def close(self):
        self._window.destroy()

    def pick_folder(self, start=""):
        """设置页"更改位置"的"浏览…"（data_move.py）：弹出 Windows 选文件夹的对话框，返回选的文件夹，没选返回 None。"""
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start or "")
        return chosen[0] if chosen else None

    def open_external(self, url):
        """把明确标记的外部链接交给 Windows 默认浏览器（base.html 的 data-external-link）。
        只收 http/https；其他协议（javascript:、file:、data:、本地路径）一律拒绝。
        返回 {"ok": bool, "url": str, "code": int|None, "error": str|None}，页面脚本不会因为异常崩掉。"""
        raw = url if isinstance(url, str) else ""
        try:
            if not isinstance(url, str):
                error = "地址不是字符串"
                _external_log(f"拒绝：{error}；收到 {type(url).__name__}")
                return {"ok": False, "url": "", "code": None, "error": error}
            url = url.strip()
            if any(ch in url for ch in ("\r", "\n", "\t", "\0")):
                error = "地址里有控制字符"
                _external_log(f"拒绝：{error}；收到 {raw!r}")
                return {"ok": False, "url": url, "code": None, "error": error}
            parts = urlsplit(url)
            if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                error = "只允许 http:// 或 https:// 地址"
                _external_log(f"拒绝：{error}；收到 {url!r}")
                return {"ok": False, "url": url, "code": None, "error": error}

            _external_log(f"收到 {url}；调用 explorer 打开（默认浏览器）")
            try:
                _explorer_open(url)
            except OSError:
                # explorer 起不来时才退回 ShellExecuteW；同样不开 shell。
                _external_log("explorer 打开失败，改用 ShellExecuteW")
                code = _shell_execute(url)
                if code > 32:
                    _external_log(f"成功：ShellExecuteW 返回 {code}；{url}")
                    return {"ok": True, "url": url, "code": code, "error": None}
                reason = SHELL_EXECUTE_ERRORS.get(code, "未知错误")
                error = f"打开系统浏览器失败（Windows 错误码 {code}：{reason}）"
                _external_log(f"失败：ShellExecuteW 返回 {code}（{reason}）；{url}")
                return {"ok": False, "url": url, "code": code, "error": error}
            _external_log(f"已交给 explorer；{url}")
            return {"ok": True, "url": url, "code": None, "error": None}
        except Exception as exc:  # noqa: BLE001 浏览器/系统异常不能让页面脚本崩掉
            error = f"打开系统浏览器失败：{exc}"
            _external_log(f"异常：{exc!r}；{url!r}")
            return {"ok": False, "url": raw, "code": None, "error": error}

    def save_image(self, filename, data_url):
        """分享战绩的"保存图片…"（panel/templates/_share.html）：弹出 Windows 的"另存为"，默认放在"图片"文件夹，
        把网页上画好的 PNG 存到选的位置。返回存好的路径；取消了返回 None。只接受 PNG。"""
        data = _png_bytes(data_url)
        pictures = Path.home() / "Pictures"
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE, directory=str(pictures) if pictures.is_dir() else "",
            save_filename=Path(filename).name, file_types=("PNG 图片 (*.png)",))
        if not chosen:
            return None
        path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
        if path.suffix.lower() != ".png":
            path = path.with_name(path.name + ".png")
        path.write_bytes(data)
        return str(path)

    def allow_foreground(self):
        """点"安装并重新启动""数据搬好后重新打开"时调用（base.html）：允许接下来新打开的 Evo 把窗口放到最前、拿到焦点。
        Windows 只让刚被点过的程序这样做，新 Evo 是后台打开的，没有这个许可窗口会被别的程序挡住。"""
        _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY：到用户下一次按键、点鼠标为止

    def _bring_to_front(self):
        def go():
            form = self._form()
            from System.Windows.Forms import FormWindowState

            if form.WindowState == FormWindowState.Minimized:
                form.WindowState = FormWindowState.Normal
            form.Show()
            form.TopMost = True  # 先置顶再取消，才能从别的程序后面跳到最前
            form.TopMost = False
            form.Activate()

        self._on_ui(go)


PNG_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_bytes(data_url):
    """网页传来的 data:image/png;base64,... → PNG 文件的内容。不是 PNG 就报错（这个口子只用来存分享图片）。"""
    if not isinstance(data_url, str) or not data_url.startswith(PNG_PREFIX):
        raise ValueError("不是 PNG 图片")
    data = base64.b64decode(data_url[len(PNG_PREFIX):], validate=True)
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("不是 PNG 图片")
    return data


def _style_window(window):
    """Windows 11：窗口四角改成圆角（没有边框的窗口默认是直角）。旧版 Windows 不支持就算了。"""
    hwnd = wintypes.HWND(window.native.Handle.ToInt64())
    round_corners = ctypes.c_int(2)  # DWMWCP_ROUND
    _dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(round_corners), ctypes.sizeof(round_corners))


def _watch_focus(api, focus):
    """托盘程序再点"打开面板"时会设置 focus：把已经打开的窗口调到最前面。"""
    while True:
        focus.wait()
        focus.clear()
        try:
            api._bring_to_front()
        except Exception:  # noqa: BLE001 窗口正在关闭等情况，忽略
            return


def _webview_storage():
    """WebView2 的缓存目录：先用数据文件夹里的 webview；这里不能写时退到临时目录，绝不因权限问题改用浏览器。"""
    folder = DATA_DIR / "webview"
    if paths.writable(folder):
        return folder
    temp = paths._tempdir()
    if temp is not None:
        # 每个数据目录一个稳定短摘要，避免不同数据目录共用同一份 WebView2 缓存。
        # 不用内置 hash()：它每个进程可能不同。
        digest = hashlib.sha256(os.path.normcase(str(DATA_DIR.resolve())).encode("utf-8")).hexdigest()[:12]
        fallback = temp / f"evo-webview-{digest}"
        if paths.writable(fallback):
            paths._warn(f"数据文件夹里的 WebView2 缓存目录不能写（{folder}），改用 {fallback}")
            return fallback
    raise RuntimeError(f"WebView2 缓存目录不能写：{folder}")


def run(url, focus=None):
    """打开 Evo 窗口，直到窗口关闭才返回。focus：托盘程序用来"把窗口调到最前"的信号（multiprocessing.Event）。"""
    # 再确认一次 pywebview 用的是 WebView2：它找不到 WebView2 时会悄悄改用 IE 内核，面板会乱成一堆文字（见 webview2.py）。
    # 不是的话报错，托盘程序改用浏览器打开
    from webview.platforms import winforms

    if winforms.renderer != "edgechromium":
        raise RuntimeError(f"没有可用的 WebView2，pywebview 要改用 {winforms.renderer}")
    storage_path = _webview_storage()
    webview.settings["ALLOW_DOWNLOADS"] = True  # 导出 CSV
    api = WindowApi()
    window = webview.create_window(
        "Evo", url, js_api=api, width=1280, height=860, min_size=(960, 640),
        frameless=True, easy_drag=False, background_color=BACKGROUND,
    )
    api._window = window

    def shown():
        _style_window(window)
        # 打开时调到最前：一键更新后新版本自己打开，不调的话窗口会被刚才在用的程序挡住，像是没打开
        api._bring_to_front()

    window.events.shown += shown
    if focus is not None:
        threading.Thread(target=_watch_focus, args=(api, focus), name="focus", daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage_path),
                  user_agent=USER_AGENT)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000/")
