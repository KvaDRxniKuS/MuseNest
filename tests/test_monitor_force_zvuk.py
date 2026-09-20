"""Tests for the force-download path in core/monitor.py (_force_one).

This is the path that puts a Zvuk failure in front of the user: when a `zvuk-`
track cannot be fetched, the result dict carries the error text into the UI and
the worker thread label. Before the v0.3.2/0.3.3 fixes that text was a canned
"требует подписку" string regardless of what actually happened.

Run:  python tests/test_monitor_force_zvuk.py
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

# the honest message core/zvuk.py produces for an expired token
EXPIRED_MSG = (
    'Токен Zvuk истёк или неверен (HTTP 401). Обновите его: войдите на zvuk.com, '
    'откройте https://zvuk.com/api/tiny/profile, скопируйте значение после "token": '
    'и вставьте в поле «Zvuk токен». Пробовал: high (токен) → HTTP 401. Токен задан.'
)

TM = {"artist": "Test Artist", "album": "Test Album",
      "track_name": "Blood Money", "track_id": "zvuk-3862635281",
      "duration_ms": 200}


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


class TestForceOneZvuk(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        self.errors = []
        self.thread_states = []
        self.search_calls = []

        def fake_search(query, limit=20, cfg=None):
            self.search_calls.append(query)
            return []  # YouTube finds nothing -> the Zvuk reason is what's left

        self._patches = [
            mock.patch.object(mon.db_mod, "add_track", lambda *a, **k: None),
            mock.patch.object(mon.status, "inc", lambda *a, **k: None),
            mock.patch("core.library.update_track_status", lambda *a, **k: None),
            mock.patch.object(mon.yt_mod, "search_youtube", fake_search),
            mock.patch.object(mon, "_stopped", lambda: False),
            mock.patch.object(mon, "set_thread_state",
                              lambda wid, st, task: self.thread_states.append((st, task))),
            mock.patch.object(mon.status.log, "warning",
                              lambda fmt, *a, **k: self.warnings.append(fmt % a if a else fmt)),
            mock.patch.object(mon.status.log, "error",
                              lambda fmt, *a, **k: self.errors.append(fmt % a if a else fmt)),
            mock.patch.object(mon, "_build_folder", lambda *a, **k: "/tmp/force-test"),
            mock.patch("os.makedirs", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_zvuk_failure_reaches_the_user_verbatim(self):
        with mock.patch.object(zv_mod, "ZvukSource", FailingZvuk):
            res = mon._force_one(TM, {"zvuk_token": "tok", "proxy": ""}, 1)

        self.assertFalse(res["ok"])
        # the real reason must survive into the message the UI shows
        self.assertIn("истёк", res["message"], res["message"])
        self.assertIn("401", res["message"], res["message"])
        self.assertIn("Zvuk:", res["message"], res["message"])
        # and the canned lie must not be there
        self.assertNotIn("требует подписк", res["message"], res["message"])

    def test_zvuk_failure_is_logged_with_the_real_reason(self):
        with mock.patch.object(zv_mod, "ZvukSource", FailingZvuk):
            mon._force_one(TM, {"zvuk_token": "tok", "proxy": ""}, 1)

        self.assertTrue(self.warnings, "the fallback to YouTube must be logged")
        w = self.warnings[0]
        self.assertIn("истёк", w, w)
        self.assertIn("Test Artist", w, w)
        self.assertIn("Blood Money", w, w)

    def test_worker_thread_is_never_left_busy(self):
        with mock.patch.object(zv_mod, "ZvukSource", FailingZvuk):
            mon._force_one(TM, {"zvuk_token": "tok", "proxy": ""}, 1)

        self.assertTrue(self.thread_states, "thread state must be set")
        self.assertEqual(self.thread_states[-1][0], "idle",
                         "the last state must release the worker")
        self.assertIn("❌", self.thread_states[-1][1], self.thread_states[-1][1])

    def test_zvuk_success_does_not_touch_youtube(self):
        with mock.patch.object(zv_mod, "ZvukSource", OkZvuk):
            res = mon._force_one(TM, {"zvuk_token": "tok", "proxy": ""}, 1)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["message"], "Скачано (Zvuk)")
        self.assertEqual(self.search_calls, [],
                         "a successful Zvuk download must not search YouTube")
        self.assertEqual(self.thread_states[-1][0], "idle")
        self.assertIn("✅", self.thread_states[-1][1])

    def test_zvuk_track_id_selects_the_zvuk_downloader(self):
        self.assertEqual(mon._downloader_for("zvuk-3862635281", {}, "Anyone"), "zvuk",
                         "a zvuk- id can only be served by Zvuk")
        self.assertEqual(mon._downloader_for("3862635281", {"downloader": "youtube"}, "Anyone"),
                         "youtube")

    def test_unexpected_error_is_caught_and_reported(self):
        class BoomZvuk:
            def __init__(self, token=None, proxy=None):
                pass

            def download_audio(self, *a, **k):
                raise RuntimeError("boom")

        with mock.patch.object(zv_mod, "ZvukSource", BoomZvuk):
            res = mon._force_one(TM, {"zvuk_token": "tok", "proxy": ""}, 1)

        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "ERR-1", res)
        self.assertEqual(self.thread_states[-1][0], "idle",
                         "even a blow-up must release the worker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
