"""Tests for the artist add/monitor pipeline persistence.

Real-world report: after adding several artists, adding new ones stopped
working and some added artists stopped being monitored for albums. Three
root causes behind that:

1. load_config() rebuilt the artist list FROM artists.txt: an artist whose
   txt line collided with another entry's name was dropped outright, and
   every cycle stripped metadata (youtube_channels, genre_path) — added
   artists vanished or degraded.
2. update_library_metadata() held the global update lock for its whole
   multi-minute network phase, stalling the scan's sync_artist_to_library.
   Its full-refresh branch also replaced the artist list wholesale, wiping
   youtube_channels.
3. /api/library/update answered 409 "Scan already running" while a scan was
   active — artists added mid-scan sat in "loading..." forever.

Run:  python tests/test_artist_pipeline.py
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import config as cfg_mod  # noqa: E402
from core import library as lib_mod  # noqa: E402
from core import monitor as mon_mod  # noqa: E402
from core import status  # noqa: E402
from core import source as src_mod  # noqa: E402
from core import yandex as ya_mod  # noqa: E402

MIRAJ_CH = "https://www.youtube.com/channel/UCp61iuJMfqfYTomVA50RlrQ"


class _TmpConfig(unittest.TestCase):
    """Point config.py at a temp data dir and restore afterwards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (cfg_mod.CONFIG_PATH, cfg_mod.DATA_DIR, cfg_mod.BASE_DIR)
        cfg_mod.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        cfg_mod.DATA_DIR = self.tmp
        cfg_mod.BASE_DIR = self.tmp

    def tearDown(self):
        cfg_mod.CONFIG_PATH, cfg_mod.DATA_DIR, cfg_mod.BASE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_txt(self, lines):
        with open(os.path.join(self.tmp, "artists.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


class ConfigIsThePrimaryStore(_TmpConfig):
    """The union merge: config artists always survive, txt only imports."""

    def test_a_config_artist_survives_a_name_collision_in_txt(self):
        # Two different artists that both round-trip as the plain name
        # "miraj" in artists.txt (one added by name, one via zvuk with an id).
        cfg = {"artists": [
            {"name": "miraj", "id": None, "source": "auto"},
            {"name": "miraj", "id": "zvuk-123", "source": "zvuk", "zvuk_id": "zvuk-123"},
        ]}
        cfg_mod.save_config(cfg)
        self.write_txt(["miraj", "miraj"])   # what save_artists_to_txt wrote

        loaded = cfg_mod.load_config()
        names = sorted(a["name"] for a in loaded["artists"])
        self.assertEqual(names, ["miraj", "miraj"],
                         "no artist may be dropped by the txt merge")
        ids = sorted((a.get("id") or "") for a in loaded["artists"])
        self.assertEqual(ids, ["", "zvuk-123"],
                         "both distinct entries must keep their ids")

    def test_metadata_survives_a_save_load_roundtrip(self):
        cfg = {"artists": [{
            "name": "Miraj", "id": "zvuk-1", "source": "zvuk",
            "zvuk_id": "zvuk-1", "genre_path": "Rock",
            "youtube_channels": [MIRAJ_CH],
        }]}
        cfg_mod.save_config(cfg)

        loaded = cfg_mod.load_config()
        a = loaded["artists"][0]
        self.assertEqual(a["youtube_channels"], [MIRAJ_CH],
                         "channels must survive the config roundtrip")
        self.assertEqual(a["genre_path"], "Rock")
        self.assertEqual(a["zvuk_id"], "zvuk-1")

    def test_new_txt_lines_are_imported_once(self):
        cfg = {"artists": [{"name": "Existing", "id": None, "source": "auto"}]}
        cfg_mod.save_config(cfg)
        self.write_txt(["Existing", "Brand New Artist"])

        loaded = cfg_mod.load_config()
        names = [a["name"] for a in loaded["artists"]]
        self.assertIn("Brand New Artist", names, "hand-edited txt lines import")
        self.assertEqual(names.count("Existing"), 1, "no duplicates from txt")

    def test_a_txt_only_artist_gets_the_full_shape(self):
        cfg = {"artists": []}
        cfg_mod.save_config(cfg)
        self.write_txt(["Кино"])

        loaded = cfg_mod.load_config()
        a = loaded["artists"][0]
        self.assertEqual(a["name"], "Кино")
        self.assertEqual(a["source"], "auto")
        self.assertEqual(a["youtube_channels"], [])

    def test_unparsable_url_lines_never_become_ghost_artists(self):
        cfg = {"artists": [{"name": "Miraj", "id": None, "source": "auto"}]}
        cfg_mod.save_config(cfg)
        # Legacy lines a broken era could leave in artists.txt
        self.write_txt([
            "https://zvuk.com/artist/121431601",
            "www.youtube.com/channel/UCp61iuJMfqfYTomVA50RlrQ",
            "Miraj",
        ])

        loaded = cfg_mod.load_config()
        names = [a["name"] for a in loaded["artists"]]
        self.assertEqual(names, ["Miraj"],
                         "URL garbage must not be imported as artists, "
                         "and the name line must not duplicate")

    def test_case_insensitive_name_duplicates_are_collapsed(self):
        cfg = {"artists": [{"name": "Miraj", "id": None, "source": "auto"}]}
        cfg_mod.save_config(cfg)
        self.write_txt(["MIRAJ"])

        loaded = cfg_mod.load_config()
        names = [a["name"] for a in loaded["artists"]]
        self.assertEqual(names, ["Miraj"], "one artist, one entry")


class MetadataUpdatePreservesArtists(unittest.TestCase):
    """update_library_metadata: channels survive the write-back, stale artists
    are pruned on a full refresh, and the library is not clobbered."""

    def setUp(self):
        self.saved_cfg = []
        self.saved_lib = []
        self._saved = {
            "multi": src_mod.MultiFallbackSource,
            "yandex": ya_mod.YandexSource,
            "save_config": cfg_mod.save_config,
            "save_library": lib_mod.save_library,
            "load_library": lib_mod.load_library,
            "check": lib_mod.check_library_files,
        }

        class _StubMulti:
            def __init__(self, cfg, priority=""):
                self.priority = priority

            def get_albums(self, artist_id=None, limit=99999, artist_name=""):
                return []

            def get_album_tracks(self, *a, **k):
                return []

        class _StubYandex:
            def __init__(self, token=None, proxy=None):
                pass

            def search_artists(self, query, limit=8):
                return []

        self._multi = _StubMulti
        self._yandex = _StubYandex
        src_mod.MultiFallbackSource = _StubMulti
        ya_mod.YandexSource = _StubYandex
        cfg_mod.save_config = lambda c: self.saved_cfg.append(copy.deepcopy(c.get("artists", [])))
        lib_mod.save_library = lambda l: self.saved_lib.append(copy.deepcopy(l))
        lib_mod.check_library_files = lambda *a, **k: None

    def tearDown(self):
        src_mod.MultiFallbackSource = self._saved["multi"]
        ya_mod.YandexSource = self._saved["yandex"]
        cfg_mod.save_config = self._saved["save_config"]
        lib_mod.save_library = self._saved["save_library"]
        lib_mod.load_library = self._saved["load_library"]
        lib_mod.check_library_files = self._saved["check"]

    def _cfg(self):
        return {"artists": [
            {"name": "Miraj", "id": "zvuk-1", "source": "zvuk",
             "zvuk_id": "zvuk-1", "youtube_channels": [MIRAJ_CH]},
            {"name": "Other", "id": None, "source": "auto",
             "youtube_channels": ["https://www.youtube.com/@other"]},
        ], "music_source": "zvuk", "max_albums_per_artist": 5,
               "download_threads": 1, "save_folder": tempfile.mkdtemp()}

    def test_full_refresh_keeps_channels_and_prunes_removed_artists(self):
        cfg = self._cfg()
        # The library still holds an artist that was removed from the config.
        lib_mod.load_library = lambda: {"artists": [
            {"name": "Miraj", "albums": []},
            {"name": "Other", "albums": []},
            {"name": "Removed Artist", "albums": []},
        ]}

        lib_mod.update_library_metadata(cfg)

        # config write-back: per-artist update, channels intact
        arts = {a["name"]: a for a in self.saved_cfg[-1]}
        self.assertEqual(arts["Miraj"]["youtube_channels"], [MIRAJ_CH],
                         "the resolved write-back must carry youtube_channels")
        self.assertEqual(arts["Miraj"]["zvuk_id"], "zvuk-1")

        # library write: the removed artist leaves, monitored ones stay
        names = sorted(a["name"] for a in self.saved_lib[-1]["artists"])
        self.assertEqual(names, ["Miraj", "Other"],
                         "a full refresh mirrors the monitored set")

    def test_single_artist_refresh_touches_only_that_artist(self):
        cfg = self._cfg()
        lib_mod.load_library = lambda: {"artists": [
            {"name": "Miraj", "albums": []},
            {"name": "Other", "albums": [{"id": "a1", "tracks": []}]},
        ]}

        lib_mod.update_library_metadata(cfg, only_name="Miraj")

        names = sorted(a["name"] for a in self.saved_lib[-1]["artists"])
        self.assertEqual(names, ["Miraj", "Other"],
                         "a single-artist refresh must not drop other nodes")
        arts = {a["name"]: a for a in self.saved_cfg[-1]}
        self.assertEqual(arts["Other"]["youtube_channels"],
                         ["https://www.youtube.com/@other"],
                         "an untouched artist keeps its channels")


class ResolveRunsEvenDuringScan(unittest.TestCase):
    """A resolve job must run immediately, scan or not: waiting on the
    "running" flag stalled every added artist for an hour when a crashed
    scan had left the flag stuck."""

    def test_the_job_resolves_right_away_while_a_scan_is_running(self):
        import app as app_mod
        called = []
        saved_fn = lib_mod.update_library_metadata
        lib_mod.update_library_metadata = \
            lambda c, only_name=None: called.append(only_name)
        saved_running = status.status.get("running", False)
        saved_cfg = cfg_mod.load_config
        cfg_mod.load_config = lambda: {"artists": []}
        status.status["running"] = True
        try:
            runner = app_mod._background_metadata_update(only_name="Miraj")
            t = threading.Thread(target=runner, daemon=True)
            t.start()
            t.join(timeout=15)
            self.assertFalse(t.is_alive(),
                             "the job must not wait for the scan to end")
            self.assertEqual(called, ["Miraj"],
                             "the artist is resolved immediately")
        finally:
            status.status["running"] = saved_running
            lib_mod.update_library_metadata = saved_fn
            cfg_mod.load_config = saved_cfg


class RunScanAlwaysResetsRunning(unittest.TestCase):
    """A crashed scan used to leave running=True forever — every resolve job
    then waited an hour and nothing was ever added to the library. The
    finally block must guarantee the reset."""

    def test_a_hard_kill_still_clears_the_flag(self):
        import copy as _copy
        snapshot = _copy.deepcopy(status.status)
        saved = {
            "scan_artist": mon_mod.scan_artist,
            "build_source": src_mod.build_source,
            "check": lib_mod.check_library_files,
            "stopped": mon_mod._stopped,
        }
        mon_mod._stopped = lambda: False
        mon_mod.scan_artist = lambda *a, **k: None
        lib_mod.check_library_files = lambda *a, **k: None

        class _Src:
            priority = ""

            def _auth(self):
                pass

        def _build(name, cfg):
            return _Src()

        src_mod.build_source = _build
        # A BaseException escapes even the per-artist try/except — the finally
        # must still run (this simulates the crash that stuck the old flag).
        def _explode_hard(*a, **k):
            raise KeyboardInterrupt

        mon_mod.scan_artist = _explode_hard
        cfg = {"artists": [{"name": "Miraj"}], "music_source": "zvuk",
               "download_threads": 1, "save_folder": tempfile.mkdtemp()}
        try:
            with self.assertRaises(KeyboardInterrupt):
                mon_mod.run_scan(cfg)
            self.assertFalse(status.status["running"],
                             "a crashed scan must clear the running flag")
            self.assertIn(status.status["current_stage"], ("✅ Завершено", "⏹ Остановлено"))
        finally:
            mon_mod.scan_artist = saved["scan_artist"]
            mon_mod._stopped = saved["stopped"]
            src_mod.build_source = saved["build_source"]
            lib_mod.check_library_files = saved["check"]
            status.status.clear()
            status.status.update(snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
