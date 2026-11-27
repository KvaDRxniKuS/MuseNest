"""Tests for the Zvuk downloader self-check (core/youtube.py::_zvuk_downloader_check).

The bug: both steps sent the *same* authenticated /profile call, so an expired
token answered 401 and the check reported "Zvuk API недоступен (нужен VPN)" —
an outage that does not exist — while the token check itself was unreachable
(`if has_token and api_reachable`), so token_valid could never be False.

Now reachability is probed anonymously and the token is validated separately.

Run:  python tests/test_zvuk_downloader_check.py
"""

import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import unittest
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import youtube as yt_mod  # noqa: E402
from core import zvuk as zv_mod  # noqa: E402

ANON_TOKEN = "anon-token-abc"
GOOD_TOKEN = "user-token-valid"
EXPIRED_TOKEN = "user-token-expired"


class FakeZvukProfile:
    """/profile only: answers anonymously, rejects a bad token."""

    def __init__(self, profile_up=True):
        self.profile_up = profile_up
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                tok = self.headers.get("X-Auth-Token")
                outer.seen.append(tok)
                if not outer.profile_up:
                    return self._j(503, {"error": "unavailable"})
                # an expired/invalid token is rejected
                if tok is not None and tok not in (ANON_TOKEN, GOOD_TOKEN):
                    return self._j(401, {"error": "unauthorized"})
                return self._j(200, {"result": {"token": ANON_TOKEN}})

            def _j(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._handler = Handler

    def __enter__(self):
        socketserver.TCPServer.allow_reuse_address = True
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.httpd = socketserver.TCPServer(("127.0.0.1", self.port), self._handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._orig = zv_mod.TINY_URL
        zv_mod.TINY_URL = "http://127.0.0.1:%d/api/tiny" % self.port
        return self

    def __exit__(self, *exc):
        zv_mod.TINY_URL = self._orig
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def anonymous_probe_sent(self):
        """True if at least one /profile call carried no X-Auth-Token."""
        return any(t is None for t in self.seen)


class TestZvukDownloaderCheck(unittest.TestCase):
    def _check(self, token):
        return yt_mod._zvuk_downloader_check(
            {"zvuk_token": token or "", "proxy": ""}, timeout=20)

    def test_valid_token_reports_active_token_and_high(self):
        with FakeZvukProfile():
            info = self._check(GOOD_TOKEN)
        self.assertTrue(info["ok"], info["test"])
        self.assertTrue(info["api_reachable"])
        self.assertTrue(info["token_valid"])
        self.assertEqual(info["quality"], "high")
        self.assertIn("токен активен", info["test"]["message"])

    def test_no_token_reports_mid_and_still_ok(self):
        with FakeZvukProfile():
            info = self._check(None)
        self.assertTrue(info["ok"], info["test"])
        self.assertTrue(info["api_reachable"])
        self.assertIsNone(info["token_valid"])
        self.assertEqual(info["quality"], "mid")
        self.assertIn("без токена", info["test"]["message"])

    def test_expired_token_is_not_reported_as_an_outage(self):
        """The core regression: 401 on the token must not read as 'API down'."""
        with FakeZvukProfile() as fz:
            info = self._check(EXPIRED_TOKEN)

        self.assertTrue(info["api_reachable"],
                        "the API IS reachable; only the token is bad")
        self.assertTrue(fz.anonymous_probe_sent(),
                        "reachability must be probed without the user token")
        self.assertIs(info["token_valid"], False)
        self.assertEqual(info["token_status"], 401)
        self.assertFalse(info["ok"])

        msg = info["test"]["message"]
        self.assertIn("истёк", msg, msg)
        self.assertIn("401", msg, msg)
        self.assertIn("zvuk.com/api/tiny/profile", msg, msg)
        self.assertNotIn("API недоступен", msg, msg)
        self.assertNotIn("VPN", msg, msg)

    def test_expired_token_mentions_mid_still_works(self):
        with FakeZvukProfile():
            info = self._check(EXPIRED_TOKEN)
        self.assertIn("mid", info["test"]["message"])

    def test_real_outage_is_reported_as_network_not_token(self):
        zv_mod.TINY_URL = "http://127.0.0.1:1/api/tiny"  # nothing listening
        try:
            info = self._check(GOOD_TOKEN)
        finally:
            zv_mod.TINY_URL = "https://zvuk.com/api/tiny"
        self.assertFalse(info["ok"])
        self.assertFalse(info["api_reachable"])
        msg = info["test"]["message"]
        detail = info["test"]["detail"]
        self.assertIn("недоступен", msg, msg)
        self.assertIn("анонимно", detail, detail)
        self.assertIn("сети/прокси/DNS", detail, detail)
        # an unreachable API must not blame the token
        self.assertNotIn("истёк", msg, msg)

    def test_server_503_is_an_outage_too(self):
        with FakeZvukProfile(profile_up=False):
            info = self._check(GOOD_TOKEN)
        self.assertFalse(info["ok"])
        self.assertFalse(info["api_reachable"])
        self.assertEqual(info["api_status"], 503)

    def test_progress_stages_are_emitted(self):
        events = []
        with FakeZvukProfile():
            yt_mod._zvuk_downloader_check(
                {"zvuk_token": GOOD_TOKEN, "proxy": ""}, timeout=20,
                on_progress=lambda p: events.append(p["stage_key"]))
        self.assertIn("prepare", events)
        self.assertIn("api", events)
        self.assertIn("token", events)
        self.assertEqual(events[-1], "done", events)

    def test_result_shape_keeps_ui_fields(self):
        with FakeZvukProfile():
            info = self._check(None)
        for key in ("ok", "mode", "ffmpeg", "zvuk_token", "quality",
                    "api_reachable", "token_valid", "test"):
            self.assertIn(key, info)
        self.assertEqual(info["mode"], "zvuk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
