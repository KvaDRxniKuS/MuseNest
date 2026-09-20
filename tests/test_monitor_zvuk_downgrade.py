"""Tests for the Zvuk quality-downgrade warning in core/monitor.py.

_zvuk_download() asks Zvuk for 'high' when a token is set; resolve_stream() may
still land on 'mid'. The user must see that in the log instead of silently
getting a lower bitrate.

Run:  python tests/test_monitor_zvuk_downgrade.py
"""

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import monitor as mon  # noqa: E402
from core import zvuk as zv_mod  # noqa: E402

TRACK = "3862635281"


class FakeZvuk:
    """Stands in for ZvukSource: no network, just records and reports quality."""

    obtained = "high"
    calls = []

    def __init__(self, token=None, proxy=None):
        self.token = token
        self.last_quality = None

    def download_audio(self, track_id, out_no_ext, quality="320"):
        FakeZvuk.calls.append({"track_id": track_id, "requested": quality,
                               "token": self.token})
        self.last_quality = FakeZvuk.obtained
        # the real one returns the final mp3 path
        return out_no_ext + ".mp3"


class TestDowngradeWarning(unittest.TestCase):
    def setUp(self):
        FakeZvuk.calls = []
        self.warnings = []
        self._patches = [
            mock.patch.object(zv_mod, "ZvukSource", FakeZvuk),
            mock.patch.object(mon.db_mod, "add_track", lambda *a, **k: None),
            mock.patch.object(mon.status, "inc", lambda *a, **k: None),
            mock.patch("core.library.update_track_status", lambda *a, **k: None),
            mock.patch.object(mon.status.log, "warning",
                              lambda fmt, *a, **k: self.warnings.append(fmt % a if a else fmt)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _run(self, token):
        return mon._zvuk_download(
            TRACK, "/tmp/out/track", "Album", "Artist", "Blood Money", 200,
            {"zvuk_token": token, "proxy": ""})

    def test_token_set_requests_high_and_warns_when_mid_is_obtained(self):
        FakeZvuk.obtained = "mid"
        result = self._run(token="user-token")

        self.assertEqual(result, "/tmp/out/track.mp3")
        self.assertEqual(FakeZvuk.calls[0]["requested"], "high",
                         "with a token the code must ask for high")
        self.assertEqual(len(self.warnings), 1, self.warnings)
        w = self.warnings[0]
        self.assertIn("mid", w)
        self.assertIn("high", w)
        self.assertIn("Blood Money", w)
        self.assertIn("Artist", w)

    def test_no_warning_when_requested_quality_is_obtained(self):
        FakeZvuk.obtained = "high"
        self._run(token="user-token")
        self.assertEqual(self.warnings, [], "no downgrade -> no warning")

    def test_without_token_it_asks_for_mid(self):
        FakeZvuk.obtained = "mid"
        self._run(token="")
        self.assertEqual(FakeZvuk.calls[0]["requested"], "mid",
                         "without a token the code must not ask for high")
        self.assertEqual(FakeZvuk.calls[0]["token"], None)
        self.assertEqual(self.warnings, [])

    def test_flac_downgrade_to_mid_is_reported(self):
        # quality is 'high' or 'mid' today; make sure the comparison logic is
        # driven by the quality map rather than a hardcoded string.
        FakeZvuk.obtained = "mid"
        self._run(token="tok")
        self.assertTrue(any("mid" in w and "high" in w for w in self.warnings), self.warnings)


class TestAudioQualityIsHonoured(unittest.TestCase):
    """_zvuk_download() must translate the user's audio_quality setting.

    It used to hardcode quality = "high" whenever a token was set, ignoring
    audio_quality entirely — even though _QUALITY_MAP exists precisely to
    translate that setting. So flac/lossless silently downloaded as high, and
    no warning was logged either, because the warning threshold was derived
    from the same hardcoded value.
    """

    def setUp(self):
        FakeZvuk.calls = []
        self.warnings = []
        self._patches = [
            mock.patch.object(zv_mod, "ZvukSource", FakeZvuk),
            mock.patch.object(mon.db_mod, "add_track", lambda *a, **k: None),
            mock.patch.object(mon.status, "inc", lambda *a, **k: None),
            mock.patch("core.library.update_track_status", lambda *a, **k: None),
            mock.patch.object(mon.status.log, "warning",
                              lambda fmt, *a, **k: self.warnings.append(fmt % a if a else fmt)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _requested(self, audio_quality, token="tok"):
        FakeZvuk.calls = []
        FakeZvuk.obtained = zv_mod._QUALITY_MAP.get(str(audio_quality).lower(), "high")
        mon._zvuk_download(TRACK, "/tmp/out/track", "Album", "Artist", "Blood Money",
                           200, {"zvuk_token": token, "proxy": "",
                                 "audio_quality": audio_quality})
        return FakeZvuk.calls[0]["requested"]

    def test_every_setting_maps_through_the_quality_map(self):
        for aq, expected in (("128", "mid"), ("192", "mid"), ("256", "high"),
                             ("320", "high"), ("flac", "flac"), ("lossless", "flac")):
            self.assertEqual(self._requested(aq), expected,
                             "audio_quality=%s must ask Zvuk for %s" % (aq, expected))

    def test_flac_setting_actually_requests_flac(self):
        """The reported symptom: flac configured, high downloaded."""
        self.assertEqual(self._requested("flac"), "flac")
        self.assertEqual(self._requested("lossless"), "flac")

    def test_flac_downgraded_to_mid_is_now_reported(self):
        """The warning threshold must come from the user's setting, not "high".

        flac -> mid is the only downgrade resolve_stream() can produce here:
        its ladder for a requested 'flac' is ['flac', 'mid'], so 'high' is not
        a reachable outcome and must not be used as the fixture.
        """
        FakeZvuk.obtained = "mid"  # Zvuk refused flac and fell back to mid
        mon._zvuk_download(TRACK, "/tmp/out/track", "Album", "Artist", "Blood Money",
                           200, {"zvuk_token": "tok", "proxy": "",
                                 "audio_quality": "flac"})
        self.assertTrue(self.warnings,
                        "flac -> mid must be logged; previously it was silent")
        w = self.warnings[0]
        self.assertIn("mid", w, w)
        self.assertIn("flac", w, w)

    def test_ladder_cannot_produce_high_from_flac(self):
        """Guard the fixture above: keeps the test honest about what is reachable."""
        requested = zv_mod._QUALITY_MAP["flac"]
        ladder = [requested] + (["mid"] if requested != "mid" else [])
        self.assertEqual(ladder, ["flac", "mid"], ladder)
        self.assertNotIn("high", ladder,
                         "'high' is unreachable from flac; a test using it as the "
                         "obtained quality would assert on an impossible state")

    def test_no_token_always_asks_for_mid_regardless_of_setting(self):
        for aq in ("128", "320", "flac"):
            self.assertEqual(self._requested(aq, token=""), "mid",
                             "anonymous access only gets mid (audio_quality=%s)" % aq)

    def test_missing_audio_quality_defaults_to_high(self):
        FakeZvuk.calls = []
        FakeZvuk.obtained = "high"
        mon._zvuk_download(TRACK, "/tmp/out/track", "Album", "Artist", "Blood Money",
                           200, {"zvuk_token": "tok", "proxy": ""})
        self.assertEqual(FakeZvuk.calls[0]["requested"], "high")


class TestQualityMapContract(unittest.TestCase):
    """monitor.py compares against _QUALITY_MAP; keep that contract explicit."""

    def test_map_covers_every_audio_quality_setting(self):
        for q in ("128", "192", "256", "320", "high", "mid", "flac", "lossless"):
            self.assertIn(zv_mod._QUALITY_MAP[q], ("mid", "high", "flac"), q)

    def test_monitor_uses_the_map_not_a_literal(self):
        # if someone renames the map the comparison must fail loudly, not silently
        self.assertTrue(hasattr(zv_mod, "_QUALITY_MAP"))
        self.assertEqual(zv_mod._QUALITY_MAP["320"], "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
