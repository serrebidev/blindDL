"""JSON manifest service for queue & batch download workflow.

Each collection (artist, playlist, top-level singles) gets a
``.sideb/manifest.json`` that tracks Deezer metadata, YouTube resolution
status, and download state across sessions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from sideb.utils.path import sanitize_filename


MANIFEST_VERSION = 1


UNRESOLVED = "__unresolved__"
NOT_FOUND = "__not_found__"


@dataclass
class ManifestTrack:
    id: str
    title: str
    artist: str
    album: str = ""
    album_type: str = ""
    year: int | None = None
    track_number: int = 1
    duration: int = 0
    isrc: str = ""
    youtube_video_id: str | None = UNRESOLVED
    youtube_channel_id: str | None = None
    downloaded: bool = False
    filepath: str | None = None
    lyrics: dict | None = None  # pre-collected lyrics: {"plain": ..., "synced": [...], "word_by_word": [...]}


@dataclass
class SourceInfo:
    type: str  # "artist" | "album" | "playlist" | "track"
    url: str = ""
    name: str = ""


@dataclass
class Manifest:
    version: int = MANIFEST_VERSION
    source: SourceInfo = field(default_factory=lambda: SourceInfo(type="track"))
    tracks: list[ManifestTrack] = field(default_factory=list)


def _manifest_dir(output_dir: Path, source_type: str, artist_dir: str | None = None) -> Path:
    """Return the ``.sideb/`` directory for a given collection."""
    if source_type in ("artist", "album") and artist_dir:
        safe = sanitize_filename(artist_dir)
        return output_dir / "artists" / safe / ".sideb"
    if source_type == "playlist":
        safe = sanitize_filename(artist_dir) if artist_dir else "untitled"
        return output_dir / "playlists" / safe / ".sideb"
    return output_dir / "singles" / ".sideb"


def _manifest_path(output_dir: Path, source_type: str, artist_dir: str | None = None) -> Path:
    return _manifest_dir(output_dir, source_type, artist_dir) / "manifest.json"


def read_manifest(output_dir: Path, source_type: str, artist_dir: str | None = None) -> Manifest | None:
    path = _manifest_path(output_dir, source_type, artist_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        src = data.get("source", {})
        tracks = [
            ManifestTrack(**t) for t in data.get("tracks", [])
        ]
        return Manifest(
            version=data.get("version", MANIFEST_VERSION),
            source=SourceInfo(type=src.get("type", "track"), url=src.get("url", ""), name=src.get("name", "")),
            tracks=tracks,
        )
    except Exception:
        return None


def write_manifest(manifest: Manifest, output_dir: Path, source_type: str, artist_dir: str | None = None) -> None:
    path = _manifest_path(output_dir, source_type, artist_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": manifest.version,
        "source": asdict(manifest.source),
        "tracks": [asdict(t) for t in manifest.tracks],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_tracks(existing: list[ManifestTrack], new: list[ManifestTrack]) -> list[ManifestTrack]:
    """Merge new tracks into existing list, keyed by Deezer track ID.
    Existing tracks keep their youtube/downloaded state; new tracks are appended.
    """
    by_id: dict[str, ManifestTrack] = {}
    for t in existing:
        by_id[t.id] = t
    for t in new:
        if t.id not in by_id:
            by_id[t.id] = t
    return list(by_id.values())


def find_manifest_dirs(output_dir: Path) -> list[tuple[str, str | None]]:
    """Scan all ``.sideb/`` directories under output_dir.

    Returns list of ``(source_type, artist_dir)`` tuples.
    """
    found: list[tuple[str, str | None]] = []

    # Per-artist manifests
    artists_dir = output_dir / "artists"
    if artists_dir.exists():
        for artist_path in artists_dir.iterdir():
            if artist_path.is_dir():
                manifest = artist_path / ".sideb" / "manifest.json"
                if manifest.exists():
                    found.append(("artist", artist_path.name))

    # Per-playlist manifests
    playlists_dir = output_dir / "playlists"
    if playlists_dir.exists():
        for pl_path in playlists_dir.iterdir():
            if pl_path.is_dir():
                manifest = pl_path / ".sideb" / "manifest.json"
                if manifest.exists():
                    found.append(("playlist", pl_path.name))

    # Top-level singles manifest
    singles_manifest = output_dir / "singles" / ".sideb" / "manifest.json"
    if singles_manifest.exists():
        found.append(("singles", None))

    return found


def export_m3u8(output_dir: Path, source_type: str, artist_dir: str | None = None) -> Path | None:
    """Export a manifest's downloaded tracks as an M3U8 playlist file.

    Returns the path to the generated .m3u8 file, or None if no tracks to export.
    """
    manifest = read_manifest(output_dir, source_type, artist_dir)
    if manifest is None:
        return None

    downloaded = [t for t in manifest.tracks if t.downloaded and t.filepath]
    if not downloaded:
        return None

    lines = ["#EXTM3U"]
    for t in downloaded:
        duration_sec = t.duration
        if t.filepath:
            lines.append(f"#EXTINF:{duration_sec},{t.artist} - {t.title}")
            lines.append(t.filepath)

    m3u_dir = _manifest_dir(output_dir, source_type, artist_dir)
    m3u_path = m3u_dir / "playlist.m3u8"
    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


def scan_undownloaded(output_dir: Path) -> list[tuple[ManifestTrack, str, str | None]]:
    """Scan all manifests for tracks that need YouTube resolution or download.

    Returns list of ``(track, source_type, artist_dir)``.
    """
    pending: list[tuple[ManifestTrack, str, str | None]] = []
    for source_type, artist_dir in find_manifest_dirs(output_dir):
        m = read_manifest(output_dir, source_type, artist_dir)
        if m is None:
            continue
        for t in m.tracks:
            if not t.downloaded and t.youtube_video_id not in (NOT_FOUND, UNRESOLVED, None):
                pending.append((t, source_type, artist_dir))
    return pending
