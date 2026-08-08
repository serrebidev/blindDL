"""YouTube Music metadata provider. Bridges YouTube Music IDs → Deezer metadata.

Flow:
  1. Parse YouTube Music URL → extract videoId / playlistId
  2. ytmusicapi → basic metadata (title, artist, album, duration)
  3. Deezer search by "{title} {artist}"
  4. Match by duration proximity → get Deezer track ID
  5. Deezer get_track() → full enriched Track (ISRC, genre, cover, etc.)
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ytmusicapi import YTMusic

from sideb.models.events import TrackCompleted, TrackQueued, WorkerFinished, WorkerStage, WorkerStarted
from sideb.models.track import Album, Artist, Track
from sideb.providers.metadata.deezer import DeezerMetadata

_DURATION_TOLERANCE = 8  # seconds


def _parse_duration(value: str | int | None) -> int:
    """Parse ytmusicapi duration (int seconds, '3:34', or None) to seconds."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    parts = str(value).split(":")
    if len(parts) == 2:
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, TypeError):
            pass
    elif len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, TypeError):
            pass
    return 0

_YT_SUFFIX_RE = re.compile(
    r"\s*\((?:Official\s+)?(?:(?:Music\s+)?Video|Audio|Lyrics?|Lyric\s+Video|Visualizer|"
    r"Performance\s+Video|Live\s+Session)\)\s*$",
    re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    return _YT_SUFFIX_RE.sub("", raw).strip()


_YT_URL_RE = re.compile(
    r"(?:youtube\.com|music\.youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)

_VIDEO_ID_RE = re.compile(r"[?&]v=([a-zA-Z0-9_-]{11})")
_PLAYLIST_ID_RE = re.compile(r"[?&]list=([a-zA-Z0-9_-]+)")
_SHORT_VIDEO_RE = re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})")


def _clean_browser_headers(path: str) -> None:
    """Remove Accept-Encoding so requests lib handles compression natively."""
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    changed = False
    for key in list(data.keys()):
        if key.lower() == "accept-encoding":
            del data[key]
            changed = True
    if changed:
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _check_browser_headers(path: str) -> None:
    """Validate the browser headers file has Authorization and Cookie."""
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    if "Authorization" not in data:
        raise ValueError(
            'browser.json is missing the "Authorization" header. '
            "Make sure you select an XHR/fetch request (with /youtubei/v1/ in the path), "
            "not the main page navigation request."
        )


class YouTubeMusicMetadata:
    """Implements MetadataProvider via ytmusicapi + Deezer enrichment."""

    def __init__(self, deezer: DeezerMetadata, oauth_file: str | None = None, *, duration_threshold: int = 600) -> None:
        self._deezer = deezer
        self._oauth_file = oauth_file
        self._duration_threshold = duration_threshold
        if oauth_file and Path(oauth_file).exists():
            _clean_browser_headers(oauth_file)
            _check_browser_headers(oauth_file)
            self._yt = YTMusic(str(oauth_file))
        else:
            self._yt = YTMusic()

    def has_oauth(self) -> bool:
        return self._oauth_file is not None and Path(self._oauth_file).exists()

    async def aclose(self) -> None:
        await self._deezer.aclose()

    # ---- URL resolution -------------------------------------------------

    async def resolve_url(self, url: str, *, on_progress=None) -> list[Track]:
        if not _YT_URL_RE.search(url):
            return await self.search(url, on_progress=on_progress)

        video_id = self._extract_video_id(url)
        if video_id:
            return [await self.get_track(video_id)]

        playlist_id = self._extract_playlist_id(url)
        if playlist_id:
            return await self.get_playlist(playlist_id)

        return await self.search(url)

    async def get_track(self, track_id: str) -> Track:
        """Get a single track by YouTube videoId → Deezer enriched."""
        song = self._yt.get_song(track_id)
        video_details = song.get("videoDetails") or {}
        title = _clean_title(video_details.get("title", ""))
        artist_name = _first_artist(song) or video_details.get("author", "")
        duration = _parse_duration(video_details.get("lengthSeconds"))
        thumbnail_url = self._largest_thumb(video_details.get("thumbnail", {}))

        return await self._match_to_deezer(
            title, artist_name, duration,
            fallback_video_id=track_id,
            thumbnail_url=thumbnail_url,
        )

    async def get_playlist(self, playlist_id: str, *, on_progress=None) -> list[Track]:
        pl = self._yt.get_playlist(playlist_id, limit=500)
        tracks_data = pl.get("tracks", [])
        total = len(tracks_data)
        results: list[Track] = []

        for i, entry in enumerate(tracks_data):
            title = _clean_title(entry.get("title", ""))
            artists = entry.get("artists") or []
            artist_name = artists[0].get("name", "") if artists else ""
            duration = _parse_duration(entry.get("duration"))
            video_id = entry.get("videoId", "")
            thumbnail_url = self._largest_thumb(entry)
            if not title or not artist_name or not video_id:
                continue
            if on_progress:
                on_progress(i + 1, total, f"Track {i + 1}/{total}: {title}")
            try:
                track = await self._match_to_deezer(
                    title, artist_name, duration,
                    fallback_video_id=video_id,
                    thumbnail_url=thumbnail_url,
                )
                track.track_number = i + 1
                results.append(track)
            except Exception:
                pass

        return results

    async def get_playlist_info(self, playlist_id: str) -> dict:
        """Return playlist metadata (title, trackCount) from YouTube Music."""
        pl = self._yt.get_playlist(playlist_id, limit=1)
        return {
            "title": pl.get("title", ""),
            "track_count": pl.get("trackCount", 0),
        }

    async def get_album(self, album_id: str) -> list[Track]:
        return []

    async def search_candidates(self, query: str, limit: int = 5) -> list[dict]:
        """Lightweight search returning raw dicts (no Deezer enrichment) for the picker."""
        results = self._yt.search(query, filter="songs", limit=limit)
        candidates = []
        for r in results:
            title = _clean_title(r.get("title", ""))
            artists = r.get("artists") or []
            artist_name = artists[0].get("name", "") if artists else ""
            video_id = r.get("videoId", "")
            if not title or not artist_name or not video_id:
                continue
            album_entry = r.get("album") or {}
            album_name = album_entry.get("name") if isinstance(album_entry, dict) else ""
            candidates.append({
                "title": title,
                "artist": artist_name,
                "album": album_name or None,
                "duration": _parse_duration(r.get("duration")),
                "videoId": video_id,
            })
        return candidates

    async def search(self, query: str, *, on_progress=None) -> list[Track]:
        results = self._yt.search(query, filter="songs", limit=5)
        total = len(results)
        tracks: list[Track] = []
        for i, entry in enumerate(results):
            title = _clean_title(entry.get("title", ""))
            artists = entry.get("artists") or []
            artist_name = artists[0].get("name", "") if artists else ""
            duration = _parse_duration(entry.get("duration"))
            video_id = entry.get("videoId", "")
            if not title or not artist_name or not video_id:
                continue
            if on_progress:
                on_progress(i + 1, total, f"Matching {i + 1}/{total}: {title}")
            try:
                track = await self._match_to_deezer(title, artist_name, duration, fallback_video_id=video_id)
                tracks.append(track)
            except Exception:
                pass
        return tracks

    async def get_liked_songs(self, limit: int = 25, *, on_progress=None, event_bus=None) -> list[Track]:
        """Fetch liked songs from the authenticated user's YouTube Music library.

        Tries Deezer enrichment for each track (ISRC, cover, genre).
        Falls back to raw ytmusicapi data when Deezer fails or has no match.
        """
        if not self.has_oauth():
            return []

        try:
            data = await asyncio.to_thread(self._yt.get_liked_songs, limit)
        except KeyError:
            return []
        tracks_data = data.get("tracks", [])
        if len(tracks_data) > limit:
            tracks_data = tracks_data[:limit]
        total = len(tracks_data)
        results: list[Track] = []

        if event_bus and tracks_data:
            first = tracks_data[0]
            t = _clean_title(first.get("title", ""))
            a = (first.get("artists") or [{}])[0].get("name", "")
            placeholder = Track(
                id=first.get("videoId", ""), title=t,
                artist=Artist(id="", name=a),
                album=Album(id="", title="", artist=Artist(id="", name=a)),
                duration=_parse_duration(first.get("duration")),
            )
            event_bus.emit(TrackQueued(track=placeholder, position=0, total=total))

        for i, entry in enumerate(tracks_data):
            title = _clean_title(entry.get("title", ""))
            artists = entry.get("artists") or []
            artist_name = artists[0].get("name", "") if artists else ""
            duration = _parse_duration(entry.get("duration"))
            video_id = entry.get("videoId", "")
            if not title or not artist_name or not video_id:
                continue
            if on_progress:
                on_progress(i + 1, total, f"Liked {i + 1}/{total}: {title}")

            if event_bus:
                placeholder = Track(
                    id=video_id, title=title,
                    artist=Artist(id="", name=artist_name),
                    album=Album(id="", title="", artist=Artist(id="", name=artist_name)),
                    duration=duration,
                )
                event_bus.emit(WorkerStarted(worker_id=1, track=placeholder))
                event_bus.emit(WorkerStage(worker_id=1, track=placeholder, stage="matching"))

            track = await self._match_to_deezer(title, artist_name, duration, fallback_video_id=video_id)
            track.track_number = i + 1

            if event_bus:
                event_bus.emit(WorkerFinished(worker_id=1, track=track))
                event_bus.emit(TrackCompleted(track=track, filepath=Path()))

            results.append(track)

        return results

    # ---- Internals -----------------------------------------------------

    async def _match_to_deezer(
        self,
        title: str,
        artist_name: str,
        duration: int,
        *,
        fallback_video_id: str | None = None,
        thumbnail_url: str | None = None,
    ) -> Track:
        """Try Deezer enrichment, or build YouTube-only Track with thumbnail as cover.

        Skips Deezer for long tracks (over ``_duration_threshold``).
        """
        if duration > self._duration_threshold:
            return self._build_fallback_track(
                title, artist_name, duration,
                video_id=fallback_video_id, thumbnail_url=thumbnail_url,
            )

        try:
            deezer_tracks = await self._deezer.search(f"{title} {artist_name}")
        except Exception:
            return self._build_fallback_track(
                title, artist_name, duration,
                video_id=fallback_video_id, thumbnail_url=thumbnail_url,
            )

        best = None
        best_delta = _DURATION_TOLERANCE + 1
        for dt in deezer_tracks:
            delta = abs(dt.duration - duration)
            if delta <= _DURATION_TOLERANCE and delta < best_delta:
                best = dt
                best_delta = delta

        if best is not None:
            try:
                return await self._deezer.get_track(best.id)
            except Exception:
                pass

        return self._build_fallback_track(
            title, artist_name, duration,
            video_id=fallback_video_id, thumbnail_url=thumbnail_url,
        )

    @staticmethod
    def _build_fallback_track(
        title: str,
        artist_name: str,
        duration: int,
        video_id: str | None = None,
        thumbnail_url: str | None = None,
    ) -> Track:
        """Minimal Track when Deezer match fails."""
        artist = Artist(id="", name=artist_name)
        album = Album(id="", title=artist_name, artist=artist, cover_url=thumbnail_url)
        return Track(
            id=video_id or "",
            title=title,
            artist=artist,
            album=album,
            duration=duration,
        )

    @staticmethod
    def _largest_thumb(container: dict) -> str | None:
        """Extract the largest thumbnail URL from a videoDetails or playlist entry dict."""
        thumbs = container.get("thumbnails") if isinstance(container.get("thumbnails"), list) else []
        if not thumbs:
            return None
        best = max(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0))
        return best.get("url")

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        m = _VIDEO_ID_RE.search(url) or _SHORT_VIDEO_RE.search(url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_playlist_id(url: str) -> str | None:
        m = _PLAYLIST_ID_RE.search(url)
        return m.group(1) if m else None


def _first_artist(song: dict) -> str | None:
    """Extract the primary artist name from a ytmusicapi get_song response."""
    # ytmusicapi returns artist names in videoDetails or in microformat
    vd = song.get("videoDetails") or {}
    author = vd.get("author", "")
    if author and author.lower() not in ("", "youtube", "topic"):
        return author
    # Try microformat / owner channel name
    micro = song.get("microformat") or {}
    micro_rend = micro.get("microformatDataRenderer") or {}
    owner = (micro_rend.get("ownerChannelName") or "") if micro_rend else (micro.get("ownerChannelName") or "")
    return owner if owner else None
