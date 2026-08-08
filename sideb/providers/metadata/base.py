"""MetadataProvider protocol. Structural typing (PEP 544) means any object
with these methods satisfies the interface — no inheritance required."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from sideb.models.track import Track

ProgressCB = Callable[[int, int, str], None]  # current, total, message


@runtime_checkable
class MetadataProvider(Protocol):
    """Fetches track/album/playlist metadata from a music catalog."""

    async def resolve_url(self, url: str, *, on_progress: ProgressCB | None = None) -> list[Track]:
        """Parse a URL (or bare search text) and return the track list."""
        ...

    async def get_track(self, track_id: str) -> Track:
        """Get a single track by its catalog ID."""
        ...

    async def get_album(self, album_id: str) -> list[Track]:
        """Get all tracks for an album."""
        ...

    async def get_playlist(self, playlist_id: str) -> list[Track]:
        """Get all tracks for a playlist."""
        ...

    async def search(self, query: str) -> list[Track]:
        """Search the catalog and return matching tracks."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (HTTP connections, etc.)."""
        ...
