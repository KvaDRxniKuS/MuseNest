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

import copy
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import source as src_mod  # noqa: E402
from core import config as cfg_mod  # noqa: E402
from core import library as lib_mod  # noqa: E402
from core import monitor as mon_mod  # noqa: E402
from core import status  # noqa: E402
from core import yandex as ya_mod  # noqa: E402

SPOTIFY_ID = "3W3dTmUEXbo2D9CQzNTSaI"   # 22-char base62, i.e. a Spotify artist id


class FakeSource:
    """Stands in for one platform; records what it was asked to do."""

    def __init__(self, name, artist=None, err=None, auth_sleep=0.0, albums=None):
        self.name = name
        self.artist = artist
        self.err = err
        self.auth_sleep = auth_sleep
        self.albums = albums or []
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

    def search_artist(self, name):
        found = self.search_artists(name, limit=1)
        return found[0] if found else None

    def get_albums(self, artist_id=None, limit=99999, artist_name=""):
        self.get_albums_calls.append((artist_id, artist_name))
        return list(self.albums)

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


def _alb(aid, name):
    return {"id": aid, "name": name, "album_type": "album", "release_date": "2020-01-01"}


class AlbumsComeFromTheConfiguredSource(unittest.TestCase):
    """Reported: source=zvuk, but the log said
    "Альбомы Яндекс Музыки для '5l33p': 65" - Zvuk was never asked."""

    def test_zvuk_is_found_by_name_when_the_id_belongs_to_another_platform(self):
        """The artist resolved to a Spotify base62 id, so _fetch_zvuk_albums used
        to return [] without ever calling Zvuk."""
        zvuk = FakeSource("zvuk", artist={"name": "5l33p", "id": "777"},
                          albums=[_alb("zvuk-1", "Sleepless")])
        src = make_multi("zvuk", zvuk=zvuk)

        got = src._fetch_zvuk_albums("3W3dTmUEXbo2D9CQzNTSaI", 50, "5l33p")

        self.assertTrue(got, "Zvuk must be searched by name")
        self.assertEqual(zvuk.search_calls, ["5l33p"])
        self.assertEqual(zvuk.get_albums_calls[0][0], "777")
        self.assertEqual(got[0]["id"], "zvuk-1")

    def test_a_real_zvuk_id_is_used_directly(self):
        zvuk = FakeSource("zvuk", albums=[_alb("zvuk-9", "Awake")])
        src = make_multi("zvuk", zvuk=zvuk)

        got = src._fetch_zvuk_albums("zvuk-9", 50, "5l33p")

        self.assertEqual(got[0]["id"], "zvuk-9")
        self.assertEqual(zvuk.search_calls, [], "no need to search when the id is Zvuk's own")

    def test_get_albums_returns_the_zvuk_catalog_when_zvuk_is_the_source(self):
        zvuk = FakeSource("zvuk", artist={"name": "5l33p", "id": "777"},
                          albums=[_alb(f"zvuk-{i}", f"Rel {i}") for i in range(1, 12)])
        yandex = FakeSource("yandex",
                            albums=[_alb(f"ya-{i}", f"YA {i}") for i in range(1, 66)])
        src = make_multi("zvuk", zvuk=zvuk, yandex=yandex)

        got = src.get_albums("3W3dTmUEXbo2D9CQzNTSaI", limit=50, artist_name="5l33p")

        self.assertTrue(got)
        self.assertTrue(all(str(a["id"]).startswith("zvuk-") for a in got),
                        f"the zvuk catalog must win, got: {[a['id'] for a in got][:5]}")

    def test_falling_back_to_another_platform_is_said_out_loud(self):
        """Silently switching to Yandex is what made this unreadable."""
        zvuk = FakeSource("zvuk", artist=None, albums=[])
        yandex = FakeSource("yandex",
                            albums=[_alb(f"ya-{i}", f"YA {i}") for i in range(1, 10)])
        src = make_multi("zvuk", zvuk=zvuk, yandex=yandex)

        with self.assertLogs(status.logger, level="WARNING") as cm:
            got = src.get_albums("3W3dTmUEXbo2D9CQzNTSaI", limit=50, artist_name="5l33p")

        self.assertTrue(got, "the fallback catalog is still used")
        joined = "\n".join(cm.output)
        self.assertIn("zvuk", joined, joined)
        self.assertIn("5l33p", joined, joined)


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


class ScanArtistPrefersTheConfiguredSourcesOwnId(unittest.TestCase):
    """With music_source=zvuk the stored spotify_id used to be looked up first,
    sending the request to Spotify (and its failing token endpoint) instead of
    asking Zvuk at all."""

    def _cfg(self):
        return {"duration_tolerance_sec": 15, "fallback_to_closest": True,
                "max_albums_per_artist": 5, "download_threads": 1,
                "save_folder": "downloads"}

    def test_a_native_zvuk_id_beats_the_spotify_id(self):
        zvuk = FakeSource("zvuk", artist={"name": "Пикник", "id": "zv-77"})
        spotify = FakeSource("spotify", artist={"name": "WRONG", "id": SPOTIFY_ID})
        src = make_multi("zvuk", zvuk=zvuk, spotify=spotify)

        mon_mod.scan_artist(src, {"name": "Пикник", "spotify_id": SPOTIFY_ID,
                                  "zvuk_id": "zv-77"}, self._cfg())

        self.assertEqual(zvuk.get_artist_calls, ["zv-77"],
                         "the native id of the configured source goes first")
        self.assertEqual(spotify.get_artist_calls, [],
                         "the foreign spotify id must not be looked up")

    def test_a_foreign_id_only_is_searched_by_name_on_the_configured_source(self):
        zvuk = FakeSource("zvuk", artist={"name": "Starset", "id": "zv-5"})
        spotify = FakeSource("spotify", artist={"name": "WRONG", "id": SPOTIFY_ID})
        deezer = FakeSource("deezer", artist={"name": "WRONG", "id": "1"})
        src = make_multi("zvuk", zvuk=zvuk, spotify=spotify, deezer=deezer)

        mon_mod.scan_artist(src, {"name": "Starset", "spotify_id": SPOTIFY_ID},
                            self._cfg())

        self.assertEqual(zvuk.search_calls[0], "Starset",
                         "the configured source is searched by name first")
        self.assertEqual(zvuk.get_artist_calls, [])
        self.assertEqual(spotify.get_artist_calls, [])
        self.assertEqual(deezer.get_artist_calls, [],
                         "no id lookup anywhere: the name resolves first")

    def test_the_foreign_id_is_only_the_last_resort(self):
        zvuk = FakeSource("zvuk", artist=None)   # name search finds nothing
        # Spotify cannot find the artist by name either, but it CAN resolve
        # the stored id once every name search has failed.
        spotify = FakeSource("spotify", artist=None)

        def _spotify_get(aid):
            spotify.get_artist_calls.append(aid)
            if str(aid) == SPOTIFY_ID:
                return {"name": "Starset", "id": str(aid)}
            raise RuntimeError(f"spotify artist not found: {aid}")

        spotify.get_artist = _spotify_get
        src = make_multi("zvuk", zvuk=zvuk, spotify=spotify)

        mon_mod.scan_artist(src, {"name": "Starset", "spotify_id": SPOTIFY_ID},
                            self._cfg())

        self.assertEqual(zvuk.search_calls[0], "Starset")
        self.assertEqual(spotify.get_artist_calls, [SPOTIFY_ID],
                         "only after the name search failed may the foreign id be used")


class SearchArtistsAskThePriorityFirst(unittest.TestCase):
    """search_artists used to hardcode Spotify -> Deezer -> Spotify ->
    MusicBrainz, so a configured Zvuk/Yandex was never searched at all."""

    def test_the_configured_source_is_searched_first(self):
        zvuk = FakeSource("zvuk", artist={"name": "Пикник", "id": "zv-1"})
        spotify = FakeSource("spotify", artist={"name": "S", "id": "s1"})
        deezer = FakeSource("deezer", artist={"name": "D", "id": "d1"})
        src = make_multi("zvuk", zvuk=zvuk, spotify=spotify, deezer=deezer)

        got = src.search_artists("Пикник")

        self.assertEqual(zvuk.search_calls, ["Пикник"])
        self.assertEqual(spotify.search_calls, [])
        self.assertEqual(deezer.search_calls, [])
        self.assertEqual(got[0]["name"], "Пикник")

    def test_an_empty_priority_result_falls_through(self):
        zvuk = FakeSource("zvuk", artist=None)
        spotify = FakeSource("spotify", artist={"name": "S", "id": "s1"})
        src = make_multi("zvuk", zvuk=zvuk, spotify=spotify)

        got = src.search_artists("X")

        self.assertEqual(zvuk.search_calls, ["X"])
        self.assertEqual(spotify.search_calls, ["X"])
        self.assertEqual(got[0]["name"], "S")


class BuildSourceResolvesAuto(unittest.TestCase):
    """The UI stamps artists with source:"auto" — that must follow the global
    music_source instead of silently degrading to Deezer."""

    def test_auto_resolves_to_the_global_music_source(self):
        src = src_mod.build_source("auto", {"music_source": "zvuk"})
        self.assertEqual(src.priority, "zvuk")

    def test_an_explicit_platform_name_passes_through(self):
        src = src_mod.build_source("yandex", {"music_source": "zvuk"})
        self.assertEqual(src.priority, "yandex")


class ArtistSourceForEntry(unittest.TestCase):
    def test_auto_follows_the_global_source(self):
        self.assertEqual(cfg_mod.artist_source_for_entry(
            {"source": "auto"}, {"music_source": "zvuk"}), "zvuk")

    def test_a_missing_source_follows_the_global_source(self):
        self.assertEqual(cfg_mod.artist_source_for_entry(
            {"name": "X"}, {"music_source": "yandex"}), "yandex")

    def test_an_explicit_per_artist_source_wins(self):
        self.assertEqual(cfg_mod.artist_source_for_entry(
            {"source": "deezer"}, {"music_source": "zvuk"}), "deezer")

    def test_a_bare_platform_name_is_trusted(self):
        # build_source("zvuk", cfg) must keep zvuk, not swap in music_source.
        self.assertEqual(cfg_mod.artist_source_for_entry(
            "zvuk", {"music_source": "deezer"}), "zvuk")

    def test_a_bare_artist_name_follows_the_global_source(self):
        self.assertEqual(cfg_mod.artist_source_for_entry(
            "Кино", {"music_source": "zvuk"}), "zvuk")

    def test_none_and_garbage_fall_back_safely(self):
        self.assertEqual(cfg_mod.artist_source_for_entry(
            None, {"music_source": "zvuk"}), "zvuk")
        self.assertEqual(cfg_mod.artist_source_for_entry(
            None, {"music_source": "нет такого"}), "deezer")


class AutoStaysAutoInTheConfigWriteBack(unittest.TestCase):
    """update_library_metadata writes the per-artist source back into
    config.json. The *resolved* platform must stay out of that write-back:
    "auto" has to keep following the global music_source."""

    def test_resolved_auto_is_not_baked_into_config(self):
        saved_cfg = []
        saved = {
            "multi": src_mod.MultiFallbackSource,
            "save_config": cfg_mod.save_config,
            "save_library": lib_mod.save_library,
            "check": lib_mod.check_library_files,
        }

        class _StubMulti:
            def __init__(self, cfg, priority=""):
                self.priority = priority

            def get_albums(self, artist_id=None, limit=99999, artist_name=""):
                return []

            def get_album_tracks(self, *a, **k):
                return []

        saved["yandex"] = ya_mod.YandexSource

        class _StubYandex:
            def __init__(self, token=None, proxy=None):
                pass

            def search_artists(self, query, limit=8):
                return []

        src_mod.MultiFallbackSource = _StubMulti
        ya_mod.YandexSource = _StubYandex
        cfg_mod.save_config = lambda c: saved_cfg.append(
            [dict(a) for a in c.get("artists", [])])
        lib_mod.save_library = lambda l: None
        lib_mod.check_library_files = lambda *a, **k: None
        cfg = {"artists": [{"name": "Кино", "source": "auto", "zvuk_id": "zv-1"}],
               "music_source": "zvuk", "max_albums_per_artist": 5,
               "download_threads": 1, "save_folder": tempfile.mkdtemp()}
        try:
            lib_mod.update_library_metadata(cfg, only_name="Кино")
        finally:
            src_mod.MultiFallbackSource = saved["multi"]
            ya_mod.YandexSource = saved["yandex"]
            cfg_mod.save_config = saved["save_config"]
            lib_mod.save_library = saved["save_library"]
            lib_mod.check_library_files = saved["check"]

        self.assertTrue(saved_cfg, "the resolved artist must be written back")
        self.assertEqual(saved_cfg[0][0]["source"], "auto",
                         "the raw per-artist choice must survive the write-back")


class RunScanUsesTheArtistsOwnSource(unittest.TestCase):
    """run_scan used to pass the raw "auto" to build_source, so every artist
    with the UI default was scanned on Deezer regardless of music_source."""

    def setUp(self):
        self._snapshot = copy.deepcopy(status.status)
        self._saved = {
            "stopped": mon_mod._stopped,
            "scan_artist": mon_mod.scan_artist,
            "build_source": src_mod.build_source,
            "check": lib_mod.check_library_files,
        }
        mon_mod._stopped = lambda: False
        self.built = []
        self.scanned = []
        built, scanned = self.built, self.scanned

        class _FakeSrc:
            def __init__(self, name):
                self.priority = name

            def _auth(self):
                pass

        def _fake_build(name, cfg):
            built.append(name)
            return _FakeSrc(name)

        def _fake_scan(src, entry, cfg):
            scanned.append((src.priority,
                            entry.get("name") if isinstance(entry, dict) else entry))

        src_mod.build_source = _fake_build
        mon_mod.scan_artist = _fake_scan
        lib_mod.check_library_files = lambda cfg: None

    def tearDown(self):
        for attr, fn in self._saved.items():
            if attr == "stopped":
                mon_mod._stopped = fn
            elif attr == "scan_artist":
                mon_mod.scan_artist = fn
            elif attr == "build_source":
                src_mod.build_source = fn
            else:
                lib_mod.check_library_files = fn
        status.status.clear()
        status.status.update(self._snapshot)

    def test_auto_artists_are_built_with_the_global_source(self):
        cfg = {"artists": [{"name": "Starset", "source": "auto"},
                           {"name": "Пикник", "source": "deezer"},
                           "Кино"],
               "music_source": "zvuk", "download_threads": 1,
               "save_folder": tempfile.mkdtemp()}

        self.assertTrue(mon_mod.run_scan(cfg))

        # warm-up + one build per artist, each resolved through music_source
        self.assertEqual(self.built, ["zvuk", "zvuk", "deezer", "zvuk"])
        self.assertEqual(self.scanned, [("zvuk", "Starset"),
                                        ("deezer", "Пикник"),
                                        ("zvuk", "Кино")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
