import re

_CLEAN_RE = re.compile(r"\(.*?\)|\[.*?\]")


def _clean(name):
    return _CLEAN_RE.sub(" ", str(name)).lower()


def find_best_match(results, spotify_duration_ms, track_name, artist_name,
                    blacklist, tolerance_sec, fallback):
    if not results:
        return None, "ERR-1"

    sp_sec = (spotify_duration_ms or 0) / 1000.0
    blacklist_l = [b.lower().strip() for b in (blacklist or []) if b and b.strip()]

    # Block if original track name contains any blacklisted word
    track_name_l = (track_name or "").lower()
    if any(b in track_name_l for b in blacklist_l):
        return None, "ERR-3"

    candidates = []
    for r in results:
        title_l = (r.get("title") or "").lower()
        if any(b in title_l for b in blacklist_l):
            continue
        candidates.append(r)

    if not candidates:
        return None, "ERR-3"  # All blacklisted - never bypass blacklist!

    track_l = _clean(track_name)
    name_matches = [
        r for r in candidates
        if track_l and track_l in (r.get("title") or "").lower()
    ]
    pool = name_matches if name_matches else candidates

    have_dur = sp_sec > 0 and any((r.get("duration") or 0) > 0 for r in pool)
    if not have_dur:
        return (pool[0] if pool else None), None

    best = None
    best_diff = None
    for r in pool:
        d = r.get("duration") or 0
        diff = abs(d - sp_sec)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = r

    if best is None:
        return None, "ERR-1"
    if best_diff <= tolerance_sec:
        return best, None
    if fallback:
        return best, None
    return None, "ERR-2"  # Duration mismatch

