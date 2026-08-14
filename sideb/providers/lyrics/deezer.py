"""Deezer GraphQL lyrics provider.

Preferred lyrics source when a user-supplied ARL cookie is available: it
returns word-level (karaoke) timestamps in addition to standard line-synced
LRC, sourced from LyricFind. Uses the same Deezer track ID already fetched
during the metadata stage — no separate search needed.

Auth flow (see ARCHITECTURE.md "Deezer GraphQL Auth Flow"):
    1. POST https://auth.deezer.com/login/arl?jo=p&rto=c&i=c  with Cookie: arl=<value>
       -> {"jwt": "..."}
    2. POST https://pipe.deezer.com/api  with Authorization: Bearer <jwt>

The ARL cookie is a persistent, IP-tied session cookie obtained by the user
from their own browser (DevTools -> Application -> Cookies -> deezer.com ->
`arl`). Side B never bundles or requests one on the user's behalf.
"""

from __future__ import annotations

import httpx

from sideb.models.track import Lyrics, Track
from sideb.utils.http import default_ssl_context

AUTH_URL = "https://auth.deezer.com/login/arl?jo=p&rto=c&i=c"
GRAPHQL_URL = "https://pipe.deezer.com/api"

_LYRICS_QUERY = """
query GetLyrics($trackId: String!) {
  track(trackId: $trackId) {
    lyrics {
      text
      synchronizedWordByWordLines { start end words { start end word } }
      synchronizedLines { lrcTimestamp line milliseconds duration }
    }
  }
}
"""


class DeezerAuthError(RuntimeError):
    pass


class DeezerLyrics:
    """Implements the LyricsProvider protocol via Deezer's internal GraphQL API."""

    def __init__(self, *, arl: str, user_agent: str, timeout: float = 15.0, proxy: str | None = None) -> None:
        self._arl = arl
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            proxy=proxy,
            verify=default_ssl_context(),
        )
        self._jwt: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _authenticate(self) -> str:
        if self._jwt:
            return self._jwt
        resp = await self._client.post(AUTH_URL, cookies={"arl": self._arl})
        resp.raise_for_status()
        data = resp.json()
        jwt = data.get("jwt")
        if not jwt:
            raise DeezerAuthError("Deezer ARL login did not return a JWT — the ARL may be invalid/expired.")
        self._jwt = jwt
        return jwt

    async def get_lyrics(self, track: Track) -> Lyrics | None:
        try:
            jwt = await self._authenticate()
        except (httpx.HTTPError, DeezerAuthError):
            return None

        try:
            resp = await self._client.post(
                GRAPHQL_URL,
                headers={"Authorization": f"Bearer {jwt}"},
                json={
                    "operationName": "GetLyrics",
                    "variables": {"trackId": track.id},
                    "query": _LYRICS_QUERY,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError:
            return None

        lyrics_data = (
            payload.get("data", {}).get("track", {}).get("lyrics") if payload.get("data") else None
        )
        if not lyrics_data:
            return None

        synced_lines = lyrics_data.get("synchronizedLines") or []
        word_lines = lyrics_data.get("synchronizedWordByWordLines") or []
        plain = lyrics_data.get("text") or None

        synced_lrc = self._build_line_lrc(synced_lines) if synced_lines else None
        word_lrc = self._build_word_lrc(word_lines) if word_lines else None

        if not synced_lrc and not word_lrc and not plain:
            return None

        return Lyrics(synced=synced_lrc, word_synced=word_lrc, plain=plain, source="deezer")

    @staticmethod
    def _build_line_lrc(lines: list[dict]) -> str:
        rows = []
        for line in lines:
            ts = line.get("lrcTimestamp", "")
            text = line.get("line", "")
            rows.append(f"{ts}{text}")
        return "\n".join(rows)

    @staticmethod
    def _build_word_lrc(lines: list[dict]) -> str:
        """Builds enhanced LRC with per-word inline timestamps:
        [mm:ss.xx] <mm:ss.xx> word <mm:ss.xx> word ..."""
        rows = []
        for line in lines:
            start_ms = line.get("start", 0)
            row = f"[{_ms_to_lrc_ts(start_ms)}]"
            for w in line.get("words", []):
                row += f" <{_ms_to_lrc_ts(w.get('start', 0))}> {w.get('word', '')}"
            rows.append(row)
        return "\n".join(rows)


def _ms_to_lrc_ts(ms: int) -> str:
    total_seconds = ms / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"
