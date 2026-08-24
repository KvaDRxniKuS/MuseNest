import time
import requests

BASE = "https://api.spotify.com/v1"


class SpotifyClient:
    def __init__(self, client_id, client_secret, proxy=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.expires = 0
        self._web_token = None
        self._web_expires = 0
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def _auth(self):
        if self.token and time.time() < self.expires - 30:
            return
        last_err = None
        attempts = [self._proxies]
        if self._proxies:
            attempts.append(None)
        for proxies in attempts:
            try:
                r = requests.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    timeout=15,
                    proxies=proxies,
                )
                r.raise_for_status()
                j = r.json()
                self.token = j["access_token"]
                self.expires = time.time() + j.get("expires_in", 3600)
                if proxies is None and self._proxies:
                    self._proxies = None
                return
            except Exception as e:
                last_err = e
        raise last_err or RuntimeError("Spotify auth failed")

    def _get(self, url, params=None):
        params = dict(params or {})
        last = None
        for _ in range(5):
            self._auth()
            headers = {"Authorization": f"Bearer {self.token}"}
            r = requests.get(url, headers=headers, params=params, timeout=20, proxies=self._proxies)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 1))
                time.sleep(wait)
                continue
            last = r
            if r.status_code in (403, 404):
                r.raise_for_status()
            r.raise_for_status()
            return r.json()
        if last is not None:
            last.raise_for_status()
        raise RuntimeError("Spotify request failed after retries")

    def _oembed_name(self, artist_id):
        try:
            r = requests.get(
                "https://open.spotify.com/oembed",
                params={"url": f"https://open.spotify.com/artist/{artist_id}"},
                headers={"User-Agent": "Mozilla/5.0 MuseNest"},
                timeout=15,
                proxies=self._proxies,
            )
            r.raise_for_status()
            return (r.json() or {}).get("title")
        except Exception:
            return None

    def _web_access_token(self):
        if self._web_token and time.time() < self._web_expires - 30:
            return self._web_token
        try:
            r = requests.get(
                "https://open.spotify.com/get_access_token",
                params={"reason": "transport", "productType": "web_player"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "App-Platform": "WebPlayer",
                },
                timeout=15,
                proxies=self._proxies,
            )
            if r.status_code != 200:
                return None
            j = r.json() or {}
            tok = j.get("accessToken") or j.get("access_token")
            if not tok:
                return None
            self._web_token = tok
            exp_ms = int(j.get("accessTokenExpirationTimestampMs") or 0)
            self._web_expires = (exp_ms / 1000.0) if exp_ms > 10**11 else time.time() + 300
            return tok
        except Exception:
            return None

    def _search_with_token(self, token, query, limit):
        r = requests.get(
            f"{BASE}/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "type": "artist", "limit": limit},
            timeout=20,
            proxies=self._proxies,
        )
        if r.status_code != 200:
            return []
        out = []
        for a in ((r.json() or {}).get("artists") or {}).get("items") or []:
            out.append({
                "id": a["id"],
                "name": a["name"],
                "followers": a.get("followers", {}).get("total", 0),
                "link": f"https://open.spotify.com/artist/{a['id']}",
            })
        return out

    def search_artist(self, name):
        j = self._get(f"{BASE}/search",
                      params={"q": name, "type": "artist", "limit": 1, "market": "US"})
        items = j.get("artists", {}).get("items", [])
        if not items:
            return None
        a = items[0]
        return {"id": a["id"], "name": a["name"], "followers": a.get("followers", {}).get("total", 0), "link": f"https://open.spotify.com/artist/{a['id']}"}

    def search_artists(self, query, limit=8):
        limit = max(1, min(int(limit or 8), 10))
        attempts = [
            {"q": query, "type": "artist", "limit": limit, "market": "US"},
            {"q": query, "type": "artist", "limit": limit},
        ]
        last_err = None
        for params in attempts:
            try:
                j = self._get(f"{BASE}/search", params=params)
            except requests.HTTPError as e:
                last_err = e
                continue
            out = []
            for a in (j.get("artists") or {}).get("items") or []:
                out.append({
                    "id": a["id"],
                    "name": a["name"],
                    "followers": a.get("followers", {}).get("total", 0),
                    "link": f"https://open.spotify.com/artist/{a['id']}",
                })
            if out:
                return out
        # Development Mode often forbids GET /search (403). Resolve via MusicBrainz Spotify links.
        try:
            from . import source as src_mod
            mb = src_mod.MusicBrainzSource(proxy=(self._proxies or {}).get("https") if self._proxies else None)
            out = mb.search_spotify_artists(query, limit=limit)
            if out:
                return out
        except Exception:
            pass
        if last_err is not None:
            code = last_err.response.status_code if last_err.response is not None else 0
            if code != 403:
                raise last_err
        return []

    def get_artist(self, artist_id):
        artist_id = str(artist_id or "").strip()
        try:
            j = self._get(f"{BASE}/artists/{artist_id}")
            return {
                "id": j["id"],
                "name": j["name"],
                "followers": j.get("followers", {}).get("total", 0),
                "link": f"https://open.spotify.com/artist/{j['id']}",
            }
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code not in (403, 404):
                raise
        for q in (f"spotify:artist:{artist_id}", artist_id):
            try:
                j = self._get(
                    f"{BASE}/search",
                    params={"q": q, "type": "artist", "limit": 5, "market": "US"},
                )
                for a in (j.get("artists") or {}).get("items") or []:
                    if a.get("id") == artist_id:
                        return {
                            "id": a["id"],
                            "name": a["name"],
                            "followers": a.get("followers", {}).get("total", 0),
                            "link": f"https://open.spotify.com/artist/{a['id']}",
                        }
            except Exception:
                continue
        name = self._oembed_name(artist_id) or artist_id
        return {
            "id": artist_id,
            "name": name,
            "followers": 0,
            "link": f"https://open.spotify.com/artist/{artist_id}",
        }

    def _search_albums_by_artist(self, artist_id, limit):
        info = self.get_artist(artist_id)
        name = info.get("name") or artist_id
        out = []
        offset = 0
        while len(out) < limit:
            j = self._get(
                f"{BASE}/search",
                params={
                    "q": f'artist:"{name}"',
                    "type": "album",
                    "limit": 50,
                    "offset": offset,
                    "market": "US",
                },
            )
            items = ((j.get("albums") or {}).get("items")) or []
            if not items:
                break
            for a in items:
                arts = a.get("artists") or []
                if not any(str(x.get("id")) == str(artist_id) for x in arts):
                    if not any((x.get("name") or "").casefold() == name.casefold() for x in arts):
                        continue
                out.append(a)
            if len(items) < 50:
                break
            offset += 50
        return out[:limit]

    def get_albums(self, artist_id, limit=99999):
        albums = []
        offset = 0
        batch_limit = 50
        try:
            while True:
                j = self._get(f"{BASE}/artists/{artist_id}/albums",
                              params={"include_groups": "album,single",
                                      "limit": batch_limit, "offset": offset})
                items = j.get("items", [])
                if not items:
                    break
                albums.extend(items)
                if len(items) < batch_limit:
                    break
                offset += batch_limit
                if len(albums) >= limit:
                    break
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code not in (403, 404):
                raise
            albums = []
        if not albums:
            try:
                albums = self._search_albums_by_artist(artist_id, limit)
            except Exception:
                albums = albums or []
        seen = set()
        uniq = []
        for a in albums:
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            atype = a.get("album_type", "album")
            if atype not in ("album", "single"):
                atype = "album" if atype == "compilation" else "single"
            uniq.append({
                "id": a["id"],
                "name": a.get("name", ""),
                "album_type": atype,
                "release_date": a.get("release_date", "")[:10],
            })
        return uniq[:limit]

    def get_album_tracks(self, album_id):
        tracks = []
        offset = 0
        while True:
            j = self._get(f"{BASE}/albums/{album_id}/tracks",
                          params={"limit": 50, "offset": offset})
            items = j.get("items", [])
            if not items:
                break
            tracks.extend(items)
            if len(items) < 50:
                break
            offset += 50
        return tracks

