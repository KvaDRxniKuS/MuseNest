import re
import requests

from . import spotify as sp_mod
from . import status

_UA = {"User-Agent": "Mozilla/5.0 (compatible; tracker/1.0)"}


def resolve_spotify_name(spotify_id):
    url = f"https://open.spotify.com/artist/{spotify_id}"
    r = requests.get("https://open.spotify.com/oembed",
                     params={"url": url}, headers=_UA, timeout=15)
    r.raise_for_status()
    return r.json().get("title")


def parse_input(text):
    """Classify user input: Spotify URL/ID, Deezer URL/ID or plain artist name.

    NOTE: a bare word is treated as a Spotify ID only when it matches the
    exact Spotify ID format (22 base62 chars). Previously any 10-40 char
    word (e.g. a one-word artist name like "Nightwishh") was misdetected
    as an ID, the oEmbed lookup failed and the search returned an error
    instead of suggestions.
    """
    if not text:
        return ("name", "")
    t = text.strip()
    m = re.search(r"open\.spotify\.com/artist/([0-9A-Za-z_]+)", t)
    if m:
        val = m.group(1)
        if len(val) == 22 and not val.isdigit():
            return ("spotify_id", val)
        elif val.isdigit():
            return ("deezer_id", val)
        else:
            return ("name", val)
            
    m = re.search(r"spotify:artist:([0-9A-Za-z_]+)", t)
    if m:
        val = m.group(1)
        if len(val) == 22 and not val.isdigit():
            return ("spotify_id", val)
        elif val.isdigit():
            return ("deezer_id", val)
        else:
            return ("name", val)
            
    m = re.search(r"deezer\.com/(?:artist|album|track)/(\d+)", t)
    if m:
        return ("deezer_id", m.group(1))
    m = re.search(r"deezer\.com/[^/]+/id(\d+)", t)
    if m:
        return ("deezer_id", m.group(1))
        
    # bare Spotify ID is exactly 22 base62-ish chars (never purely numeric)
    if re.fullmatch(r"[0-9A-Za-z_]{22}", t) and not t.isdigit():
        return ("spotify_id", t)
    # bare numeric id -> Deezer
    if re.fullmatch(r"\d{2,12}", t):
        return ("deezer_id", t)
    return ("name", t)


class DeezerSource:
    BASE = "https://api.deezer.com"

    def __init__(self, proxy=None):
        self._proxy = proxy

    def _proxies(self):
        if self._proxy:
            return {"http": self._proxy, "https": self._proxy}
        return None

    def _auth(self):
        pass

    def _get(self, path, params=None):
        r = requests.get(self.BASE + path, params=params, headers=_UA, timeout=20, proxies=self._proxies())
        r.raise_for_status()
        return r.json()

    def search_artists(self, query, limit=8):
        j = self._get("/search/artist", {"q": query, "limit": limit})
        return [{"id": str(a["id"]), "name": a["name"], "followers": a.get("nb_fan", 0)} for a in j.get("data", [])]

    def get_artist(self, artist_id):
        j = self._get(f"/artist/{artist_id}")
        if "error" in j:
            raise RuntimeError("Deezer artist not found: " + str(artist_id))
        return {"id": str(j["id"]), "name": j["name"], "followers": j.get("nb_fan", 0)}

    def get_albums(self, artist_id, limit=99999):
        out = []
        index = 0
        limit_per_req = 100
        while index < limit:
            j = self._get(f"/artist/{artist_id}/albums", {"index": index, "limit": limit_per_req})
            data = j.get("data", [])
            if not data:
                break
            for a in data:
                rt = a.get("record_type", "album")
                if rt not in ("album", "single"):
                    continue
                rd = a.get("release_date", "") or ""
                out.append({
                    "id": str(a["id"]),
                    "name": a.get("title", ""),
                    "release_date": rd[:10],
                    "album_type": rt,
                })
            if len(data) < limit_per_req:
                break
            index += limit_per_req
            if len(out) >= limit:
                break
        return out[:limit]

    def get_album_tracks(self, album_id):
        j = self._get(f"/album/{album_id}/tracks")
        out = []
        for s in j.get("data", []):
            dur = int(s.get("duration", 0)) * 1000
            art = (s.get("artist") or {}).get("name", "")
            out.append({
                "id": str(s.get("id")),
                "name": s.get("title", ""),
                "duration_ms": dur,
                "artists": [art],
            })
        return out


class MusicBrainzSource:
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, proxy=None):
        self._proxy = proxy

    def _proxies(self):
        if self._proxy:
            return {"http": self._proxy, "https": self._proxy}
        return None

    def _auth(self):
        pass

    def _get(self, path, params=None):
        headers = {"User-Agent": "SpotifyYoutubeTracker/1.0 (contact@example.com)"}
        p = dict(params or {})
        p["fmt"] = "json"
        r = requests.get(self.BASE + path, params=p, headers=headers, timeout=20, proxies=self._proxies())
        r.raise_for_status()
        return r.json()

    def search_artists(self, query, limit=8):
        try:
            j = self._get("/artist/", {"query": query, "limit": limit})
            out = []
            for a in j.get("artists", []):
                out.append({"id": a["id"], "name": a["name"]})
            return out
        except Exception:
            return []

    def get_artist(self, artist_id):
        j = self._get(f"/artist/{artist_id}")
        return {"id": j["id"], "name": j["name"]}

    def get_albums(self, artist_id, limit=99999):
        try:
            j = self._get("/release-group/", {"artist": artist_id, "type": "album|single", "limit": min(limit, 100)})
            out = []
            for rg in j.get("release-groups", []):
                out.append({
                    "id": rg["id"],
                    "name": rg["title"],
                    "release_date": rg.get("first-release-date", ""),
                    "album_type": "album" if "Album" in rg.get("primary-type", "") else "single"
                })
            return out
        except Exception:
            return []

    def get_album_tracks(self, album_id):
        try:
            rg = self._get(f"/release-group/{album_id}", {"inc": "releases"})
            releases = rg.get("releases", [])
            if not releases:
                return []
            rel_id = releases[0]["id"]
            rel = self._get(f"/release/{rel_id}", {"inc": "recordings"})
            tracks = []
            for media in rel.get("media", []):
                for t in media.get("tracks", []):
                    rec = t.get("recording", {})
                    dur = int(rec.get("length", 0))
                    tracks.append({
                        "id": rec.get("id", t.get("id")),
                        "name": t.get("title", rec.get("title", "")),
                        "duration_ms": dur,
                        "artists": [a.get("name", "") for a in rec.get("artist-credit", [])]
                    })
            return tracks
        except Exception:
            return []


class MultiFallbackSource:
    def __init__(self, cfg):
        self.cfg = cfg
        proxy = (cfg.get("proxy") or "").strip() or None
        self.deezer = DeezerSource(proxy=proxy)
        self.spotify = None
        cid = (cfg.get("spotify_client_id") or "").strip()
        csec = (cfg.get("spotify_client_secret") or "").strip()
        if cid and csec:
            try:
                self.spotify = sp_mod.SpotifyClient(cid, csec, proxy=proxy)
            except Exception:
                pass
        self.musicbrainz = MusicBrainzSource(proxy=proxy)

    def _auth(self):
        self.deezer._auth()
        if self.spotify:
            try:
                self.spotify._auth()
            except Exception:
                pass
        self.musicbrainz._auth()

    def search_artists(self, query, limit=8):
        # Priority: Spotify first (base62 IDs + followers), then Deezer, then MusicBrainz
        if self.spotify:
            try:
                results = self.spotify.search_artists(query, limit=limit)
                if results:
                    return results
            except Exception as e:
                status.log.debug("Spotify search error: %s", e)
        results = []
        try:
            results = self.deezer.search_artists(query, limit=limit)
        except Exception as e:
            status.log.debug("Deezer search error: %s", e)
        if results:
            return results
        if self.spotify:
            status.log.info("ℹ️ В Deezer ничего не найдено по запросу '%s', проверяем Spotify...", query)
            try:
                results = self.spotify.search_artists(query, limit=limit)
                if results:
                    return results
            except Exception as e:
                status.log.debug("Spotify search error: %s", e)
        if not results:
            status.log.info("ℹ️ В Deezer и Spotify ничего не найдено по запросу '%s', проверяем MusicBrainz...", query)
            try:
                results = self.musicbrainz.search_artists(query, limit=limit)
            except Exception as e:
                status.log.debug("MusicBrainz search error: %s", e)
        return results

    def search_artist(self, name):
        results = self.search_artists(name, limit=1)
        return results[0] if results else None

    def get_artist(self, artist_id):
        # If base62 Spotify ID, resolve via Spotify first
        if self.spotify and isinstance(artist_id, str) and len(str(artist_id)) == 22 and not str(artist_id).isdigit():
            try:
                return self.spotify.get_artist(artist_id)
            except Exception:
                pass
        try:
            return self.deezer.get_artist(artist_id)
        except Exception as e:
            if self.spotify:
                try:
                    return self.spotify.get_artist(artist_id)
                except Exception:
                    pass
            try:
                return self.musicbrainz.get_artist(artist_id)
            except Exception:
                pass
            raise e

    def get_albums(self, artist_id, limit=99999):
        albums = []
        artist_name = ""
        try:
            albums = self.deezer.get_albums(artist_id, limit=limit)
            if albums:
                try:
                    da = self.deezer.get_artist(artist_id)
                    artist_name = da.get("name", "")
                except Exception:
                    pass
        except Exception as e:
            status.log.debug("Deezer get_albums error: %s", e)

        if not albums and self.spotify:
            status.log.info("ℹ️ Альбомы не найдены в Deezer, ищем в Spotify...")
            try:
                if not artist_name:
                    try:
                        da = self.deezer.get_artist(artist_id)
                        artist_name = da.get("name", "")
                    except Exception:
                        pass
                if artist_name and not str(artist_id).isdigit():
                    albums = self.spotify.get_albums(artist_id, limit=limit)
                elif artist_name:
                    s_artist = self.spotify.search_artist(artist_name)
                    if s_artist:
                        albums = self.spotify.get_albums(s_artist["id"], limit=limit)
                elif not str(artist_id).isdigit():
                    albums = self.spotify.get_albums(artist_id, limit=limit)
            except Exception as e:
                status.log.debug("Spotify fallback get_albums error: %s", e)

        if not albums:
            status.log.info("ℹ️ Альбомы не найдены в Deezer и Spotify, задействуем MusicBrainz...")
            try:
                if not artist_name:
                    try:
                        da = self.deezer.get_artist(artist_id)
                        artist_name = da.get("name", "")
                    except Exception:
                        pass
                mb_id = artist_id
                if artist_name and str(artist_id).isdigit():
                    mb_res = self.musicbrainz.search_artists(artist_name, limit=1)
                    if mb_res:
                        mb_id = mb_res[0]["id"]
                albums = self.musicbrainz.get_albums(mb_id, limit=limit)
            except Exception as e:
                status.log.debug("MusicBrainz fallback get_albums error: %s", e)

        return albums

    def get_album_tracks(self, album_id):
        tracks = []
        try:
            tracks = self.deezer.get_album_tracks(album_id)
        except Exception as e:
            status.log.debug("Deezer get_album_tracks error: %s", e)

        if not tracks and self.spotify:
            try:
                tracks = self.spotify.get_album_tracks(album_id)
            except Exception as e:
                status.log.debug("Spotify fallback get_album_tracks error: %s", e)

        if not tracks:
            try:
                tracks = self.musicbrainz.get_album_tracks(album_id)
            except Exception as e:
                status.log.debug("MusicBrainz fallback get_album_tracks error: %s", e)

        return tracks


def build_source(name, cfg):
    name = (name or "deezer").lower()
    proxy = (cfg.get("proxy") or "").strip() or None
    if name == "spotify":
        return sp_mod.SpotifyClient(
            cfg.get("spotify_client_id", ""),
            cfg.get("spotify_client_secret", ""),
            proxy=proxy,
        )
    if name == "musicbrainz":
        return MusicBrainzSource(proxy=proxy)
    return MultiFallbackSource(cfg)

