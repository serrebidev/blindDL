"""Orchestrates the multi-provider lyrics fetch chain.

Priority order (see ARCHITECTURE.md "Lyrics Fetch Chain"):
    1. Deezer GraphQL (only if an ARL cookie was supplied) — word-level + synced
    2. LRCLIB (exact -> search -> variant-stripped title)
"""

from __future__ import annotations

from typing import Sequence

from sideb.models.track import Lyrics, Track
from sideb.providers.lyrics.base import LyricsProvider


class LyricsChain:
    def __init__(self, providers_in_priority_order: Sequence[LyricsProvider]) -> None:
        self._providers = providers_in_priority_order

    async def fetch(self, track: Track) -> Lyrics | None:
        best: Lyrics | None = None
        for provider in self._providers:
            try:
                result = await provider.get_lyrics(track)
            except Exception:
                continue
            if result is None or result.instrumental:
                continue
            if result.synced or result.word_synced:
                return result
            if best is None:
                best = result
        return best

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()
