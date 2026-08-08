"""LRCLIB lyrics provider.

Direct HTTP client against https://lrclib.net/api (no auth, no rate limit).
Response schema verified against LRCLIB's own API docs (lrclib.net/docs):

    GET /api/get?artist_name=..&track_name=..&album_name=..&duration=..
    -> {"id", "trackName", "artistName", "albumName", "duration",
        "instrumental", "plainLyrics", "syncedLyrics"}

Implements the search chain described in ARCHITECTURE.md's "Lyrics Fetch
Chain": exact match+duration -> search without duration -> variant-suffix
stripped title -> parenthetical-suffix stripped title.
"""

from __future__ import annotations

import re

import httpx

from sideb.models.track import Lyrics, Track

BASE_URL = "https://lrclib.net/api"

_DASH_VARIANT_RE = re.compile(
    r"\s*[-\u2013\u2014]\s*(?:lofi|lo-fi|lo fi|slowed|reverb|sped up|nightcore|"
    r"acoustic|instrumental|karaoke|remix|edit|version|cover|extended|"
    r"radio edit|demo|live).*$",
    re.IGNORECASE,
)

_PAREN_SUFFIX_RE = re.compile(
    r"\s*[\(\[](?![^\)\]]*\b(?:feat\.?|ft\.?|with)\b)[^\(\)\[\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def strip_dash_variant(title: str) -> str:
    return _DASH_VARIANT_RE.sub("", title).strip()


def strip_paren_suffix(title: str) -> str:
    return _PAREN_SUFFIX_RE.sub("", title).strip()


class LRCLIBLyrics:
    """Implements the LyricsProvider protocol against lrclib.net."""

    def __init__(self, *, user_agent: str, timeout: float = 10.0, proxy: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            proxy=proxy,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_lyrics(self, track: Track) -> Lyrics | None:
        result = await self._get(
            track_name=track.title, artist_name=track.artist.name, duration=track.duration
        )
        if result:
            return result

        result = await self._search(track_name=track.title, artist_name=track.artist.name)
        if result:
            return result

        cleaned = strip_dash_variant(track.title)
        if cleaned != track.title:
            result = await self._search(track_name=cleaned, artist_name=track.artist.name)
            if result:
                return result

        cleaned2 = strip_paren_suffix(track.title)
        if cleaned2 != track.title:
            result = await self._search(track_name=cleaned2, artist_name=track.artist.name)
            if result:
                return result

        return None

    async def _get(self, *, track_name: str, artist_name: str, duration: int) -> Lyrics | None:
        try:
            resp = await self._client.get(
                "/get",
                params={
                    "track_name": track_name,
                    "artist_name": artist_name,
                    "duration": duration,
                },
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._parse(resp.json())
        except httpx.HTTPError:
            return None

    async def _search(self, *, track_name: str, artist_name: str) -> Lyrics | None:
        try:
            resp = await self._client.get(
                "/search", params={"track_name": track_name, "artist_name": artist_name}
            )
            resp.raise_for_status()
            results = resp.json()
            if not results:
                return None
            return self._parse(results[0])
        except httpx.HTTPError:
            return None

    @staticmethod
    def _parse(data: dict) -> Lyrics | None:
        if data.get("instrumental"):
            return Lyrics(instrumental=True, source="lrclib")
        synced = data.get("syncedLyrics") or None
        plain = data.get("plainLyrics") or None
        if not synced and not plain:
            return None
        return Lyrics(synced=synced, plain=plain, source="lrclib")
