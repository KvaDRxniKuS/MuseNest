import re
import requests

from . import spotify as sp_mod
from . import status

_UA = {"User-Agent": "Mozilla/5.0 (compatible; tracker/1.0)"}


def resolve_spotify_name(spotify_id):
    from . import catalog as cat_mod
    title = None
    try:
        url = f"https://open.spotify.com/artist/{spotify_id}"
        r = requests.get("https://open.spotify.com/oembed",
                         params={"url": url}, headers=_UA, timeout=15)
        r.raise_for_status()
        title = r.json().get("title")
    except Exception:
        title = None
    if cat_mod.is_real_artist_name(title, spotify_id):
        return title
    name = cat_mod.resolve_public_artist_name(spotify_id, fallback=None)
    if name:
        return name
    return title if cat_mod.is_real_artist_name(title, spotify_id) else None


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
        headers = {"User-Agent": "MuseNest/1.0 (https://github.com/KvaDRxniKuS/MuseNest)"}
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

    def spotify_id_for_mbid(self, mbid):
        try:
            j = self._get(f"/artist/{mbid}", {"inc": "url-rels"})
        except Exception:
            return None
        for rel in j.get("relations") or []:
            url = ((rel.get("url") or {}).get("resource") or "")
            if "open.spotify.com/artist/" in url:
                sid = url.rstrip("/").split("/")[-1]
                if len(sid) == 22 and not sid.isdigit():
                    return sid
        return None

    def search_spotify_artists(self, query, limit=8):
        out = []
        seen = set()
        for a in self.search_artists(query, limit=min(limit, 4)) or []:
            sid = self.spotify_id_for_mbid(a["id"])
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append({
                "id": sid,
                "name": a.get("name") or query,
                "followers": 0,
                "link": f"https://open.spotify.com/artist/{sid}",
            })
            if len(out) >= limit:
                break
        return out

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
        from . import yandex as ya_mod
        self.yandex = ya_mod.YandexSource(token=(cfg.get("yandex_token") or "").strip() or None)

    def _auth(self):
        self.deezer._auth()
        if self.spotify:
            try:
                self.spotify._auth()
            except Exception:
                pass
        try:
            self.yandex._auth()
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

    def _merge_albums(self, *lists):
        seen = set()
        out = []
        for lst in lists:
            for a in lst or []:
                key = re.sub(r"[^a-z0-9]+", "", (a.get("name") or "").casefold())
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(a)
        return out

    def get_albums(self, artist_id, limit=99999, artist_name=""):
        sid = str(artist_id or "")
        spotify_albs = []
        if self.spotify:
            try:
                if len(sid) == 22 and not sid.isdigit():
                    spotify_albs = self.spotify.get_albums(sid, limit=limit) or []
                elif artist_name:
                    sa = self.spotify.search_artist(artist_name)
                    if sa:
                        spotify_albs = self.spotify.get_albums(sa["id"], limit=limit) or []
            except Exception as e:
                status.log.debug("Spotify get_albums error: %s", e)

        # Official Spotify discography is complete enough — keep it.
        if len(spotify_albs) >= 8:
            return spotify_albs[:limit]

        yandex_albs = []
        try:
            ya_key = sid if str(sid).startswith("ya-") or str(sid).replace("ya-", "").isdigit() else sid
            yandex_albs = self.yandex.get_albums(ya_key, limit=limit, artist_name=artist_name) or []
            if yandex_albs:
                status.log.info("ℹ️ Альбомы Яндекс Музыки для '%s': %d", artist_name or sid, len(yandex_albs))
        except Exception as e:
            status.log.debug("Yandex get_albums error: %s", e)

        deezer_albs = []
        try:
            if sid.isdigit():
                deezer_albs = self.deezer.get_albums(sid, limit=limit) or []
            elif artist_name:
                want = artist_name.casefold().strip()
                da = [
                    x for x in (self.deezer.search_artists(artist_name, limit=8) or [])
                    if (x.get("name") or "").casefold().strip() == want
                ]
                if da:
                    deezer_albs = self.deezer.get_albums(da[0]["id"], limit=limit) or []
        except Exception as e:
            status.log.debug("Deezer get_albums error: %s", e)

        mb_albs = []
        try:
            mb_id = sid
            if artist_name and ("-" not in sid or sid.isdigit() or len(sid) == 22):
                mb_res = self.musicbrainz.search_artists(artist_name, limit=1)
                if mb_res:
                    mb_id = mb_res[0]["id"]
            if mb_id and ("-" in str(mb_id)):
                mb_albs = self.musicbrainz.get_albums(mb_id, limit=limit) or []
        except Exception as e:
            status.log.debug("MusicBrainz fallback get_albums error: %s", e)

        # Deezer often has a single real release (e.g. DALNOBOY) — merge, do not stop there.
        merged = self._merge_albums(yandex_albs, spotify_albs, mb_albs, deezer_albs)
        return merged[:limit]

    def _enrich_track_meta(self, tracks, album_name="", artist_name=""):
        if not tracks:
            return tracks
        if all(int(t.get("duration_ms") or 0) > 0 for t in tracks):
            return tracks
        by_name = {}
        q = " ".join(x for x in (artist_name, album_name) if x).strip()
        if q:
            try:
                j = self.deezer._get("/search/track", {"q": q, "limit": 50})
                for s in j.get("data") or []:
                    nm = (s.get("title") or "").casefold().strip()
                    if nm:
                        by_name[nm] = int(s.get("duration") or 0) * 1000
            except Exception:
                pass
        if not by_name and album_name:
            try:
                recs = self.musicbrainz._get("/recording/", {"query": f'recording:"{album_name}"', "limit": 25})
                for rec in recs.get("recordings") or []:
                    nm = (rec.get("title") or "").casefold().strip()
                    ln = int(rec.get("length") or 0)
                    if nm and ln:
                        by_name[nm] = ln
            except Exception:
                pass
        if not by_name:
            return tracks
        for t in tracks:
            if int(t.get("duration_ms") or 0) > 0:
                continue
            nm = (t.get("name") or "").casefold().strip()
            if nm in by_name:
                t["duration_ms"] = by_name[nm]
        return tracks

    def get_album_tracks(self, album_id, album_name="", artist_name=""):
        tracks = []
        aid = str(album_id or "")
        if self.spotify and len(aid) == 22 and not aid.isdigit():
            try:
                tracks = self.spotify.get_album_tracks(aid)
            except Exception as e:
                status.log.debug("Spotify get_album_tracks error: %s", e)
        if not tracks and str(aid).startswith("ya-"):
            try:
                tracks = self.yandex.get_album_tracks(aid, album_name=album_name, artist_name=artist_name)
            except Exception as e:
                status.log.debug("Yandex get_album_tracks error: %s", e)
        if not tracks and aid.isdigit():
            try:
                tracks = self.deezer.get_album_tracks(aid)
            except Exception as e:
                status.log.debug("Deezer get_album_tracks error: %s", e)
        if not tracks and (album_name or artist_name):
            try:
                found = self.yandex.search_artists(artist_name or album_name, limit=1)
                if found:
                    yalbums = self.yandex.get_albums(found[0]["yandex_id"], limit=50, artist_name=artist_name)
                    match = next((a for a in yalbums if (a.get("name") or "").casefold() == (album_name or "").casefold()), None)
                    if match:
                        tracks = self.yandex.get_album_tracks(match["id"])
            except Exception as e:
                status.log.debug("Yandex track enrich error: %s", e)
        if not tracks:
            try:
                tracks = self.musicbrainz.get_album_tracks(aid)
            except Exception as e:
                status.log.debug("MusicBrainz get_album_tracks error: %s", e)
        return self._enrich_track_meta(tracks, album_name=album_name, artist_name=artist_name)


def build_source(name, cfg):
    return MultiFallbackSource(cfg)

