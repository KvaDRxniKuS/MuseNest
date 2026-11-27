"""Tests for Zvuk anti-bot handling (pacing + HTTP 418 retries).

Real-world failure from a scan with parallel download workers:

    WARNING: Zvuk failed for 5l33p - Basic Bitches, trying YouTube:
    Zvuk не отдал поток для трека zvuk-121431601 (запрошено mid). Пробовал:
    mid (аноним) → HTTP 418. Токен не задан...

HTTP 418 is Zvuk's anti-bot answer: anonymous /track/stream requests that
arrive in a burst (the parallel workers!) get teapots, while spaced requests
are served normally — which is why only *some* tracks failed. The fix:

  * every direct zvuk.com request takes a slot on a global pacing timeline
    (minimum spacing + jitter across all threads);
  * a 418/429 attempt is retried after a growing pause before giving up;
  * when even the retries are refused, the error says "антибот-защита".

Run:  python tests/test_zvuk_stream_418.py
"""

import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import time
import unittest
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import zvuk as zv_mod  # noqa: E402

ANON_TOKEN = "anon-token-418"
STREAM_URL = "https://cdn.example.invalid/audio/ok.bin"


class ScriptedZvuk:
    """Local stand-in for https://zvuk.com/api/tiny with a per-quality script.

    ``script`` maps a quality to a *list* of responses served one per request:
    "ok" -> 200 with a stream URL, or an int status (418, 429, ...).
    When the script runs out, "ok" is served.
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.times = []          # monotonic timestamps of /track/stream hits
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path.endswith("/profile"):
                    return self._json(200, {"result": {"token": ANON_TOKEN}})
                if parsed.path.endswith("/track/stream"):
                    with outer.lock:
                        outer.times.append(time.monotonic())
                        seq = outer.script.get(qs.get("quality"), [])
                        rule = seq.pop(0) if seq else "ok"
                    if rule == "ok":
                        return self._json(200, {"stream": STREAM_URL})
                    return self._json(int(rule), {"error": "teapot"})
                return self._json(404, {"error": "not found"})

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._srv.allow_reuse_address = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        zv_mod.TINY_URL = "http://127.0.0.1:%d/api/tiny" % self.port

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


def _source():
    return zv_mod.ZvukSource(token=None, proxy="")


class ZvukStream418(unittest.TestCase):

    def setUp(self):
        self._saved_url = zv_mod.TINY_URL
        self._saved_delay = zv_mod.STREAM_DELAY_SEC
        zv_mod.set_stream_pacing(0.0)   # the stand is local: no pacing needed
        self.stand = ScriptedZvuk()

    def tearDown(self):
        self.stand.stop()
        zv_mod.TINY_URL = self._saved_url
        zv_mod.set_stream_pacing(self._saved_delay)

    def test_a_418_is_retried_until_the_stream_arrives(self):
        self.stand.script = {"mid": [418]}
        src = _source()

        url, q = src.resolve_stream("121431601", quality="mid")

        self.assertEqual(url, STREAM_URL)
        self.assertEqual(q, "mid")
        self.assertEqual(len(self.stand.times), 2,
                         "one retry must follow the refused request")

    def test_two_418s_take_two_retries(self):
        self.stand.script = {"mid": [418, 429]}
        src = _source()

        url, _q = src.resolve_stream("121431601", quality="mid")

        self.assertEqual(url, STREAM_URL)
        self.assertEqual(len(self.stand.times), 3,
                         "initial request + a retry for each refusal")

    def test_persistent_418_says_the_honest_antibot_word(self):
        self.stand.script = {"mid": [418, 418, 418, 418]}
        src = _source()

        with self.assertRaises(zv_mod.ZvukStreamError) as cm:
            src.resolve_stream("121431601", quality="mid")

        msg = str(cm.exception)
        self.assertIn("418", msg, msg)
        self.assertIn("антибот", msg.lower(), msg)
        self.assertIn("токен", msg.lower(), msg)
        self.assertEqual(len(self.stand.times), 3,
                         "no fourth try beyond the retry budget")

    def test_the_ladder_still_falls_back_to_mid_after_a_high_418(self):
        self.stand.script = {"high": [418, 418, 418], "mid": [418]}
        src = _source()

        url, q = src.resolve_stream("121431601", quality="high")

        self.assertEqual(url, STREAM_URL)
        self.assertEqual(q, "mid",
                         "high exhausted (incl. retries) -> the mid ladder continues")


class ZvukPacing(unittest.TestCase):

    def setUp(self):
        self._saved_url = zv_mod.TINY_URL
        self._saved_delay = zv_mod.STREAM_DELAY_SEC
        self.stand = ScriptedZvuk()

    def tearDown(self):
        self.stand.stop()
        zv_mod.TINY_URL = self._saved_url
        zv_mod.set_stream_pacing(self._saved_delay)

    def test_parallel_workers_cannot_burst_the_api(self):
        """The exact regression: parallel workers firing identical requests
        at the same instant. The pacing gate must space them out."""
        zv_mod.set_stream_pacing(0.15)
        errs = []

        def _one():
            try:
                src = _source()
                src.resolve_stream("121431601", quality="mid")
            except Exception as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=_one) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])

        times = sorted(self.stand.times)
        self.assertEqual(len(times), 3)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.14 for g in gaps),
                        "consecutive API requests must be spaced by "
                        "STREAM_DELAY_SEC, got %s" % gaps)

    def test_a_single_idle_request_is_not_put_in_debt(self):
        zv_mod.set_stream_pacing(0.15)
        time.sleep(0.05)

        t0 = time.monotonic()
        src = _source()
        src.resolve_stream("121431601", quality="mid")
        elapsed = time.monotonic() - t0

        # Two paced calls happen (anon /profile + /track/stream), each with
        # up to 0.6 s of anti-pattern jitter — but no 1.5 s of "debt" from
        # past reservations, because the timeline was idle.
        self.assertLess(elapsed, 1.8,
                        "an idle gate must serve the first requests at once "
                        "(jitter only), got %.2fs" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
