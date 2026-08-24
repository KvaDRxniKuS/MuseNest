"""Yandex Music catalog: official client if installed, else public HTTP API."""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class YandexSource:
    API = "https://api.music.yandex.net"

    def __init__(self, token=None):
        self._token = (token or "").strip() or None
        self._client = None

    def _cli(self):
        if self._client is not None:
            return self._client
        try:
            from yandex_music import Client
        except ImportError:
            self._client = False
            return None
        try:
            cli = Client(self._token) if self._token else Client()
            try:
                cli.init()
            except Exception:
                pass
            self._client = cli
            return cli
        except Exception:
            self._client = False
            return None

    def _http(self, path, params=None):
        url = self.API + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "User-Agent": "Mozilla/5.0 MuseNest",
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = "OAuth " + self._token
        req = Request(url, headers=headers)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def _http_result(self, path, params=None):
        try:
            j = self._http(path, params)
        except Exception:
            return None
        if not isinstance(j, dict):
            return None
        return j.get("result", j)

    def _item(self, aid, name, followers=0):
        return {
            "id": f"ya-{aid}",
            "name": name,
            "followers": int(followers or 0),
            "yandex_id": str(aid),
            "link": f"https://music.yandex.ru/artist/{aid}",
        }

    def _from_obj(self, a):
        aid = getattr(a, "id", None)
        name = getattr(a, "name", None)
        likes = getattr(a, "likes_count", None) or 0
        ratings = getattr(a, "ratings", None)
        month = getattr(ratings, "month", None) if ratings else None
        return self._item(aid, name, month or likes)

    def _from_dict(self, a):
        if not isinstance(a, dict):
            return None
        aid = a.get("id")
        name = a.get("name") or ""
        if aid is None or not name:
            return None
        likes = a.get("likesCount") or a.get("likes_count") or 0
        ratings = a.get("ratings") or {}
        month = ratings.get("month") if isinstance(ratings, dict) else None
        return self._item(aid, name, month or likes)

    def search_artists(self, query, limit=8):
        q = " ".join(str(query or "").split())
        if not q or (len(q) == 22 and not q.isdigit()):
            return []
        want = q.casefold()
        scored = []
        cli = self._cli()
        arts = []
        if cli:
            try:
                res = cli.search(q, type_="artist")
                if res and getattr(res, "artists", None) and res.artists.results:
                    arts = list(res.artists.results)
            except Exception:
                arts = []
            for a in arts:
                item = self._from_obj(a)
                if not item:
                    continue
                if (item["name"] or "").casefold() == want:
                    scored.insert(0, item)
                else:
                    scored.append(item)
        if not scored:
            res = self._http_result("/search", {"text": q, "type": "artist", "page": 0})
            block = (res or {}).get("artists") or {}
            arts = block.get("results") or block.get("items") or []
            for a in arts:
                item = self._from_dict(a)
                if not item:
                    continue
                if (item["name"] or "").casefold() == want:
                    scored.insert(0, item)
                else:
                    scored.append(item)
        exact = [x for x in scored if (x.get("name") or "").casefold() == want]
        return (exact or scored)[: max(1, int(limit or 8))]

    def _auth(self):
        self._cli()

    def get_artist(self, artist_id):
        yid = str(artist_id or "").replace("ya-", "")
        cli = self._cli()
        if cli and yid.isdigit():
            try:
                brief = cli.artists_brief_info(yid)
                art = getattr(brief, "artist", None) if brief else None
                if art is not None:
                    return self._from_obj(art)
            except Exception:
                pass
        if yid.isdigit():
            brief = self._http_result(f"/artists/{yid}/brief-info")
            art = (brief or {}).get("artist") if isinstance(brief, dict) else None
            item = self._from_dict(art) if art else None
            if item:
                return item
        found = self.search_artists(yid if not yid.isdigit() else "", limit=1)
        return found[0] if found else {"id": f"ya-{yid}", "name": yid, "followers": 0}

    def get_albums(self, artist_id, limit=99999, artist_name=""):
        yid = str(artist_id or "").replace("ya-", "")
        if not yid.isdigit():
            found = self.search_artists(artist_name, limit=5) if artist_name else []
            if not found:
                return []
            yid = found[0]["yandex_id"]
        out = []
        cli = self._cli()
        page = 0
        while len(out) < limit:
            albums = []
            if cli:
                try:
                    pack = cli.artists_direct_albums(yid, page=page, page_size=50)
                    albums = list(getattr(pack, "albums", None) or [])
                except Exception:
                    albums = []
            if not albums:
                pack = self._http_result(
                    f"/artists/{yid}/direct-albums",
                    {"page": page, "page-size": 50},
                )
                if isinstance(pack, dict):
                    albums = pack.get("albums") or pack.get("items") or []
                elif isinstance(pack, list):
                    albums = pack
                else:
                    albums = []
            if not albums:
                break
            for a in albums:
                if isinstance(a, dict):
                    kind = (a.get("type") or "album") or "album"
                    title = a.get("title") or ""
                    aid = a.get("id")
                    year = a.get("year") or a.get("releaseDate") or ""
                else:
                    kind = (getattr(a, "type", None) or "album") or "album"
                    title = getattr(a, "title", "") or ""
                    aid = getattr(a, "id", None)
                    year = getattr(a, "year", None) or getattr(a, "release_date", "") or ""
                if str(kind).lower() not in ("album", "single", "ep"):
                    continue
                atype = "single" if str(kind).lower() in ("single", "ep") else "album"
                out.append({
                    "id": f"ya-{aid}",
                    "name": title,
                    "album_type": atype,
                    "release_date": str(year)[:10],
                })
                if len(out) >= limit:
                    break
            if len(albums) < 50:
                break
            page += 1
        return out[:limit]

    def get_album_tracks(self, album_id, album_name="", artist_name=""):
        yid = str(album_id or "").replace("ya-", "")
        if not yid.isdigit():
            return []
        volumes = []
        cli = self._cli()
        if cli:
            try:
                alb = cli.albums_with_tracks(yid)
                volumes = list(getattr(alb, "volumes", None) or [])
            except Exception:
                volumes = []
        if not volumes:
            alb = self._http_result(f"/albums/{yid}/with-tracks")
            if isinstance(alb, dict):
                volumes = alb.get("volumes") or []
        out = []
        for vol in volumes:
            for t in vol or []:
                if isinstance(t, dict):
                    out.append({
                        "id": f"ya-{t.get('id')}",
                        "name": t.get("title") or "",
                        "duration_ms": int(t.get("durationMs") or t.get("duration_ms") or 0),
                        "artists": [
                            x.get("name") for x in (t.get("artists") or [])
                            if isinstance(x, dict) and x.get("name")
                        ],
                    })
                else:
                    out.append({
                        "id": f"ya-{t.id}",
                        "name": t.title or "",
                        "duration_ms": int(getattr(t, "duration_ms", 0) or 0),
                        "artists": [
                            x.name for x in (getattr(t, "artists", None) or [])
                            if getattr(x, "name", None)
                        ],
                    })
        return out
