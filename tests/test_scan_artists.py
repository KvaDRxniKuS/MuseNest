"""Tests for the artist scanner (tools/scan_artists.py).

The utility exists to answer one question - "who do I actually listen to" -
over an old library, so the tests pin the three things that decide the answer:

* the file-name convention parser, including the trap that a band starting
  with a digit ("3 Doors Down") must not lose that digit;
* the embedded-tag readers, exercised against synthesised ID3v2.2/2.3/2.4 and
  FLAC Vorbis-comment files (UTF-8 and UTF-16 both), because a
  Artist/Album/01 - Title library has no artist in the file name at all;
* the folder fallback, which has to skip year / "Disc N" folders and must not
  report the scanned root folder itself as a performer.

Plus one end-to-end run over a temporary tree, asserting the exact contents of
the produced artists.txt and that MuseNest's own load_artists_from_txt() can
read that file back.

Run:  python tests/test_scan_artists.py
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import scan_artists as sa  # noqa: E402


def _synchsafe(n):
    out = bytearray(4)
    for i in range(3, -1, -1):
        out[i] = n & 0x7F
        n >>= 7
    return bytes(out)


def make_mp3(artist, major=3, enc=3):
    """A minimal mp3: ID3v2 tag (TPE1/TP1) followed by junk 'audio' bytes."""
    if enc == 3:
        payload = bytes([3]) + artist.encode("utf-8")
    elif enc == 1:
        payload = bytes([1]) + artist.encode("utf-16")  # includes the BOM
    elif enc == 2:
        payload = bytes([2]) + artist.encode("utf-16-be")
    else:
        payload = bytes([0]) + artist.encode("latin-1")

    if major == 2:
        frame = b"TP1" + len(payload).to_bytes(3, "big")
    else:
        frame = b"TPE1" + len(payload).to_bytes(4, "big") + b"\x00\x00"

    body = frame + payload
    header = b"ID3" + bytes([major, 0, 0]) + _synchsafe(len(body))
    return header + body + b"\xff\xfb\x90\x00" + b"\x00" * 64


def make_flac(artist, key="ARTIST", extra=("TITLE=Some Song", "ALBUM=Test")):
    """A minimal flac: fLaC magic + one VORBIS_COMMENT block."""
    def field(text):
        raw = text.encode("utf-8")
        return len(raw).to_bytes(4, "little") + raw

    entries = list(extra) + [f"{key}={artist}"]
    body = field("reference libFLAC 1.4.3")
    body += len(entries).to_bytes(4, "little") + b"".join(field(e) for e in entries)
    block = bytes([0x84]) + len(body).to_bytes(3, "big") + body  # last block, type 4
    return b"fLaC" + block + b"\x00" * 32


def touch(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class FileNameParsing(unittest.TestCase):
    def test_artist_dash_title(self):
        self.assertEqual(sa.artist_from_filename("Starset - My Demons"), "Starset")

    def test_artist_dash_album_dash_title(self):
        self.assertEqual(sa.artist_from_filename("Starset - Horizons - Satellite"), "Starset")

    def test_leading_track_number_is_stripped(self):
        self.assertEqual(sa.artist_from_filename("01. Starset - My Demons"), "Starset")
        self.assertEqual(sa.artist_from_filename("01) Starset - My Demons"), "Starset")
        self.assertEqual(sa.artist_from_filename("#01 Starset - My Demons"), "Starset")
        self.assertEqual(sa.artist_from_filename("12 - Пикник - Твоё сердце"), "Пикник")

    def test_band_starting_with_a_digit_keeps_it(self):
        """'3 Doors Down' must survive: only a number followed by ./)/- is a track no."""
        self.assertEqual(sa.artist_from_filename("3 Doors Down - It's Not Enough"), "3 Doors Down")
        self.assertEqual(sa.artist_from_filename("30 Seconds To Mars - The Kill"),
                         "30 Seconds To Mars")
        self.assertEqual(sa.artist_from_filename("2Pac - Changes"), "2Pac")

    def test_bare_track_number_yields_nothing(self):
        """'01 - Title' has no artist in the name - the caller must use the folder."""
        self.assertEqual(sa.artist_from_filename("01 - Satellite"), "")
        self.assertEqual(sa.artist_from_filename("01. Satellite"), "")

    def test_no_separator_yields_nothing(self):
        self.assertEqual(sa.artist_from_filename("Satellite"), "")

    def test_cyrillic(self):
        self.assertEqual(sa.artist_from_filename("Король и Шут - Кукла колдуна"), "Король и Шут")

    def test_en_dash_and_fullwidth_dash_are_normalised(self):
        self.assertEqual(sa.artist_from_filename("Starset \u2013 My Demons"), "Starset")

    def test_clean_name_collapses_spaces_and_trims(self):
        self.assertEqual(sa.clean_name("  Starset   "), "Starset")
        self.assertEqual(sa.clean_name(" 30  Seconds   To Mars "), "30 Seconds To Mars")


class JunkDetection(unittest.TestCase):
    def test_junk_artists(self):
        for name in ("Unknown", "Unknown Artist", "VA", "Various Artists", "Неизвестный",
                     "Разные исполнители", "1234", "x", ""):
            self.assertTrue(sa.is_junk_artist(name), name)

    def test_real_artists_are_not_junk(self):
        for name in ("Starset", "Пикник", "3 Doors Down", "AC/DC", "t.A.T.u."):
            self.assertFalse(sa.is_junk_artist(name), name)

    def test_junk_folders(self):
        for name in ("2005", "Disc 1", "CD2", "Disk 12", "discography", "", "2011-2015"):
            self.assertTrue(sa.is_junk_folder(name), name)

    def test_real_folders(self):
        for name in ("Starset", "Linkin Park", "Пикник"):
            self.assertFalse(sa.is_junk_folder(name), name)


class TagReading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _mp3(self, name, artist, major=3, enc=3):
        return touch(os.path.join(self.dir, name), make_mp3(artist, major=major, enc=enc))

    def test_id3v23_utf8(self):
        path = self._mp3("a.mp3", "Starset")
        self.assertEqual(sa.read_id3_artist(path), "Starset")

    def test_id3v23_utf16(self):
        path = self._mp3("b.mp3", "Король и Шут", enc=1)
        self.assertEqual(sa.read_id3_artist(path), "Король и Шут")

    def test_id3v23_utf16be(self):
        path = self._mp3("c.mp3", "Пикник", enc=2)
        self.assertEqual(sa.read_id3_artist(path), "Пикник")

    def test_id3v23_latin1(self):
        path = self._mp3("d.mp3", "Rammstein", enc=0)
        self.assertEqual(sa.read_id3_artist(path), "Rammstein")

    def test_id3v24_synchsafe_size(self):
        path = self._mp3("e.mp3", "Linkin Park", major=4)
        self.assertEqual(sa.read_id3_artist(path), "Linkin Park")

    def test_id3v22_three_byte_frames(self):
        path = self._mp3("f.mp3", "Audioslave", major=2)
        self.assertEqual(sa.read_id3_artist(path), "Audioslave")

    def test_no_tag_returns_none(self):
        path = touch(os.path.join(self.dir, "plain.mp3"), b"\xff\xfb\x90\x00" * 20)
        self.assertIsNone(sa.read_id3_artist(path))

    def test_flac_vorbis_artist(self):
        path = touch(os.path.join(self.dir, "t.flac"), make_flac("Пикник"))
        self.assertEqual(sa.read_flac_artist(path), "Пикник")

    def test_flac_falls_back_to_albumartist(self):
        path = touch(os.path.join(self.dir, "u.flac"),
                     make_flac("Various", key="ALBUMARTIST", extra=("TITLE=X",)))
        self.assertEqual(sa.read_flac_artist(path), "Various")

    def test_flac_without_magic_returns_none(self):
        path = touch(os.path.join(self.dir, "v.flac"), b"not a flac at all")
        self.assertIsNone(sa.read_flac_artist(path))

    def test_tag_wins_over_file_name(self):
        """A tagged file inside Artist/Album is resolved by the tag, not the folder."""
        path = touch(os.path.join(self.dir, "Starset", "Horizons", "01 - Satellite.mp3"),
                     make_mp3("Starset"))
        artist, how = sa.resolve_artist(path, self.dir)
        self.assertEqual((artist, how), ("Starset", "tag"))


class FolderFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_parent_folder_is_the_artist(self):
        path = touch(os.path.join(self.root, "Linkin Park", "01 - Numb.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root), ("Linkin Park", "folder"))

    def test_bare_track_number_falls_back_to_folder(self):
        path = touch(os.path.join(self.root, "Starset", "01.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root), ("Starset", "folder"))

    def test_year_folder_is_skipped(self):
        path = touch(os.path.join(self.root, "Audioslave", "2005", "01 - Be Yourself.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root)[0], "Audioslave")

    def test_disc_folder_is_skipped(self):
        path = touch(os.path.join(self.root, "Пикник", "Disc 1", "01.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root), ("Пикник", "folder"))

    def test_root_folder_is_never_an_artist(self):
        """Files lying straight in the scanned folder carry no artist info."""
        path = touch(os.path.join(self.root, "loose.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root), ("", "none"))

    def test_only_junk_folders_all_the_way_up(self):
        path = touch(os.path.join(self.root, "2005", "Disc 1", "01.mp3"))
        self.assertEqual(sa.resolve_artist(path, self.root), ("", "none"))


class ScanEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self):
        touch(os.path.join(self.root, "Starset - My Demons.mp3"))
        touch(os.path.join(self.root, "STARSET - Gravity Of You.mp3"))       # case dupe
        touch(os.path.join(self.root, "Starset", "Horizons", "01 - Satellite.mp3"))
        touch(os.path.join(self.root, "Starset", "Horizons", "02. Linkin Park - Numb.mp3"))
        touch(os.path.join(self.root, "2005", "Audioslave - Be Yourself.mp3"))
        touch(os.path.join(self.root, "Пикник", "Disc 1", "01 - Кукла колдуна.mp3"))
        touch(os.path.join(self.root, "tagged", "01 - Satellite.mp3"), make_mp3("Пикник"))
        touch(os.path.join(self.root, "tagged", "02.flac"), make_flac("Король и Шут"))
        touch(os.path.join(self.root, "loose.mp3"))                          # no info at all
        touch(os.path.join(self.root, "notes.txt"), b"not audio")            # ignored

    def test_scan_collects_deduplicated_artists(self):
        self._build()
        result = sa.scan(self.root)
        self.assertEqual(
            result["artists"],
            ["Audioslave", "Horizons", "Linkin Park", "Starset", "Король и Шут", "Пикник"],
        )
        self.assertEqual(result["stats"]["files"], 9)
        self.assertEqual(result["stats"]["none"], 1)  # loose.mp3

    def test_no_tags_mode_uses_names_and_folders(self):
        self._build()
        result = sa.scan(self.root, use_tags=False)
        # Without tags the two files under tagged/ resolve from their folder,
        # so "Король и Шут" (flac tag only) disappears and "tagged" appears.
        self.assertEqual(
            result["artists"],
            ["Audioslave", "Horizons", "Linkin Park", "Starset", "tagged", "Пикник"],
        )
        self.assertEqual(result["stats"]["tag"], 0)

    def test_written_file_is_readable_by_musenest(self):
        """artists.txt must be droppable into data/ without any conversion."""
        self._build()
        out = os.path.join(self.root, "artists.txt")
        result = sa.scan(self.root)
        sa.write_list(out, result["artists"], self.root, result["stats"])

        with open(out, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw.decode("utf-8").count("\r\n"), 0, "must be LF-only UTF-8")
        self.assertIn("# MuseNest artist scan", raw.decode("utf-8"))

        from core import config as cfg_mod
        loaded = cfg_mod.load_artists_from_txt(out)
        names = [a["name"] for a in loaded]
        self.assertEqual(names, result["artists"])
        self.assertNotIn("#", names)


class ProgressReporting(unittest.TestCase):
    """A scan of a big library used to print nothing until the very end, so the
    console looked dead. These pin the "still working" feedback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _collect(self, **kw):
        calls = []
        result = sa.scan(self.root, progress=lambda d, f, a: calls.append((d, f, a)), **kw)
        return calls, result

    def test_every_folder_is_reported_including_empty_ones(self):
        touch(os.path.join(self.root, "Starset", "01 - My Demons.mp3"))
        os.makedirs(os.path.join(self.root, "empty", "deeper"))
        calls, _ = self._collect()
        dirs = {os.path.relpath(c[0], self.root) for c in calls}
        self.assertIn(os.path.join("empty", "deeper"), dirs)
        self.assertIn("Starset", dirs)

    def test_artist_count_grows_while_scanning(self):
        # One folder per artist: the walk reports a rising count as it goes.
        for i, name in enumerate(("Starset", "Muse", "Пикник")):
            touch(os.path.join(self.root, name, f"01 - Track {i}.mp3"))
        calls, result = self._collect()
        counts = [c[2] for c in calls]
        self.assertEqual(counts, sorted(counts), "the reported count must never go down")
        self.assertTrue(any(c > 0 for c in counts), counts)
        self.assertEqual(len(result["artists"]), 3)

    def test_a_flat_folder_reports_midway_every_200_files(self):
        for i in range(450):
            touch(os.path.join(self.root, f"Starset - Track {i}.mp3"))
        calls, _ = self._collect()
        mid = [c[1] for c in calls if c[1] and c[1] % 200 == 0]
        self.assertIn(200, mid)
        self.assertIn(400, mid)

    def test_cli_prints_a_header_and_progress_before_the_summary(self):
        # Two folders, so the walk has something non-zero to report.
        touch(os.path.join(self.root, "Starset", "01 - My Demons.mp3"))
        touch(os.path.join(self.root, "Muse", "01 - Uprising.mp3"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sa.main([self.root, "-o", os.path.join(self.root, "artists.txt")])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("MuseNest artist scanner", out)
        self.assertIn("Scanning:", out)
        self.assertLess(out.index("Scanning:"), out.index("Scanned :"),
                        "the header must come before the summary")
        self.assertIn("files |", out)  # at least one progress line

    def test_quiet_keeps_the_summary_but_drops_progress(self):
        touch(os.path.join(self.root, "Starset", "01 - My Demons.mp3"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sa.main([self.root, "-o", os.path.join(self.root, "artists.txt"), "--quiet"])
        out = buf.getvalue()
        self.assertIn("Scanned :", out)
        self.assertNotIn("files |", out)

    def test_empty_folder_says_so_instead_of_printing_nothing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sa.main([self.root, "-o", os.path.join(self.root, "artists.txt"), "--quiet"])
        out = buf.getvalue()
        self.assertIn("No artists found", out)
        self.assertIn("Folders : 1", out)


class WindowsQuoting(unittest.TestCase):
    """A batch file passing "%~dp0" makes the trailing backslash escape the
    closing quote, so Python receives the path *with* a quote glued to it.
    That was reported from a real run as:
        error: not a folder: E:\\sync\\музыка\\моё"
    """

    def test_stray_trailing_quote_is_stripped(self):
        self.assertEqual(sa.clean_path_arg('E:\\sync\\музыка\\моё"'), 'E:\\sync\\музыка\\моё')

    def test_matching_pair_of_quotes_is_stripped(self):
        self.assertEqual(sa.clean_path_arg('"C:\\music"'), 'C:\\music')

    def test_plain_path_is_untouched(self):
        self.assertEqual(sa.clean_path_arg('/tmp/lib'), '/tmp/lib')
        self.assertEqual(sa.clean_path_arg('  /tmp/lib/  '), '/tmp/lib/')

    def test_empty_stays_empty(self):
        self.assertEqual(sa.clean_path_arg(''), '')
        self.assertEqual(sa.clean_path_arg('""'), '')


class CliBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_trailing_quote_from_a_batch_file_still_works(self):
        """main() must accept the exact argument `"%~dp0"` produces."""
        touch(os.path.join(self.root, "Starset - My Demons.mp3"))
        out = os.path.join(self.root, "artists.txt")
        rc = sa.main([self.root + '"', "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            names = [ln.rstrip("\n") for ln in fh if not ln.startswith("#") and ln.strip()]
        self.assertEqual(names, ["Starset"])

    def test_missing_folder_returns_2(self):
        rc = sa.main([os.path.join(self.root, "nope"), "-o", os.path.join(self.root, "a.txt")])
        self.assertEqual(rc, 2)

    def test_runs_and_writes_next_to_the_out_path(self):
        touch(os.path.join(self.root, "Starset - My Demons.mp3"))
        out = os.path.join(self.root, "artists.txt")
        rc = sa.main([self.root, "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if not ln.startswith("#")]
        self.assertEqual([ln for ln in lines if ln], ["Starset"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
