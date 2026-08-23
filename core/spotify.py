import time
import requests

BASE = "https://api.spotify.com/v1"


class SpotifyClient:
    def __init__(self, client_id, client_secret, proxy=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.expires = 0
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
        if "market" not in params and "type" in params:
            params.setdefault("market", "US")
        for _ in range(5):
            self._auth()
            headers = {"Authorization": f"Bearer {self.token}"}
            r = requests.get(url, headers=headers, params=params, timeout=20, proxies=self._proxies)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 1))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Spotify request failed after retries")

    def search_artist(self, name):
        j = self._get(f"{BASE}/search",
                      params={"q": name, "type": "artist", "limit": 1, "market": "US"})
        items = j.get("artists", {}).get("items", [])
        if not items:
            return None
        a = items[0]
        return {"id": a["id"], "name": a["name"], "followers": a.get("followers", {}).get("total", 0), "link": f"https://open.spotify.com/artist/{a['id']}"}

    def search_artists(self, query, limit=8):
        j = self._get(f"{BASE}/search",
                      params={"q": query, "type": "artist", "limit": limit, "market": "US"})
        out = []
        for a in j.get("artists", {}).get("items", []):
            out.append({"id": a["id"], "name": a["name"], "followers": a.get("followers", {}).get("total", 0), "link": f"https://open.spotify.com/artist/{a['id']}"})
        return out

    def get_artist(self, artist_id):
        j = self._get(f"{BASE}/artists/{artist_id}")
        return {"id": j["id"], "name": j["name"], "followers": j.get("followers", {}).get("total", 0), "link": f"https://open.spotify.com/artist/{j['id']}"}

    def get_albums(self, artist_id, limit=99999):
        albums = []
        offset = 0
        batch_limit = 50
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

