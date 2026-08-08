from __future__ import annotations

from typing import Protocol, runtime_checkable

from sideb.models.track import Lyrics, Track


@runtime_checkable
class LyricsProvider(Protocol):
    """Fetches synced or plain lyrics for a track."""

    async def get_lyrics(self, track: Track) -> Lyrics | None:
        """Return lyrics for the given track, or None if not found."""
        ...

    async def aclose(self) -> None:
        ...
