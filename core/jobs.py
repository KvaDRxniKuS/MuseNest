"""Lightweight background job queue for long-running library/resolution work.

A single daemon worker thread consumes jobs one at a time. This means heavy
network operations (resolving an artist's discography, re-checking files,
refreshing the whole library) never block the caller, and config.json /
library.json are never written concurrently by two jobs (which would cause
lost updates).
"""

import threading
import time
import traceback
import queue

_lock = threading.Lock()
_queue = queue.Queue()
_jobs = {}
_job_order = 0

MAX_JOBS_KEPT = 300
# How long (seconds) to keep a job's result available for the UI to fetch.
RESULT_TTL = 300

# Kinds of background work we track (used by the UI to display progress).
KIND_RESOLVE = "resolve"       # resolve one artist (or all) catalog
KIND_FILES = "files"           # check/sort files on disk
KIND_SCAN = "scan"             # full monitor scan


def _new_job_id():
    global _job_order
    # Guarded by the caller's lock (submit holds _lock), so no race on _job_order.
    _job_order += 1
    return f"bg{_job_order}"


def _record(job_id, **updates):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(updates)


def submit(kind, name, fn):
    """Queue a job. Returns the job_id immediately, without blocking."""
    with _lock:
        job_id = _new_job_id()
        _jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "name": name or "",
            "state": "queued",
            "message": "",
            "started": None,
            "finished": None,
            "error": None,
            "result": None,
        }
    _queue.put({"job_id": job_id, "fn": fn})
    return job_id


def _worker():
    while True:
        item = _queue.get()
        job_id = item["job_id"]
        fn = item["fn"]
        _record(job_id, state="running", started=time.time())
        try:
            result = fn()
            _record(job_id, state="done", finished=time.time(), error=None, result=result)
        except Exception as e:
            traceback.print_exc()
            _record(job_id, state="error", finished=time.time(), error=str(e), result=None)
        finally:
            # Keep the queue bounded — prune the oldest terminal jobs.
            with _lock:
                terminal = sorted(
                    k for k, v in _jobs.items()
                    if v.get("state") in ("done", "error")
                )
                while len(terminal) > MAX_JOBS_KEPT:
                    _jobs.pop(terminal.pop(0), None)
            _queue.task_done()


_worker_started = threading.Event()


def start():
    """Start the single background worker thread (idempotent)."""
    if _worker_started.is_set():
        return
    _worker_started.set()
    t = threading.Thread(target=_worker, daemon=True, name="musenest-jobs")
    t.start()


def list_jobs(include_result=False):
    with _lock:
        out = []
        for v in _jobs.values():
            d = dict(v)
            if not include_result:
                d.pop("result", None)
            out.append(d)
        return out


def get_job(job_id):
    """Return a copy of a single job (including its result), or None."""
    with _lock:
        d = _jobs.get(job_id)
        return dict(d) if d else None


def is_running():
    with _lock:
        return any(v.get("state") == "running" for v in _jobs.values())
