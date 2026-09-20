import time
import os
import tempfile
import shutil
import random
import logging
import threading
import yt_dlp

from . import status

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_log = logging.getLogger("tracker")

# A small, stable, very old public YouTube video used as the downloader self-test.
_DOWNLOADER_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo"

# Global rate limiter
_YT_RATE_LOCK = threading.Lock()
_YT_LAST_REQUEST_TIME = 0.0
_YT_MIN_INTERVAL = 3.5


def _cookies_path():
    data_path = os.path.join(_BASE_DIR, "data", "cookies.txt")
    if os.path.exists(data_path):
        return data_path
    return os.path.join(_BASE_DIR, "cookies.txt")


def _stopped():
    return status.status.get("stop_requested", False)


def _interruptible_sleep(seconds):
    """Sleep that can be interrupted by stop_requested. Checks every 0.2s."""
    end = time.time() + seconds
    while time.time() < end:
        if _stopped():
            return True  # interrupted
        time.sleep(min(0.2, max(0, end - time.time())))
    return False  # completed normally


def _rate_limit():
    """Global rate limiter — minimum interval between YouTube requests across all threads."""
    global _YT_LAST_REQUEST_TIME
    with _YT_RATE_LOCK:
        now = time.time()
        elapsed = now - _YT_LAST_REQUEST_TIME
        if elapsed < _YT_MIN_INTERVAL:
            wait = _YT_MIN_INTERVAL - elapsed + random.uniform(0.5, 2.5)
            _interruptible_sleep(wait)
        else:
            # Even if elapsed is long enough, inject a small random jitter to avoid fixed timing patterns
            _interruptible_sleep(random.uniform(0.2, 1.0))
        _YT_LAST_REQUEST_TIME = time.time()


def _is_cookie_error(err_msg):
    cookie_errors = [
        "CookieLoadError", "failed to load cookies",
        "Could not copy Chrome cookie database",
        "Could not copy", "cookie database",
    ]
    return any(ce in err_msg for ce in cookie_errors)


def _test_browser_cookies(browser):
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True,
            "cookiesfrombrowser": (browser,), "skip_download": True,
        }) as ydl:
            ydl.extract_info("ytsearch1:test", download=False)
        return True
    except Exception as e:
        if _is_cookie_error(str(e)):
            return False
        return True


class YtDlpAbortLogger:
    def debug(self, msg):
        if status.status.get("stop_requested", False):
            raise RuntimeError("Stop requested")

    def info(self, msg):
        if status.status.get("stop_requested", False):
            raise RuntimeError("Stop requested")

    def warning(self, msg):
        if status.status.get("stop_requested", False):
            raise RuntimeError("Stop requested")

    def error(self, msg):
        if status.status.get("stop_requested", False):
            raise RuntimeError("Stop requested")


def _base_opts(cfg=None):
    cfg = cfg or {}
    cookies_path = _cookies_path()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": YtDlpAbortLogger(),
        "user_agent": _UA,
        "extractor_retries": 1,
        "retries": 2,
        "retry_sleep": {"http": 1},
        "socket_timeout": 30,
        "sleep_interval": 3,          # sleep between downloads to avoid block
        "max_sleep_interval": 8,      # randomized sleep range up to 8s
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Accept": "*/*",
        },
    }
    if os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    else:
        browser = (cfg.get("youtube_cookie_browser") or "").strip().lower()
        if browser:
            if _test_browser_cookies(browser):
                opts["cookiesfrombrowser"] = (browser,)
            else:
                _log.warning("Cannot load cookies from '%s', working without cookies", browser)
    
    proxy = (cfg.get("proxy") or "").strip()
    if proxy:
        opts["proxy"] = proxy
    
    return opts


def search_youtube(query, limit=20, cfg=None):
    if _stopped():
        return []
    _rate_limit()
    if _stopped():
        return []
    
    ydl_opts = _base_opts(cfg)
    ydl_opts.update({
        "skip_download": True, "noplaylist": True,
        "ignoreerrors": True, "default_search": "ytsearch",
        "extract_flat": False,
    })

    max_attempts = 3
    cookie_fallback_done = False
    
    for attempt in range(max_attempts):
        if _stopped():
            return []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = info.get("entries", []) or []
            results = []
            for e in entries:
                if not e:
                    continue
                vid = e.get("id")
                if not vid:
                    continue
                results.append({
                    "id": vid,
                    "title": e.get("title") or "",
                    "duration": int(e.get("duration") or 0),
                    "webpage_url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                })
            return results
        except Exception as e:
            if _stopped():
                return []
            err_msg = str(e)
            if _is_cookie_error(err_msg) and not cookie_fallback_done:
                if "cookiesfrombrowser" in ydl_opts or "cookiefile" in ydl_opts:
                    ydl_opts.pop("cookiesfrombrowser", None)
                    ydl_opts.pop("cookiefile", None)
                    cookie_fallback_done = True
                    continue
            if attempt < max_attempts - 1:
                delay = 3 * (attempt + 1) + random.uniform(0, 1)
                if _interruptible_sleep(delay):
                    return []
                _rate_limit()
            else:
                _log.warning("YouTube search failed after %d attempts: %s (%s)",
                             max_attempts, query, e)
                raise
    return []


def download_audio(url, output_path_no_ext, quality="320", progress_hook=None, cfg=None):
    if _stopped():
        return
    _rate_limit()
    if _stopped():
        return
    
    ydl_opts = _base_opts(cfg)
    ydl_opts.update({
        "format": "bestaudio/best",
        "outtmpl": output_path_no_ext + ".%(ext)s",
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(quality),
        }],
    })
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    max_attempts = 3
    cookie_fallback_done = False
    
    for attempt in range(max_attempts):
        if _stopped():
            return
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return
        except Exception as e:
            if _stopped():
                return
            err_msg = str(e)
            if _is_cookie_error(err_msg) and not cookie_fallback_done:
                if "cookiesfrombrowser" in ydl_opts or "cookiefile" in ydl_opts:
                    ydl_opts.pop("cookiesfrombrowser", None)
                    ydl_opts.pop("cookiefile", None)
                    cookie_fallback_done = True
                    continue
            if attempt < max_attempts - 1:
                delay = 3 * (attempt + 1) + random.uniform(0, 2)
                if _interruptible_sleep(delay):
                    return
                _rate_limit()
            else:
                _log.warning("Download failed after %d attempts: %s (%s)",
                             max_attempts, url, e)
                raise


def _progress(on_progress, stage_key, stage, percent=None, message=""):
    """Report downloader-check progress. Never lets a bad callback break the check.

    ``percent`` is ``None`` when the stage has no measurable progress — the UI
    then shows an indeterminate (sliding) bar instead of a frozen number.
    """
    if not on_progress:
        return
    try:
        on_progress({
            "stage_key": stage_key,
            "stage": stage,
            "percent": None if percent is None else max(0.0, min(100.0, float(percent))),
            "message": message or "",
        })
    except Exception:
        pass


def _fmt_bytes(n):
    """Human-readable size; ``None``/garbage means "unknown" -> ``?``."""
    if n is None:
        return "?"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%d %s" % (n, unit)) if unit == "Б" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "?"


def _youtube_downloader_check(cfg, test_url, timeout, on_progress=None):
    """Live self-test for the YouTube (yt-dlp) download path."""
    info = {
        "ok": True,
        "mode": "youtube",
        "yt_dlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": shutil.which("ffmpeg") or None,
        "cookie_source": (
            "file" if os.path.exists(_cookies_path())
            else ((cfg.get("youtube_cookie_browser") or "").strip().lower() or "none")
        ),
        "blacklist": None,
        "test": {"ok": None, "message": "", "detail": ""},
    }

    _progress(on_progress, "prepare", "Подготовка", 5,
              "yt-dlp %s, ffmpeg: %s, cookies: %s" % (
                  info["yt_dlp_version"],
                  "есть" if info["ffmpeg"] else "нет",
                  info["cookie_source"]))

    tmp_dir = tempfile.mkdtemp(prefix="musenest-ytprobe-")
    result = {}

    # Progress bar mapping: extract 5-25%, download 25-85%, convert 85-100%.
    def _dl_hook(d):
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done * 100.0 / total) if total else None
            speed = d.get("speed")
            eta = d.get("eta")
            bits = []
            if pct is not None:
                bits.append("%.0f%%" % pct)
            bits.append("%s / %s" % (_fmt_bytes(done), _fmt_bytes(total) if total else "?"))
            if speed:
                bits.append("%s/с" % _fmt_bytes(speed))
            if eta is not None:
                bits.append("осталось ~%d с" % int(eta))
            _progress(on_progress, "download", "Скачивание аудио",
                      25 + (pct or 0) * 0.60, " · ".join(bits))
        elif st == "finished":
            _progress(on_progress, "download_done", "Файл получен", 85,
                      _fmt_bytes(d.get("total_bytes") or d.get("downloaded_bytes")))

    def _pp_hook(d):
        if d.get("status") == "started":
            _progress(on_progress, "convert", "Конвертация в mp3 (ffmpeg)", 88,
                      str(d.get("postprocessor") or ""))
        elif d.get("status") == "finished":
            _progress(on_progress, "convert_done", "Конвертация завершена", 97, "")

    def _probe():
        opts = _base_opts(cfg)
        opts.update({
            "format": "worstaudio/worst",
            "outtmpl": os.path.join(tmp_dir, "probe.%(ext)s"),
            "noplaylist": True,
            "socket_timeout": min(timeout, 30),
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_dl_hook],
            "postprocessor_hooks": [_pp_hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
        })
        try:
            _progress(on_progress, "extract", "Извлечение форматов", 12, test_url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                # This performs a tiny real download + mp3 conversion.
                ydl.download([test_url])
            files = [
                f for f in os.listdir(tmp_dir)
                if f.endswith(".mp3") or f.endswith(".m4a") or f.endswith(".webm") or f.endswith(".ogg")
            ]
            result["ok"] = bool(files)
            result["message"] = "Загрузчик работает (тест успешно скачал аудио)" if files else "Тест завершился без аудиофайла"
            result["files"] = files
            _progress(on_progress, "done" if files else "failed",
                      "Готово" if files else "Нет аудиофайла", 100,
                      ", ".join(files) if files else "")
        except Exception as e:
            result["ok"] = False
            result["message"] = str(e)
            _progress(on_progress, "failed", "Ошибка проверки", 100, str(e)[:200])

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        info["ok"] = False
        info["test"] = {"ok": False, "message": "Проверка зависла (таймаут)", "detail": ""}
        _progress(on_progress, "timeout", "Таймаут проверки", 100, "%d с" % timeout)
    else:
        # Downgrade to an extraction-only check if the download test failed for
        # reasons unrelated to the downloader (e.g. geo/network), so the user
        # still gets a useful hint rather than a raw traceback.
        info["test"] = {
            "ok": bool(result.get("ok")),
            "message": result.get("message") or "",
            "detail": (result.get("files") if isinstance(result.get("files"), list) else str(result.get("files") or "")),
        }
        if not result.get("ok"):
            info["ok"] = False

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return info


def _zvuk_downloader_check(cfg, timeout, on_progress=None):
    """Live self-test for the Zvuk direct-download path.

    Checks ffmpeg presence (needed for conversion), whether a token is set
    (mid vs high/flac), and whether the Zvuk Tiny API is reachable. Does NOT
    touch YouTube.
    """
    from . import zvuk as zv_mod
    proxy = (cfg.get("proxy") or "").strip() or None
    token = (cfg.get("zvuk_token") or "").strip() or None
    zv = zv_mod.ZvukSource(token=token, proxy=proxy)
    has_token = bool(token)
    quality = "high" if has_token else "mid"

    info = {
        "ok": True,
        "mode": "zvuk",
        "yt_dlp_version": None,
        "ffmpeg": shutil.which("ffmpeg") or None,
        "cookie_source": "none",
        "blacklist": None,
        "zvuk_token": has_token,
        "quality": quality,
        "test": {"ok": None, "message": "", "detail": ""},
    }

    _progress(on_progress, "prepare", "Подготовка", 8,
              "токен: %s, качество: %s, ffmpeg: %s" % (
                  "есть" if has_token else "нет", quality,
                  "есть" if shutil.which("ffmpeg") else "нет"))
    _progress(on_progress, "api", "Проверка Zvuk API", 30, "/api/tiny/profile")

    # 1) Tiny API reachability — probed ANONYMOUSLY (token=None).
    #    Both steps used to send the same authenticated /profile call, so an
    #    expired token answered 401 and was reported as "Zvuk API недоступен
    #    (нужен VPN)" — an outage that does not exist.
    api_reachable = False
    api_error = ""
    api_status = None
    try:
        zv._tiny("/profile", token=None)
        api_reachable = True
    except Exception as e:
        d = zv_mod._http_detail(e)
        api_status = d["status"]
        api_error = d["error"]

    # 2) Token validity — a separate, explicitly authenticated call.
    if has_token and api_reachable:
        _progress(on_progress, "token", "Проверка токена", 60, "токен задан")
    token_valid = None
    token_status = None
    if has_token and api_reachable:
        try:
            zv._tiny("/profile", token=token)
            token_valid = True
        except Exception as e:
            d = zv_mod._http_detail(e)
            token_valid = False
            token_status = d["status"]
            api_error = d["error"]
    elif has_token and not api_reachable:
        _progress(on_progress, "token", "Проверка токена", 60,
                  "пропущена — API недоступен")

    info["api_reachable"] = api_reachable
    info["api_status"] = api_status
    info["token_valid"] = token_valid
    info["token_status"] = token_status

    if api_reachable:
        if has_token and token_valid is False:
            # The API is up, so this is a credential problem, not an outage.
            info["ok"] = False
            info["test"] = {
                "ok": False,
                "message": ("Токен Zvuk истёк или неверен (HTTP %s). Скачивание продолжит "
                            "работать только в качестве mid. Обновите токен: войдите на "
                            "zvuk.com, откройте https://zvuk.com/api/tiny/profile, "
                            'скопируйте значение после "token": и вставьте в поле '
                            "«Zvuk токен»." % (token_status or "?")),
                "detail": "api_reachable=True; token_status=%s" % token_status,
            }
        else:
            info["ok"] = True
            quality_part = f"качество: {quality}" + (" (нужна подписка для high/flac)" if quality == "high" else " (аноним, mid) — добавьте токен для high/flac")
            token_part = ("токен активен" if token_valid else "без токена")
            info["test"] = {
                "ok": True,
                "message": f"Zvuk API доступен, {token_part}, {quality_part}",
                "detail": f"zvuk_token={'есть' if has_token else 'нет'}; quality={quality}",
            }
    else:
        info["ok"] = False
        info["test"] = {
            "ok": False,
            "message": "Zvuk API недоступен: %s" % (api_error or "нет ответа"),
            "detail": ("Проверка шла анонимно (без токена), значит дело в сети/прокси/DNS, "
                       "а не в токене. status=%s" % api_status),
        }
    _progress(on_progress, "done" if info.get("ok") else "failed",
              "Готово" if info.get("ok") else "Проверка не пройдена", 100,
              info["test"].get("message", ""))
    return info


def downloader_check(cfg=None, test_url=_DOWNLOADER_TEST_URL, timeout=45, on_progress=None):
    """Perform a live self-test of the configured download path.

    Dispatches on the chosen ``downloader`` (default ``youtube``):
      * ``youtube`` -> tests yt-dlp + ffmpeg by fetching a tiny video;
      * ``zvuk``    -> tests the Zvuk Tiny API + token + ffmpeg (no YouTube).
    Returns a dict describing whether the active downloader can actually fetch
    and convert audio right now.
    """
    cfg = cfg or {}
    downloader = (cfg.get("downloader") or "youtube").lower().strip()

    # The self-test is a manual diagnostic and must not be aborted by a leftover
    # stop_requested flag (e.g. from a scan the user pressed Stop on). Snapshot
    # and temporarily clear it for the duration of the test, then restore it so
    # a genuinely-running scan keeps its stop intent after the test.
    _was_stopped = status.status.get("stop_requested", False)
    status.status["stop_requested"] = False

    try:
        if downloader == "zvuk":
            return _zvuk_downloader_check(cfg, timeout, on_progress=on_progress)
        return _youtube_downloader_check(cfg, test_url, timeout, on_progress=on_progress)
    finally:
        # Restore the prior stop_requested so a running scan is not affected.
        status.status["stop_requested"] = _was_stopped



