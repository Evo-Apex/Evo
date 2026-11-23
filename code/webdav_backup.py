"""WebDAV 战绩备份：手动上传与每日自动上传，SQLite 在线快照、设置白名单、DPAPI 保存连接凭证。

只向用户保存的服务器发送；不开自动备份时仅点击测试或备份才联网。不跟随重定向，避免凭证转发。
压缩包只含 data/evo.db、常规设置、清单和恢复说明，不含 WebDAV 凭证、CDK、截图或录像。
"""

import base64
import hashlib
import json
import secrets
import sqlite3
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

import certifi

import app_settings
import paths
import secret_store

CREDENTIAL_PATH = paths.DB_DIR / "webdav_credentials.bin"
STATE_KEY = "webdav_backup_state"
TIMEOUT = 45
RETRY_SECONDS = 3600
SETTING_KEYS = ("sidebar_auto_seconds", "window_theme", "sounds_on", "sound_pack", "sound_volume",
                "game_language", "game_aspect", "account_display_names", "keep_all_screenshots")
_lock = threading.RLock()
_job = {}


class BackupError(Exception):
    """可直接显示的错误；不包含服务器原始回复、请求地址或凭证。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_url(value):
    value = str(value).strip()
    if not value or len(value) > 2048 or any(ord(c) < 32 for c in value) or "\\" in value:
        raise BackupError("请填写有效的 WebDAV 备份目录地址。")
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError:
        raise BackupError("WebDAV 地址格式不正确。") from None
    if url.scheme not in ("https", "http") or not url.hostname or url.username is not None or url.password is not None:
        raise BackupError("地址须以 https:// 或 http:// 开头，账号和密码请填写在下面。")
    if url.query or url.fragment:
        raise BackupError("请填写目录地址，不要使用分享链接、查询参数或锚点。")
    segments = [urllib.parse.unquote(part) for part in url.path.split("/") if part]
    if any(part in (".", "..") or "/" in part or "\\" in part or any(ord(c) < 32 for c in part) for part in segments):
        raise BackupError("备份目录路径不正确。")
    try:
        hostname = url.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise BackupError("服务器域名格式不正确。") from None
    if any(c.isspace() for c in hostname):
        raise BackupError("服务器域名不能包含空格。")
    authority = "[" + hostname + "]" if ":" in hostname else hostname
    if port is not None:
        authority += ":" + str(port)
    path = "/" + "/".join(urllib.parse.quote(part, safe="") for part in segments)
    return urllib.parse.urlunsplit((url.scheme, authority, path.rstrip("/") + "/", "", ""))


def config():
    value = secret_store.load(CREDENTIAL_PATH)
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(key), str) for key in ("url", "username", "password")):
        return None
    try:
        value["url"] = normalize_url(value["url"])
    except BackupError:
        return None
    return value


def busy():
    with _lock:
        return _job.get("phase") == "running"


def _ensure_idle():
    import backup_manager
    if backup_manager.pending():
        raise BackupError("正在等待重启恢复，请从托盘退出并重新打开 Evo。")
    if busy():
        raise BackupError("正在执行 WebDAV 任务，请完成后再操作。")
    import data_move
    if data_move.busy():
        raise BackupError("正在更改数据位置，请完成后再操作。")


def save_config(url, username, password, automatic, *, remote_root=None, profile="default", provider="custom", on_change=False):
    with _lock:
        _ensure_idle()
        url = normalize_url(url)
        username = username.strip()
        if len(username) > 512 or ":" in username or any(ord(c) < 32 for c in username) or len(password) > 4096:
            raise BackupError("账号或密码格式不正确。")
        old = config()
        same = old and old["url"] == url and old["username"] == username
        if not password and same:
            password = old["password"]
        elif not password and old and old["password"]:
            raise BackupError("地址或账号已更改，请重新填写对应的密码。")
        if username and not password:
            raise BackupError("请填写密码或应用专用密码。")
        if password and not username:
            raise BackupError("请同时填写 WebDAV 账号。")
        extra = {}
        if remote_root is not None:
            import webdav_sync
            extra = webdav_sync.fields(url, remote_root, profile, provider, on_change)
        secret_store.save(CREDENTIAL_PATH, {"url": url, "username": username,
                                          "password": password, "automatic": bool(automatic), **extra})
        app_settings.set_value("webdav_sync_paused", False)
        _job.clear()


def clear_config():
    with _lock:
        _ensure_idle()
        # 删除失败时显示错误，不能把仍会自动上传的配置误报为已清除。
        CREDENTIAL_PATH.unlink(missing_ok=True)
        _job.clear()


def _identity(cfg):
    return hashlib.sha256((cfg.get("sync_url", cfg["url"]) + "\n" + cfg["username"]).encode("utf-8")).hexdigest()


def _stored_state():
    value = app_settings.get(STATE_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


def _stamp(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def state():
    with _lock:
        cfg = config()
        stored = _stored_state()
        last = stored.get("success") if cfg and stored.get("config_id") == _identity(cfg) else None
        return {"job": dict(_job), "last_success": last,
                "last_error": stored.get("last_error") if cfg and stored.get("config_id") == _identity(cfg) else None}


def view():
    cfg = config()
    return {"url": cfg["url"] if cfg else "", "username": cfg["username"] if cfg else "",
            "configured": bool(cfg), "password_set": bool(cfg and cfg["password"]),
            "automatic": bool(cfg and cfg.get("automatic") is True),
            "remote_root": cfg.get("remote_root", "evo-sync") if cfg else "evo-sync",
            "profile": cfg.get("profile", "default") if cfg else "default",
            "provider": "custom",
            "on_change": bool(cfg and cfg.get("on_change")),
            "paused": app_settings.get("webdav_sync_paused", False), **state()}


def _tls_context():
    # 保留系统信任和 Python 发行版的证书拒绝策略，补充随程序分发的公共根证书。
    # 新电脑的 Windows 根证书库可能尚未自动补全；不依赖浏览器访问来触发更新。
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        return context
    except OSError:
        raise BackupError("无法加载 HTTPS 证书库，请完整解压新版程序并保留 _internal 文件夹。") from None


def _certificate_error(error):
    code = getattr(error, "verify_code", None)
    messages = {
        9: "HTTPS 证书尚未生效，请同步 Windows 日期、时间和时区；时间正确时请联系服务器管理员。",
        10: "HTTPS 证书已过期，请同步 Windows 日期、时间和时区；时间正确时请联系服务器管理员续期证书。",
        18: "HTTPS 服务器使用自签名证书，请确认服务器证书配置或联系管理员。",
        19: "HTTPS 证书链包含不受信任的自签名证书，请检查服务器或代理的证书配置。",
        20: "HTTPS 证书链不受信任，可能缺少中间证书或网络代理使用了私有证书，请检查服务器和代理配置。",
        21: "HTTPS 证书链不完整，请联系服务器管理员补全中间证书，并检查网络代理配置。",
        23: "HTTPS 证书已被吊销，请联系服务器管理员更换证书。",
        28: "HTTPS 证书被信任策略拒绝，请联系服务器管理员更换证书。",
        62: "HTTPS 证书与服务器域名不匹配，请检查 WebDAV 地址和代理配置。",
        64: "HTTPS 证书与服务器 IP 地址不匹配，请使用证书对应的域名连接。",
    }
    message = messages.get(code, "HTTPS 证书校验失败，请检查 Windows 时间、服务器证书和网络代理配置。")
    # 仅输出校验代码，不回显异常文本中的地址、凭证或服务器回复。
    return message + (f"（校验码 {code}）" if isinstance(code, int) else "")


def _request(cfg, url, method, body=None, headers=None, *, response_headers=False, output=None):
    request_headers = {"User-Agent": "Evo-WebDAV-Backup", **(headers or {})}
    if cfg["username"]:
        auth = base64.b64encode((cfg["username"] + ":" + cfg["password"]).encode("utf-8")).decode("ascii")
        request_headers["Authorization"] = "Basic " + auth
    request = urllib.request.Request(url, method=method, data=body, headers=request_headers)
    handlers = [_NoRedirect()]
    if urllib.parse.urlsplit(url).scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=_tls_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if output is not None:
                total = 0
                with Path(output).open("wb") as dst:
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > 256 * 1024 * 1024:
                            raise BackupError("云端备份超过 256 MB，已取消下载。")
                        dst.write(block)
                content = b""
            else:
                content = response.read(65537) if method in ("PROPFIND", "GET") else b""
            if len(content) > 65536:
                raise BackupError("服务器回复过大，请检查目录地址。")
            if response_headers:
                return response.status, content, dict(response.headers)
            return response.status, content
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 404 or (code == 409 and method == "PROPFIND"):
            # 部分服务在深层目录尚不存在时返回 409。仅目录探测按缺失处理；
            # PUT / GET / MKCOL 的 409 仍是失败，不能误报上传成功或吞掉冲突。
            return (code, b"", {}) if response_headers else (code, b"")
        if method == "MKCOL" and code == 405:
            return 405, b""
        operation = {"GET": "读取云端备份", "PUT": "写入云端备份", "MKCOL": "创建云端目录",
                     "DELETE": "清理云端写入锁"}.get(method, "访问云端")
        messages = {401: "账号或密码不正确，部分服务需要应用专用密码。", 403: "服务器拒绝访问，请检查目录权限。",
                    409: f"{operation}时服务器返回 HTTP 409：父目录不存在或路径冲突，请核对服务器目录、远程根目录和同步配置名，并检查路径是否被同名文件占用。",
                    412: "云端已发生变化，已停止覆盖。请先下载查看云端版本。",
                    413: "备份超过服务器允许的上传大小。", 423: "远端目录被锁定，请稍后重试。",
                    507: "WebDAV 存储空间不足。"}
        if 300 <= code < 400:
            raise BackupError("服务器要求跳转，请填写跳转后的 WebDAV 目录地址。") from None
        raise BackupError(messages.get(code, f"WebDAV 请求失败（HTTP {code}）。")) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise BackupError(_certificate_error(reason)) from None
        raise BackupError("无法连接 WebDAV 服务器或连接超时，请检查地址和网络。") from None


def _collection(cfg, url):
    status, body = _request(cfg, url, "PROPFIND", b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
                            {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
    if status in (404, 409):
        return False
    try:
        doc = ET.fromstring(body)
    except ET.ParseError:
        raise BackupError("服务器未返回 WebDAV 目录信息，请检查是否填成了网页登录地址。") from None
    if status != 207 or doc.find(".//{DAV:}collection") is None:
        raise BackupError("该地址不是可用的 WebDAV 目录。")
    return True


def ensure_collection(cfg):
    if _collection(cfg, cfg["url"]):
        return
    url = urllib.parse.urlsplit(cfg["url"])
    current = url.scheme + "://" + url.netloc + "/"
    # MKCOL 逐级创建缺少的目录；绝不覆盖文件，也不删除已有目录。
    for part in url.path.strip("/").split("/"):
        if not part:
            continue
        current += part + "/"
        if not _collection(cfg, current):
            status, _ = _request(cfg, current, "MKCOL")
            if status not in (201, 405) or not _collection(cfg, current):
                raise BackupError("创建备份目录失败，请检查父目录和写入权限。")
    if not _collection(cfg, cfg["url"]):
        raise BackupError("找不到备份目录。")


def create_archive(folder):
    """在线快照包含 WAL 中已提交的数据；不复制正在使用的数据库文件。"""
    database = paths.DB_PATH
    if not database.is_file():
        raise BackupError("还没有战绩数据库，请先记录对局后再备份。")
    folder = Path(folder)
    snapshot = folder / "evo.db"
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise BackupError("数据库暂时繁忙，请稍后重新备份。")

    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise BackupError("数据库快照校验失败，已取消上传。")
    except sqlite3.Error:
        raise BackupError("无法生成战绩快照，请检查数据库和磁盘空间。") from None
    settings = app_settings.load()
    public_settings = {key: settings[key] for key in SETTING_KEYS if key in settings}
    with snapshot.open("rb") as saved_db:
        digest = hashlib.file_digest(saved_db, "sha256").hexdigest()
    manifest = {"format": "evo-webdav-backup", "version": 1, "created_at": datetime.now().astimezone().isoformat(),
                "database_sha256": digest,
                "contains": ["data/evo.db", "settings.json"], "settings_keys": list(public_settings)}
    archive = folder / "backup.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(snapshot, "data/evo.db")
        z.writestr("settings.json", json.dumps(public_settings, ensure_ascii=False, indent=2))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("恢复说明.txt", "Evo WebDAV 备份\n\n包含全部账号的数据库和常规设置，不含截图、录像、自定义提示音文件和登录凭证。\n"
                   "恢复前先完全退出 Evo，并把现有数据目录另存一份。\n"
                   "将原 data 文件夹整体另存后，在新的 data 文件夹放入备份的 evo.db，避免混入旧的 WAL/SHM 文件。不要在 Evo 运行时替换数据库。\n"
                   "settings.json 只含常规设置，可按需合并；WebDAV、匿名统计等连接需要重新设置。\n"
                   "数据库仍保留原有截图和录像的关联，但对应文件未备份，恢复后不能查看未保留的文件。\n"
                   "建议使用与备份时相同或更新的 Evo 版本。manifest.json 提供数据库 SHA-256 供核对。\n")
    return archive


def _perform(kind, cfg):
    if cfg.get("sync_url") or kind == "download":
        import webdav_sync
        return webdav_sync.perform(kind, cfg)
    ensure_collection(cfg)
    if kind == "test":
        return {"message": "连接成功，备份目录可访问；上传权限将在备份时检查。"}
    with tempfile.TemporaryDirectory(prefix="evo-webdav-") as temporary:
        archive = create_archive(temporary)
        filename = "Evo-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(6) + ".zip"
        size = archive.stat().st_size
        with archive.open("rb") as body:
            status, _ = _request(cfg, cfg["url"] + filename, "PUT", body,
                                 {"Content-Type": "application/zip", "Content-Length": str(size), "If-None-Match": "*"})
        if status not in (200, 201, 204):
            raise BackupError("备份上传未成功，请检查远端目录。")
        return {"message": "备份已上传。", "filename": filename, "bytes": size, "at": _stamp(time.time())}


def _run(kind, cfg):
    try:
        result = _perform(kind, cfg)
        with _lock:
            if kind == "backup":
                saved = _stored_state()
                saved.update(config_id=_identity(cfg), success=result, last_error=None)
                app_settings.set_value(STATE_KEY, saved)
            _job.update(phase="done", **result)
    except Exception as error:
        message = str(error) if isinstance(error, BackupError) else "备份任务未完成，请检查磁盘空间、目录权限和网络后重试。"
        with _lock:
            _job.update(phase="failed", message=message)
            if kind == "backup":
                try:
                    saved = _stored_state()
                    saved.update(config_id=_identity(cfg), last_error=message)
                    app_settings.set_value(STATE_KEY, saved)
                except OSError:
                    pass


def due(cfg, now):
    if not cfg or cfg.get("automatic") is not True:
        return False
    saved = _stored_state()
    if saved.get("config_id") != _identity(cfg):
        return True
    last = saved.get("success")
    if isinstance(last, dict) and str(last.get("at", ""))[:10] == _stamp(now)[:10]:
        return False
    attempt = saved.get("attempt", 0)
    return not isinstance(attempt, (int, float)) or now < attempt or now - attempt >= RETRY_SECONDS


def start(kind="backup", automatic=False, now=None, expected=None):
    if kind not in ("backup", "test", "download", "force"):
        raise BackupError("不支持的 WebDAV 操作。")
    with _lock:
        _ensure_idle()
        cfg = config()
        if not cfg:
            raise BackupError("请先保存 WebDAV 连接配置。")
        if kind == "force":
            if not expected or expected != _job.get("conflict_revision"):
                raise BackupError("请重新上传检查冲突后再确认覆盖。")
            cfg["expected"] = expected
            kind = "backup"
        now = time.time() if now is None else now
        if automatic and not due(cfg, now):
            return False
        if kind == "backup":
            saved = _stored_state()
            if saved.get("config_id") != _identity(cfg):
                saved = {}
            saved.update(config_id=_identity(cfg), attempt=now)
            app_settings.set_value(STATE_KEY, saved)
        _job.clear()
        messages = {"test": "正在检查连接……", "download": "正在下载并校验云端备份……", "backup": "正在生成并上传备份……"}
        _job.update(phase="running", kind=kind, message=messages[kind])
        threading.Thread(target=_run, args=(kind, cfg), name="webdav-backup", daemon=True).start()
        return True


def start_scheduler(stop, game_running=lambda: False, interval=60):
    """托盘后台每分钟检查，每个自然日最多成功一次；失败一小时后重试，游戏运行时延后。"""
    def loop():
        while not stop.wait(interval):
            try:
                if game_running():
                    continue
                import backup_manager
                import webdav_sync
                backup_manager.tick(time.time())
                cfg = config()
                if not app_settings.get("webdav_sync_paused", False):
                    if webdav_sync.changed_due(cfg, time.time()):
                        start()
                    elif due(cfg, time.time()):
                        start(automatic=True)
            except (BackupError, OSError):
                continue

    thread = threading.Thread(target=loop, name="webdav-scheduler", daemon=True)
    thread.start()
    return thread
