"""真实本机 TLS 握手；测试依赖：pip install -r tests/requirements-webdav-tls.txt。"""

import ipaddress
import ssl
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fake_game import report  # noqa: F401，必须先隔离数据目录
import certifi
try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    x509 = None


import webdav_backup as backup


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received += 1
        self.send_response(200)
        self.send_header("Content-Length", "14")
        self.end_headers()
        self.wfile.write(b"verified bytes")


@unittest.skipIf(x509 is None, "请安装 tests/requirements-webdav-tls.txt 后运行 TLS 测试")
class WebDavTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="evo-tls-test-")
        cls.folder = Path(cls.tmp.name)
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Evo isolated TLS test CA")])
        cls.now = datetime.now(timezone.utc)
        cls.ca = (x509.CertificateBuilder().subject_name(cls.name).issuer_name(cls.name)
                  .public_key(cls.key.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(cls.now - timedelta(days=2)).not_valid_after(cls.now + timedelta(days=2))
                  .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                  .sign(cls.key, hashes.SHA256()))
        cls.ca_path = cls.folder / "ca.pem"
        cls.ca_path.write_bytes(cls.ca.public_bytes(serialization.Encoding.PEM))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @contextmanager
    def server(self, *, hostname=True, expired=False, future=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                .issuer_name(self.name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(self.now + timedelta(hours=1) if future else self.now - timedelta(days=1))
                .not_valid_after(self.now - timedelta(hours=1) if expired else self.now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")) if hostname else x509.DNSName("wrong.invalid")]), False)
                .sign(self.key, hashes.SHA256()))
        cert_path, key_path = self.folder / "server.pem", self.folder / "server-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.received, server.daemon_threads = 0, True
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, f"https://127.0.0.1:{server.server_port}/backup.zip"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def request(self, url, **kwargs):
        return backup._request({"username": "fixture-user", "password": "fixture-secret"}, url, "GET", **kwargs)

    def test_supplemental_ca_works_without_system_roots_and_downloads(self):
        with self.server() as (server, url), mock.patch.object(ssl.SSLContext, "load_default_certs"), \
                mock.patch.object(certifi, "where", return_value=str(self.ca_path)):
            target = self.folder / "download.zip"
            self.assertEqual(self.request(url, output=target), (200, b""))
            self.assertEqual(target.read_bytes(), b"verified bytes")
            self.assertEqual(server.received, 1)

    def test_system_trust_is_preserved(self):
        context = ssl.create_default_context(cafile=str(self.ca_path))
        with self.server() as (_, url), mock.patch.object(ssl, "create_default_context", return_value=context):
            self.assertEqual(self.request(url), (200, b"verified bytes"))
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_unknown_ca_rejected_before_credentials_sent(self):
        with self.server() as (server, url), mock.patch.object(ssl.SSLContext, "load_default_certs"):
            with self.assertRaisesRegex(backup.BackupError, "证书链.*校验码 20"):
                self.request(url)
            self.assertEqual(server.received, 0)

    def test_invalid_certificates_rejected_with_specific_reason(self):
        for options, message in (({"hostname": False}, "IP 地址不匹配.*64"),
                                 ({"expired": True}, "已过期.*10"), ({"future": True}, "尚未生效.*9")):
            with self.subTest(options=options), self.server(**options) as (server, url), \
                    mock.patch.object(certifi, "where", return_value=str(self.ca_path)):
                with self.assertRaisesRegex(backup.BackupError, message):
                    self.request(url)
                self.assertEqual(server.received, 0)

    def test_missing_bundle_fails_closed(self):
        with mock.patch.object(certifi, "where", return_value=str(self.folder / "missing.pem")):
            with self.assertRaisesRegex(backup.BackupError, "完整解压"):
                backup._tls_context()

    def test_public_bundle_is_loaded_with_verification_enabled(self):
        with mock.patch.object(ssl.SSLContext, "load_default_certs"):
            context = backup._tls_context()
            self.assertGreater(context.cert_store_stats()["x509_ca"], 100)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_errors_do_not_echo_untrusted_exception_text(self):
        error = ssl.SSLCertVerificationError(1, "https://private-user:private-password@private.example")
        error.verify_code = 999
        message = backup._certificate_error(error)
        self.assertIn("999", message)
        self.assertNotIn("private", message)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WebDavTLSTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
