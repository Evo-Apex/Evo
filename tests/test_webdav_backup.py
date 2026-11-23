"""WebDAV 备份：本机真实 HTTP 上传、WAL 快照、凭证隔离、每日调度与页面接口，不连接外部服务器。"""

import base64
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fake_game import report  # noqa: F401  隔离数据目录，必须先导入

import app_settings
import panel_app
import paths
import secret_store
import storage
import webdav_backup as backup


class DavHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        self.server.calls.append((self.command, self.path))
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", self.server.destination)
            self.end_headers()
            return False
        if self.server.failure:
            self.reply(self.server.failure)
            return False
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"backup-user:test-app-password").decode():
            self.reply(401, b"server message with private info")
            return False
        return True

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.authorized():
            return
        if self.path in self.server.directories:
            self.reply(207, b'<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>')
        else:
            self.reply(404)

    def do_MKCOL(self):
        if not self.authorized():
            return
        parent = self.path.rstrip("/").rsplit("/", 1)[0] + "/"
        if parent not in self.server.directories:
            self.reply(409)
            return
        self.server.directories.add(self.path)
        self.reply(201)

    def do_PUT(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.authorized():
            return
        if self.headers.get("If-None-Match") != "*":
            self.reply(412)
            return
        self.server.files[self.path] = content
        self.reply(201)


class WebDavTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DavHandler)
        cls.server.directories = {"/", "/dav/"}
        cls.server.calls, cls.server.files = [], {}
        cls.server.failure, cls.server.destination = None, "http://127.0.0.1:9/do-not-forward"
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        with closing(storage.connect(paths.DB_PATH)):
            pass
        panel_app.app.config.update(DB=paths.DB_PATH, TESTING=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.calls.clear()
        self.server.files.clear()
        self.server.failure = None
        backup._job.clear()
        backup.CREDENTIAL_PATH.unlink(missing_ok=True)
        app_settings.set_value(backup.STATE_KEY, {})
        panel_app.app.config["DB"] = paths.DB_PATH

    def save(self, path="/dav/Evo/", automatic=False):
        backup.save_config(self.base + path, "backup-user", "test-app-password", automatic)

    def wait(self):
        deadline = time.monotonic() + 8
        while backup.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(backup.busy(), "Background job did not finish")
        return backup.state()["job"]

    def test_credentials_encrypted_and_not_echoed(self):
        self.save()
        encrypted = backup.CREDENTIAL_PATH.read_bytes()
        self.assertNotIn(b"test-app-password", encrypted)
        self.assertEqual(secret_store.load(backup.CREDENTIAL_PATH)["password"], "test-app-password")
        backup.save_config(self.base + "/dav/Evo/", "backup-user", "", True)
        self.assertEqual(backup.config()["password"], "test-app-password")
        self.assertTrue(backup.config()["automatic"])
        with self.assertRaises(backup.BackupError):
            backup.save_config(self.base + "/other/", "backup-user", "", True)
        view = backup.view()
        self.assertNotIn("password", view)
        html = panel_app.app.test_client().get("/settings").get_data(as_text=True)
        self.assertIn("备份与云同步", html)
        self.assertNotIn("test-app-password", html)
        self.assertEqual(self.server.calls, [])

    def test_snapshot_contains_committed_wal_and_no_credentials(self):
        app_settings.set_value("mirrorchyan_cdk", "never-back-up-this-cdk")
        app_settings.set_value("cloud_token", "never-back-up-this-token")
        app_settings.set_value("sidebar_auto_seconds", 7)
        self.save()
        with closing(sqlite3.connect(paths.DB_PATH)) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("CREATE TABLE IF NOT EXISTS backup_marker(value TEXT)")
            live.execute("DELETE FROM backup_marker")
            live.execute("INSERT INTO backup_marker VALUES ('committed-in-wal')")
            live.commit()
            with tempfile.TemporaryDirectory() as folder:
                archive = backup.create_archive(folder)
                with zipfile.ZipFile(archive) as z:
                    self.assertEqual(set(z.namelist()), {"data/evo.db", "settings.json", "manifest.json", "恢复说明.txt"})
                    self.assertIsNone(z.testzip())
                    settings = json.loads(z.read("settings.json"))
                    self.assertEqual(settings["sidebar_auto_seconds"], 7)
                    self.assertNotIn("mirrorchyan_cdk", settings)
                    self.assertNotIn("cloud_token", settings)
                    dest = Path(folder) / "restore.db"
                    dest.write_bytes(z.read("data/evo.db"))
                with closing(sqlite3.connect(dest)) as restored:
                    self.assertEqual(restored.execute("SELECT value FROM backup_marker").fetchone()[0], "committed-in-wal")
                    self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_real_upload_mkdir_unique_names_and_daily_success(self):
        self.save("/dav/Evo/子目录/", automatic=True)
        self.assertTrue(backup.due(backup.config(), time.time()))
        backup.start()
        job = self.wait()
        self.assertEqual(job["phase"], "done", job)
        self.assertEqual(len(self.server.files), 1)
        self.assertIn("/dav/Evo/%E5%AD%90%E7%9B%AE%E5%BD%95/", self.server.directories)
        with zipfile.ZipFile(io.BytesIO(next(iter(self.server.files.values())))) as archive:
            self.assertIn("data/evo.db", archive.namelist())
        self.assertFalse(backup.due(backup.config(), time.time()))
        self.assertFalse(backup.start(automatic=True))
        backup.start()
        self.assertEqual(self.wait()["phase"], "done")
        self.assertEqual(len(self.server.files), 2)

    def test_failure_backoff_error_redaction_and_connection_test(self):
        self.save(automatic=True)
        self.server.failure = 401
        backup.start()
        job = self.wait()
        self.assertEqual(job["phase"], "failed")
        self.assertNotIn("private info", json.dumps(job))
        self.assertNotIn("test-app-password", json.dumps(job))
        now = time.time()
        self.assertFalse(backup.due(backup.config(), now + 60))
        self.assertTrue(backup.due(backup.config(), now + 3601))
        self.server.failure = None
        backup.start("test")
        self.assertEqual(self.wait()["phase"], "done")
        self.assertEqual(self.server.files, {})
        self.assertIsNone(backup.state()["last_success"])

    def test_daily_restart_and_next_day_and_disabled(self):
        self.save(automatic=True)
        now = time.time()
        app_settings.set_value(backup.STATE_KEY, {"config_id": backup._identity(backup.config()), "attempt": now,
            "success": {"at": backup._stamp(now), "filename": "old.zip"}})
        backup._job.clear()
        self.assertFalse(backup.due(backup.config(), now))
        tomorrow = (datetime.fromtimestamp(now) + timedelta(days=1)).timestamp()
        self.assertTrue(backup.due(backup.config(), tomorrow))
        backup.save_config(self.base + "/dav/Evo/", "backup-user", "", False)
        self.assertFalse(backup.due(backup.config(), tomorrow))
        backup.clear_config()
        self.assertIsNone(backup.config())

    def test_reject_bad_urls_and_do_not_forward_redirect(self):
        for url in ("file:///test", "ftp://host/test", "https://user:secret@host/dav/", "https://host/dav/?token=x",
                    "https://host/../dav/", "https://host/%2e%2e/dav/", "https://host/dav/#share", "https://host:abc/"):
            with self.subTest(url=url), self.assertRaises(backup.BackupError):
                backup.normalize_url(url)
        self.save("/redirect/")
        backup.start("test")
        result = self.wait()
        self.assertEqual(result["phase"], "failed")
        self.assertIn("跳转", result["message"])
        self.assertEqual(self.server.calls, [("PROPFIND", "/redirect/")])

    def test_busy_serializes_save_and_clear(self):
        self.save()
        entered, release = threading.Event(), threading.Event()
        def blocking(*args):
            entered.set()
            release.wait(4)
            return {"message": "test done"}
        with mock.patch.object(backup, "_perform", side_effect=blocking):
            backup.start("test")
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(backup.BackupError):
                    backup.start()
                with self.assertRaises(backup.BackupError):
                    backup.clear_config()
                with self.assertRaises(backup.BackupError):
                    self.save()
            finally:
                release.set()
                self.wait()

    def test_scheduler_stops_and_waits_for_game(self):
        self.save(automatic=True)
        stop, attempted = threading.Event(), threading.Event()
        gaming = [True]
        with mock.patch.object(backup, "start", side_effect=lambda **kw: attempted.set()):
            thread = backup.start_scheduler(stop, lambda: gaming[0], interval=0.02)
            try:
                self.assertFalse(attempted.wait(0.08))
                gaming[0] = False
                self.assertTrue(attempted.wait(1))
            finally:
                stop.set()
                thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_routes_require_token_and_demo_guard(self):
        client = panel_app.app.test_client()
        endpoint = "/settings/webdav/save"
        self.assertEqual(client.post(endpoint, data={}).status_code, 403)
        data = {"token": panel_app.app.config["TOKEN"], "url": self.base + "/dav/Evo/", "username": "backup-user",
                "password": "test-app-password"}
        self.assertEqual(client.post(endpoint, data=data).status_code, 302)
        status = client.get("/settings/webdav/status")
        self.assertNotIn(b"test-app-password", status.data)
        self.assertIn("no-store", status.headers["Cache-Control"])
        panel_app.app.config["DB"] = paths.DB_DIR / "demo.db"
        self.assertEqual(client.post(endpoint, data=data).status_code, 403)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WebDavTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
