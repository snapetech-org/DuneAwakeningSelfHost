#!/usr/bin/env python3
import pathlib
import stat
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "admin"))
from http_url import require_http_url
import addon_admin
import docker_api_proxy


class SecurityBoundariesTests(unittest.TestCase):
    def test_http_transport_rejects_local_files_and_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/file", "data:text/plain,secret",
                    "http:///missing-host", "http://user:password@example.com", "http://localhost:0",
                    "http://example.com\nInjected:yes", "http://example.com:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_http_url(url)

    def test_http_transport_preserves_configured_private_endpoints(self):
        for url in ("http://text-router:8080/v0/auth/user", "https://example.com/api?q=a%20b",
                    "http://127.0.0.1:18080/api", "http://[::1]:18080/api"):
            self.assertEqual(require_http_url(url), url)

    def test_broker_socket_excludes_other_users(self):
        with tempfile.TemporaryDirectory(prefix="dune-security-socket-") as directory:
            path = pathlib.Path(directory) / "docker.sock"
            server = docker_api_proxy.ProxyServer(str(path))
            try:
                self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                server.server_close()

    def test_addon_redirect_is_rejected_before_contacting_unapproved_host(self):
        with mock.patch.object(addon_admin.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = RuntimeError("stop before network")
            with self.assertRaisesRegex(RuntimeError, "stop before network"):
                addon_admin._fetch("https://raw.githubusercontent.com/owner/repo/main/addon.json", 100)
            handler = build.call_args.args[0]
            request = urllib.request.Request("https://raw.githubusercontent.com/owner/repo/main/addon.json")
            for destination in ("http://127.0.0.1/internal", "https://unapproved.example/addon.zip", "file:///etc/passwd"):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    handler.redirect_request(request, None, 302, "redirect", {}, destination)


if __name__ == "__main__":
    unittest.main()
