"""Tests for Zvuk stream resolution (core/zvuk.py::resolve_stream).

Reproduces the real failure from the log:

    No Zvuk stream for 3862635281 (quality=high). Без токена доступен только
    анонимный mid — этот трек требует подписку.

The old code requested `high`, swallowed the HTTP error and always printed that
hardcoded guess. These tests run the real code against a local HTTP server that
answers like Zvuk does (401 for an expired token, 403 for a quality the
subscription does not cover) and assert the new behaviour:

  * quality ladder high -> mid, so the track still downloads,
  * 401 -> retry anonymously, and download with the token that actually worked,
  * honest error text carrying the real HTTP statuses,
  * the token itself never leaks into a message or log.

Run:  python tests/test_zvuk_stream.py
"""

import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import unittest
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import zvuk as zv_mod  # noqa: E402

ANON_TOKEN = "anon-token-abc123"
USER_TOKEN = "user-token-expired"
GOOD_TOKEN = "user-token-valid"
STREAM_URL = "https://cdn.example.invalid/audio/track.bin"


class FakeZvuk:
    """Local stand-in for https://zvuk.com/api/tiny.

    ``rules`` maps a quality to one of:
      "ok"        -> 200 with a stream URL
      401 / 403   -> that HTTP status
      "no_stream" -> 200 without the stream field
    ``accept_tokens`` restricts which X-Auth-Token values are honoured; others
    get 401 (an expired/invalid token).
    """

    def __init__(self, rules=None, accept_tokens=(ANON_TOKEN, GOOD_TOKEN)):
        self.rules = rules or {"mid": "ok", "high": 403, "flac": 403}
        self.accept_tokens = set(accept_tokens)
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                tok = self.headers.get("X-Auth-Token")
                outer.requests.append({"path": parsed.path, "qs": qs, "token": tok})

                if parsed.path.endswith("/profile"):
                    # Anonymous profile must work without a token.
                    return self._json(200, {"result": {"token": ANON_TOKEN}})

                if parsed.path.endswith("/track/stream"):
                    if tok is not None and tok not in outer.accept_tokens:
                        return self._json(401, {"error": "unauthorized"})
                    q = qs.get("quality")
                    rule = outer.rules.get(q, "ok")
                    if rule == "ok":
                        return self._json(200, {"stream": STREAM_URL})
                    if rule == "no_stream":
                        return self._json(200, {"result": {"url": "unexpected-shape"}})
                    return self._json(int(rule), {"error": "forbidden"})

                return self._json(404, {"error": "not found"})

            def _json(self, code, payload):
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
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.httpd = socketserver.TCPServer(("127.0.0.1", self.port), self._handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self._orig_tiny = zv_mod.TINY_URL
        zv_mod.TINY_URL = "http://127.0.0.1:%d/api/tiny" % self.port
        return self

    def __exit__(self, *exc):
        zv_mod.TINY_URL = self._orig_tiny
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def qualities_tried(self):
        return [r["qs"].get("quality") for r in self.requests
                if r["path"].endswith("/track/stream")]


TRACK = "3862635281"  # the exact track id from the reported log


class TestQualityLadder(unittest.TestCase):
    def test_high_refused_falls_back_to_mid_instead_of_failing(self):
        """The reported bug: no token + high -> must still download at mid."""
        with FakeZvuk(rules={"mid": "ok", "high": 403, "flac": 403}) as fz:
            zv = zv_mod.ZvukSource(token=None)
            url, used = zv.resolve_stream(TRACK, quality="high")
        self.assertEqual(url, STREAM_URL)
        self.assertEqual(used, "mid")
        self.assertEqual(zv.last_quality, "mid")
        self.assertEqual(fz.qualities_tried(), ["high", "mid"])

    def test_mid_requested_does_not_retry_mid_twice(self):
        with FakeZvuk(rules={"mid": "ok"}) as fz:
            zv = zv_mod.ZvukSource(token=None)
            url, used = zv.resolve_stream(TRACK, quality="128")
        self.assertEqual((url, used), (STREAM_URL, "mid"))
        self.assertEqual(fz.qualities_tried(), ["mid"])

    def test_valid_token_keeps_high(self):
        with FakeZvuk(rules={"mid": "ok", "high": "ok"}, accept_tokens=(ANON_TOKEN, GOOD_TOKEN)) as fz:
            zv = zv_mod.ZvukSource(token=GOOD_TOKEN)
            url, used = zv.resolve_stream(TRACK, quality="320")
        self.assertEqual(used, "high")
        self.assertEqual(fz.qualities_tried(), ["high"])

    def test_audio_quality_numbers_map_correctly(self):
        self.assertEqual(zv_mod._QUALITY_MAP["256"], "high")
        self.assertEqual(zv_mod._QUALITY_MAP["320"], "high")
        self.assertEqual(zv_mod._QUALITY_MAP["flac"], "flac")


class TestExpiredToken(unittest.TestCase):
    def test_401_retries_anonymously_and_downloads_with_the_working_token(self):
        """Expired user token must not kill the download."""
        with FakeZvuk(rules={"mid": "ok", "high": "ok"},
                      accept_tokens=(ANON_TOKEN,)) as fz:
            zv = zv_mod.ZvukSource(token=USER_TOKEN)  # expired -> 401
            url, used = zv.resolve_stream(TRACK, quality="high")

        self.assertEqual(url, STREAM_URL, "must recover via the anonymous token")
        self.assertEqual(used, "high")
        # the anonymous retry must have been sent for the same quality
        self.assertEqual(fz.qualities_tried(), ["high", "high"])
        tokens = [r["token"] for r in fz.requests if r["path"].endswith("/track/stream")]
        self.assertEqual(tokens, [USER_TOKEN, ANON_TOKEN])
        # download must reuse the token that worked, not the expired one
        self.assertEqual(zv._stream_token, ANON_TOKEN)

    def test_expired_token_and_high_refused_still_lands_on_mid(self):
        with FakeZvuk(rules={"mid": "ok", "high": 403}, accept_tokens=(ANON_TOKEN,)) as fz:
            zv = zv_mod.ZvukSource(token=USER_TOKEN)
            url, used = zv.resolve_stream(TRACK, quality="high")
        self.assertEqual((url, used), (STREAM_URL, "mid"))
        self.assertEqual(fz.qualities_tried(), ["high", "high", "mid"])


class TestErrorMessages(unittest.TestCase):
    def test_expired_token_message_says_401_not_subscription(self):
        with FakeZvuk(rules={"mid": 403, "high": 403}, accept_tokens=(ANON_TOKEN,)):
            zv = zv_mod.ZvukSource(token=USER_TOKEN)
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        msg = str(ctx.exception)
        self.assertIn("401", msg, msg)
        self.assertIn("Токен Zvuk истёк", msg, msg)
        self.assertIn("zvuk.com/api/tiny/profile", msg, msg)
        self.assertIn("high (токен) → HTTP 401", msg, msg)

    def test_message_does_not_claim_no_token_when_a_token_is_set(self):
        """The old hardcoded text lied about this."""
        with FakeZvuk(rules={"mid": 403, "high": 403}, accept_tokens=(GOOD_TOKEN,)):
            zv = zv_mod.ZvukSource(token=GOOD_TOKEN)
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        msg = str(ctx.exception)
        self.assertIn("Токен задан.", msg, msg)
        self.assertNotIn("Без токена доступен только", msg, msg)
        self.assertIn("403", msg, msg)

    def test_no_token_message_states_that(self):
        with FakeZvuk(rules={"mid": 403, "high": 403}):
            zv = zv_mod.ZvukSource(token=None)
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        self.assertIn("Токен не задан", str(ctx.exception))

    def test_200_without_stream_field_is_diagnosable(self):
        with FakeZvuk(rules={"mid": "no_stream", "high": "no_stream"}):
            zv = zv_mod.ZvukSource(token=None)
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="mid")
        msg = str(ctx.exception)
        self.assertIn("нет поля 'stream'", msg, msg)
        self.assertIn("unexpected-shape", msg, msg)

    def test_bad_track_id(self):
        zv = zv_mod.ZvukSource(token=None)
        with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
            zv.resolve_stream("zvuk-not-a-number", quality="high")
        self.assertEqual(ctx.exception.detail.get("reason"), "bad_id")

    def test_zvuk_prefix_is_stripped(self):
        with FakeZvuk(rules={"mid": "ok"}) as fz:
            zv = zv_mod.ZvukSource(token=None)
            zv.resolve_stream("zvuk-" + TRACK, quality="mid")
        ids = [r["qs"].get("id") for r in fz.requests if r["path"].endswith("/track/stream")]
        self.assertEqual(ids, [TRACK])


class TestNetworkFailure(unittest.TestCase):
    def test_unreachable_api_is_reported_as_network_not_subscription(self):
        zv = zv_mod.ZvukSource(token=None)
        # port 1 on loopback: connection refused, no server
        zv_mod.TINY_URL = "http://127.0.0.1:1/api/tiny"
        try:
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        finally:
            zv_mod.TINY_URL = "https://zvuk.com/api/tiny"
        msg = str(ctx.exception)
        self.assertIn("недоступен", msg, msg)
        # names the concrete reason rather than a generic "сеть/прокси/DNS" list
        self.assertIn("нет соединения", msg, msg)
        self.assertNotIn("требует подписку", msg, msg)
        self.assertIn("Токен не задан", msg, msg)

    def test_network_error_message_stays_short_and_readable(self):
        """The raw requests blob must not be dumped into the log line."""
        zv = zv_mod.ZvukSource(token=None)
        zv_mod.TINY_URL = "http://127.0.0.1:1/api/tiny"
        try:
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        finally:
            zv_mod.TINY_URL = "https://zvuk.com/api/tiny"
        msg = str(ctx.exception)
        self.assertNotIn("HTTPConnectionPool", msg, msg)
        self.assertNotIn("Max retries exceeded", msg, msg)
        self.assertIn("нет соединения", msg, msg)
        self.assertLess(len(msg), 260, "log line too long: %d chars" % len(msg))
        # the full text must still be available for debugging
        raw = json.dumps(ctx.exception.detail, ensure_ascii=False, default=str)
        self.assertIn("HTTPConnectionPool", raw)

    def test_short_err_maps_common_failures(self):
        self.assertEqual(zv_mod._short_err("HTTPSConnectionPool(host='x'): Max retries exceeded"),
                         "нет соединения")
        self.assertEqual(zv_mod._short_err("Read timed out. (read timeout=8)"), "таймаут")
        self.assertEqual(zv_mod._short_err("[Errno -2] Name or service not known"),
                         "DNS не резолвится")
        self.assertEqual(zv_mod._short_err("ProxyError: tunnel connection failed"),
                         "прокси не отвечает")
        self.assertEqual(zv_mod._short_err(""), "ошибка")
        self.assertEqual(zv_mod._short_err(None), "ошибка")


class TestNoSecretLeak(unittest.TestCase):
    def test_token_never_appears_in_message_or_detail(self):
        with FakeZvuk(rules={"mid": 403, "high": 403}, accept_tokens=(ANON_TOKEN,)):
            zv = zv_mod.ZvukSource(token=USER_TOKEN)
            with self.assertRaises(zv_mod.ZvukStreamError) as ctx:
                zv.resolve_stream(TRACK, quality="high")
        blob = str(ctx.exception) + json.dumps(ctx.exception.detail, ensure_ascii=False, default=str)
        self.assertNotIn(USER_TOKEN, blob)
        self.assertNotIn(ANON_TOKEN, blob)


class TestCompatWrapper(unittest.TestCase):
    def test_get_stream_url_still_returns_none_on_failure(self):
        with FakeZvuk(rules={"mid": 403, "high": 403}):
            zv = zv_mod.ZvukSource(token=None)
            self.assertIsNone(zv.get_stream_url(TRACK, quality="high"))

    def test_get_stream_url_returns_url_on_success(self):
        with FakeZvuk(rules={"mid": "ok", "high": 403}):
            zv = zv_mod.ZvukSource(token=None)
            self.assertEqual(zv.get_stream_url(TRACK, quality="high"), STREAM_URL)


class TestDownloadUsesWorkingToken(unittest.TestCase):
    """download_audio() must fetch the file with the token that produced the
    stream URL — not with the user token that was already rejected (401).

    Without this the v0.3.2 fix would be cosmetic: resolve_stream() would
    recover via the anonymous token, then the actual download would fail again
    on the expired one.
    """

    AUDIO = b"ID3-fake-audio-payload" * 64

    def _server(self, valid_tokens):
        outer = self
        self.audio_tokens = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                tok = self.headers.get("X-Auth-Token")

                if parsed.path.endswith("/profile"):
                    return self._j(200, {"result": {"token": ANON_TOKEN}})

                if parsed.path.endswith("/track/stream"):
                    if tok is not None and tok not in valid_tokens:
                        return self._j(401, {"error": "unauthorized"})
                    if qs.get("quality") == "high":
                        return self._j(403, {"error": "forbidden"})
                    return self._j(200, {"stream": self._audio_url()})

                if parsed.path.endswith("/audio.bin"):
                    outer.audio_tokens.append(tok)
                    if tok is not None and tok not in valid_tokens:
                        return self._j(401, {"error": "unauthorized"})
                    body = outer.AUDIO
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                return self._j(404, {"error": "not found"})

            def _audio_url(self):
                return "http://127.0.0.1:%d/audio.bin" % self.server.server_address[1]

            def _j(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler

    def _run(self, token, valid_tokens):
        """Run download_audio() against the fake server; returns the ZvukSource."""
        import tempfile
        import shutil
        from unittest import mock

        socketserver.TCPServer.allow_reuse_address = True
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        httpd = socketserver.TCPServer(("127.0.0.1", port), self._server(valid_tokens))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        orig_tiny = zv_mod.TINY_URL
        zv_mod.TINY_URL = "http://127.0.0.1:%d/api/tiny" % port
        outdir = tempfile.mkdtemp(prefix="zvuk-dl-test-")
        target = os.path.join(outdir, "track")

        # ffmpeg is not required to prove which token was sent; stub the
        # conversion so the test exercises the download plumbing only.
        def fake_convert(src, out_no_ext, quality):
            with open(out_no_ext + ".mp3", "wb") as fh:
                fh.write(b"converted")

        zv = zv_mod.ZvukSource(token=token)
        try:
            with mock.patch.object(zv_mod, "_convert_to_mp3", fake_convert):
                self.result = zv.download_audio(TRACK, target, quality="320")
        finally:
            zv_mod.TINY_URL = orig_tiny
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(outdir, ignore_errors=True)
        return zv

    def test_expired_token_stream_and_audio_both_use_the_anonymous_token(self):
        zv = self._run(USER_TOKEN, valid_tokens=(ANON_TOKEN,))
        self.assertTrue(self.result.endswith(".mp3"), self.result)
        self.assertEqual(zv.last_quality, "mid")
        self.assertEqual(self.audio_tokens, [ANON_TOKEN],
                         "the audio must be fetched with the token that worked, "
                         "got: %r" % (self.audio_tokens,))
        self.assertNotIn(USER_TOKEN, self.audio_tokens)

    def test_valid_token_is_used_for_the_audio_too(self):
        zv = self._run(GOOD_TOKEN, valid_tokens=(ANON_TOKEN, GOOD_TOKEN))
        self.assertTrue(self.result.endswith(".mp3"), self.result)
        self.assertEqual(zv.last_quality, "mid")
        self.assertEqual(self.audio_tokens, [GOOD_TOKEN],
                         "a working user token must be kept for the download, "
                         "got: %r" % (self.audio_tokens,))

    def test_download_fails_loudly_when_the_audio_is_refused(self):
        """Guard: if the token plumbing regressed, this is what the user sees."""
        with self.assertRaises(Exception) as ctx:
            # nobody is allowed to fetch the audio
            self._run(USER_TOKEN, valid_tokens=())
        self.assertIn("401", str(ctx.exception), str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
