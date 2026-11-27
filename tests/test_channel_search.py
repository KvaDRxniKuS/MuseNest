"""Tests for the per-artist YouTube channel search zone.

Real-world failure: for the artist "miraj"
(https://www.youtube.com/channel/UCp61iuJMfqfYTomVA50RlrQ) the global YouTube
search returned a pile of unrelated ("Indian") uploads, which got downloaded.

The fix follows the user's model: the metadata page (zvuk/spotify/...) stays
the source of truth for WHAT to download; per-artist channel links constrain
WHERE the audio is searched — the channel listing is flat-extracted once,
cached, and candidates are matched by name similarity, so unrelated uploads
can never enter the results while a channel is set.

Run:  python tests/test_channel_search.py
"""

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import youtube as yt_mod  # noqa: E402
from core import config as cfg_mod  # noqa: E402
from core import monitor as mon_mod  # noqa: E402
from core import status  # noqa: E402

MIRAJ_CHANNEL = "https://www.youtube.com/channel/UCp61iuJMfqfYTomVA50RlrQ"
MIRAJ_ID = "UCp61iuJMfqfYTomVA50RlrQ"

CHANNEL_ENTRIES = [
    {"id": "vid_good", "title": "MIRAJ — Basic Bitches", "duration": 158},
    {"id": "vid_live", "title": "MIRAJ — Basic Bitches (Live)", "duration": 171},
    {"id": "vid_other", "title": "MIRAJ — Другой трек", "duration": 200},
    {"id": "vid_garbage", "title": "Индийская песня про кринж 2024", "duration": 180},
    {"id": "vid_garbage2", "title": "Trending Bollywood Mashup", "duration": 160},
]

SECOND_CHANNEL_ENTRIES = [
    {"id": "vid_only_here", "title": "MIRAJ feat. X — Basic Bitches (Remix)", "duration": 162},
]


class _FakeYdl:
    """Flat stand-in for yt_dlp.YoutubeDL: a channel listing per URL."""
    listings = {}
    extracts = []
    active = None

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        _FakeYdl.extracts.append(url)
        return {"entries": [dict(e) for e in _FakeYdl.listings.get(url, [])]}


class ChannelSearch(unittest.TestCase):

    def setUp(self):
        self._saved_ydl = yt_mod.yt_dlp.YoutubeDL
        yt_mod.yt_dlp.YoutubeDL = _FakeYdl
        _FakeYdl.extracts = []
        _FakeYdl.listings = {
            MIRAJ_CHANNEL + "/videos": CHANNEL_ENTRIES,
            "https://www.youtube.com/@remixes/videos": SECOND_CHANNEL_ENTRIES,
        }
        yt_mod.clear_channel_cache()

    def tearDown(self):
        yt_mod.yt_dlp.YoutubeDL = self._saved_ydl
        yt_mod.clear_channel_cache()

    def test_unrelated_uploads_cannot_enter_the_results(self):
        res = yt_mod.search_in_channels([MIRAJ_CHANNEL], "Basic Bitches", "Miraj")

        ids = [r["id"] for r in res]
        self.assertIn("vid_good", ids)
        self.assertNotIn("vid_garbage", ids,
                         "unrelated uploads must never be offered for download")
        self.assertNotIn("vid_garbage2", ids)
        self.assertEqual(res[0]["title"], "MIRAJ — Basic Bitches",
                         "the exact-title hit must come first")

    def test_results_match_the_search_youtube_shape(self):
        res = yt_mod.search_in_channels([MIRAJ_CHANNEL], "Basic Bitches", "Miraj")
        r = res[0]
        for key in ("id", "title", "duration", "webpage_url", "url"):
            self.assertIn(key, r)
        self.assertEqual(r["webpage_url"],
                         "https://www.youtube.com/watch?v=vid_good")
        self.assertEqual(r["duration"], 158)

    def test_channel_listing_is_extracted_once_and_cached(self):
        yt_mod.search_in_channels([MIRAJ_CHANNEL], "Basic Bitches", "Miraj")
        yt_mod.search_in_channels([MIRAJ_CHANNEL], "Другой трек", "Miraj")

        extracts = [u for u in _FakeYdl.extracts if u.startswith(MIRAJ_CHANNEL)]
        self.assertEqual(len(extracts), 1,
                         "the channel listing must be flat-extracted once per TTL")

    def test_handle_links_and_bare_ids_are_supported(self):
        self.assertEqual(yt_mod.normalize_channel_input(MIRAJ_ID),
                         "https://www.youtube.com/channel/" + MIRAJ_ID)
        self.assertEqual(yt_mod.normalize_channel_input(MIRAJ_CHANNEL + "/videos?si=x"),
                         "https://www.youtube.com/channel/" + MIRAJ_ID)
        self.assertEqual(yt_mod.normalize_channel_input("https://www.youtube.com/@remixes"),
                         "https://www.youtube.com/@remixes")

    def test_a_video_link_is_not_a_channel(self):
        self.assertIsNone(yt_mod.normalize_channel_input(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertIsNone(yt_mod.normalize_channel_input("https://vimeo.com/123"))
        self.assertIsNone(yt_mod.normalize_channel_input(""))

    def test_a_second_channel_is_searched_when_the_first_has_nothing(self):
        res = yt_mod.search_in_channels(
            [MIRAJ_CHANNEL, "https://www.youtube.com/@remixes"],
            "Basic Bitches Remix", "Miraj")

        ids = [r["id"] for r in res]
        self.assertIn("vid_only_here", ids)

    def test_empty_inputs_yield_nothing(self):
        self.assertEqual(yt_mod.search_in_channels([], "x", "y"), [])
        self.assertEqual(yt_mod.search_in_channels([MIRAJ_CHANNEL], "", "Miraj"), [])
        self.assertEqual(yt_mod.search_in_channels(["not a channel"], "Basic Bitches", "M"), [])

    def test_a_broken_channel_fails_softly(self):
        _FakeYdl.listings["https://www.youtube.com/broken/videos"] = None
        yt_mod.yt_dlp.YoutubeDL = _FakeYdl
        # extract_info returning None -> entries [] -> no candidates
        res = yt_mod.search_in_channels(["https://www.youtube.com/broken"], "x", "y")
        self.assertEqual(res, [])


class ArtistChannelsLookup(unittest.TestCase):

    def test_lookup_is_case_insensitive_and_reads_the_artist_entry(self):
        cfg = {"artists": [{"name": "Miraj", "youtube_channels": [MIRAJ_CHANNEL]}]}
        self.assertEqual(mon_mod._artist_channels("miraj", cfg), [MIRAJ_CHANNEL])

    def test_a_string_value_is_accepted_and_an_entry_without_channels_is_empty(self):
        cfg = {"artists": [{"name": "A", "youtube_channels": MIRAJ_CHANNEL},
                           {"name": "B"}]}
        self.assertEqual(mon_mod._artist_channels("A", cfg), [MIRAJ_CHANNEL])
        self.assertEqual(mon_mod._artist_channels("B", cfg), [])
        self.assertEqual(mon_mod._artist_channels("нет такого", cfg), [])


class ChannelConfigSanitized(unittest.TestCase):

    def test_channels_are_kept_normalized_per_artist(self):
        c = cfg_mod.sanitize_config({"artists": [
            {"name": "Miraj", "youtube_channels":
             [MIRAJ_CHANNEL, MIRAJ_CHANNEL.upper(), "  ", "https://www.youtube.com/@r"]},
            {"name": "String artist", "youtube_channels": "https://www.youtube.com/@one"},
        ]})
        miraj = next(a for a in c["artists"] if a["name"] == "Miraj")
        self.assertEqual(miraj["youtube_channels"],
                         [MIRAJ_CHANNEL, "https://www.youtube.com/@r"],
                         "duplicates and blanks are dropped")
        plain = next(a for a in c["artists"] if a["name"] == "String artist")
        self.assertEqual(plain["youtube_channels"],
                         ["https://www.youtube.com/@one"],
                         "a single string becomes a list")
        nobody = next(a for a in c["artists"] if a["name"] == "String artist")
        self.assertTrue("youtube_channels" in nobody)


class MonitorUsesTheZone(unittest.TestCase):

    def setUp(self):
        self._saved = {
            "sic": mon_mod.search_in_channels if hasattr(mon_mod, "search_in_channels") else None,
            "chan": yt_mod.search_in_channels,
            "glob": yt_mod.search_youtube,
            "match": mon_mod.mat_mod.find_best_match,
            "lib": __import__("core.library", fromlist=["x"]).update_track_status,
            "stop": status.status.get("stop_requested", False),
        }
        self.calls = {"chan": [], "glob": []}
        status.status["stop_requested"] = False
        mon_mod.set_thread_state = lambda *a, **k: None

        def _chan(channels, track_name, artist_name, **kw):
            self.calls["chan"].append(list(channels))
            return [{"id": "vid_good", "title": "MIRAJ — Basic Bitches",
                     "duration": 158, "webpage_url": "w", "url": "w"}]

        def _glob(query, **kw):
            self.calls["glob"].append(query)
            return [{"id": "g1", "title": "indian thing", "duration": 1,
                     "webpage_url": "w", "url": "w"}]

        yt_mod.search_in_channels = _chan
        yt_mod.search_youtube = _glob
        mon_mod.mat_mod.find_best_match = lambda *a, **k: (None, "ERR-1")

        import core.library as lib_mod
        lib_mod.update_track_status = lambda *a, **k: None

    def tearDown(self):
        yt_mod.search_in_channels = self._saved["chan"]
        yt_mod.search_youtube = self._saved["glob"]
        mon_mod.mat_mod.find_best_match = self._saved["match"]
        import core.library as lib_mod
        lib_mod.update_track_status = self._saved["lib"]
        status.status["stop_requested"] = self._saved["stop"]

    def _cfg(self, channels):
        return {"artists": [{"name": "Miraj", "youtube_channels": channels}],
                "blacklist": [], "duration_tolerance_sec": 15,
                "fallback_to_closest": False, "save_folder": tempfile.mkdtemp(),
                "download_threads": 1, "audio_quality": "320"}

    def test_a_track_with_channels_is_searched_inside_them(self):
        track = {"name": "Basic Bitches", "id": "zvuk-1", "album_name": "A",
                 "duration_ms": 158000}

        mon_mod.process_track(track, "Miraj", self._cfg([MIRAJ_CHANNEL]), worker_id=1)

        self.assertEqual(self.calls["chan"], [[MIRAJ_CHANNEL]],
                         "the channel search must receive the artist's channels")
        self.assertEqual(self.calls["glob"], [],
                         "the global search must be OFF when channels are set")

    def test_a_track_without_channels_keeps_the_global_search(self):
        track = {"name": "Basic Bitches", "id": "zvuk-2", "album_name": "A",
                 "duration_ms": 158000}

        mon_mod.process_track(track, "Miraj", self._cfg([]), worker_id=1)

        self.assertEqual(self.calls["chan"], [])
        self.assertEqual(len(self.calls["glob"]), 1)

    def test_force_download_respects_the_zone_too(self):
        tm = {"artist": "Miraj", "track_name": "Basic Bitches", "track_id": "zvuk-3",
              "album_name": "A", "duration_ms": 158000}

        res = mon_mod._force_one(tm, self._cfg([MIRAJ_CHANNEL]), 1)

        self.assertEqual(self.calls["chan"], [[MIRAJ_CHANNEL]])
        self.assertEqual(self.calls["glob"], [])
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
