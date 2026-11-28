"""One unavailable video must not kill the download.

YouTube refuses particular videos ("This video is not available" — region,
embed or takedown blocks) while the TRACK itself exists. The matcher now
returns the whole ranked pool, and the downloader falls through to the next
candidate (up to 3) before giving up with ERR-4.
"""
import os
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import monitor as mon  # noqa: E402
from core import matcher as mat  # noqa: E402
from core import covers as cov_mod  # noqa: E402
from core import status  # noqa: E402

DEAD = {"id": "vid_dead",
        "title": "Три дня дождя — Привычка (official audio)",
        "duration": 180,
        "webpage_url": "https://www.youtube.com/watch?v=vid_dead",
        "url": "https://www.youtube.com/watch?v=vid_dead"}
ALIVE = {"id": "vid_alive",
         "title": "Три дня дождя — Привычка",
         "duration": 182,
         "webpage_url": "https://www.youtube.com/watch?v=vid_alive",
         "url": "https://www.youtube.com/watch?v=vid_alive"}
OTHER = {"id": "vid_other",
         "title": "Не то видео совсем",
         "duration": 180,
         "webpage_url": "https://www.youtube.com/watch?v=vid_other",
         "url": "https://www.youtube.com/watch?v=vid_other"}

NOT_AVAILABLE = "[youtube] vid_dead: This video is not available"


class RankMatches(unittest.TestCase):

    def test_the_pool_is_ordered_by_duration_proximity(self):
        pool, err = mat.rank_matches(
            [OTHER, ALIVE, DEAD], 180000, "Привычка", "Три дня дождя",
            [], 15, True)

        self.assertIsNone(err)
        self.assertEqual([r["id"] for r in pool], ["vid_dead", "vid_alive"],
                         "180 s is closer than 182; 'other' is out of the name pool")

    def test_find_best_match_picks_the_head_of_the_ranked_pool(self):
        best, err = mat.find_best_match(
            [ALIVE, DEAD], 182000, "Привычка", "Три дня дождя", [], 15, True)

        self.assertIsNone(err)
        self.assertEqual(best["id"], "vid_alive")


def _mp3_writer(path):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 5)


class ProcessTrackRotation(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"blacklist": [], "duration_tolerance_sec": 15,
                    "fallback_to_closest": True, "save_folder": self.dir,
                    "audio_quality": "320", "artists": [],
                    "download_threads": 1}
        self.track = {"name": "Привычка", "id": "sp-1",
                      "album_name": "Альбом", "duration_ms": 180000}
        self.added = []
        self.updates = []
        self._patches = [
            mock.patch.object(mon, "_stopped", lambda: False),
            mock.patch.object(mon, "_downloader_for",
                              lambda tid, cfg, name: "youtube"),
            mock.patch.object(mon, "_artist_channels",
                              lambda a, cfg: []),
            mock.patch.object(mon.db_mod, "add_track",
                              lambda *a, **k: self.added.append(a)),
            mock.patch.object(cov_mod, "get_cover_url",
                              lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self._update_patch = mock.patch(
            "core.library.update_track_status",
            side_effect=lambda *a, **k: self.updates.append((a, k)))
        self._update_patch.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._update_patch.stop()

    def _search(self, results):
        return mock.patch.object(mon.yt_mod, "search_youtube",
                                 lambda q, limit=15, cfg=None: results)

    def test_an_unavailable_video_falls_through_to_the_next_candidate(self):
        calls = []

        def _dl(url, out_no_ext, quality="320", progress_hook=None, cfg=None):
            calls.append(url)
            if "vid_dead" in url:
                raise RuntimeError(NOT_AVAILABLE)
            _mp3_writer(out_no_ext + ".mp3")

        with self._search([DEAD, ALIVE]), \
             mock.patch.object(mon.yt_mod, "download_audio", _dl):
            mon.process_track(self.track, "Три дня дождя", self.cfg, 1)

        self.assertEqual(calls, [DEAD["url"], ALIVE["url"]],
                         "the dead candidate is tried first, then skipped")
        self.assertEqual(self.added[0][3], "vid_alive",
                         "the ALIVE video is the one registered in the db")
        self.assertTrue(any(k.get("downloaded") is True
                            for _a, k in self.updates),
                        "the track is marked downloaded")

    def test_all_candidates_dead_means_ERR4(self):
        calls = []

        def _dl(url, out_no_ext, quality="320", progress_hook=None, cfg=None):
            calls.append(url)
            raise RuntimeError(NOT_AVAILABLE)

        with self._search([DEAD, ALIVE]), \
             mock.patch.object(mon.yt_mod, "download_audio", _dl):
            mon.process_track(self.track, "Три дня дождя", self.cfg, 1)

        self.assertEqual(len(calls), 2, "both candidates are tried")
        self.assertEqual(self.added, [], "nothing is registered")
        self.assertTrue(any(k.get("error_code") == "ERR-4"
                            for _a, k in self.updates),
                        "ERR-4 after every candidate failed")


class ForceRotation(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"blacklist": [], "duration_tolerance_sec": 15,
                    "fallback_to_closest": True, "save_folder": self.dir,
                    "audio_quality": "320", "artists": []}
        self.tm = {"artist": "Три дня дождя", "album": "Альбом",
                   "track_name": "Привычка", "track_id": "sp-9",
                   "duration_ms": 180000}
        self.added = []
        self._patches = [
            mock.patch.object(mon, "_stopped", lambda: False),
            mock.patch.object(mon, "_downloader_for",
                              lambda tid, cfg, name: "youtube"),
            mock.patch.object(mon, "_artist_channels", lambda a, cfg: []),
            mock.patch.object(mon.db_mod, "add_track",
                              lambda *a, **k: self.added.append(a)),
            mock.patch.object(cov_mod, "get_cover_url",
                              lambda *a, **k: None),
            mock.patch.object(mon.yt_mod, "search_youtube",
                              lambda q, limit=20, cfg=None: [DEAD, ALIVE]),
        ]
        for p in self._patches:
            p.start()
        self._update_patch = mock.patch(
            "core.library.update_track_status", lambda *a, **k: None)
        self._update_patch.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._update_patch.stop()

    def test_force_falls_through_to_a_playable_candidate(self):
        calls = []

        def _dl(url, out_no_ext, quality="320", progress_hook=None, cfg=None):
            calls.append(url)
            if "vid_dead" in url:
                raise RuntimeError(NOT_AVAILABLE)
            _mp3_writer(out_no_ext + ".mp3")

        with mock.patch.object(mon.yt_mod, "download_audio", _dl):
            res = mon._force_one(self.tm, self.cfg, 1)

        self.assertEqual(calls, [DEAD["url"], ALIVE["url"]])
        self.assertTrue(res["ok"], "the track IS on YouTube — one dead video "
                                   "must not fail the force job")
        self.assertEqual(self.added[0][3], "vid_alive")

    def test_force_reports_the_real_error_when_everything_is_dead(self):
        def _dl(url, out_no_ext, quality="320", progress_hook=None, cfg=None):
            raise RuntimeError(NOT_AVAILABLE)

        with mock.patch.object(mon.yt_mod, "download_audio", _dl):
            res = mon._force_one(self.tm, self.cfg, 1)

        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "ERR-4")
        self.assertIn("not available", res["message"],
                      "the real yt-dlp error stays visible")


if __name__ == "__main__":
    unittest.main()
