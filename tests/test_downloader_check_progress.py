"""Tests for the downloader self-check progress reporting (core/youtube.py).

These run fully offline: a throwaway local HTTP server stands in for YouTube, so
the *real* ``downloader_check()`` -> yt-dlp -> ``progress_hooks`` path is
exercised and we can assert that progress actually advances.

Run with:  python tests/test_downloader_check_progress.py
"""

import http.server
import os
import socket
import socketserver
import sys
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import youtube as yt_mod  # noqa: E402

PAYLOAD = b"MUSENEST-PROBE-" * 20000  # ~300 KB so download progress is measurable
FILENAME = "probe.mp3"


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAYLOAD
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *args):
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LocalServerFixture:
    def __enter__(self):
        self.port = _free_port()
        self.httpd = socketserver.TCPServer(("127.0.0.1", self.port), _QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return "http://127.0.0.1:%d/%s" % (self.port, FILENAME)

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


class TestProgressHelper(unittest.TestCase):
    def test_clamps_and_passes_payload(self):
        got = []
        yt_mod._progress(got.append, "download", "Скачивание", 150, "msg")
        self.assertEqual(got[0]["stage_key"], "download")
        self.assertEqual(got[0]["percent"], 100.0, "percent must be clamped to 100")
        self.assertEqual(got[0]["message"], "msg")

    def test_none_percent_stays_none(self):
        got = []
        yt_mod._progress(got.append, "extract", "Извлечение", None, "")
        self.assertIsNone(got[0]["percent"])

    def test_bad_callback_does_not_break_check(self):
        def boom(_):
            raise RuntimeError("callback exploded")

        # must not raise
        yt_mod._progress(boom, "prepare", "Подготовка", 1, "")

    def test_no_callback_is_noop(self):
        self.assertIsNone(yt_mod._progress(None, "prepare", "Подготовка", 1, ""))

    def test_fmt_bytes(self):
        self.assertEqual(yt_mod._fmt_bytes(512), "512 Б")
        self.assertEqual(yt_mod._fmt_bytes(2048), "2.0 КБ")
        self.assertEqual(yt_mod._fmt_bytes(5 * 1024 * 1024), "5.0 МБ")
        self.assertEqual(yt_mod._fmt_bytes(None), "?")


class TestLiveProgress(unittest.TestCase):
    """Real downloader_check() against a local HTTP source."""

    def setUp(self):
        self.events = []
        self.lock = threading.Lock()

    def _collect(self, payload):
        with self.lock:
            self.events.append(dict(payload))

    def stages(self):
        with self.lock:
            return [e["stage_key"] for e in self.events]

    def percents(self, key):
        with self.lock:
            return [e["percent"] for e in self.events
                    if e["stage_key"] == key and e["percent"] is not None]

    def test_youtube_check_reports_advancing_progress(self):
        with LocalServerFixture() as url:
            info = yt_mod.downloader_check(
                {"downloader": "youtube"}, test_url=url, timeout=40,
                on_progress=self._collect)

        stages = self.stages()
        self.assertIn("prepare", stages, stages)
        self.assertIn("extract", stages, stages)
        self.assertIn("download", stages, "download stage must be reported via progress_hooks")
        self.assertEqual(stages[-1], "done" if info["ok"] else "failed", stages)

        dl = self.percents("download")
        self.assertTrue(dl, "download progress must carry percentages")
        self.assertGreater(max(dl), 0.0, "download percentage must advance past 0")
        self.assertLessEqual(max(dl), 85.0, "download maps into the 25-85% band")
        self.assertEqual(max(dl), dl[-1] if dl[-1] == max(dl) else max(dl))

        # percent must never go backwards inside the download stage
        for a, b in zip(dl, dl[1:]):
            self.assertLessEqual(a, b + 0.001, "progress went backwards: %s" % dl)

        # prepare/extract precede download
        self.assertLess(stages.index("prepare"), stages.index("download"))
        self.assertLess(stages.index("extract"), stages.index("download"))

        # every event has the fields the UI renders
        with self.lock:
            for e in self.events:
                self.assertTrue(isinstance(e["stage_key"], str) and e["stage_key"],
                                "stage_key must be a non-empty string")
                self.assertTrue(e["stage"], "stage label must not be empty")
                self.assertIn("percent", e)
                self.assertIn("message", e)

    def test_result_shape_unchanged(self):
        """The returned dict must keep the fields the existing UI renders."""
        with LocalServerFixture() as url:
            info = yt_mod.downloader_check({"downloader": "youtube"}, test_url=url,
                                           timeout=40, on_progress=self._collect)
        for key in ("ok", "mode", "yt_dlp_version", "ffmpeg", "cookie_source", "test"):
            self.assertIn(key, info)
        self.assertEqual(info["mode"], "youtube")
        self.assertIn("ok", info["test"])
        self.assertIn("message", info["test"])

    def test_zvuk_check_reports_progress_without_network(self):
        """Zvuk path must also emit stages (it fails offline, but not silently)."""
        events = []
        info = yt_mod.downloader_check({"downloader": "zvuk", "proxy": "socks5://127.0.0.1:1099"},
                                       timeout=20, on_progress=lambda p: events.append(p))
        keys = [e["stage_key"] for e in events]
        self.assertIn("prepare", keys)
        self.assertIn("api", keys)
        self.assertEqual(keys[-1], "done" if info["ok"] else "failed", keys)
        self.assertEqual(info["mode"], "zvuk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
