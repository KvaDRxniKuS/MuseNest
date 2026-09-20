"""Tests for core/vpn_probe.py — the yt-dlp / VPN diagnostics engine.

Run with:  python tests/test_vpn_probe.py        (or: python -m pytest tests/)

Most tests are offline. The two ``LiveFailures`` tests open a real socket to a
deliberately dead local proxy (127.0.0.1:1099) so the error-classification and
no-raise guarantees are exercised through the actual yt-dlp networking stack.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import vpn_probe as vp  # noqa: E402

DEAD_PROXY = "socks5://127.0.0.1:1099"


class TestNormalizeProxy(unittest.TestCase):
    def test_direct_words(self):
        for raw in ("", "direct", "none", "DIRECT", "без vpn", "напрямую", "-", None):
            self.assertIsNone(vp.normalize_proxy(raw), raw)

    def test_bare_host_port_defaults_to_socks5(self):
        self.assertEqual(vp.normalize_proxy("127.0.0.1:1080"), "socks5://127.0.0.1:1080")

    def test_socks_alias_and_case(self):
        self.assertEqual(vp.normalize_proxy("socks://host:1"), "socks5://host:1")
        self.assertEqual(vp.normalize_proxy("SOCKS5://Host:1080"), "socks5://Host:1080")
        self.assertEqual(vp.normalize_proxy("HTTP://h:8080"), "http://h:8080")

    def test_credentials_preserved(self):
        self.assertEqual(vp.normalize_proxy("http://u:p@h:8080"), "http://u:p@h:8080")


class TestProxyLabel(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(vp.proxy_label({"proxy": None}), "direct (без VPN)")

    def test_credentials_masked(self):
        label = vp.proxy_label({"proxy": "socks5://user:secret@1.2.3.4:1080"})
        self.assertNotIn("secret", label)
        self.assertIn("1.2.3.4:1080", label)
        self.assertIn("***", label)


class TestParseProfileSpec(unittest.TestCase):
    def test_text_with_equals_comma_and_comments(self):
        text = """
        # комментарий
        Нидерланды SOCKS5 = socks5://127.0.0.1:1080
        Германия HTTP, http://user:pass@1.2.3.4:8080
        direct
        target: https://www.youtube.com/watch?v=dQw4w9WgXcQ
        """
        profiles, targets = vp.parse_profile_spec(text)
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[0]["name"], "Нидерланды SOCKS5")
        self.assertEqual(profiles[0]["proxy"], "socks5://127.0.0.1:1080")
        self.assertEqual(profiles[1]["proxy"], "http://user:pass@1.2.3.4:8080")
        self.assertIsNone(profiles[2]["proxy"])
        self.assertEqual(targets, ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])

    def test_json_object(self):
        raw = ('{"profiles": [{"name": "NL", "proxy": "socks5://h:1080"},'
               ' {"name": "off", "proxy": ""}], "targets": ["https://youtu.be/x"]}')
        profiles, targets = vp.parse_profile_spec(raw)
        self.assertEqual([p["name"] for p in profiles], ["NL", "off"])
        self.assertEqual(profiles[0]["proxy"], "socks5://h:1080")
        self.assertIsNone(profiles[1]["proxy"])
        self.assertEqual(targets, ["https://youtu.be/x"])

    def test_json_array_of_strings(self):
        profiles, _ = vp.parse_profile_spec('["socks5://h:1080", "direct"]')
        self.assertEqual(profiles[0]["proxy"], "socks5://h:1080")
        self.assertIsNone(profiles[1]["proxy"])

    def test_empty(self):
        self.assertEqual(vp.parse_profile_spec(""), ([], []))
        self.assertEqual(vp.parse_profile_spec(None), ([], []))

    def test_dedupe(self):
        profiles = [
            {"name": "A", "proxy": "socks5://h:1080"},
            {"name": "A", "proxy": "socks5://h:1080"},
            {"name": "", "proxy": "socks5://h:1080"},
            {"name": "B", "proxy": ""},
        ]
        out = vp.dedupe_profiles(profiles)
        self.assertEqual(len(out), 3, out)
        self.assertEqual([p["name"] for p in out], ["A", "socks5://h:1080", "B"])
        self.assertEqual([p["proxy"] for p in out],
                         ["socks5://h:1080", "socks5://h:1080", None])


class TestClassifyError(unittest.TestCase):
    CASES = [
        ("Sign in to confirm you're not a bot", "bot_check"),
        ("HTTP Error 429: Too Many Requests", "rate_limited"),
        ("The uploader has not made this video available in your country", "geo_blocked"),
        ("Video unavailable. This content isn't available", "geo_blocked"),
        ("The requested IP address is blocked", "ip_blocked"),
        ("ProxyError: tunnel connection failed", "proxy_dead"),
        ("Connection refused", "proxy_dead"),
        ("Temporary failure in name resolution", "dns_failed"),
        ("The read operation timed out", "timeout"),
        ("[youtube] nsig extraction failed", "nsig_throttled"),
        ("Requested format is not available", "format_unavailable"),
        ("yt-dlp is out of date. Update yt-dlp", "outdated_ytdlp"),
        ("certificate verify failed", "ssl_error"),
        ("This video is private", "private_or_paid"),
    ]

    def test_known_errors(self):
        for text, code in self.CASES:
            self.assertEqual(vp.classify_error(text)["code"], code, text)

    def test_unknown(self):
        got = vp.classify_error("совершенно новая ошибка")
        self.assertEqual(got["code"], "unknown")
        self.assertEqual(got["severity"], "fatal")

    def test_never_raises_on_none(self):
        self.assertEqual(vp.classify_error(None)["code"], "unknown")


def _synthetic(result):
    base = {"name": "p", "proxy": "socks5://h:1080", "proxy_label": "socks5://h:1080"}
    base.update(result)
    return base


class TestVerdicts(unittest.TestCase):
    def test_all_good(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": True, "ip": "1.2.3.4", "country": "NL", "org": "ISP"},
            "reach": {"ok": True},
            "extract": {"ok": True},
            "search": {"ok": True},
            "download": {"ok": True, "throttled": False},
        }))
        self.assertEqual(v["verdict"], "ok")

    def test_throttled(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": True}, "reach": {"ok": True}, "extract": {"ok": True},
            "search": {"ok": True}, "download": {"ok": True, "throttled": True},
        }))
        self.assertEqual(v["verdict"], "throttled")
        self.assertTrue(v["verdict_text"])

    def test_bot_check_wins_over_throttle(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": True}, "reach": {"ok": True},
            "extract": {"ok": False, "code": "bot_check"},
            "download": {"ok": True, "throttled": True},
        }))
        self.assertEqual(v["verdict"], "bot_check")

    def test_geo_blocked(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": True}, "reach": {"ok": True},
            "extract": {"ok": False, "code": "geo_blocked"},
        }))
        self.assertEqual(v["verdict"], "geo_blocked")
        self.assertTrue(any("страну" in h for h in v["hints"]))

    def test_no_route_with_proxy(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": False, "code": "proxy_dead"},
            "reach": {"ok": False, "code": "proxy_dead"},
            "extract": {"ok": False, "code": "proxy_dead"},
        }))
        self.assertEqual(v["verdict"], "no_route")
        self.assertIn("VPN", v["verdict_text"])

    def test_no_route_without_proxy_says_system_route(self):
        v = vp.verdict_for(_synthetic({
            "proxy": None,
            "egress": {"ok": False, "code": "ssl_error"},
            "reach": {"ok": False, "code": "ssl_error"},
        }))
        self.assertEqual(v["verdict"], "no_route")
        self.assertIn("системный маршрут", v["verdict_text"])
        self.assertIn("VPN-клиент", v["hints"][0])

    def test_extract_only(self):
        v = vp.verdict_for(_synthetic({
            "egress": {"ok": True}, "reach": {"ok": True}, "extract": {"ok": True},
            "search": {"ok": True}, "download": {"ok": False, "code": "nsig_throttled"},
        }))
        self.assertEqual(v["verdict"], "extract_only")


class TestReporting(unittest.TestCase):
    def setUp(self):
        # NB: verdict_for() returns only the verdict — merge it back into the
        # probe blocks, exactly like run_profile() does.
        base = _synthetic({
            "egress": {"ok": True, "ip": "1.2.3.4", "country": "Netherlands",
                       "country_code": "NL", "city": "Amsterdam", "org": "Example ISP",
                       "asn": "AS12345", "ms": 210},
            "reach": {"ok": True, "status": 200, "ms": 120},
            "extract": {"ok": True, "title": "Me at the zoo", "format_count": 18,
                        "audio_formats": 5, "max_height": 360, "ms": 900},
            "clients": [{"client": "web", "ok": True}, {"client": "android", "ok": False,
                                                        "code": "bot_check"}],
            "search": {"ok": True, "count": 5, "ms": 800},
            "download": {"ok": True, "bytes": 524288, "seconds": 1.2, "kbps": 3495.2,
                         "throttled": False, "files": ["probe.webm"]},
        })
        base.update(vp.verdict_for(base))
        base["name"] = "Нидерланды"
        base["proxy_label"] = "socks5://127.0.0.1:1080"
        self.results = [base]

    def test_text_report(self):
        text = vp.format_text_report(self.results, env=vp.env_report())
        self.assertIn("Нидерланды", text)
        self.assertIn("1.2.3.4", text)
        self.assertIn("Me at the zoo", text)

    def test_markdown_report(self):
        md = vp.format_markdown_report(self.results, env=vp.env_report(), label="тест")
        self.assertIn("| Профиль |", md)
        self.assertIn("Нидерланды", md)

    def test_reports_survive_empty_results(self):
        env = vp.env_report()
        self.assertIsInstance(vp.format_text_report([], env=env), str)
        self.assertIsInstance(vp.format_markdown_report([], env=env), str)

    def test_clip(self):
        self.assertEqual(vp._clip("abcdef", 4), "abc…")
        self.assertEqual(vp._clip("ab", 4), "ab")
        self.assertEqual(vp._clip(None, 4), "")


class TestEnvReport(unittest.TestCase):
    def test_keys(self):
        env = vp.env_report()
        for key in ("yt_dlp", "yt_dlp_ok", "ffmpeg", "pysocks", "python", "platform"):
            self.assertIn(key, env)
        self.assertIsInstance(vp.env_hints(env), list)


class LiveFailures(unittest.TestCase):
    """Real calls against a dead proxy: must classify, never raise."""

    def test_egress_dead_proxy(self):
        res = vp.probe_egress(proxy=DEAD_PROXY, timeout=8)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "proxy_dead")
        self.assertEqual(res["ip"], "")

    def test_reachability_dead_proxy(self):
        res = vp.probe_reachability(proxy=DEAD_PROXY, timeout=8)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "proxy_dead")

    def test_full_profile_dead_proxy_never_raises(self):
        res = vp.run_profile(
            {"name": "dead", "proxy": DEAD_PROXY},
            cfg={}, targets=[vp.DEFAULT_TARGET],
            options={"timeout": 8, "search": True, "download": True,
                     "clients": ["web"], "mp3": True, "throttle_kbps": 60},
        )
        self.assertEqual(res["verdict"], "no_route")
        self.assertEqual(res["egress"]["code"], "proxy_dead")
        self.assertEqual(res["extract"]["code"], "proxy_dead")
        self.assertEqual(res["search"]["code"], "proxy_dead")
        self.assertEqual(res["download"]["code"], "proxy_dead")
        self.assertEqual(res["clients"][0]["code"], "proxy_dead")
        self.assertIn(res["mp3"]["code"], ("no_ffmpeg", "proxy_dead"))
        self.assertGreaterEqual(res["elapsed_s"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
