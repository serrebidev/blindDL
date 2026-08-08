"""Core catalog data models. All values on these models are sourced from the
metadata provider (Deezer) — never from the audio provider (YouTube)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class Artist:
    id: str
    name: str


@dataclass(slots=True)
class Album:
    id: str
    title: str
    artist: Artist
    cover_url: str | None = None
    cover_url_xl: str | None = None
    release_date: date | None = None
    track_count: int | None = None
    genre: str | None = None
    label: str | None = None
    album_type: str = ""  # "album" | "ep" | "compilation" | "single" | ""

    @property
    def release_year(self) -> int | None:
        return self.release_date.year if self.release_date else None


@dataclass(slots=True)
class Track:
    id: str
    title: str
    artist: Artist
    album: Album
    duration: int  # seconds
    track_number: int = 1
    disk_number: int = 1
    isrc: str = ""
    bpm: float | None = None
    explicit: bool = False
    has_featured_artist: bool = False
    is_deluxe: bool = False
    is_original_release: bool = True
    contributors: list[Artist] = field(default_factory=list)
    metadata_quality: int = 0  # count of populated optional fields; used for dedup scoring
    lyrics: Lyrics | None = None  # pre-collected lyrics (set by --pre-collect)

    @property
    def search_query(self) -> str:
        """The query used against the audio provider. Deezer metadata only —
        never YouTube's own titles/descriptions. Strips leading/trailing
        special characters (dashes, dots, parens) that confuse YouTube search."""
        title = self.title.strip("-.")
        if title.startswith("(") and title.endswith(")"):
            title = title[1:-1].strip()
        return f"{self.artist.name} {title}"


@dataclass(slots=True)
class Lyrics:
    synced: str | None = None          # standard LRC: [mm:ss.xx] line
    word_synced: str | None = None     # enhanced LRC: per-word inline timestamps
    plain: str | None = None
    source: str = ""                   # "deezer" | "lrclib"
    instrumental: bool = False
