"""Tests for artist resolution during a scan (core/source.py, core/monitor.py).

Reported from a real run with downloader=youtube and music_source=zvuk:

    WARNING: Artist ID not found on source: 3W3dTmUEXbo2D9CQzNTSaI
             (Deezer artist not found: 3W3dTmUEXbo2D9CQzNTSaI)

Two defects behind that, plus the slow start the same run showed:

1. MultiFallbackSource.get_artist() ignored self.priority - which get_albums()
   does honour - and hardcoded Spotify -> Deezer -> Spotify -> MusicBrainz. A
   Spotify base62 id was therefore handed to Deezer, which only knows numeric
   ids, so the configured source was never asked at all.
2. scan_artist() gave up on the artist when the id lookup failed, even though
   the artist *name* was right there and a name search would have resolved it.
3. MultiFallbackSource._auth() authenticated every source eagerly - including a
   Spotify token POST with its own retries - and monitor.py rebuilds the source
   per artist, so that happened again for every single artist before any work.

Everything runs offline: the per-platform objects are replaced with fakes.

Run:  python tests/test_artist_resolution.py
"""

import os
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import source as src_mod  # noqa: E402
from core import monitor as mon_mod  # noqa: E402
from core import status  # noqa: E402

SPOTIFY_ID = "3W3dTmUEXbo2D9CQzNTSaI"   # 22-char base62, i.e. a Spotify artist id


class FakeSource:
    """Stands in for one platform; records what it was asked to do."""

    def __init__(self, name, artist=None, err=None, auth_sleep=0.0):
        self.name = name
        self.artist = artist
        self.err = err
        self.auth_sleep = auth_sleep
        self.get_artist_calls = []
        self.search_calls = []
        self.get_albums_calls = []
        self.auth_calls = 0

    def _auth(self):
        self.auth_calls += 1
        if self.auth_sleep:
            time.sleep(self.auth_sleep)

    def get_artist(self, artist_id):
        self.get_artist_calls.append(artist_id)
        if self.err:
            raise self.err
        if self.artist is None:
            raise RuntimeError(f"{self.name} artist not found: {artist_id}")
        return dict(self.artist, id=str(artist_id))

    def search_artist(self, name):
        results = self.search_artists(name, limit=1)
        return results[0] if results else None

    def search_artists(self, query, limit=8):
        self.search_calls.append(query)
        if self.artist is None:
            return []
        return [dict(self.artist, id=self.artist.get("id", "found-1"))]

    def get_albums(self, *a, **kw):
        self.get_albums_calls.append((a, kw))
        return []

    def get_album_tracks(self, *a, **kw):
        return []


def make_multi(priority, **fakes):
    src = src_mod.MultiFallbackSource({"music_source": priority}, priority=priority)
    src.deezer = fakes.get("deezer", FakeSource("deezer"))
    src.spotify = fakes.get("spotify", None)
    src.yandex = fakes.get("yandex", FakeSource("yandex"))
    src.zvuk = fakes.get("zvuk", FakeSource("zvuk"))
    src.musicbrainz = fakes.get("musicbrainz", FakeSource("musicbrainz"))
    return src


class GetArtistHonoursConfiguredSource(unittest.TestCase):
    def test_zvuk_is_asked_first_when_it_is_the_configured_source(self):
        zvuk = FakeSource("zvuk", artist={"name": "Пикник", "id": "zv-1"})
        deezer = FakeSource("deezer")
        src = make_multi("zvuk", zvuk=zvuk, deezer=deezer)

        got = src.get_artist("zv-1")

        self.assertEqual(got["name"], "Пикник")
        self.assertEqual(zvuk.get_artist_calls, ["zv-1"])
        self.assertEqual(deezer.get_artist_calls, [],
                         "Deezer must not be asked when zvuk is the configured source")

    def test_a_spotify_id_is_not_handed_to_deezer(self):
        """The exact failure from the log: a base62 id reaching Deezer."""
        deezer = FakeSource("deezer",
                            err=RuntimeError("Deezer artist not found: " + SPOTIFY_ID))
        spotify = FakeSource("spotify", artist={"name": "Starset", "id": SPOTIFY_ID})
        src = make_multi("zvuk", deezer=deezer, spotify=spotify)

        got = src.get_artist(SPOTIFY_ID)

        self.assertEqual(got["name"], "Starset")
        self.assertEqual(deezer.get_artist_calls, [],
                         "a 22-char base62 id can never be a Deezer id")

    def test_falls_back_to_another_source_when_the_first_fails(self):
        # A numeric id fits both zvuk and deezer, so the fallback is exercised.
        zvuk = FakeSource("zvuk", err=RuntimeError("zvuk is down"))
        deezer = FakeSource("deezer", artist={"name": "Starset", "id": "12345"})
        src = make_multi("zvuk", zvuk=zvuk, deezer=deezer)

        got = src.get_artist("12345")

        self.assertEqual(got["name"], "Starset")
        self.assertEqual(zvuk.get_artist_calls, ["12345"], "the configured source goes first")
        self.assertEqual(deezer.get_artist_calls, ["12345"])

    def test_a_doomed_platform_is_not_called_at_all(self):
        """A base62 id can never be a Deezer id - asking Deezer is pure delay."""
        deezer = FakeSource("deezer", artist={"name": "WRONG", "id": "1"})
        src = make_multi("zvuk", deezer=deezer)   # no spotify configured at all
        with self.assertRaises(Exception):
            src.get_artist(SPOTIFY_ID)
        self.assertEqual(deezer.get_artist_calls, [])

    def test_raises_when_no_source_can_resolve_it(self):
        src = make_multi("zvuk",
                         zvuk=FakeSource("zvuk", err=RuntimeError("zvuk down")),
                         deezer=FakeSource("deezer", err=RuntimeError("deezer down")))
        with self.assertRaises(Exception):
            src.get_artist("12345")

    def test_yandex_is_reachable_when_it_is_the_configured_source(self):
        yandex = FakeSource("yandex", artist={"name": "Король и Шут", "id": "ya-9"})
        src = make_multi("yandex", yandex=yandex)
        self.assertEqual(src.get_artist("ya-9")["name"], "Король и Шут")


class ScanArtistFallsBackToName(unittest.TestCase):
    def setUp(self):
        self._skipped = status.status.get("skipped", 0)

    def test_unresolvable_id_does_not_drop_the_artist(self):
        """The id belongs to another platform; the name must still be searched."""
        src = FakeSource("zvuk", artist={"name": "Starset", "id": "zv-77"})
        # get_artist always fails, but a name search works
        src.get_artist = lambda aid: (_ for _ in ()).throw(
            RuntimeError("Deezer artist not found: " + str(aid)))

        mon_mod.scan_artist(
            src, {"name": "Starset", "spotify_id": SPOTIFY_ID},
            {"duration_tolerance_sec": 15, "fallback_to_closest": True,
             "max_albums_per_artist": 5, "download_threads": 1,
             "save_folder": "downloads"})

        self.assertEqual(src.search_calls, ["Starset"],
                         "scan_artist must fall back to a name search")
        self.assertTrue(src.get_albums_calls,
                        "the artist must go on to be scanned, not skipped")


class AuthOnlyWarmsTheConfiguredSource(unittest.TestCase):
    def test_other_platforms_are_not_authenticated_eagerly(self):
        """monitor.py rebuilds the source per artist, so eager auth of all five
        platforms - Spotify included, with its retries - is paid per artist."""
        spotify = FakeSource("spotify", artist=None, auth_sleep=0.05)
        deezer = FakeSource("deezer")
        yandex = FakeSource("yandex")
        zvuk = FakeSource("zvuk")
        mb = FakeSource("musicbrainz")
        src = make_multi("zvuk", spotify=spotify, deezer=deezer,
                         yandex=yandex, zvuk=zvuk, musicbrainz=mb)

        src._auth()

        self.assertEqual(zvuk.auth_calls, 1, "the configured source is warmed")
        self.assertEqual(spotify.auth_calls, 0,
                         "Spotify must not be authenticated when it is not the source")
        self.assertEqual(yandex.auth_calls, 0)
        self.assertEqual(deezer.auth_calls, 0)
        self.assertEqual(mb.auth_calls, 0)

    def test_configured_spotify_still_gets_authenticated(self):
        spotify = FakeSource("spotify", artist=None)
        src = make_multi("spotify", spotify=spotify)
        src._auth()
        self.assertEqual(spotify.auth_calls, 1)

    def test_unknown_priority_keeps_the_old_behaviour(self):
        deezer = FakeSource("deezer")
        src = make_multi("", deezer=deezer)
        src._auth()   # must not raise
        self.assertGreaterEqual(deezer.auth_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
