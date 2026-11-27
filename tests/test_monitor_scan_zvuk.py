"""Tests for the normal scan path in core/monitor.py (process_track).

This is the path that produced the log line the user reported:

    Zvuk failed for ... - ..., trying YouTube: <error>

so the <error> text is exactly what a user reads when a scan fails. It had no
coverage; before v0.3.2 it was a canned "требует подписку" string.

Run:  python tests/test_monitor_scan_zvuk.py
"""

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import monitor as mon  # noqa: E402
from core import zvuk as zv_mod  # noqa: E402

EXPIRED_MSG = (
    'Токен Zvuk истёк или неверен (HTTP 401). Обновите его: войдите на zvuk.com, '
    'откройте https://zvuk.com/api/tiny/profile, скопируйте значение после "token": '
    'и вставьте в поле «Zvuk токен». Пробовал: high (токен) → HTTP 401; '
    'high (аноним) → HTTP 403; mid (аноним) → HTTP 403. Токен задан.'
)

TRACK = {"name": "Blood Money", "album_name": "Test Album",
         "id": "zvuk-3862635281", "duration_ms": 200}


class FailingZvuk:
    def __init__(self, token=None, proxy=None):
        self.token = token
        self.last_quality = None

    def download_audio(self, track_id, out_no_ext, quality="320"):
        raise zv_mod.ZvukStreamError(EXPIRED_MSG)


class OkZvuk:
    def __init__(self, token=None, proxy=None):
        self.token = token
        self.last_quality = "high"

    def download_audio(self, track_id, out_no_ext, quality="320"):
        return out_no_ext + ".mp3"


class TestScanZvukPath(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        self.infos = []
        self.errors = []
        self.thread_states = []
        self.search_calls = []
        self.library_calls = []

        def fake_search(query, limit=15, cfg=None):
            self.search_calls.append(query)
            return []

        def fake_lib_status(*a, **k):
            self.library_calls.append(k)

        self._patches = [
            mock.patch.object(mon.db_mod, "add_track", lambda *a, **k: None),
            mock.patch.object(mon.status, "inc", lambda *a, **k: None),
            mock.patch("core.library.update_track_status", fake_lib_status),
            mock.patch.object(mon.yt_mod, "search_youtube", fake_search),
            mock.patch.object(mon, "_stopped", lambda: False),
            mock.patch.object(mon, "set_thread_state",
                              lambda wid, st, task: self.thread_states.append((st, task))),
            mock.patch.object(mon.status.log, "warning",
                              lambda fmt, *a, **k: self.warnings.append(fmt % a if a else fmt)),
            mock.patch.object(mon.status.log, "info",
                              lambda fmt, *a, **k: self.infos.append(fmt % a if a else fmt)),
            mock.patch.object(mon.status.log, "error",
                              lambda fmt, *a, **k: self.errors.append(fmt % a if a else fmt)),
            mock.patch.object(mon, "_build_folder", lambda *a, **k: "/tmp/scan-test"),
            mock.patch("os.makedirs", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    CFG = {"zvuk_token": "tok", "proxy": "", "blacklist": [],
           "duration_tolerance_sec": 15, "fallback_to_closest": False}

    def _run(self, zvuk_cls, token="tok"):
        cfg = dict(self.CFG)
        cfg["zvuk_token"] = token
        with mock.patch.object(zv_mod, "ZvukSource", zvuk_cls):
            mon.process_track(TRACK, "Test Artist", cfg, 1)

    def test_scan_log_names_the_expired_token_not_a_canned_reason(self):
        self._run(FailingZvuk)

        self.assertTrue(self.warnings, "the Zvuk failure must be logged")
        line = self.warnings[0]
        self.assertTrue(line.startswith("Zvuk failed for Test Artist - Blood Money"), line)
        # the honest reason is in the line the user reads
        self.assertIn("истёк", line, line)
        self.assertIn("401", line, line)
        self.assertIn("zvuk.com/api/tiny/profile", line, line)
        # the old lie must not come back
        self.assertNotIn("требует подписк", line, line)
        self.assertNotIn("подписка не оформлена", line, line)

    def test_scan_falls_back_to_youtube_after_a_zvuk_failure(self):
        self._run(FailingZvuk)

        self.assertEqual(self.search_calls, ["Test Artist Blood Money"],
                         "a zvuk- track must still try YouTube by name")
        self.assertTrue(any("YouTube (после Zvuk)" in t for _, t in self.thread_states),
                        self.thread_states)

    def test_scan_success_skips_youtube_entirely(self):
        self._run(OkZvuk)

        self.assertEqual(self.search_calls, [],
                         "a successful Zvuk download must not search YouTube")
        self.assertTrue(any("Downloaded (Zvuk)" in m for m in self.infos), self.infos)
        self.assertEqual(self.thread_states[-1], ("idle", "✅ Готово: Blood Money"),
                         self.thread_states[-1])
        self.assertEqual(self.warnings, [],
                         "the requested quality was obtained -> no warning: %r"
                         % (self.warnings,))

    def test_scan_logs_a_downgrade_when_mid_is_obtained_instead_of_high(self):
        class MidZvuk(OkZvuk):
            def __init__(self, token=None, proxy=None):
                OkZvuk.__init__(self, token, proxy)
                self.last_quality = "mid"

        self._run(MidZvuk)
        self.assertTrue(self.warnings, "a silent quality downgrade must be logged")
        w = self.warnings[0]
        self.assertIn("mid", w, w)
        self.assertIn("high", w, w)
        self.assertIn("Blood Money", w, w)

    def test_scan_success_marks_the_track_downloaded(self):
        self._run(OkZvuk)

        self.assertTrue(self.library_calls, "library status must be updated")
        self.assertTrue(any(c.get("downloaded") is True for c in self.library_calls),
                        self.library_calls)

    def test_thread_is_released_on_both_outcomes(self):
        self._run(FailingZvuk)
        self.assertEqual(self.thread_states[-1][0], "idle", self.thread_states[-1])

        self.thread_states.clear()
        self._run(OkZvuk)
        self.assertEqual(self.thread_states[-1][0], "idle", self.thread_states[-1])

    def test_zvuk_track_id_never_uses_the_youtube_downloader_directly(self):
        self.assertEqual(mon._downloader_for("zvuk-3862635281", {"downloader": "youtube"},
                                             "Anyone"), "zvuk",
                         "a zvuk- id must not be routed to youtube by config")

    def test_stop_request_short_circuits_before_any_download(self):
        with mock.patch.object(mon, "_stopped", lambda: True):
            mon.process_track(TRACK, "Test Artist", dict(self.CFG), 1)
        self.assertEqual(self.thread_states, [("idle", "Остановлено")], self.thread_states)
        self.assertEqual(self.warnings, [])
        self.assertEqual(self.infos, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
