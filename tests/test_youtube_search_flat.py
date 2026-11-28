"""The global YouTube search must not come back empty for tracks that exist.

The old search resolved EVERY result's full page (extract_flat=False, 20+
requests per search) — exactly where YouTube's bot-check turned entries into
None, so a search for an existing track reported «не найдено результатов».
The search is now FLAT (one page request); titles/urls are enough for the
matcher, the download fetches the full page anyway.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import youtube as yt_mod  # noqa: E402
from core import matcher as mat_mod  # noqa: E402

QUERY = "Три дня дождя Не стой на ветру"


class _FakeSearchYdl:
    """Captures the opts each search ran with; a scripted outcome per URL."""
    opts_history = []
    scripts = {}

    def __init__(self, opts):
        self._opts = dict(opts)
        _FakeSearchYdl.opts_history.append(self._opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        out = _FakeSearchYdl.scripts[url].pop(0)
        if isinstance(out, Exception):
            raise out
        return out


FLAT_RESULTS = {"entries": [
    {"id": "vid_hit", "title": "Три дня дождя — Не стой на ветру (official)",
     "url": "https://www.youtube.com/watch?v=vid_hit", "duration": 187},
    {"id": "vid_live", "title": "Три дня дождя — Не стой на ветру (Live)",
     "url": "https://www.youtube.com/watch?v=vid_live", "duration": None},
    {"id": None, "title": "eaten by the bot-check", "url": "https://x"},
]}


class YoutubeSearchFlat(unittest.TestCase):

    def setUp(self):
        self._saved_ydl = yt_mod.yt_dlp.YoutubeDL
        self._saved_sleep = yt_mod._interruptible_sleep
        yt_mod.yt_dlp.YoutubeDL = _FakeSearchYdl
        yt_mod._interruptible_sleep = lambda seconds: False   # retries: instant
        _FakeSearchYdl.opts_history = []
        _FakeSearchYdl.scripts = {}

    def tearDown(self):
        yt_mod.yt_dlp.YoutubeDL = self._saved_ydl
        yt_mod._interruptible_sleep = self._saved_sleep

    def _cfg(self):
        return {"search_delay_sec": 0.01}   # keep the gate instant in tests

    def test_the_search_is_flat(self):
        _FakeSearchYdl.scripts = {"ytsearch5:" + QUERY: [{"entries": []}]}
        yt_mod.search_youtube(QUERY, limit=5, cfg=self._cfg())

        self.assertTrue(_FakeSearchYdl.opts_history[-1]["extract_flat"] is True,
                        "search must not fully-resolve every result page")

    def test_flat_entries_are_shaped_like_full_ones(self):
        _FakeSearchYdl.scripts = {"ytsearch5:" + QUERY: [FLAT_RESULTS]}

        res = yt_mod.search_youtube(QUERY, limit=5, cfg=self._cfg())

        self.assertEqual(len(res), 2, "the None entry is dropped")
        hit = res[0]
        self.assertEqual(hit["id"], "vid_hit")
        self.assertEqual(hit["duration"], 187)
        self.assertEqual(hit["url"],
                         "https://www.youtube.com/watch?v=vid_hit")
        unknown = res[1]
        self.assertEqual(unknown["duration"], 0,
                         "a missing flat duration becomes 0 (unknown)")

    def test_eaten_entries_cannot_crash_the_search(self):
        _FakeSearchYdl.scripts = {
            "ytsearch5:" + QUERY: [{"entries": [None, None, {}]}]}

        res = yt_mod.search_youtube(QUERY, limit=5, cfg=self._cfg())

        self.assertEqual(res, [], "empty is empty — but no exception")

    def test_a_failed_attempt_is_retried(self):
        _FakeSearchYdl.scripts = {"ytsearch5:" + QUERY: [
            RuntimeError("Sign in to confirm you're not a bot"),
            FLAT_RESULTS,
        ]}

        res = yt_mod.search_youtube(QUERY, limit=5, cfg=self._cfg())

        self.assertEqual([r["id"] for r in res], ["vid_hit", "vid_live"])

    def test_the_matcher_consumes_flat_results(self):
        _FakeSearchYdl.scripts = {"ytsearch5:" + QUERY: [FLAT_RESULTS]}
        res = yt_mod.search_youtube(QUERY, limit=5, cfg=self._cfg())

        best, err = mat_mod.find_best_match(
            res, 187000, "Не стой на ветру", "Три дня дождя",
            [], 15, True)

        self.assertIsNone(err)
        self.assertEqual(best["id"], "vid_hit",
                         "the exact-title flat hit wins")


if __name__ == "__main__":
    unittest.main()
