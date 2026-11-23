"""备份中心：真实本机 WebDAV 冲突/下载、恢复事务与账号连续性、损坏档案拒绝和保留数量。"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fake_game import report  # noqa: F401
from test_webdav_backup import DavHandler
import app_settings
import backup_manager as local
import webdav_sync as cloud
import paths
import panel_app
import storage
import webdav_backup as dav


class SyncHandler(DavHandler):
    def do_PROPFIND(self):
        failure = getattr(self.server, "probe_failure", None)
        if not failure and self.path not in self.server.directories:
            failure = getattr(self.server, "missing_probe_status", None)
        if failure:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.authorized():
                self.reply(failure)
            return
        super().do_PROPFIND()

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        mode = getattr(self.server, "etag_mode", "strong")
        if self.path in self.server.files and mode != "missing":
            value = hashlib.sha256(self.server.files[self.path]).hexdigest()
            if mode != "unquoted":
                value = '"' + value + '"'
            self.send_header("ETag", 'W/' + value if mode == "weak" else value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.authorized():
            with self.server.lock:
                body = self.server.files.get(self.path)
                self.reply(200, body) if body is not None else self.reply(404)

    def do_MKCOL(self):
        if not self.authorized():
            return
        if getattr(self.server, "mkdir_failure", None):
            self.reply(self.server.mkdir_failure)
            return
        with self.server.lock:
            if self.path in self.server.directories or self.path.rstrip("/") in self.server.files:
                self.reply(405)
            elif self.path.rstrip('/').rsplit('/', 1)[0] + '/' not in self.server.directories:
                self.reply(409)
            else:
                self.server.directories.add(self.path)
                self.reply(201)

    def do_DELETE(self):
        if not self.authorized():
            return
        with self.server.lock:
            if self.path in self.server.directories:
                self.server.directories.remove(self.path)
                self.reply(204)
            else:
                self.reply(404)

    def do_PUT(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.authorized():
            return
        with self.server.lock:
            old = self.server.files.get(self.path)
            etag = '"' + hashlib.sha256(old).hexdigest() + '"' if old is not None else None
            if self.headers.get("If-None-Match") == "*" and old is not None:
                self.reply(412)
                return
            if self.headers.get("If-Match") and self.headers["If-Match"] != etag:
                self.reply(412)
                return
            self.server.files[self.path] = content
            self.reply(201)


class BackupCenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SyncHandler)
        cls.server.lock = threading.Lock()
        cls.server.directories, cls.server.files, cls.server.calls = {"/", "/dav/"}, {}, []
        cls.server.failure, cls.server.destination = None, "http://127.0.0.1:9/blocked"
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/dav/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        import fake_game
        assert paths.DATA_DIR.resolve() == fake_game._TEMP.resolve()
        local.PENDING.unlink(missing_ok=True)
        shutil.rmtree(local.ROOT, ignore_errors=True)
        shutil.rmtree(paths.DB_DIR / "restore-undo", ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(paths.DB_PATH) + suffix).unlink(missing_ok=True)
        with closing(storage.connect(paths.DB_PATH)) as conn:
            storage.insert_match(conn, {"player_name": "[OLD] PilotI", "ended_at": "2026-11-23 10:00:00"})
            storage.insert_match(conn, {"player_name": "EarlierName", "ended_at": "2026-11-23 11:00:00"})
            self.account_id = storage.accounts(conn)[0]["id"]
            storage.answer_name(conn, "EarlierName", self.account_id)
        dav._job.clear()
        local._hooks.clear()
        self.server.files.clear()
        self.server.calls.clear()
        self.server.failure = None
        self.server.probe_failure = self.server.missing_probe_status = self.server.mkdir_failure = None
        self.server.etag_mode = "strong"
        self.server.directories = {"/", "/dav/"}
        local.atomic_json(app_settings.SETTINGS_PATH, {})
        dav.save_config(self.url, "backup-user", "test-app-password", True,
                        remote_root="evo-sync", profile="default", on_change=True)
        panel_app.app.config.update(DB=paths.DB_PATH, TESTING=True)

    def wait(self):
        end = time.monotonic() + 10
        while dav.busy() and time.monotonic() < end:
            time.sleep(.01)
        self.assertFalse(dav.busy())
        return dav.state()["job"]

    def upload(self):
        dav.start()
        self.assertEqual(self.wait()["phase"], "done")

    def count(self):
        with closing(sqlite3.connect(paths.DB_PATH)) as conn:
            return conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    def restore(self, path):
        local.prepare_restore(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        local.apply_pending()
        self.assertFalse(local.pending())

    def test_cloud_roundtrip_new_device_conflict_and_account_identity(self):
        self.upload()
        first_head = self.server.files["/dav/evo-sync/default/latest.json"]
        # 新设备：没有已同步 revision，绝不把自己的数据库直接覆盖到云端。
        app_settings.set_value(cloud.KEY, {})
        dav.start()
        job = self.wait()
        self.assertEqual(job["phase"], "failed")
        self.assertTrue(job["conflict_revision"])
        self.assertEqual(first_head, self.server.files["/dav/evo-sync/default/latest.json"])
        dav.start("download")
        job = self.wait()
        self.assertEqual(job["phase"], "done")
        with closing(storage.connect(paths.DB_PATH)) as conn:
            storage.insert_match(conn, {"player_name": "Unsynced", "ended_at": "2026-11-23 12:00:00"})
        self.restore(local.file_path(job["filename"]))
        self.assertEqual(self.count(), 2)
        self.assertTrue(app_settings.get("webdav_sync_paused"))
        self.assertEqual(len([i for i in local.history() if i["protected"]]), 1)
        with closing(storage.connect(paths.DB_PATH)) as conn:
            storage.insert_match(conn, {"player_name": "[NEW] Pilot1", "ended_at": "2026-11-23 13:00:00"})
            accounts = storage.accounts(conn)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["id"], self.account_id)
            self.assertEqual(accounts[0]["games"], 3)
        self.upload()

    def test_force_requires_reviewed_revision_and_conditional_race(self):
        self.upload()
        app_settings.set_value(cloud.KEY, {})
        dav.start()
        expected = self.wait()["conflict_revision"]
        with self.assertRaises(dav.BackupError):
            dav.start("force", expected="invalid")
        dav.start("force", expected=expected)
        self.assertEqual(self.wait()["phase"], "done")
        original = dav._request
        def race(cfg, url, method, body=None, headers=None, **kwargs):
            if method == "PUT" and url.endswith("latest.json"):
                self.server.files["/dav/evo-sync/default/latest.json"] = b'{"other_device":true}'
            return original(cfg, url, method, body, headers, **kwargs)
        with mock.patch.object(dav, "_request", side_effect=race):
            dav.start()
            self.assertEqual(self.wait()["phase"], "failed")
        self.assertEqual(self.server.files["/dav/evo-sync/default/latest.json"], b'{"other_device":true}')

    def test_download_corruption_leaves_live_database_unchanged(self):
        self.upload()
        head = json.loads(self.server.files["/dav/evo-sync/default/latest.json"])
        self.server.files["/dav/evo-sync/default/" + head["file"]] = b"corrupt"
        dav.start("download")
        self.assertEqual(self.wait()["phase"], "failed")
        self.assertEqual(self.count(), 2)
        self.assertEqual(local.history(), [])

    def test_download_and_test_use_only_get_even_while_another_device_holds_lock(self):
        self.upload()
        cfg = dav.config()
        with cloud.writer_lock(cfg):
            before = dict(self.server.files)
            directories = set(self.server.directories)
            self.server.probe_failure = self.server.mkdir_failure = 409
            self.server.calls.clear()
            dav.start("test")
            job = self.wait()
            self.assertEqual(job["phase"], "done", job)
            self.assertIn("已找到云端快照", job["message"])
            dav.start("download")
            job = self.wait()
            self.assertEqual(job["phase"], "done", job)
            self.assertTrue(local.file_path(job["filename"]).is_file())
            self.assertEqual(self.count(), 2)
            self.assertEqual(self.server.files, before)
            self.assertEqual(self.server.directories, directories)
            self.assertTrue(self.server.calls)
            self.assertTrue(all(method == "GET" for method, _ in self.server.calls), self.server.calls)

    def test_missing_download_and_connection_test_do_not_create_cloud_directories(self):
        before = set(self.server.directories)
        dav.start("download")
        job = self.wait()
        self.assertEqual(job["phase"], "failed")
        self.assertIn("当前目录未找到", job["message"])
        dav.start("test")
        job = self.wait()
        self.assertEqual(job["phase"], "done", job)
        self.assertIn("尚无云端快照", job["message"])
        self.assertEqual(self.server.directories, before)
        self.assertTrue(all(method in ("GET", "PROPFIND") for method, _ in self.server.calls))
        self.assertEqual(self.server.files, {})
        self.assertEqual(local.history(), [])

    def test_upload_creates_parents_when_missing_propfind_returns_409(self):
        self.server.missing_probe_status = 409
        self.upload()
        self.assertIn("/dav/evo-sync/default/latest.json", self.server.files)
        self.assertIn("/dav/evo-sync/", self.server.directories)
        self.assertIn("/dav/evo-sync/default/", self.server.directories)
        dav.start("download")
        self.assertEqual(self.wait()["phase"], "done")

    def test_create_directory_409_does_not_continue_upload(self):
        self.server.mkdir_failure = 409
        dav.start()
        job = self.wait()
        self.assertEqual(job["phase"], "failed")
        self.assertIn("创建云端目录", job["message"])
        self.assertIn("HTTP 409", job["message"])
        self.assertFalse(any(method == "PUT" for method, _ in self.server.calls))
        self.assertEqual(self.server.files, {})

    def test_same_name_file_blocks_directory_without_overwrite(self):
        self.server.files["/dav/evo-sync"] = b"keep this file"
        self.server.missing_probe_status = 409
        dav.start()
        self.assertEqual(self.wait()["phase"], "failed")
        self.assertEqual(self.server.files, {"/dav/evo-sync": b"keep this file"})
        self.assertFalse(any(method == "PUT" for method, _ in self.server.calls))

    def test_get_and_put_409_are_reported_as_errors(self):
        self.server.failure = 409
        cfg = dav.config()
        for method, operation in (("GET", "读取云端备份"), ("PUT", "写入云端备份")):
            with self.subTest(method=method), self.assertRaisesRegex(dav.BackupError, operation + ".*HTTP 409"):
                dav._request(cfg, cfg["sync_url"] + "latest.json", method, b"" if method == "PUT" else None)
        self.assertEqual(self.server.files, {})

    def test_missing_weak_unquoted_etag_roundtrip_and_conflicts(self):
        for mode in ("missing", "weak", "unquoted"):
            with self.subTest(mode=mode):
                self.server.files.clear()
                app_settings.set_value(cloud.KEY, {})
                self.server.etag_mode = mode
                self.upload()
                self.assertTrue(app_settings.get(cloud.KEY)["revision"].startswith("sha256:"))
                self.upload()
                app_settings.set_value(cloud.KEY, {})
                dav.start()
                job = self.wait()
                self.assertEqual(job["phase"], "failed")
                self.assertTrue(job["conflict_revision"].startswith("sha256:"))
                dav.start("force", expected=job["conflict_revision"])
                self.assertEqual(self.wait()["phase"], "done")
                dav.start("download")
                job = self.wait()
                self.assertEqual(job["phase"], "done")
                self.restore(local.file_path(job["filename"]))
                self.assertEqual(self.count(), 2)
                self.upload()
                self.assertNotIn("/dav/evo-sync/default/evo-write-lock/", self.server.directories)

    def test_missing_etag_concurrent_writer_cannot_overwrite(self):
        self.server.etag_mode = "missing"
        self.upload()
        cfg = dav.config()
        before = self.server.files["/dav/evo-sync/default/latest.json"]
        with cloud.writer_lock(cfg):
            dav.start()
            job = self.wait()
            self.assertEqual(job["phase"], "failed")
            self.assertIn("evo-write-lock", job["message"])
            self.assertEqual(self.server.files["/dav/evo-sync/default/latest.json"], before)
            self.assertIn("/dav/evo-sync/default/evo-write-lock/", self.server.directories)
        self.upload()

    def test_hash_recheck_detects_changes_and_releases_lock(self):
        self.server.etag_mode = "missing"
        self.upload()
        original = dav._request
        changed = []
        def race(cfg, url, method, body=None, headers=None, **kwargs):
            result = original(cfg, url, method, body, headers, **kwargs)
            if method == "PUT" and url.endswith(".zip"):
                value = json.loads(self.server.files["/dav/evo-sync/default/latest.json"])
                value["at"] = "changed-by-other-device"
                self.server.files["/dav/evo-sync/default/latest.json"] = json.dumps(value).encode()
                changed.append(True)
            return result
        with mock.patch.object(dav, "_request", side_effect=race):
            dav.start()
            self.assertEqual(self.wait()["phase"], "failed")
        self.assertTrue(changed)
        self.assertIn(b"changed-by-other-device", self.server.files["/dav/evo-sync/default/latest.json"])
        self.assertNotIn("/dav/evo-sync/default/evo-write-lock/", self.server.directories)

    def test_previous_upload_without_etag_can_be_downloaded(self):
        self.upload()
        self.server.etag_mode = "missing"
        app_settings.set_value(cloud.KEY, {})
        dav.start("test")
        self.assertEqual(self.wait()["phase"], "done")
        dav.start("download")
        job = self.wait()
        self.assertEqual(job["phase"], "done")
        self.restore(local.file_path(job["filename"]))
        self.upload()

    def test_open_backup_folder_and_custom_provider(self):
        dav.save_config(self.url, "backup-user", "", False, remote_root="evo-sync", provider="jianguoyun")
        self.assertEqual(dav.config()["provider"], "custom")
        client = panel_app.app.test_client()
        html = client.get("/settings").get_data(as_text=True)
        self.assertIn("备份文件夹</button>", html)
        self.assertNotIn('id="webdav-provider"', html)
        self.assertNotIn('value="jianguoyun"', html)
        with mock.patch.object(panel_app.os, "startfile") as startfile:
            self.assertEqual(client.post("/settings/backups/open-folder").status_code, 403)
            startfile.assert_not_called()
            response = client.post("/settings/backups/open-folder", data={"token": panel_app.app.config["TOKEN"], "name": "C:/Windows"})
            self.assertEqual(response.status_code, 200)
            startfile.assert_called_once_with(local.ROOT)
            self.assertTrue(local.ROOT.is_dir())

    def test_save_config_json_without_redirect_preserves_secret_and_reports_errors(self):
        client = panel_app.app.test_client()
        args = {"token": panel_app.app.config["TOKEN"], "url": self.url,
                "username": "backup-user", "password": "", "remote_root": "evo-sync",
                "profile": "saved-profile", "automatic": "1", "on_change": "1"}
        headers = {"Accept": "application/json"}
        response = client.post("/settings/webdav/save", data=args, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.json["configured"])
        self.assertTrue(response.json["password_set"])
        self.assertNotIn("test-app-password", response.get_data(as_text=True))
        self.assertEqual(dav.config()["profile"], "saved-profile")
        self.assertEqual(dav.config()["password"], "test-app-password")
        failed = client.post("/settings/webdav/save", data={**args, "profile": "../bad"}, headers=headers)
        self.assertEqual(failed.status_code, 400)
        self.assertIn("error", failed.json)
        self.assertEqual(dav.config()["profile"], "saved-profile")
        self.assertEqual(client.post("/settings/webdav/save", data={**args, "token": "wrong"}, headers=headers).status_code, 403)
        # Non-JavaScript clients retain the existing redirect fallback.
        self.assertEqual(client.post("/settings/webdav/save", data=args).status_code, 302)

    def test_history_fragment_tracks_create_rename_import_delete_and_auto_backup(self):
        client = panel_app.app.test_client()
        token = {"token": panel_app.app.config["TOKEN"]}
        def listing():
            response = client.get("/settings/backups/history")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.get_data(as_text=True)
        self.assertIn("还没有本地备份", listing())
        self.assertEqual(client.post("/settings/backups/create", data=token).status_code, 200)
        name = local.history()[0]["name"]
        self.assertIn(name, listing())
        label = '<img src=x onerror=alert(1)>'
        self.assertEqual(client.post("/settings/backups/rename", data={**token, "name": name, "label": label}).status_code, 200)
        self.assertIn("&lt;img", listing())
        self.assertNotIn(label, listing())
        content = local.file_path(name).read_bytes()
        self.assertEqual(client.post("/settings/backups/import", data={**token, "archive": (io.BytesIO(content), "import.zip")}).status_code, 200)
        self.assertIn("共 2 份", listing())
        for item in local.history():
            self.assertEqual(client.post("/settings/backups/delete", data={**token, "name": item["name"]}).status_code, 200)
        self.assertIn("共 0 份", listing())
        self.assertIn("还没有本地备份", listing())
        auto = local.create_local("auto")
        self.assertIn(auto.name, listing())
        panel_app.app.config["DB"] = paths.DB_DIR / "demo.db"
        self.assertEqual(client.get("/settings/backups/history").status_code, 403)

    def test_reject_traversal_newer_schema_and_altered_db(self):
        good = local.create_local()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "bad.zip"
            with zipfile.ZipFile(good) as z:
                entries = {name: z.read(name) for name in z.namelist()}
            variants = [dict(entries, **{"../escape": b"bad"}), dict(entries, **{"data/evo.db": b"bad"}),
                        dict(entries, **{"manifest.json": b"[]"}), dict(entries, **{"settings.json": b'{"account_display_names":[]}'})]
            db = Path(folder) / "new.db"
            db.write_bytes(entries["data/evo.db"])
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("PRAGMA user_version=999")
            manifest = json.loads(entries["manifest.json"])
            manifest["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            variants.append(dict(entries, **{"data/evo.db": db.read_bytes(), "manifest.json": json.dumps(manifest).encode()}))
            for content in variants:
                with zipfile.ZipFile(dest, "w") as z:
                    for key, value in content.items():
                        z.writestr(key, value)
                with self.assertRaises(dav.BackupError):
                    local.import_archive(io.BytesIO(dest.read_bytes()))
        self.assertEqual(self.count(), 2)

    def test_restore_failure_rolls_back_database_and_settings(self):
        app_settings.set_value("window_theme", "dark")
        archive = local.create_local()
        with closing(storage.connect(paths.DB_PATH)) as conn:
            storage.insert_match(conn, {"player_name": "Extra", "ended_at": "2026-11-23 14:00:00"})
        app_settings.set_value("window_theme", "light")
        local.prepare_restore(archive.name, hashlib.sha256(archive.read_bytes()).hexdigest())
        original = os.replace
        failed = []
        def fail_once(src, dst):
            if Path(dst) == app_settings.SETTINGS_PATH and not failed:
                failed.append(True)
                raise OSError("disk full")
            return original(src, dst)
        with mock.patch.object(local.os, "replace", side_effect=fail_once):
            local.apply_pending()
        self.assertEqual(self.count(), 3)
        self.assertEqual(app_settings.get("window_theme"), "light")
        self.assertIn("未完成", app_settings.get("restore_result"))

    def test_retention_protects_manual_and_rollback_and_local_schedule(self):
        local.save_preferences(1, 5)
        local.create_local("manual")
        local.create_local("rollback")
        for _ in range(7):
            local.create_local("auto")
        self.assertEqual(len(local.history()), 7)
        with mock.patch.object(local, "create_local") as create:
            local.tick(time.time())
            create.assert_not_called()
            local.tick(time.time() + 3601)
            create.assert_called_once_with("auto")

    def test_interrupted_restore_is_undone_before_startup(self):
        undo = paths.DB_DIR / "restore-undo"
        undo.mkdir()
        local.atomic_json(undo / "settings.json", {"window_theme": "dark"})
        local.atomic_json(undo / "files.json", {"present": [""]})
        shutil.copy2(paths.DB_PATH, undo / "evo.db")
        (undo / "ready").touch()
        with closing(sqlite3.connect(paths.DB_PATH)) as conn:
            conn.execute("DELETE FROM matches")
            conn.commit()
        local.recover_interrupted()
        self.assertEqual(self.count(), 2)
        self.assertEqual(app_settings.get("window_theme"), "dark")
        self.assertFalse(undo.exists())

    def test_missing_columns_rejected_even_with_valid_hash(self):
        good = local.create_local()
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(good) as z:
                entries = {name: z.read(name) for name in z.namelist()}
            db = Path(folder) / "bad.db"
            db.write_bytes(entries["data/evo.db"])
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("DROP TABLE teammates")
                conn.commit()
            manifest = json.loads(entries["manifest.json"])
            manifest["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            entries.update({"data/evo.db": db.read_bytes(), "manifest.json": json.dumps(manifest).encode()})
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as z:
                for key, value in entries.items():
                    z.writestr(key, value)
            payload.seek(0)
            with self.assertRaises(dav.BackupError):
                local.import_archive(payload)

    def test_changed_upload_and_failure_backoff(self):
        self.upload()
        self.assertFalse(cloud.changed_due(dav.config(), time.time()))
        app_settings.set_value("window_theme", "light")
        self.assertTrue(cloud.changed_due(dav.config(), time.time()))
        self.server.failure = 503
        dav.start()
        self.assertEqual(self.wait()["phase"], "failed")
        self.assertFalse(cloud.changed_due(dav.config(), time.time()))
        self.assertTrue(cloud.changed_due(dav.config(), time.time() + 3601))

    def test_restore_requires_confirmation_and_blocks_game_and_pending_writes(self):
        client = panel_app.app.test_client()
        path = local.create_local()
        args = {"token": panel_app.app.config["TOKEN"], "name": path.name}
        self.assertEqual(client.post("/settings/backups/restore", data=args).status_code, 400)
        preview = client.post("/settings/backups/preview", data=args).get_json()
        self.assertEqual(preview["matches"], 2)
        local.connect(game_running=lambda: True)
        args.update(confirm="1", fingerprint=preview["fingerprint"])
        self.assertEqual(client.post("/settings/backups/restore", data=args).status_code, 400)
        local.connect(game_running=lambda: False)
        self.assertEqual(client.post("/settings/backups/restore", data=args).status_code, 200)
        self.assertEqual(client.post("/settings/backups/delete", data=args).status_code, 409)
        local.PENDING.unlink()
        self.assertEqual(client.post("/settings/backups/delete", data={"name": path.name}).status_code, 403)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BackupCenterTests))
    return result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
