import logging
import collections
import threading
import datetime

logger = logging.getLogger("tracker")
logger.setLevel(logging.INFO)
logger.propagate = False

_log_buffer = collections.deque(maxlen=1000)
_lock = threading.Lock()
_counter_lock = threading.Lock()


class BufferHandler(logging.Handler):
    def emit(self, record):
        with _lock:
            _log_buffer.append({
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
            })


if not any(isinstance(h, BufferHandler) for h in logger.handlers):
    logger.addHandler(BufferHandler())

log = logger

status = {
    "running": False,
    "stop_requested": False,
    "current_artist": "",
    "current_stage": "Ожидание",
    "threads_info": {},
    "processed": 0,
    "found": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "last_scan": None,
    "next_scan": None,
    "ffmpeg": False,
}


def get_logs():
    with _lock:
        return list(_log_buffer)


def inc(key, n=1):
    with _counter_lock:
        status[key] = status.get(key, 0) + n


def set_thread_state(worker_id, state, task):
    """Set worker state/task. Always resets download progress."""
    with _counter_lock:
        status["threads_info"][str(worker_id)] = {
            "state": state,
            "task": task,
            "progress": None,
        }


def set_thread_progress(worker_id, progress):
    """Update download progress for a worker without touching state/task."""
    with _counter_lock:
        info = status["threads_info"].setdefault(
            str(worker_id), {"state": "downloading", "task": "", "progress": None}
        )
        info["progress"] = progress


def reset_counters():
    with _counter_lock:
        for k in ("processed", "found", "downloaded", "skipped", "failed"):
            status[k] = 0
        status["current_artist"] = ""
        status["current_stage"] = "Ожидание"
        status["threads_info"] = {}
        status["stop_requested"] = False

