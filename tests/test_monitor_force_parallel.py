"""Tests for the concurrent force_download_tracks (core/monitor.py).

Force-download used to run strictly sequentially in its background job. It now
fans out over the scan thread pool (``download_threads`` workers, the same
threads the scan uses) while keeping two properties the caller and the UI rely
on:

1. the returned results keep the *task order* of collect_download_tasks;
2. every thread the job touched ends up idle ("Форс-загрузка завершена").

Everything runs offline: _force_one is replaced with a concurrency probe.

Run:  python tests/test_monitor_force_parallel.py
"""

import os
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import monitor as mon_mod  # noqa: E402


def _task(i):
    return {"artist": f"Artist {i}", "track_name": f"Track {i}",
            "track_id": f"t-{i}", "album_name": f"Album {i}"}


class ForceDownloadParallel(unittest.TestCase):

    def setUp(self):
        self._saved = {n: getattr(mon_mod, n)
                       for n in ("_force_one", "_stopped", "set_thread_state")}
        mon_mod._stopped = lambda: False
        mon_mod.set_thread_state = lambda *a, **k: None
        self.lock = threading.Lock()
        self.cur = 0
        self.max_cur = 0
        self.wids = set()
        self.ran = 0

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(mon_mod, name, fn)

    def _probe(self, tm, cfg, wid):
        """Stand-in for _force_one: tracks peak concurrency and worker ids."""
        with self.lock:
            self.cur += 1
            self.max_cur = max(self.max_cur, self.cur)
            self.wids.add(wid)
            self.ran += 1
        time.sleep(0.05)
        with self.lock:
            self.cur -= 1
        return {"ok": True, "track": tm["track_name"], "message": "ок"}

    def test_tasks_overlap_on_the_pool_up_to_download_threads(self):
        mon_mod._force_one = self._probe
        tasks = [_task(i) for i in range(8)]

        res = mon_mod.force_download_tracks(tasks, {"download_threads": 4}, 1)

        self.assertEqual(len(res), 8, "every task yields a result")
        self.assertGreaterEqual(self.max_cur, 2,
                                "tasks must actually overlap on the pool")
        self.assertLessEqual(self.max_cur, 4,
                             "never more workers than download_threads")
        self.assertTrue(self.wids <= {1, 2, 3, 4}, f"odd worker ids: {self.wids}")
        self.assertEqual([r["track"] for r in res],
                         [t["track_name"] for t in tasks],
                         "results must keep the task order")

    def test_download_threads_one_stays_sequential(self):
        mon_mod._force_one = self._probe
        tasks = [_task(i) for i in range(4)]

        res = mon_mod.force_download_tracks(tasks, {"download_threads": 1}, 1)

        self.assertEqual(len(res), 4)
        self.assertEqual(self.max_cur, 1, "one thread — no overlap")
        self.assertEqual(self.wids, {1})

    def test_missing_download_threads_defaults_to_four(self):
        mon_mod._force_one = self._probe
        tasks = [_task(i) for i in range(8)]

        res = mon_mod.force_download_tracks(tasks, {}, 1)

        self.assertEqual(len(res), 8)
        self.assertGreaterEqual(self.max_cur, 2)
        self.assertLessEqual(self.max_cur, 4)

    def test_a_stop_request_before_the_first_task_yields_nothing(self):
        tasks = [_task(i) for i in range(3)]
        for threads in (1, 4):
            with self.subTest(download_threads=threads):
                self.setUp()  # fresh probe counters (also re-arms _stopped=False)
                mon_mod._stopped = lambda: True
                mon_mod._force_one = self._probe
                res = mon_mod.force_download_tracks(
                    tasks, {"download_threads": threads}, 1)
                self.assertEqual(res, [], "a stopped job returns no results")
                self.assertEqual(self.ran, 0, "no task may start after a stop")
                self.assertEqual(self.max_cur, 0)

    def test_an_exploding_task_still_resets_worker_states(self):
        calls = []
        mon_mod.set_thread_state = lambda wid, state, task="": calls.append((wid, state))

        def _boom(tm, cfg, wid):
            raise RuntimeError("взрыв")

        mon_mod._force_one = _boom
        tasks = [_task(i) for i in range(2)]

        # Single-threaded path: the exception propagates, the finally-block
        # still resets every worker state it touched.
        with self.assertRaises(RuntimeError):
            mon_mod.force_download_tracks(tasks, {"download_threads": 1}, 1)
        self.assertEqual(calls[-1], (1, "idle"))

        # Pool path: a worker exception is converted into a failure result,
        # and both pool workers end up idle.
        calls.clear()
        res = mon_mod.force_download_tracks(tasks, {"download_threads": 2}, 1)
        self.assertEqual(len(res), 2)
        self.assertFalse(any(r["ok"] for r in res))
        self.assertIn("взрыв", res[0]["message"])
        for wid in (1, 2):
            self.assertIn((wid, "idle"), calls)

    def test_results_are_filled_even_when_a_task_fails(self):
        def _flaky(tm, cfg, wid):
            if tm["track_name"] == "Track 1":
                return {"ok": False, "track": tm["track_name"],
                        "message": "истёк", "error_code": "ERR-4"}
            return {"ok": True, "track": tm["track_name"], "message": "ок"}

        mon_mod._force_one = _flaky
        tasks = [_task(i) for i in range(4)]

        res = mon_mod.force_download_tracks(tasks, {"download_threads": 2}, 1)

        self.assertEqual(len(res), 4, "a failure must not swallow later tasks")
        self.assertEqual([r["track"] for r in res],
                         [t["track_name"] for t in tasks])
        self.assertEqual([r["ok"] for r in res], [True, False, True, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
