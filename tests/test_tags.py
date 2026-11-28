"""Artist must live in the mp3 METADATA (ID3 tags), not just the filename.

Historically the converter stripped every tag (`-map_metadata -1`) and
nothing ever wrote artist/title — players showed an anonymous file. Now:
every produced file is tagged, and a backfill pass fixes existing files
(tracks DB first, then the save-folder layout Artist/Album/Track.mp3).
"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tags as tags_mod  # noqa: E402
from core import db as db_mod  # noqa: E402
from core import monitor as mon_mod  # noqa: E402
from core import zvuk as zv_mod  # noqa: E402
from core import library as lib_mod  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402


def _minimal_mp3(path):
    """A tiny but structurally valid MPEG-1 Layer III stream (no ffmpeg
    needed in the sandbox): 30 x 417-byte frames, 128 kbps / 44100 Hz."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413   # 417 bytes
    with open(path, "wb") as f:
        f.write(frame * 30)
    return path


def _artist_of(path):
    fr = ID3(path).getall("TPE1")
    return str(fr[0].text[0]) if fr and fr[0].text else ""


def _title_of(path):
    fr = ID3(path).getall("TIT2")
    return str(fr[0].text[0]) if fr and fr[0].text else ""


def _album_of(path):
    fr = ID3(path).getall("TALB")
    return str(fr[0].text[0]) if fr and fr[0].text else ""


class TagMp3(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_writes_artist_title_album_into_id3(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))

        self.assertTrue(tags_mod.tag_mp3(p, "Miraj", "Basic Bitches", "Album X"))

        self.assertEqual(_artist_of(p), "Miraj")
        self.assertEqual(_title_of(p), "Basic Bitches")
        self.assertEqual(_album_of(p), "Album X")

    def test_an_already_tagged_file_is_not_rewritten(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))
        tags_mod.tag_mp3(p, "Miraj", "Basic Bitches")
        mtime = os.path.getmtime(p)

        self.assertFalse(tags_mod.tag_mp3(p, "Miraj", "Basic Bitches"),
                         "identical tags must be skipped (no mtime churn)")
        self.assertEqual(os.path.getmtime(p), mtime)

    def test_a_wrong_artist_is_corrected(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))
        tags_mod.tag_mp3(p, "SomeUploader", "Basic Bitches")

        self.assertTrue(tags_mod.tag_mp3(p, "Miraj", "Basic Bitches"))
        self.assertEqual(_artist_of(p), "Miraj")

    def test_tagging_never_raises_on_a_missing_file(self):
        self.assertFalse(tags_mod.tag_mp3(
            os.path.join(self.dir, "gone.mp3"), "Miraj"))


class Backfill(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved_get_all = db_mod.get_all_tracks
        self._saved_backfill_lock = tags_mod._BACKFILL_LOCK

    def tearDown(self):
        db_mod.get_all_tracks = self._saved_get_all
        tags_mod._BACKFILL_LOCK = self._saved_backfill_lock

    def test_files_on_disk_get_the_artist_from_the_folder_layout(self):
        album_dir = os.path.join(self.dir, "Miraj", "Album X")
        os.makedirs(album_dir)
        p = _minimal_mp3(os.path.join(album_dir, "Basic Bitches.mp3"))
        deep = os.path.join(self.dir, "Rock", "Miraj", "Album Y")
        os.makedirs(deep)
        p2 = _minimal_mp3(os.path.join(deep, "Other Song.mp3"))

        stats = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(_artist_of(p), "Miraj")
        self.assertEqual(_album_of(p), "Album X")
        self.assertEqual(_artist_of(p2), "Miraj",
                         "genre subfolders do not hide the artist")
        self.assertEqual(stats["tagged"], 2)

    def test_db_rows_win_over_the_folder_names(self):
        album_dir = os.path.join(self.dir, "Folder Name", "Album")
        os.makedirs(album_dir)
        p = _minimal_mp3(os.path.join(album_dir, "Track.mp3"))
        db_mod.get_all_tracks = lambda limit=1000: [
            {"artist": "Real Artist", "track": "Real Title", "filepath": p}]

        stats = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(_artist_of(p), "Real Artist")
        self.assertEqual(_title_of(p), "Real Title")
        self.assertEqual(stats["tagged"], 1,
                         "the file must not be visited twice (db then disk)")

    def test_a_second_run_is_a_noop(self):
        album_dir = os.path.join(self.dir, "Miraj", "Album X")
        os.makedirs(album_dir)
        _minimal_mp3(os.path.join(album_dir, "Basic Bitches.mp3"))

        first = tags_mod.backfill_tags({"save_folder": self.dir})
        second = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(first["tagged"], 1)
        self.assertEqual(second["tagged"], 0,
                         "correctly tagged files are skipped")
        self.assertEqual(second["skipped"], 1)

    def test_db_missing_rows_do_not_break_the_pass(self):
        def _boom(limit=1000):
            raise RuntimeError("db locked")
        db_mod.get_all_tracks = _boom
        album_dir = os.path.join(self.dir, "Miraj", "Album X")
        os.makedirs(album_dir)
        p = _minimal_mp3(os.path.join(album_dir, "Song.mp3"))

        stats = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(_artist_of(p), "Miraj",
                         "disk walk still fixes what the db cannot")


class FallbackWriter(unittest.TestCase):
    """mutagen is optional: a missing installation must never mean «теги не
    записаны» — the built-in pure-python ID3v2.3 writer takes over. The
    (installed) mutagen reader then acts as an independent verifier."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved_have = tags_mod._HAVE_MUTAGEN
        self._saved_noted = tags_mod._FALLBACK_NOTED
        tags_mod._HAVE_MUTAGEN = False
        tags_mod._FALLBACK_NOTED = True   # keep the one-time notice quiet

    def tearDown(self):
        tags_mod._HAVE_MUTAGEN = self._saved_have
        tags_mod._FALLBACK_NOTED = self._saved_noted

    def test_without_mutagen_the_artist_is_still_written(self):
        p = _minimal_mp3(os.path.join(self.dir, "Слабый человек.mp3"))

        self.assertTrue(tags_mod.tag_mp3(
            p, "Три дня дождя", "Слабый человек", "Любовь, аддикция и"))

        self.assertEqual(_artist_of(p), "Три дня дождя")
        self.assertEqual(_title_of(p), "Слабый человек")
        self.assertEqual(_album_of(p), "Любовь, аддикция и")

    def test_fallback_tags_replace_the_old_tag_exactly_once(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))
        tags_mod.tag_mp3(p, "SomeUploader", "Track")
        self.assertTrue(tags_mod.tag_mp3(p, "Miraj", "Track"))

        with open(p, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, b"ID3")
        self.assertEqual(_artist_of(p), "Miraj",
                         "the old tag must be replaced, not nested")
        self.assertFalse(tags_mod.tag_mp3(p, "Miraj", "Track"),
                         "the second identical run is a no-op")

    def test_the_fallback_reader_understands_its_own_and_mutagen_tags(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))
        tags_mod.tag_mp3(p, "Miraj", "Song")

        frames = tags_mod._read_fallback_frames(p)
        self.assertEqual(frames.get("artist"), "Miraj")

        # A mutagen-written tag is also readable by the fallback parser
        tags_mod._HAVE_MUTAGEN = True
        tags_mod.tag_mp3(p, "Другой", "Song")   # mutagen rewrites the tag
        tags_mod._HAVE_MUTAGEN = False
        self.assertEqual(tags_mod._read_fallback_frames(p).get("artist"),
                         "Другой")

    def test_backfill_works_without_mutagen(self):
        album_dir = os.path.join(self.dir, "Три дня дождя", "Альбом")
        os.makedirs(album_dir)
        p = _minimal_mp3(os.path.join(album_dir, "Слабый человек.mp3"))

        stats = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(_artist_of(p), "Три дня дождя")
        self.assertEqual(stats["tagged"], 1,
                         "a missing mutagen must not skip the backfill")


class FutureDownloads(unittest.TestCase):
    """Every freshly downloaded file is tagged at the moment it appears."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved_zvuk = zv_mod.ZvukSource
        self._saved_add = db_mod.add_track
        self._saved_update = lib_mod.update_track_status

        class _FakeZvuk:
            last_quality = "mid"

            def __init__(self, token=None, proxy=None):
                pass

            def download_audio(self, track_id, out_no_ext, quality="mid"):
                _minimal_mp3(out_no_ext + ".mp3")

        zv_mod.ZvukSource = _FakeZvuk
        db_mod.add_track = lambda *a, **k: None
        lib_mod.update_track_status = lambda *a, **k: None

    def tearDown(self):
        zv_mod.ZvukSource = self._saved_zvuk
        db_mod.add_track = self._saved_add
        lib_mod.update_track_status = self._saved_update

    def test_a_zvuk_download_carries_the_artist_in_its_tags(self):
        out = os.path.join(self.dir, "Basic Bitches")

        mon_mod._zvuk_download("121431601", out, "Album X", "Miraj",
                               "Basic Bitches", 158000,
                               {"save_folder": self.dir})

        p = out + ".mp3"
        self.assertEqual(_artist_of(p), "Miraj",
                         "the monitored artist must be in the metadata")
        self.assertEqual(_title_of(p), "Basic Bitches")
        self.assertEqual(_album_of(p), "Album X")


if __name__ == "__main__":
    unittest.main()
