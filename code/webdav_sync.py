"""WebDAV 云同步：不可变快照、远端写锁、版本校验及跨设备冲突提示。"""

import hashlib
import json
import re
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import app_settings
import backup_manager as local
import paths
import webdav_backup as dav

KEY = "webdav_sync_state"


def fields(url, root, profile, provider, on_change):
    root, profile = root.strip().strip("/"), profile.strip()
    parts = root.split("/") + [profile]
    if not root or any(not part or part in (".", "..") or len(part) > 100 or
                       any(c in part for c in '\\/%?#:') or any(ord(c) < 32 for c in part) for part in parts):
        raise dav.BackupError("请填写有效的远程目录和同步配置名，不能包含路径跳转或特殊符号。")
    return {"remote_root": root, "profile": profile, "provider": "custom",
            "sync_url": url + "/".join(urllib.parse.quote(p, safe="") for p in parts) + "/",
            "on_change": bool(on_change)}


def signature():
    parts = []
    for path in (paths.DB_PATH, Path(str(paths.DB_PATH) + "-wal")):
        try:
            stat = path.stat()
            parts.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            parts.append(None)
    settings = app_settings.load()
    parts.append({key: settings.get(key) for key in dav.SETTING_KEYS})
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def changed_due(cfg, now):
    if not cfg or not cfg.get("on_change") or not paths.DB_PATH.exists():
        return False
    state = app_settings.get(KEY, {})
    saved = dav._stored_state()
    if saved.get("config_id") != dav._identity(cfg):
        saved = {}
    attempt = saved.get("attempt", 0)
    if state.get("config_id") == dav._identity(cfg) and state.get("signature") == signature():
        return False
    return not saved.get("last_error") or now < attempt or now - attempt >= dav.RETRY_SECONDS


def strong_etag(value):
    return isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]{0,198}"', value) is not None


def head(cfg):
    status, body, headers = dav._request(cfg, cfg["sync_url"] + "latest.json", "GET",
                                        headers={"Cache-Control": "no-cache"}, response_headers=True)
    if status == 404:
        return None, None
    etag = next((v for k, v in headers.items() if k.lower() == "etag"), "")
    # Weak/unquoted/missing ETags cannot be used with If-Match. Hash the exact
    # manifest instead; writers serialize with MKCOL, including the first upload.
    revision = etag if strong_etag(etag) else "sha256:" + hashlib.sha256(body).hexdigest()
    try:
        data = json.loads(body)
        if data.get("format") != "evo-sync-head" or data.get("version") != 1:
            raise ValueError()
        if not re.fullmatch(r"snapshot-[a-f0-9]{64}\.zip", data["file"]):
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{64}", data["sha256"]):
            raise ValueError()
        if data["file"] != "snapshot-" + data["sha256"] + ".zip":
            raise ValueError()
        return data, revision
    except (ValueError, TypeError, KeyError, AttributeError):
        raise dav.BackupError("云端版本信息无效，已停止同步。") from None


@contextmanager
def writer_lock(cfg):
    # MKCOL must fail with 405 for an existing collection. Never take over a
    # lock on a timer: an offline/slow writer could otherwise resume and overwrite.
    url = cfg["sync_url"] + "evo-write-lock/"
    status, _ = dav._request(cfg, url, "MKCOL")
    if status != 201:
        raise dav.BackupError("云端正在上传或上次上传异常中断。请稍后重试；若一直提示，请先退出所有设备的 Evo，再在网盘同步目录中删除空文件夹 evo-write-lock。")
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            status, _ = dav._request(cfg, url, "DELETE")
            if status not in (200, 204, 404):
                raise dav.BackupError("未能释放云端写入锁。")
        except dav.BackupError:
            if not failed:
                raise dav.BackupError("数据已上传，但云端写入锁未释放。请先退出所有设备的 Evo，再在网盘同步目录中删除空文件夹 evo-write-lock。") from None


def perform(kind, cfg):
    if not cfg.get("sync_url"):
        raise dav.BackupError("请先保存远程根目录和同步配置名，再使用云端下载。")
    if kind == "test":
        remote, _ = head(cfg)
        if remote is not None:
            return {"message": "连接成功，已找到云端快照，可以下载。"}
        if not dav._collection(cfg, cfg["url"]):
            raise dav.BackupError("服务器目录不存在或无法访问，请核对 WebDAV 服务器地址。")
        return {"message": "连接成功，但当前同步配置尚无云端快照。请核对两台设备的目录与配置名，并在原设备上传。"}
    if kind == "download":
        remote, etag = head(cfg)
        if remote is None:
            raise dav.BackupError("当前目录未找到云端快照，请核对两台设备的服务器、账号、远程根目录和同步配置名，并确认原设备“上传到云端”已成功。")
        with tempfile.TemporaryDirectory(prefix="evo-cloud-") as folder:
            archive = Path(folder) / "cloud.zip"
            status, _ = dav._request(cfg, cfg["sync_url"] + remote["file"], "GET", output=archive)
            if status != 200 or hashlib.sha256(archive.read_bytes()).hexdigest() != remote["sha256"]:
                raise dav.BackupError("云端快照缺失或校验失败，未更改本地数据。")
            checked = local.validate(archive, folder)
            path = local.store_archive(archive, "cloud")
        versions = app_settings.get("downloaded_backups", {})
        versions[path.name] = {"config_id": dav._identity(cfg), "revision": etag}
        app_settings.set_value("downloaded_backups", versions)
        return {"message": f"已下载并校验：{checked['matches']} 局战绩、{checked['accounts']} 个账号。请在本地备份列表点击恢复。",
                "filename": path.name, "at": dav._stamp(time.time())}
    # 下载和测试只读远端；即使账号没有建目录权限，仍可读取已有快照。
    # 只有上传才准备目录并取得写锁。
    dav.ensure_collection({**cfg, "url": cfg["sync_url"]})
    with writer_lock(cfg):
        return upload(cfg)


def upload(cfg):
    remote, etag = head(cfg)
    state = app_settings.get(KEY, {})
    known = state.get("revision") if state.get("config_id") == dav._identity(cfg) else None
    if remote is not None and etag != (cfg.get("expected") or known):
        with dav._lock:
            dav._job["conflict_revision"] = etag
        raise dav.BackupError("云端存在本机尚未同步的版本。请先下载恢复，或确认用本机覆盖；两端战绩不会自动合并。")
    if cfg.get("expected") and etag != cfg["expected"]:
        raise dav.BackupError("云端版本再次变化，请重新检查冲突。")
    before = signature()
    with tempfile.TemporaryDirectory(prefix="evo-sync-") as folder:
        archive = dav.create_archive(folder)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        filename = "snapshot-" + digest + ".zip"
        size = archive.stat().st_size
        with archive.open("rb") as stream:
            status, _ = dav._request(cfg, cfg["sync_url"] + filename, "PUT", stream,
                                    {"If-None-Match": "*", "Content-Length": str(size), "Content-Type": "application/zip"})
        if status not in (200, 201, 204):
            raise dav.BackupError("云端快照未上传成功。")
        manifest = {"format": "evo-sync-head", "version": 1, "file": filename,
                    "sha256": digest, "at": dav._stamp(time.time())}
        body = json.dumps(manifest).encode()
        # Recheck after the potentially long ZIP upload, while holding the lock.
        # Keep If-Match as an extra safeguard where a strong ETag is available.
        if head(cfg)[1] != etag:
            raise dav.BackupError("云端版本再次变化，请重新检查冲突。")
        headers = {"Content-Type": "application/json"}
        if strong_etag(etag):
            headers["If-Match"] = etag
        elif remote is None:
            headers["If-None-Match"] = "*"
        status, _ = dav._request(cfg, cfg["sync_url"] + "latest.json", "PUT", body, headers)
        if status not in (200, 201, 204):
            raise dav.BackupError("云端版本指针未更新，请重新检查同步状态。")
        current, revision = head(cfg)
        if not current or current["sha256"] != digest:
            raise dav.BackupError("上传后云端版本发生变化，请先下载检查。")
        app_settings.set_value(KEY, {"config_id": dav._identity(cfg), "revision": revision, "signature": before})
        app_settings.set_value("webdav_sync_paused", False)
    return {"message": "已上传到云端。", "filename": filename, "bytes": size, "at": dav._stamp(time.time())}
