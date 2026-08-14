"""YouTube audio provider.

Search uses ytmusicapi with title+artist matching against Deezer metadata.
Download is delegated to `yt-dlp`. YouTube's own metadata (title, channel,
description) is used only to validate a match — never for tagging.
Metadata always comes from Deezer.
"""

from __future__ import annotations

import asyncio
import re
import string
from pathlib import Path
from typing import Any

import yt_dlp
from ytmusicapi import YTMusic

from sideb.providers.audio.base import ProgressCallback
from sideb.models.track import Track

_INSTRUMENTAL_RE = re.compile(r"\binstrumental\b", re.IGNORECASE)

_YT_SUFFIX_RE = re.compile(
    r"\s*\((?:Official\s+)?(?:(?:Music\s+)?Video|Audio|Lyrics?|Lyric\s+Video|Visualizer|"
    r"Performance\s+Video|Live\s+Session|Official|HQ|HD|4K|Remaster(?:ed)?|"
    r"Explicit|Clean)\)\s*",
    re.IGNORECASE,
)

_TRANSLATOR = str.maketrans("", "", string.punctuation)

# Artist name -> YouTube Music channel id, for the whole process. The answer
# does not change between tracks, and finding it costs a search, an artist
# fetch and a playlist parse.
_ARTIST_CHANNELS: dict[str, str | None] = {}


def is_instrumental(title: str) -> bool:
    """Detect instrumental versions so they can be skipped entirely."""
    return bool(_INSTRUMENTAL_RE.search(title))


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(text.lower().translate(_TRANSLATOR).split())


def _clean_yt_title(title: str) -> str:
    """Strip common YouTube suffixes so we can compare against canonical title."""
    cleaned = _YT_SUFFIX_RE.sub("", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _titles_match(track_title: str, yt_title: str) -> bool:
    """Check if the cleaned/normalized YouTube title matches the track title."""
    a = _normalize(track_title)
    b = _normalize(_clean_yt_title(yt_title))
    return a == b or a in b or b in a


def _artist_in_result(artist_name: str, result_artists: list[dict]) -> bool:
    """Check if the given artist name appears in the result's artist list."""
    target = artist_name.lower().strip()
    for art in result_artists:
        name = art.get("name", "").lower().strip()
        if name == target or target in name or name in target:
            return True
    return False


class YouTubeAudio:
    """Implements the AudioProvider protocol via ytmusicapi (search) + yt-dlp (download)."""

    def __init__(
        self,
        *,
        cookies_file: Path | None = None,
        cookies_from_browser: str | None = None,
        isrc_tolerance: int = 5,
        title_tolerance: int = 8,
        channel_tolerance: int = 8,
        proxy: str | None = None,
        user_agent: str | None = None,
        concurrent_fragments: int = 5,
    ) -> None:
        self._cookies_file = cookies_file
        self._cookies_from_browser = cookies_from_browser
        self._isrc_tol = isrc_tolerance
        self._title_tol = title_tolerance
        self._channel_tol = channel_tolerance
        self._proxy = proxy
        self._user_agent = user_agent
        self._concurrent_fragments = concurrent_fragments
        self._yt = YTMusic()
        # Shared, because callers build one of these per track: an
        # instance-scoped memo never saw a second look-up, so every track of
        # the same artist re-searched the channel and re-parsed a
        # five-hundred entry playlist to find it.
        self._artist_channel_cache = _ARTIST_CHANNELS

    async def aclose(self) -> None:
        return None

    # ---- Search strategy ------------------------------------------------

    async def search(self, track: Track) -> str | None:
        return await asyncio.to_thread(self._search_video_id, track)

    def _search_video_id(self, track: Track) -> str | None:
        vid = self._search_by_isrc(track)
        if vid:
            return vid

        vid = self._search_in_artist_channel(track)
        if vid:
            return vid

        return self._search_title(track)

    # ---- Helpers --------------------------------------------------------

    def _resolve_artist_channel(self, artist_name: str) -> str | None:
        if artist_name in self._artist_channel_cache:
            return self._artist_channel_cache[artist_name]
        channel_id = None
        try:
            results = self._yt.search(artist_name, filter="artists", limit=3)
            for r in results:
                if r.get("artist", "").lower() == artist_name.lower():
                    channel_id = r.get("browseId")
                    break
            if channel_id is None and results:
                channel_id = results[0].get("browseId")
        except Exception:
            channel_id = None
        self._artist_channel_cache[artist_name] = channel_id
        return channel_id

    @staticmethod
    def _score_result(
        result: dict,
        track: Track,
        dur_tol: int,
    ) -> tuple[int, bool, int] | None:
        """Score a search result. Returns (title_score, artist_match, dur_delta)
        or None if the result fails minimum criteria."""
        vid = result.get("videoId")
        if not vid:
            return None
        yt_title = result.get("title", "")
        yt_artists: list[dict] = result.get("artists", [])
        dur = result.get("duration_seconds") or 0

        title_ok = _titles_match(track.title, yt_title)
        artist_ok = _artist_in_result(track.artist.name, yt_artists)

        if not title_ok and not artist_ok:
            return None

        if dur and track.duration:
            delta = abs(int(dur) - track.duration)
            if delta > dur_tol:
                return None
        else:
            delta = 0

        title_score = 2 if _normalize(track.title) == _normalize(_clean_yt_title(yt_title)) else 1
        return (title_score, artist_ok, delta)

    @staticmethod
    def _best_result(
        results: list[dict],
        track: Track,
        dur_tol: int,
    ) -> str | None:
        """Score all results and return the videoId of the best match."""
        scored: list[tuple[tuple[int, bool, int], str]] = []
        for r in results:
            s = YouTubeAudio._score_result(r, track, dur_tol)
            if s:
                scored.append((s, r["videoId"]))

        if not scored:
            return None
        scored.sort(key=lambda x: (x[0][0], x[0][1], -x[0][2]), reverse=True)
        return scored[0][1]

    # ---- Pass 1: ISRC search -------------------------------------------

    def _search_by_isrc(self, track: Track) -> str | None:
        """Search by ISRC code — globally unique per recording."""
        if not track.isrc:
            return None
        try:
            results = self._yt.search(f"ISRC:{track.isrc}", filter="songs", limit=5)
            for r in results:
                vid = r.get("videoId")
                if not vid:
                    continue
                dur = r.get("duration_seconds") or 0
                if dur and track.duration:
                    delta = abs(int(dur) - track.duration)
                    if delta <= self._isrc_tol:
                        return vid
                else:
                    return vid
        except Exception:
            pass
        return None

    # ---- Pass 2: Artist channel search ---------------------------------

    def _search_in_artist_channel(self, track: Track) -> str | None:
        """Resolve the artist's official YTMusic channel and match by title
        + artist + duration within the channel's song list."""
        try:
            channel_id = self._resolve_artist_channel(track.artist.name)
            if not channel_id:
                return None
            artist_data = self._yt.get_artist(channel_id)
            songs_pl_id = artist_data.get("songs", {}).get("browseId")
            if not songs_pl_id:
                return None
            pl_data = self._yt.get_playlist(songs_pl_id, limit=500)
            tol = max(self._channel_tol, int(track.duration * 0.15))
            scored: list[tuple[tuple[int, bool, int], str]] = []
            for t in pl_data.get("tracks", []):
                s = self._score_result(t, track, tol)
                if s:
                    scored.append((s, t["videoId"]))
            if not scored:
                return None
            scored.sort(key=lambda x: (x[0][0], x[0][1], -x[0][2]), reverse=True)
            return scored[0][1]
        except Exception:
            return None

    # ---- Pass 3: Title+artist search -----------------------------------

    def _search_title(self, track: Track) -> str | None:
        """Search with title+artist query, filter=songs. Match by title,
        artist, and duration. Tries exact search first, falls back to
        default spelling."""
        tol = max(self._title_tol, int(track.duration * 0.15))
        for ignore_spelling in (True, False):
            try:
                results = self._yt.search(
                    track.search_query,
                    filter="songs",
                    limit=10,
                    ignore_spelling=ignore_spelling,
                )
                vid = self._best_result(results, track, tol)
                if vid:
                    return vid
            except Exception:
                continue
        return None

    # ---- Channel info (UI helpers) -------------------------------------

    def get_artist_channel_info(self, artist_name: str) -> dict | None:
        """Return YouTube channel info (name, subscribers) for an artist.
        Used by the UI to confirm the correct channel before download."""
        try:
            channel_id = self._resolve_artist_channel(artist_name)
            if not channel_id:
                return None
            artist_data = self._yt.get_artist(channel_id)
            subs = artist_data.get("subscribers", "unknown")
            return {
                "id": channel_id,
                "name": artist_data.get("name", artist_name),
                "subscribers": str(subs),
            }
        except Exception:
            return None

    def search_artist_channels(self, artist_name: str, limit: int = 5) -> list[dict]:
        """Search YouTube for artist channels and return their top tracks.
        Returns list of {artist: {id, name, subscribers}, tracks: [{title, ...}]}."""
        results: list[dict] = []
        try:
            yt_results = list(self._yt.search(artist_name, filter="artists", limit=limit))[:limit]
            for r in yt_results:
                browse_id = r.get("browseId")
                if not browse_id:
                    continue
                try:
                    artist_data = self._yt.get_artist(browse_id)
                    top_tracks = self._get_channel_top_tracks(browse_id)
                    results.append({
                        "artist": {
                            "id": browse_id,
                            "name": artist_data.get("name", r.get("artist", "?")),
                            "subscribers": str(artist_data.get("subscribers", "?")),
                        },
                        "tracks": top_tracks[:3],
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return results

    def _get_channel_top_tracks(self, browse_id: str, limit: int = 3) -> list[dict]:
        """Get first few tracks from a YouTube channel's songs playlist."""
        try:
            artist_data = self._yt.get_artist(browse_id)
            songs_pl_id = artist_data.get("songs", {}).get("browseId")
            if not songs_pl_id:
                return []
            pl = self._yt.get_playlist(songs_pl_id, limit=limit)
            return [
                {"title": t.get("title", "?"), "videoId": t.get("videoId")}
                for t in pl.get("tracks", [])[:limit]
                if t.get("videoId")
            ]
        except Exception:
            return []

    # ---- Download via yt-dlp -------------------------------------------

    async def download(
        self,
        track: Track,
        url: str,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        return await asyncio.to_thread(self._download_sync, track, url, dest, on_progress)

    def _download_sync(
        self,
        track: Track,
        url: str,
        dest: Path,
        on_progress: ProgressCallback | None,
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)

        def hook(d: dict) -> None:
            if on_progress is None:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                progress = (downloaded / total) if total else 0.0
                speed = d.get("_speed_str", "")
                on_progress(progress, speed)
            elif d.get("status") == "finished":
                on_progress(1.0, "")

        # Prefer native Opus in webm (no transcoding), fall back to m4a/AAC.
        # No postprocessor: we keep whatever yt-dlp downloads natively.
        audio_format_str = (
            "bestaudio[acodec=opus][ext=webm]"
            "/251"
            "/bestaudio[acodec=opus]"
            "/bestaudio[ext=m4a]"
            "/140"
            "/bestaudio"
        )
        ydl_opts: dict[str, Any] = {
            "format": audio_format_str,
            "outtmpl": str(dest.with_suffix("")) + ".%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "progress_hooks": [hook],
            "extractor_retries": 3,
            "file_access_retries": 3,
            "fragment_retries": 10,
            "retry_sleep_functions": {
                "http": lambda n: 2 ** n,
                "fragment": lambda n: 2 ** n,
                "file_access": lambda n: 2 ** n,
                "extractor": lambda n: 2 ** n,
            },
            "throttledratelimit": 100000,
            "concurrent_fragment_downloads": self._concurrent_fragments,
            "buffersize": 65536,
            "sleep_requests": 1.0,
            "sleep_interval": 10,
            "max_sleep_interval": 20,
            "extractor_args": {"youtube": {"player_skip": ["webpage", "configs", "initial_data"]}},
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Origin": "https://music.youtube.com",
                "Referer": "https://music.youtube.com/",
            },
        }
        if self._cookies_file:
            ydl_opts["cookiefile"] = str(self._cookies_file)
        elif self._cookies_from_browser:
            ydl_opts["cookiesfrombrowser"] = (self._cookies_from_browser,)
        if self._proxy:
            ydl_opts["proxy"] = self._proxy
        if self._user_agent:
            ydl_opts["http_headers"]["User-Agent"] = self._user_agent

        video_url = url if url.startswith("http") else f"https://music.youtube.com/watch?v={url}"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType]
            ydl.download([video_url])

        # Find whatever file yt-dlp produced — keep the native container
        candidates = list(dest.parent.glob(dest.stem + ".*"))
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"yt-dlp did not produce an output file for {track.title}")
