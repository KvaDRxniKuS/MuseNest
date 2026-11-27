"""Tests for POST /api/artist_search.

Reported from real use: "artist search stopped working, more than 10 seconds and
not a single result". Two defects in the route explain that:

1. The route collects the four source workers with ``as_completed(..., timeout=6)``
   but does it inside ``with ThreadPoolExecutor(...)``. Leaving a ``with`` block
   calls ``shutdown(wait=True)``, so the response still waits for every hung
   worker to hit its own socket timeout - the 6 second budget was decoration.
2. When every source failed the route answered ``[]`` with HTTP 200, which the UI
   renders as "nothing found - Enter will add as is". An outage and an empty
   result looked identical, so the real cause never reached the user.

Everything runs offline: the four source classes are patched in-process, no
network is touched.

Run:  python tests/test_artist_search.py
"""

import os
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import app as app_mod  # noqa: E402
from core import source as src_mod  # noqa: E402
from core import yandex as ya_mod  # noqa: E402
from core import zvuk as zv_mod  # noqa: E402
zv_mod.set_stream_pacing(0.0)  # local test stand: no anti-bot pacing
from core import spotify as sp_mod  # noqa: E402


def _artist(name, aid):
    return {"id": aid, "name": name, "followers": 100, "link": None}


class ArtistSearchRoute(unittest.TestCase):
    def setUp(self):
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()
        self._saved = {
            "dz": src_mod.DeezerSource.search_artists,
            "mb": src_mod.MusicBrainzSource.search_artists,
            "ya": ya_mod.YandexSource.search_artists,
            "zv": zv_mod.ZvukSource.search_artists,
            "sp": sp_mod.SpotifyClient.search_artists,
        }

    def tearDown(self):
        src_mod.DeezerSource.search_artists = self._saved["dz"]
        src_mod.MusicBrainzSource.search_artists = self._saved["mb"]
        ya_mod.YandexSource.search_artists = self._saved["ya"]
        zv_mod.ZvukSource.search_artists = self._saved["zv"]
        sp_mod.SpotifyClient.search_artists = self._saved["sp"]

    def search(self, q="starset"):
        return self.client.post("/api/artist_search", json={"query": q})

    def _patch_all(self, fn):
        src_mod.DeezerSource.search_artists = fn
        src_mod.MusicBrainzSource.search_artists = fn
        ya_mod.YandexSource.search_artists = fn
        zv_mod.ZvukSource.search_artists = fn
        sp_mod.SpotifyClient.search_artists = fn

    # ------------------------------------------------------------------

    def test_results_come_back_as_a_list(self):
        self._patch_all(lambda self, query, limit=8: [_artist("Starset", "1")])
        resp = self.search()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, list)
        self.assertTrue(any(r["name"] == "Starset" for r in body), body)

    def test_empty_query_answers_immediately(self):
        resp = self.search("")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_one_working_source_is_enough(self):
        def mixed(self, query, limit=8):
            if isinstance(self, src_mod.DeezerSource):
                return [_artist("Starset", "dz1")]
            raise ConnectionError("offline")
        self._patch_all(mixed)
        resp = self.search()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, list)
        self.assertTrue(any(r["name"] == "Starset" for r in body), body)

    def test_a_hanging_source_does_not_block_the_response(self):
        """The budget in the route is 6 s; the answer must respect it."""
        def hanging(self, query, limit=8):
            if isinstance(self, src_mod.DeezerSource):
                time.sleep(20)          # far past the budget and any socket timeout
                return []
            return [_artist("Starset", "1")]
        self._patch_all(hanging)

        started = time.time()
        resp = self.search()
        elapsed = time.time() - started

        self.assertLess(elapsed, 10.0,
                        f"response waited {elapsed:.1f}s for a hung worker - the "
                        f"6s budget is not enforced")
        # the other sources did answer, so their results must not be lost
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, list)
        self.assertTrue(any(r["name"] == "Starset" for r in body), body)

    def test_all_sources_failing_says_so_instead_of_an_empty_list(self):
        def dead(self, query, limit=8):
            raise ConnectionError("TLS/SSL connection has been closed (EOF)")
        self._patch_all(dead)

        resp = self.search()
        self.assertNotEqual(resp.status_code, 200,
                            "an outage must not answer 200 with [] - the UI would "
                            "show 'nothing found'")
        body = resp.get_json()
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("TLS/SSL", body["error"], body["error"])

    def test_all_sources_hanging_says_so_instead_of_an_empty_list(self):
        def hanging(self, query, limit=8):
            time.sleep(20)
            return []
        self._patch_all(hanging)

        started = time.time()
        resp = self.search()
        elapsed = time.time() - started

        self.assertLess(elapsed, 10.0, f"waited {elapsed:.1f}s")
        self.assertNotEqual(resp.status_code, 200)
        self.assertIn("error", resp.get_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
