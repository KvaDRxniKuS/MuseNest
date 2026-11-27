"""Tests for the YouTube request router (anti rate-limit) in core/youtube.py.

YouTube rate-limited a real session for up to an hour:
    [youtube] hXs2lJpiR0I: This content isn't available, try again later.
    ... It is recommended to use `-t sleep` ...

The old "_rate_limit" slept *while holding its lock*, so the 8 scan threads
took turns at the gate but then ran their yt-dlp sessions fully concurrently —
and searches shared one timeline with downloads, so a busy download queue
stalled every search. The router now keeps the user's model:

* searches run concurrently, with a light start staggering (search_delay_sec);
* downloads are queued — download_max_parallel exclusive slots (default 1,
  strictly one by one) plus a minimum spacing between starts
  (download_delay_sec, default 3.5 s) + jitter;
* the two timelines are independent.

Everything runs offline: yt_dlp.YoutubeDL is replaced with a probe.

Run:  python tests/test_download_router.py
"""

import os
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import youtube as yt_mod  # noqa: E402
from core import config as cfg_mod  # noqa: E402
from core import status  # noqa: E402


class _Probe:
    """Fake YoutubeDL: records start moments and peak concurrency."""

    def __init__(self, lock):
        self._lock = lock
        self.times = []
        self.active = 0
        self.peak = 0
        self.delay = 0.05
        self.fail_first = False

    def record(self):
        with self._lock:
            self.times.append(time.monotonic())
            self.active += 1
            self.peak = max(self.peak, self.active)

    def done(self):
        delay = self.delay
        time.sleep(delay)   # the download itself: still counts as active
        with self._lock:
            self.active -= 1

    def as_ydl(self):
        probe = self

        class _FakeYdl:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, urls):
                probe.record()
                if probe.fail_first and len(probe.times) == 1:
                    raise RuntimeError("boom once")
                probe.done()

            def extract_info(self, query, download=False):
                probe.record()
                probe.done()
                return {"entries": [{"id": "v1", "title": "T", "duration": 100,
                                     "webpage_url": "https://www.youtube.com/watch?v=v1"}]}

        return _FakeYdl


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        self.lock = threading.Lock()
        self.probe = _Probe(self.lock)
        self._saved_ydl = yt_mod.yt_dlp.YoutubeDL
        yt_mod.yt_dlp.YoutubeDL = self.probe.as_ydl()
        self._saved_stop = status.status.get("stop_requested", False)
        status.status["stop_requested"] = False
        self._saved_gates = (yt_mod._DOWNLOAD_GATE, yt_mod._SEARCH_GATE)
        yt_mod._DOWNLOAD_GATE = _NoJitterGate()
        yt_mod._SEARCH_GATE = _NoJitterGate()
        self._reset_router()

    def tearDown(self):
        yt_mod.yt_dlp.YoutubeDL = self._saved_ydl
        yt_mod._DOWNLOAD_GATE, yt_mod._SEARCH_GATE = self._saved_gates
        status.status["stop_requested"] = self._saved_stop
        self._reset_router()

    @staticmethod
    def _reset_router():
        with yt_mod._DL_COND:
            yt_mod._DL_ACTIVE = 0
            yt_mod._DL_LIMIT = 1

    def _run_parallel(self, fn, n):
        errs = []

        def _one():
            try:
                fn()
            except Exception as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=_one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])
        return threads


class _NoJitterGate(yt_mod._StartSlotGate):
    """Gate without the anti-pattern jitter, so tests get exact timing."""

    def reserve(self, interval, jitter=None):
        return super().reserve(interval, jitter=(0.0, 0.0))


class DownloadsGoOneByOne(RouterTestBase):
    def test_single_slot_serializes_downloads(self):
        cfg = {"download_max_parallel": 1, "download_delay_sec": 0.05}

        self._run_parallel(lambda: yt_mod.download_audio(
            "https://www.youtube.com/watch?v=x", "/tmp/out", cfg=cfg), 4)

        self.assertEqual(len(self.probe.times), 4, "every download ran")
        self.assertEqual(self.probe.peak, 1,
                         "download_max_parallel=1 must never overlap downloads")
        gaps = [b - a for a, b in zip(sorted(self.probe.times),
                                      sorted(self.probe.times)[1:])]
        self.assertTrue(all(g >= 0.05 for g in gaps),
                        f"starts must be spaced by download_delay_sec: {gaps}")

    def test_two_slots_allow_partial_overlap(self):
        cfg = {"download_max_parallel": 2, "download_delay_sec": 0.02}
        self.probe.delay = 0.1

        self._run_parallel(lambda: yt_mod.download_audio(
            "https://www.youtube.com/watch?v=x", "/tmp/out", cfg=cfg), 4)

        self.assertEqual(len(self.probe.times), 4)
        self.assertEqual(self.probe.peak, 2,
                         "with download_max_parallel=2 two downloads may overlap, "
                         "never more")

    def test_retry_reserves_a_fresh_slot(self):
        cfg = {"download_max_parallel": 1, "download_delay_sec": 0.02}
        self.probe.fail_first = True

        yt_mod.download_audio("https://www.youtube.com/watch?v=x", "/tmp/out", cfg=cfg)

        self.assertEqual(len(self.probe.times), 2,
                         "a failed attempt is retried through the router again")


class SearchStaysConcurrent(RouterTestBase):
    def test_searches_overlap_and_are_only_lightly_staggered(self):
        cfg = {"search_delay_sec": 0.02}
        self.probe.delay = 0.1

        self._run_parallel(lambda: yt_mod.search_youtube("query", cfg=cfg), 3)

        self.assertEqual(len(self.probe.times), 3, "every search ran")
        self.assertGreaterEqual(self.probe.peak, 2,
                                "searches must stay concurrent")

    def test_a_busy_download_queue_does_not_block_searches(self):
        # A download holds the only slot for a while; a search must not wait.
        cfg = {"download_max_parallel": 1, "download_delay_sec": 0.05}
        self.probe.delay = 0.5          # the holder keeps its slot for 0.5 s
        holder = threading.Thread(target=yt_mod.download_audio,
                                  kwargs={"url": "https://www.youtube.com/watch?v=h",
                                          "output_path_no_ext": "/tmp/out", "cfg": cfg})
        holder.start()
        time.sleep(0.05)                # let the holder grab the slot
        self.probe.delay = 0.05         # the search itself is quick

        t0 = time.monotonic()
        yt_mod.search_youtube("query", cfg=cfg)
        elapsed = time.monotonic() - t0

        holder.join(timeout=10)
        self.assertLess(elapsed, 0.4,
                        "a queued download must not stall the search timeline")


class StopReleasesTheQueue(RouterTestBase):
    def test_queued_downloader_exits_without_running_when_stopped(self):
        cfg = {"download_max_parallel": 1, "download_delay_sec": 0.05}
        self.probe.delay = 0.5
        holder = threading.Thread(target=yt_mod.download_audio,
                                  kwargs={"url": "https://www.youtube.com/watch?v=h",
                                          "output_path_no_ext": "/tmp/out", "cfg": cfg})
        holder.start()
        time.sleep(0.05)   # holder owns the slot now
        status.status["stop_requested"] = True

        t0 = time.monotonic()
        yt_mod.download_audio("https://www.youtube.com/watch?v=x",
                              "/tmp/out2", cfg=cfg)
        elapsed = time.monotonic() - t0

        holder.join(timeout=10)
        status.status["stop_requested"] = False
        self.assertEqual(len(self.probe.times), 1,
                         "the queued download must never start")
        self.assertLess(elapsed, 0.4, "stop must release the waiter quickly")


class GateMath(unittest.TestCase):
    def test_reserved_starts_are_strictly_spaced(self):
        gate = yt_mod._StartSlotGate()
        starts = [gate.reserve(0.5, jitter=(0.0, 0.0)) for _ in range(5)]
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(abs(g - 0.5) < 1e-6 for g in gaps),
                         f"contended reserves must be exactly one interval apart: {gaps}")

    def test_an_idle_gate_starts_right_away(self):
        gate = yt_mod._StartSlotGate()
        gate.reserve(10.0, jitter=(0.0, 0.0))   # leaves next_free far in the future
        time.sleep(0.01)
        # An idle timeline (nothing reserved recently) must not inherit a debt.
        fresh = yt_mod._StartSlotGate()
        start = fresh.reserve(0.1, jitter=(0.0, 0.0))
        self.assertLessEqual(start - time.time(), 0.1)


class RouterConfigSanitized(unittest.TestCase):
    def test_defaults(self):
        c = cfg_mod.sanitize_config({})
        self.assertEqual(c["download_max_parallel"], 1)
        self.assertEqual(c["download_delay_sec"], 3.5)
        self.assertEqual(c["search_delay_sec"], 1.0)

    def test_string_values_are_coerced_and_bounded(self):
        c = cfg_mod.sanitize_config({"download_max_parallel": "3",
                                     "download_delay_sec": "5",
                                     "search_delay_sec": "0"})
        self.assertEqual(c["download_max_parallel"], 3)
        self.assertEqual(c["download_delay_sec"], 5.0)
        self.assertEqual(c["search_delay_sec"], 0.0)

    def test_out_of_range_values_fall_back_to_defaults(self):
        c = cfg_mod.sanitize_config({"download_max_parallel": "99",
                                     "download_delay_sec": "-4",
                                     "search_delay_sec": "1000"})
        self.assertEqual(c["download_max_parallel"], 1)
        self.assertEqual(c["download_delay_sec"], 3.5)
        self.assertEqual(c["search_delay_sec"], 1.0)


class AnsiCodesAreStripped(unittest.TestCase):
    def test_error_messages_lose_their_colors(self):
        raw = "\x1b[0;31mERROR:\x1b[0m [youtube] hXs2lJpiR0I: rate-limited"
        clean = yt_mod._clean_ansi(raw)
        self.assertEqual(clean, "ERROR: [youtube] hXs2lJpiR0I: rate-limited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
