"""Embedded album covers (~300×300, the economical option the user chose).

One cover per album: resolved via Deezer's public album search (no auth),
downloaded once (cached), embedded into ID3 APIC. Files that already carry a
cover are never refetched or rewritten; failures degrade to text-only tags.
"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import covers as cov_mod  # noqa: E402
from core import tags as tags_mod  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402

JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 40)  # jpeg-looking


def _minimal_mp3(path):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 30)
    return path


def _cover_of(path):
    pics = ID3(path).getall("APIC")
    if not pics:
        return None
    return pics[0].mime, bytes(pics[0].data)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = payload if isinstance(payload, bytes) else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class CoverResolve(unittest.TestCase):

    def setUp(self):
        self._saved_get = cov_mod.requests.get
        cov_mod.clear_caches()

    def tearDown(self):
        cov_mod.requests.get = self._saved_get
        cov_mod.clear_caches()

    def test_a_cover_is_resolved_via_the_public_deezer_search(self):
        seen = {}

        def _get(url, params=None, timeout=None, proxies=None):
            seen["url"], seen["q"] = url, params["q"]
            return _FakeResponse({"data": [{
                "title": "Album", "cover_medium": "https://e.cd/300.jpg"}]})

        cov_mod.requests.get = _get

        url = cov_mod.get_cover_url("Три дня дождя", "Любовь, аддикция и")

        self.assertEqual(url, "https://e.cd/300.jpg")
        self.assertIn("api.deezer.com/search/album", seen["url"])
        self.assertEqual(seen["q"], "Три дня дождя Любовь, аддикция и")

    def test_the_resolution_is_cached_per_album(self):
        calls = []

        def _get(url, params=None, timeout=None, proxies=None):
            calls.append(params["q"])
            return _FakeResponse({"data": [
                {"cover_medium": "https://e.cd/300.jpg"}]})

        cov_mod.requests.get = _get

        cov_mod.get_cover_url("Miraj", "Album X")
        cov_mod.get_cover_url("Miraj", "Album X")   # same album, any case
        cov_mod.get_cover_url("MIRAJ", "album x")

        self.assertEqual(len(calls), 1, "one resolve per album per TTL")

    def test_a_failed_lookup_degrades_to_none_and_is_retried_later(self):
        def _get(url, params=None, timeout=None, proxies=None):
            raise RuntimeError("no network")

        cov_mod.requests.get = _get

        self.assertIsNone(cov_mod.get_cover_url("Miraj", "Album X"))
        self.assertIsNone(cov_mod.get_cover_url("Miraj", "Album X"))
        cov_mod.clear_caches()
        calls = []

        def _ok(url, params=None, timeout=None, proxies=None):
            calls.append(1)
            return _FakeResponse({"data": [{"cover_medium": "https://e/1.jpg"}]})

        cov_mod.requests.get = _ok
        self.assertEqual(cov_mod.get_cover_url("Miraj", "Album X"),
                         "https://e/1.jpg")

    def test_bytes_are_downloaded_once_per_url(self):
        calls = []

        def _get(url, params=None, timeout=None, proxies=None):
            calls.append(url)
            return _FakeResponse(JPEG)

        cov_mod.requests.get = _get

        self.assertEqual(cov_mod.get_cover_bytes("https://e.cd/300.jpg"), JPEG)
        self.assertEqual(cov_mod.get_cover_bytes("https://e.cd/300.jpg"), JPEG)
        self.assertEqual(len(calls), 1)


class CoverEmbed(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved_get = cov_mod.requests.get
        self._saved_have = tags_mod._HAVE_MUTAGEN
        cov_mod.clear_caches()
        cov_mod.requests.get = lambda url, **k: _FakeResponse(JPEG)

    def tearDown(self):
        cov_mod.requests.get = self._saved_get
        tags_mod._HAVE_MUTAGEN = self._saved_have
        cov_mod.clear_caches()

    def test_the_cover_lands_in_the_tags_via_url(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))

        self.assertTrue(tags_mod.tag_mp3(
            p, "Три дня дождя", "Слабый человек", "Любовь, аддикция и",
            cover_url="https://e.cd/300.jpg"))

        mime, data = _cover_of(p)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data, JPEG)

    def test_a_file_that_already_has_a_cover_is_not_refetched(self):
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))
        tags_mod.tag_mp3(p, "Miraj", "Track", "Album X",
                         cover_bytes=JPEG)

        self.assertFalse(tags_mod.tag_mp3(
            p, "Miraj", "Track", "Album X",
            cover_url="https://e.cd/300.jpg"),
            "cover present + same text tags = no rewrite, no fetch")

    def test_the_fallback_writer_embeds_the_cover_too(self):
        tags_mod._HAVE_MUTAGEN = False
        tags_mod._FALLBACK_NOTED = True
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))

        self.assertTrue(tags_mod.tag_mp3(
            p, "Три дня дождя", "Слабый человек", "Альбом",
            cover_url="https://e.cd/300.jpg"))

        # mutagen (the independent reader) verifies the fallback-written APIC
        mime, data = _cover_of(p)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data, JPEG)
        self.assertEqual(str(ID3(p).getall("TPE1")[0].text[0]),
                         "Три дня дождя")

    def test_without_a_network_the_text_tags_still_land(self):
        def _get(url, params=None, timeout=None, proxies=None):
            raise RuntimeError("no network")

        cov_mod.requests.get = _get
        p = _minimal_mp3(os.path.join(self.dir, "Track.mp3"))

        self.assertTrue(tags_mod.tag_mp3(
            p, "Miraj", "Track", "Album X", cover_url="https://e.cd/300.jpg"))

        self.assertIsNone(_cover_of(p), "no bytes -> no APIC, but:")
        self.assertEqual(str(ID3(p).getall("TPE1")[0].text[0]), "Miraj")


class BackfillCovers(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved_get = cov_mod.requests.get
        self._saved_rows = tags_mod.db_mod.get_all_tracks
        cov_mod.clear_caches()
        cov_mod.requests.get = lambda url, **k: _FakeResponse(JPEG)

    def tearDown(self):
        cov_mod.requests.get = self._saved_get
        tags_mod.db_mod.get_all_tracks = self._saved_rows
        cov_mod.clear_caches()

    def test_backfill_resolves_one_cover_per_album(self):
        album_dir = os.path.join(self.dir, "Miraj", "Album X")
        os.makedirs(album_dir)
        for i in range(3):
            _minimal_mp3(os.path.join(album_dir, "Song %d.mp3" % i))
        calls = []

        def _get(url, params=None, timeout=None, proxies=None):
            calls.append((url, (params or {}).get("q")))
            if "search/album" in url:
                return _FakeResponse({"data": [{"cover_medium":
                                                "https://e.cd/300.jpg"}]})
            return _FakeResponse(JPEG)

        cov_mod.requests.get = _get

        stats = tags_mod.backfill_tags({"save_folder": self.dir})

        self.assertEqual(stats["tagged"], 3)
        resolves = [c for c in calls if "search/album" in c[0]]
        self.assertEqual(len(resolves), 1,
                         "one cover per album, not per track: %s" % calls)
        downloads = [c for c in calls if "search/album" not in c[0]]
        self.assertEqual(len(downloads), 1, "image bytes fetched once")


if __name__ == "__main__":
    unittest.main()
