"""Deezer REST metadata provider.

See ARCHITECTURE.md sections 3 and 7.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import date, datetime

import httpx

from sideb.models.track import Album, Artist, Track

ProgressCB = Callable[[int, int, str], None]

BASE_URL = "https://api.deezer.com"

_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/(\d+)",
    re.IGNORECASE,
)

_RATE_LIMIT_WINDOW = 5
_RATE_LIMIT_CAP = 45
_RETRIES = 3
_RETRY_BACKOFF = 2.0


class RateLimiter:
    """Asyncio-based sliding-window rate limiter (45 req / 5s)."""

    def __init__(self, max_calls: int, window: int) -> None:
        self._max = max_calls
        self._window = window
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        import time
        async with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < self._window]
            if len(self._timestamps) >= self._max:
                sleep = self._timestamps[0] + self._window - now
                if sleep > 0:
                    await asyncio.sleep(sleep)
                self._timestamps = [t for t in self._timestamps if now - t < self._window]
            self._timestamps.append(time.time())


class DeezerError(RuntimeError):
    pass


class DeezerMetadata:
    """Implements the MetadataProvider protocol against api.deezer.com."""

    def __init__(self, *, user_agent: str, timeout: float = 15.0, proxy: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            proxy=proxy,
        )
        self._rate_limiter = RateLimiter(_RATE_LIMIT_CAP, _RATE_LIMIT_WINDOW)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- URL resolution -------------------------------------------------

    async def resolve_url(self, url: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        from sideb.utils.url_resolver import resolve_url

        url = await resolve_url(url)
        match = _URL_RE.search(url)
        if not match:
            return await self.search(url, on_progress=on_progress)

        kind, obj_id = match.group(1).lower(), match.group(2)
        if kind == "track":
            return [await self.get_track(obj_id)]
        if kind == "album":
            return await self.get_album(obj_id, on_progress=on_progress)
        if kind == "playlist":
            return await self.get_playlist(obj_id, on_progress=on_progress)
        if kind == "artist":
            return await self._get_artist_top_tracks(obj_id, on_progress=on_progress)
        raise DeezerError(f"Unsupported Deezer URL kind: {kind}")

    # ---- Single resources -------------------------------------------------

    async def get_track(self, track_id: str) -> Track:
        data = await self._get_json(f"/track/{track_id}")
        album_id = str(data.get("album", {}).get("id", ""))
        album_hint = None
        if album_id:
            try:
                album_hint = await self._get_json(f"/album/{album_id}")
            except Exception:
                pass
        return self._parse_track(data, album_hint=album_hint)

    async def get_album(self, album_id: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        album_data = await self._get_json(f"/album/{album_id}")
        tracks_data = await self._get_paginated(f"/album/{album_id}/tracks")
        tracks: list[Track] = []
        for i, item in enumerate(tracks_data):
            try:
                tracks.append(self._parse_track(item, album_hint=album_data))
            except Exception:
                pass
        return tracks

    async def get_album_info(self, album_id: str) -> dict:
        return await self._get_json(f"/album/{album_id}")

    async def get_playlist_info(self, playlist_id: str) -> dict:
        return await self._get_json(f"/playlist/{playlist_id}")

    async def get_playlist(self, playlist_id: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        tracks_data = await self._get_paginated(f"/playlist/{playlist_id}/tracks")

        # Collect unique album IDs, then fetch them in parallel
        unique_albums: dict[str, str] = {}
        for item in tracks_data:
            album_id = str(item.get("album", {}).get("id", ""))
            if album_id and album_id not in unique_albums:
                unique_albums[album_id] = album_id

        album_cache: dict[str, dict] = {}
        sem = asyncio.Semaphore(10)

        async def _fetch_album(aid: str) -> tuple[str, dict]:
            async with sem:
                try:
                    return aid, await self._get_json(f"/album/{aid}")
                except Exception:
                    return aid, {}

        results = await asyncio.gather(*(_fetch_album(aid) for aid in unique_albums))
        album_cache = dict(results)

        tracks: list[Track] = []
        for i, item in enumerate(tracks_data):
            album_id = str(item.get("album", {}).get("id", ""))
            if on_progress:
                on_progress(i + 1, len(tracks_data), f"Parsing track {i + 1}/{len(tracks_data)}")
            try:
                tracks.append(self._parse_track(item, album_hint=album_cache.get(album_id, {}), position=i + 1))
            except Exception:
                pass
        return tracks

    async def search(self, query: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        data = await self._get_json("/search/track", params={"q": query})
        results = data.get("data", [])
        tracks: list[Track] = []
        for item in results:
            try:
                tracks.append(self._parse_track(item))
            except Exception:
                pass
        return tracks

    async def _get_artist_top_tracks(self, artist_id: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        album_list: list[dict] = []
        next_url: str | None = f"/artist/{artist_id}/albums?limit=100"
        while next_url:
            resp = await self._get_json(next_url)
            album_list.extend(resp.get("data", []))
            next_url = resp.get("next")

        total = len(album_list)
        tracks: list[Track] = []
        seen: set[str] = set()
        sem = asyncio.Semaphore(5)
        done = 0

        async def fetch(album_item: dict) -> list[Track]:
            nonlocal done
            album_id = str(album_item["id"])
            async with sem:
                try:
                    album_tracks = await self.get_album(album_id)
                    if on_progress:
                        done += 1
                        on_progress(min(done, total), total, f"Fetching album {done}/{total}")
                    return album_tracks
                except Exception:
                    if on_progress:
                        done += 1
                        on_progress(min(done, total), total, f"Fetching album {done}/{total}")
                    return []

        results = await asyncio.gather(*(fetch(a) for a in album_list))
        for album_tracks in results:
            for t in album_tracks:
                if t.id not in seen:
                    seen.add(t.id)
                    tracks.append(t)
        return tracks

    # ---- Artist search for the UI selector ------------------------------

    async def get_artist_albums(self, artist_id: str) -> list[dict]:
        """Get all albums for an artist with record_type and release_date info."""
        album_list: list[dict] = []
        next_url: str | None = f"/artist/{artist_id}/albums?limit=100"
        while next_url:
            resp = await self._get_json(next_url)
            album_list.extend(resp.get("data", []))
            next_url = resp.get("next")
        return album_list

    async def search_artists(self, query: str, limit: int = 5) -> list[dict]:
        data = await self._get_json("/search/artist", params={"q": query, "limit": limit})
        return data.get("data", [])

    async def get_artist_info(self, artist_id: str) -> dict:
        return await self._get_json(f"/artist/{artist_id}")

    async def get_artist_top_tracks(self, artist_id: str, limit: int = 3) -> list[dict]:
        data = await self._get_json(f"/artist/{artist_id}/top", params={"limit": limit})
        return data.get("data", [])

    # ---- Internals ----------------------------------------------------

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        last_err: Exception | None = None
        for attempt in range(_RETRIES):
            await self._rate_limiter.wait()
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    raise DeezerError(f"Deezer API error {err.get('code')}: {err.get('message')}")
                return data
            except (DeezerError, httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                backoff = _RETRY_BACKOFF * (2 ** attempt)
                await asyncio.sleep(backoff)
        raise last_err  # type: ignore[misc]

    async def _get_paginated(self, path: str) -> list[dict]:
        all_items: list[dict] = []
        idx = 0
        while True:
            data = await self._get_json(path, params={"index": idx, "limit": 100})
            items = data.get("data", [])
            all_items.extend(items)
            if len(items) < 100:
                break
            idx += 100
        return all_items

    @staticmethod
    def _parse_track(data: dict, album_hint: dict | None = None, position: int | None = None) -> Track:
        artist_data = data.get("artist", {})
        artist = Artist(id=str(artist_data.get("id", "")), name=artist_data.get("name", ""))

        album_data = album_hint if album_hint is not None else data.get("album", {})
        album_artist_data = album_data.get("artist", artist_data)
        album_artist = Artist(
            id=str(album_artist_data.get("id", "")),
            name=album_artist_data.get("name", ""),
        )

        release_date = _parse_date(album_data.get("release_date"))
        genres = album_data.get("genres", {}).get("data", [])
        genre = genres[0]["name"] if genres else None

        album = Album(
            id=str(album_data.get("id", "")),
            title=album_data.get("title", ""),
            artist=album_artist,
            cover_url=album_data.get("cover_big"),
            cover_url_xl=album_data.get("cover_xl"),
            release_date=release_date,
            track_count=album_data.get("nb_tracks"),
            genre=genre,
            label=album_data.get("label"),
            album_type=album_data.get("record_type", ""),
        )

        contributors_data = data.get("contributors", [])
        contributors = [
            Artist(id=str(c.get("id", "")), name=c.get("name", "")) for c in contributors_data
        ]
        has_featured = any(c.get("role", "").lower() == "featured" for c in contributors_data) or (
            "feat." in data.get("title", "").lower() or "ft." in data.get("title", "").lower()
        )

        quality_fields = [
            album_data.get("label"),
            genre,
            data.get("isrc"),
            album_data.get("cover_big"),
            data.get("bpm"),
        ]
        metadata_quality = sum(1 for f in quality_fields if f)

        track_number = position if position is not None else int(data.get("track_position", 1) or 1)

        return Track(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            artist=artist,
            album=album,
            duration=int(data.get("duration", 0)),
            track_number=track_number,
            disk_number=int(data.get("disk_number", 1) or 1),
            isrc=data.get("isrc", "") or "",
            bpm=float(data["bpm"]) if data.get("bpm") else None,
            explicit=bool(data.get("explicit_lyrics", False)),
            has_featured_artist=has_featured,
            is_deluxe="deluxe" in album_data.get("title", "").lower(),
            contributors=contributors,
            metadata_quality=metadata_quality,
        )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
