"""Tags downloaded audio files with Deezer metadata.

Supports OGG/Opus (mutagen Vorbis comments), WebM (ffmpeg -c:a copy), and
M4A (mutagen iTunes atoms). All tag values come from Deezer — never from
YouTube. See ARCHITECTURE.md section 4/10 ("File Tagging Schema").
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

from sideb.models.track import Track
from sideb.utils.http import default_ssl_context


class Tagger:
    def __init__(self, *, user_agent: str, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            verify=default_ssl_context(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def tag_file(self, filepath: Path, track: Track) -> None:
        cover_bytes = await self._fetch_cover(track)

        suffix = filepath.suffix.lower()
        if suffix in (".opus", ".ogg"):
            self._tag_vorbis(filepath, track, cover_bytes)
        elif suffix == ".webm":
            self._tag_webm(filepath, track, cover_bytes)
        elif suffix == ".m4a":
            self._tag_m4a(filepath, track, cover_bytes)
        else:
            raise ValueError(f"Unsupported audio container for tagging: {suffix}")

    async def _fetch_cover(self, track: Track) -> bytes | None:
        url = track.album.cover_url_xl or track.album.cover_url
        if not url:
            return None
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError:
            return None

    def _tag_vorbis(self, filepath: Path, track: Track, cover_bytes: bytes | None) -> None:
        audio = OggOpus(filepath)
        audio["TITLE"] = track.title
        audio["ARTIST"] = track.artist.name
        audio["ALBUM"] = track.album.title
        if track.album.release_date:
            audio["DATE"] = str(track.album.release_date)
        audio["TRACKNUMBER"] = str(track.track_number)
        audio["DISCNUMBER"] = str(track.disk_number)
        if track.isrc:
            audio["ISRC"] = track.isrc
        if track.album.genre:
            audio["GENRE"] = track.album.genre

        if cover_bytes:
            picture = _build_flac_picture(cover_bytes)
            audio["metadata_block_picture"] = [picture]

        audio.save()

    def _tag_webm(self, filepath: Path, track: Track, cover_bytes: bytes | None) -> None:
        meta_flags = [
            "-metadata", f"title={track.title}",
            "-metadata", f"artist={track.artist.name}",
            "-metadata", f"album={track.album.title}",
            "-metadata", f"date={track.album.release_date or ''}",
            "-metadata", f"track={track.track_number}",
            "-metadata", f"disc={track.disk_number}",
        ]
        if track.isrc:
            meta_flags += ["-metadata", f"isrc={track.isrc}"]
        if track.album.genre:
            meta_flags += ["-metadata", f"genre={track.album.genre}"]

        tmp_out = filepath.with_suffix(".tagged.webm")
        try:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(filepath),
                "-c:a", "copy",
            ] + meta_flags + [str(tmp_out)]
            res = subprocess.run(cmd, capture_output=True, timeout=60)
            if res.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 1000:
                filepath.unlink()
                tmp_out.rename(filepath)
            else:
                tmp_out.unlink(missing_ok=True)
        except Exception:
            tmp_out.unlink(missing_ok=True)

    def _tag_m4a(self, filepath: Path, track: Track, cover_bytes: bytes | None) -> None:
        audio = MP4(filepath)
        audio["\xa9nam"] = track.title
        audio["\xa9ART"] = track.artist.name
        audio["\xa9alb"] = track.album.title
        if track.album.release_date:
            audio["\xa9day"] = str(track.album.release_date)
        audio["trkn"] = [(track.track_number, track.album.track_count or 0)]
        audio["disk"] = [(track.disk_number, 0)]
        if track.isrc:
            audio["----:com.apple.iTunes:ISRC"] = track.isrc.encode("utf-8")
        if track.album.genre:
            audio["\xa9gen"] = track.album.genre

        if cover_bytes:
            audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

        audio.save()


def _build_flac_picture(image_bytes: bytes) -> str:
    """Builds a base64-encoded FLAC picture block for the Vorbis
    `metadata_block_picture` tag (used by Opus/OGG cover art)."""
    import base64

    from mutagen.flac import Picture

    picture = Picture()
    picture.data = image_bytes
    picture.type = 3  # front cover
    picture.mime = "image/jpeg"
    return base64.b64encode(picture.write()).decode("ascii")
