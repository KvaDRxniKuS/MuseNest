"""Downloader selection and dispatch for MuseNest.

A *tracker* (e.g. Spotify/Deezer/Zvuk/Yandex) tells us where to *find* the
catalog metadata. A *downloader* (e.g. YouTube / Zvuk) tells us where to
*get* the audio file.

Global defaults:
    music_source -> tracker key (spotify/deezer/yandex/zvuk/musicbrainz)
    downloader   -> downloader key (youtube/zvuk)

Every artist may override one or both in config.json / library.json with the
fields `source` (tracker) and `downloader` (downloader). Empty -> use global.
"""

from . import config as cfg_mod


def resolve_downloader(downloader=None, cfg=None, artist=None):
    """Return a normalized downloader key for an artist/track.

    Resolution order:
      1. explicit ``downloader`` argument (track/genre already decided),
      2. per-artist ``downloader`` field (from the artist config),
      3. global config ``downloader``,
      4. fallback ``youtube``.
    """
    if downloader:
        d = str(downloader).lower().strip()
        if d in cfg_mod.VALID_DOWNLOADERS:
            return d
    if artist and isinstance(artist, dict):
        d = str(artist.get("downloader") or "").lower().strip()
        if d in cfg_mod.VALID_DOWNLOADERS:
            return d
    if cfg:
        d = str(cfg.get("downloader") or "").lower().strip()
        if d in cfg_mod.VALID_DOWNLOADERS:
            return d
    return "youtube"


def artist_downloader_from_cfg(cfg, artist_name):
    """Find the per-artist downloader for ``artist_name`` from config artists."""
    for a in (cfg or {}).get("artists", []):
        if isinstance(a, dict) and str(a.get("name") or "").strip().casefold() == str(artist_name or "").strip().casefold():
            return resolve_downloader(artist=a, cfg=cfg)
    return resolve_downloader(cfg=cfg)


def artist_source_from_cfg(cfg, artist_name):
    """Return the effective tracker key for an artist (or the global default)."""
    for a in (cfg or {}).get("artists", []):
        if isinstance(a, dict) and str(a.get("name") or "").strip().casefold() == str(artist_name or "").strip().casefold():
            src = str(a.get("source") or "").lower().strip()
            if src in cfg_mod.VALID_SOURCES:
                return src
    src = str((cfg or {}).get("music_source") or "deezer").lower().strip()
    return src if src in cfg_mod.VALID_SOURCES else "deezer"
