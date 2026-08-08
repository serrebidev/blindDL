from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from sideb.models.track import Track

ProgressCallback = Callable[[float, str], None]  # (progress 0..1, speed string)


@runtime_checkable
class AudioProvider(Protocol):
    """Searches for and downloads audio from a streaming platform."""

    async def search(self, track: Track) -> str | None:
        """Search for the track and return a video/stream URL, or None."""
        ...

    async def download(
        self,
        track: Track,
        url: str,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """Download audio from the URL to dest. Returns the final file path."""
        ...

    async def aclose(self) -> None:
        ...
