import re

_CLEAN_RE = re.compile(r"\(.*?\)|\[.*?\]")


def _clean(name):
    return _CLEAN_RE.sub(" ", str(name)).lower()


def rank_matches(results, spotify_duration_ms, track_name, artist_name,
                 blacklist, tolerance_sec, fallback):
    """All plausible candidates, best first: ``(pool, err_code)``.

    Same rules as :func:`find_best_match` (blacklist, name-match pool,
    duration proximity, fallback) — but the whole ordered pool comes back,
    so the downloader can fall through to the next candidate when YouTube
    refuses the first ("This video is not available" etc.).
    """
    if not results:
        return [], "ERR-1"

    sp_sec = (spotify_duration_ms or 0) / 1000.0
    blacklist_l = [b.lower().strip() for b in (blacklist or []) if b and b.strip()]

    # Block if original track name contains any blacklisted word
    track_name_l = (track_name or "").lower()
    if any(b in track_name_l for b in blacklist_l):
        return [], "ERR-3"

    candidates = []
    for r in results:
        title_l = (r.get("title") or "").lower()
        if any(b in title_l for b in blacklist_l):
            continue
        candidates.append(r)

    if not candidates:
        return [], "ERR-3"  # All blacklisted - never bypass blacklist!

    track_l = _clean(track_name)
    name_matches = [
        r for r in candidates
        if track_l and track_l in (r.get("title") or "").lower()
    ]
    pool = name_matches if name_matches else candidates

    have_dur = sp_sec > 0 and any((r.get("duration") or 0) > 0 for r in pool)
    if not have_dur:
        return pool, None

    def _diff(r):
        return abs((r.get("duration") or 0) - sp_sec)

    pool = sorted(pool, key=_diff)   # stable: ties keep the search order
    if _diff(pool[0]) <= tolerance_sec or fallback:
        return pool, None
    return [], "ERR-2"  # Duration mismatch


def find_best_match(results, spotify_duration_ms, track_name, artist_name,
                    blacklist, tolerance_sec, fallback):
    pool, err = rank_matches(results, spotify_duration_ms, track_name,
                             artist_name, blacklist, tolerance_sec, fallback)
    return (pool[0] if pool else None), err

