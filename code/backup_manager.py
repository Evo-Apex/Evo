"""本地备份、保留数量、ZIP 校验和重启恢复；保留账号映射，恢复前保存可回退快照。"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
import zlib
from contextlib import closing
from pathlib import Path

import app_settings
import paths
import storage
import webdav_backup as dav

ROOT = paths.DB_DIR / "backups"
PENDING = paths.DB_DIR / "restore-pending.json"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_DATABASE = 1024 * 1024 * 1024
_hooks = {}


def connect(**hooks):
    _hooks.update(hooks)


def pending():
    return PENDING.exists()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def preferences():
    value = app_settings.get("local_backup", {})
    return {"hours": value.get("hours", 24), "keep": value.get("keep", 10)}


def save_preferences(hours, keep):
    try:
        hours, keep = int(hours), int(keep)
    except (TypeError, ValueError):
        raise dav.BackupError("请选择有效的备份间隔和保留数量。") from None
    if hours not in (0, 1, 6, 12, 24, 72, 168) or keep not in (5, 10, 20, 50):
        raise dav.BackupError("请选择有效的备份间隔和保留数量。")
    app_settings.set_value("local_backup", {"hours": hours, "keep": keep})


def file_path(name):
    if not re.fullmatch(r"[a-z]+-[0-9]{8}-[0-9]{6}-[a-f0-9]{12}\.zip", str(name)):
        raise dav.BackupError("备份文件名无效。")
    path = ROOT / name
    if not path.is_file() or path.is_symlink():
        raise dav.BackupError("备份文件不存在。")
    return path


def history():
    result = []
    labels = app_settings.get("backup_labels", {})
    for path in ROOT.glob("*.zip"):
        try:
            file_path(path.name)
            stat = path.stat()
            result.append({"name": path.name, "label": labels.get(path.name, path.stem),
                           "at": dav._stamp(stat.st_mtime), "bytes": stat.st_size,
                           "protected": path.name.startswith("rollback-")})
        except (OSError, dav.BackupError):
            continue
    return sorted(result, key=lambda item: item["name"].split("-", 1)[1], reverse=True)


def retain():
    items = [item for item in history() if item["name"].startswith("auto-")]
    for item in items[preferences()["keep"]:]:
        file_path(item["name"]).unlink(missing_ok=True)


def store_archive(archive, prefix="manual"):
    ROOT.mkdir(parents=True, exist_ok=True)
    name = prefix + "-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(6) + ".zip"
    target = ROOT / name
    shutil.copyfile(archive, target.with_suffix(".tmp"))
    os.replace(target.with_suffix(".tmp"), target)
    return target


def create_local(prefix="manual"):
    with tempfile.TemporaryDirectory(prefix="evo-local-") as folder:
        path = store_archive(dav.create_archive(folder), prefix)
    if prefix == "auto":
        app_settings.set_value("local_backup_last", time.time())
        retain()
    return path


def validate(archive, folder):
    """只提取固定白名单成员；限制体积/类型/表结构，并核验数据库哈希。"""
    folder = Path(folder)
    try:
        if Path(archive).stat().st_size > MAX_ARCHIVE:
            raise ValueError()
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            expected = {"data/evo.db", "settings.json", "manifest.json", "恢复说明.txt"}
            if len(names) != len(set(names)) or set(names) - expected or not expected - {"恢复说明.txt"} <= set(names):
                raise ValueError()
            for member in z.infolist():
                limit = MAX_DATABASE if member.filename == "data/evo.db" else 65536
                if member.file_size > limit or member.flag_bits & 1:
                    raise ValueError()
            manifest = json.loads(z.read("manifest.json"))
            settings = json.loads(z.read("settings.json"))
            if manifest.get("format") != "evo-webdav-backup" or manifest.get("version") != 1 or not isinstance(settings, dict):
                raise ValueError()
            types = {"sidebar_auto_seconds": int, "window_theme": str, "sounds_on": bool,
                     "sound_pack": str, "sound_volume": (int, float), "game_language": str,
                     "game_aspect": str, "account_display_names": dict, "keep_all_screenshots": bool}
            for key, kind in types.items():
                if key in settings and not isinstance(settings[key], kind):
                    raise ValueError()
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in settings.get("account_display_names", {}).items()):
                raise ValueError()
            database = folder / "evo.db"
            with z.open("data/evo.db") as src, database.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            with database.open("rb") as saved:
                digest = hashlib.file_digest(saved, "sha256").hexdigest()
            if digest != manifest.get("database_sha256"):
                raise ValueError()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 30
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > storage.SCHEMA_VERSION:
                raise dav.BackupError("该备份来自更新版本的 Evo，请先升级程序再恢复。")
            if version < 1 or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError()
            objects = conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if any(kind in ("trigger", "view") for kind, _ in objects):
                raise ValueError()
            tables = {name for kind, name in objects if kind == "table"}
            if not {"matches", "player_names"} <= tables:
                raise ValueError()
            matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            accounts = conn.execute("SELECT COUNT(DISTINCT account_id) FROM player_names").fetchone()[0]
        # 在暂存文件上升级并核对程序所需列；不接受伪装成当前版本的残缺数据库。
        with closing(storage.connect(database)) as actual, closing(storage.connect(":memory:")) as expected:
            for (table,) in expected.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                columns = {row[1] for row in expected.execute('PRAGMA table_info("' + table + '")')}
                found = {row[1] for row in actual.execute('PRAGMA table_info("' + table + '")')}
                if not columns <= found:
                    raise ValueError()
        return {"database": database, "settings": {k: settings[k] for k in dav.SETTING_KEYS if k in settings},
                "matches": matches, "accounts": accounts, "created_at": manifest.get("created_at", ""),
                "digest": digest}
    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, zipfile.BadZipFile, zlib.error, RuntimeError, UnicodeError):
        raise dav.BackupError("备份校验失败：文件损坏、格式不支持或数据库不完整。") from None


def import_archive(stream):
    with tempfile.TemporaryDirectory(prefix="evo-import-") as folder:
        path = Path(folder) / "import.zip"
        total = 0
        with path.open("wb") as dst:
            while block := stream.read(1024 * 1024):
                total += len(block)
                if total > MAX_ARCHIVE:
                    raise dav.BackupError("备份文件不能超过 256 MB。")
                dst.write(block)
        validate(path, folder)
        return store_archive(path, "import")


def prepare_restore(name, fingerprint):
    with dav._lock:
        dav._ensure_idle()
        if _hooks.get("game_running", lambda: False)():
            raise dav.BackupError("请先退出 Apex，再恢复备份。")
        path = file_path(name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint:
            raise dav.BackupError("备份文件已发生变化，请重新预览。")
        with tempfile.TemporaryDirectory(prefix="evo-validate-") as folder:
            validate(path, folder)
        _hooks.get("hold", lambda value: None)(True)
        try:
            atomic_json(PENDING, {"name": name, "sha256": fingerprint})
        except Exception:
            _hooks.get("hold", lambda value: None)(False)
            raise
        restart = _hooks.get("restart")
        if restart:
            def restart_later():
                try:
                    restart()
                except Exception:
                    app_settings.set_value("restore_result", "恢复已准备好，请从托盘退出并重新打开 Evo。")
            timer = threading.Timer(2, restart_later)
            timer.daemon = True
            timer.start()


def apply_pending():
    """仅主程序获得单实例锁后、开启任何数据库连接前调用；失败保留原数据库。"""
    if not pending():
        return
    try:
        info = json.loads(PENDING.read_text(encoding="utf-8"))
        archive = file_path(info["name"])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != info["sha256"]:
            raise dav.BackupError("待恢复备份已改变，已取消恢复。")
        with tempfile.TemporaryDirectory(prefix="evo-restore-", dir=paths.DB_DIR) as folder:
            checked = validate(archive, folder)
            # 先生成离线恢复点，包含旧数据库 WAL 中已提交的内容。
            rollback = create_local("rollback") if paths.DB_PATH.exists() else None
            with closing(storage.connect(checked["database"])) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
            # 双文件提交用持久化撤销目录；断电后下次启动先完成撤销。
            undo = paths.DB_DIR / "restore-undo"
            undo.mkdir()
            old_settings = app_settings.load()
            atomic_json(undo / "settings.json", old_settings)
            atomic_json(undo / "files.json", {"present": [suffix for suffix in ("", "-wal", "-shm")
                        if Path(str(paths.DB_PATH) + suffix).exists()]})
            for suffix in ("", "-wal", "-shm"):
                old = Path(str(paths.DB_PATH) + suffix)
                if old.exists():
                    shutil.copy2(old, undo / ("evo.db" + suffix))
            (undo / "ready").touch()
            try:
                for suffix in ("-wal", "-shm"):
                    Path(str(paths.DB_PATH) + suffix).unlink(missing_ok=True)
                os.replace(checked["database"], paths.DB_PATH)
                old_settings.update(checked["settings"])
                # 恢复之后自动上传先暂停，防止旧快照在未经确认时覆盖云端。
                old_settings["webdav_sync_paused"] = True
                downloaded = old_settings.get("downloaded_backups", {}).get(info["name"])
                if downloaded:
                    old_settings["webdav_sync_state"] = downloaded
                old_settings["restore_result"] = "恢复成功，账号和战绩已保留。" + ("恢复前备份：" + rollback.name if rollback else "")
                atomic_json(app_settings.SETTINGS_PATH, old_settings)
                (undo / "committed").touch()
            except Exception:
                recover_interrupted()
                raise
            shutil.rmtree(undo)
        PENDING.unlink(missing_ok=True)
    except Exception as error:
        # 撤销也失败时禁止继续启动；保留撤销目录供下次启动重试。
        recover_interrupted()
        PENDING.unlink(missing_ok=True)
        message = str(error) if isinstance(error, dav.BackupError) else "恢复未完成，已保留原数据，请检查磁盘空间和权限。"
        app_settings.set_value("restore_result", message)


def recover_interrupted():
    undo = paths.DB_DIR / "restore-undo"
    if not undo.exists():
        return
    if (undo / "ready").exists() and not (undo / "committed").exists():
        info = json.loads((undo / "files.json").read_text(encoding="utf-8"))
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(paths.DB_PATH) + suffix)
            if suffix in info["present"]:
                shutil.copy2(undo / ("evo.db" + suffix), target)
            else:
                target.unlink(missing_ok=True)
        atomic_json(app_settings.SETTINGS_PATH, json.loads((undo / "settings.json").read_text(encoding="utf-8")))
    shutil.rmtree(undo)


def tick(now):
    hours = preferences()["hours"]
    last = app_settings.get("local_backup_last", 0)
    if hours and (now < last or now - last >= hours * 3600):
        with dav._lock:
            dav._ensure_idle()
            create_local("auto")


def view():
    return {**preferences(), "history": history(), "pending": pending(),
            "result": app_settings.get("restore_result", "")}
