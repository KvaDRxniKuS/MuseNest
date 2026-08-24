"""Yandex Music catalog via unofficial API (MarshalX/yandex-music-api)."""


class YandexSource:
    def __init__(self, token=None):
        self._token = (token or "").strip() or None
        self._client = None

    def _cli(self):
        if self._client is not None:
            return self._client
        try:
            from yandex_music import Client
        except ImportError as e:
            raise RuntimeError("Установите пакет yandex-music") from e
        cli = Client(self._token) if self._token else Client()
        try:
            cli.init()
        except Exception:
            pass
        self._client = cli
        return cli

    def _auth(self):
        self._cli()

    def search_artists(self, query, limit=8):
        try:
            res = self._cli().search(query, type_="artist")
        except Exception:
            return []
        arts = []
        if res and getattr(res, "artists", None) and res.artists.results:
            arts = res.artists.results
        out = []
        for a in arts[: max(1, int(limit or 8))]:
            likes = getattr(a, "likes_count", None) or 0
            ratings = getattr(a, "ratings", None)
            month = getattr(ratings, "month", None) if ratings else None
            out.append({
                "id": f"ya-{a.id}",
                "name": a.name,
                "followers": int(month or likes or 0),
                "yandex_id": str(a.id),
                "link": f"https://music.yandex.ru/artist/{a.id}",
            })
        return out

    def get_artist(self, artist_id):
        yid = str(artist_id or "").replace("ya-", "")
        brief = self._cli().artists_brief_info(yid)
        art = getattr(brief, "artist", None) if brief else None
        if art is None:
            found = self.search_artists(yid, limit=1)
            return found[0] if found else {"id": f"ya-{yid}", "name": yid, "followers": 0}
        likes = getattr(art, "likes_count", None) or 0
        ratings = getattr(art, "ratings", None)
        month = getattr(ratings, "month", None) if ratings else None
        return {
            "id": f"ya-{art.id}",
            "name": art.name,
            "followers": int(month or likes or 0),
            "yandex_id": str(art.id),
            "link": f"https://music.yandex.ru/artist/{art.id}",
        }

    def get_albums(self, artist_id, limit=99999, artist_name=""):
        yid = str(artist_id or "").replace("ya-", "")
        if not yid.isdigit() and artist_name:
            found = self.search_artists(artist_name, limit=1)
            if not found:
                return []
            yid = found[0]["yandex_id"]
        out = []
        page = 0
        while len(out) < limit:
            try:
                pack = self._cli().artists_direct_albums(yid, page=page, page_size=50)
            except Exception:
                break
            albums = getattr(pack, "albums", None) or []
            if not albums:
                break
            for a in albums:
                kind = (getattr(a, "type", None) or "album") or "album"
                if str(kind).lower() not in ("album", "single", "ep"):
                    continue
                atype = "single" if str(kind).lower() in ("single", "ep") else "album"
                year = getattr(a, "year", None) or getattr(a, "release_date", "") or ""
                out.append({
                    "id": f"ya-{a.id}",
                    "name": a.title or "",
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
        try:
            alb = self._cli().albums_with_tracks(yid)
        except Exception:
            return []
        out = []
        volumes = getattr(alb, "volumes", None) or []
        for vol in volumes:
            for t in vol or []:
                out.append({
                    "id": f"ya-{t.id}",
                    "name": t.title or "",
                    "duration_ms": int(getattr(t, "duration_ms", 0) or 0),
                    "artists": [x.name for x in (getattr(t, "artists", None) or []) if getattr(x, "name", None)],
                })
        return out
