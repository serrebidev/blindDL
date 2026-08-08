"""The download pipeline: a per-track sequence of stages executed across a
configurable worker pool. Each worker runs one track end-to-end (metadata is
already fetched by resolve, so per-track work is: search -> download -> tag
-> lyrics -> move). See ARCHITECTURE.md sections 6 and "Design Rationale".
"""

from __future__ import annotations

import asyncio
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sideb.app.events_bus import EventBus
from sideb.config.settings import Settings
from sideb.models.events import (
    AudioDownloaded,
    LyricsFound,
    TagsApplied,
    TrackCompleted,
    TrackFailed,
    TrackQueued,
    TrackSkipped,
    WorkerFinished,
    WorkerStage,
    WorkerStarted,
)
from sideb.models.track import Track
from sideb.providers.audio.base import AudioProvider
from sideb.providers.audio.youtube import is_instrumental
from sideb.services.embedder import Embedder
from sideb.services.lyrics_chain import LyricsChain
from sideb.services.manifest import Manifest, write_manifest
from sideb.services.tagger import Tagger
from sideb.services.video_cache import find_live_path, register
from sideb.utils.path import sanitize_filename

_RATE_LIMITED_RE = re.compile(
    r"(?:rate.limited|try.again.later|too.many.requests|"
    r"HTTP Error 429|HTTP Error 402|"
    r"This content isn.?t available|Video unavailable)",
    re.IGNORECASE,
)


@dataclass
class SourceContext:
    """Describes the original input that produced the track list.

    - source_type: "artist" | "album" | "playlist" | "track" | None
    - source_name: human-readable name (artist name, playlist name, album title, etc.)
    - source_id: Deezer ID of the source (e.g. artist_id, album_id)
    """
    source_type: str | None
    source_name: str = ""
    source_id: str = ""


@dataclass
class TrackResult:
    track: Track
    success: bool
    filepath: Path | None = None
    had_lyrics: str | None = None  # lyrics source: "deezer", "lrclib", or None
    skipped_reason: str | None = None
    error: str | None = None


_HISTORY_FILE = ".dlhistory"


def _load_history(artist_dir: Path) -> set[str]:
    path = artist_dir / _HISTORY_FILE
    if not path.exists():
        return set()
    try:
        return set(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return set()


def _save_history_entry(artist_dir: Path, video_id: str) -> None:
    path = artist_dir / _HISTORY_FILE
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(video_id + "\n")
    except Exception:
        pass


class Pipeline:
    """Runs the download pipeline for a batch of tracks."""

    def __init__(
        self,
        *,
        settings: Settings,
        audio_provider: AudioProvider,
        tagger: Tagger,
        embedder: Embedder,
        lyrics_chain: LyricsChain | None,
        event_bus: EventBus,
    ) -> None:
        self._settings = settings
        self._audio = audio_provider
        self._tagger = tagger
        self._embedder = embedder
        self._lyrics_chain = lyrics_chain
        self._bus = event_bus
        self._source_ctx: SourceContext | None = None
        self._manifest: Manifest | None = None
        self._consec_failures = 0
        self._cb_threshold = 5
        self._cb_pause = 60
        self._rl_cooldown_until: float = 0.0  # all workers pause until this monotonic time when rate-limited

    @staticmethod
    def _is_rate_limited(error_str: str) -> bool:
        """Detect YouTube rate-limit errors vs other transient failures."""
        return bool(_RATE_LIMITED_RE.search(error_str))

    @staticmethod
    def _retry_backoff(attempt: int, *, rate_limited: bool = False) -> float:
        """Jittered exponential backoff with rate-limit-aware scaling.
        Uses full jitter (random 0..cap) to prevent thundering herd.
        Normal: 2^attempt capped at 60s. Rate-limited: 60*2^attempt capped at 3600s.
        """
        if rate_limited:
            cap = 3600.0
            base = 60.0
        else:
            cap = 60.0
            base = 2.0
        delay = min(cap, base * 2 ** attempt)
        return random.uniform(0, delay)

    async def _yt_sleep(self, *, worker_id: int = 0, track: Track | None = None) -> None:
        """Sleep between YouTube video requests to avoid rate limiting.
        Uses yt_sleep as base, randomized up to yt_sleep_random if set,
        matching yt-dlp's --sleep-interval / --max-sleep-interval behavior.
        Emits a WorkerStage so the UI shows the sleeping state.
        """
        base = self._settings.yt_sleep
        if base <= 0:
            return
        max_sleep = self._settings.yt_sleep_random
        if max_sleep > base:
            delay = random.uniform(base, max_sleep)
        else:
            delay = base
        if worker_id and track is not None:
            self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="sleep"))
        await asyncio.sleep(delay)

    async def run(self, tracks: list[Track], source_ctx: SourceContext | None = None,
                  manifest: Manifest | None = None) -> list[TrackResult]:
        self._source_ctx = source_ctx
        self._manifest = manifest
        self._consec_failures = 0
        self._total_tracks = len(tracks)
        total = self._total_tracks
        worker_pool: asyncio.Queue[int] = asyncio.Queue()
        for wid in range(1, self._settings.workers + 1):
            await worker_pool.put(wid)

        for i, track in enumerate(tracks, start=1):
            self._bus.emit(TrackQueued(track=track, position=i, total=total))

        async def _bounded(track: Track) -> TrackResult:
            wid = await worker_pool.get()
            self._bus.emit(WorkerStarted(worker_id=wid, track=track))
            retries = self._settings.download_retries
            try:
                # Global rate-limit cooldown: all workers wait if another was rate-limited
                now = time.monotonic()
                if self._rl_cooldown_until > now:
                    remaining = self._rl_cooldown_until - now
                    self._bus.emit(WorkerStage(worker_id=wid, track=track, stage=f"cooldown {remaining:.0f}s"))
                    self._bus.emit(TrackSkipped(track=track, reason=f"rate-limit cooldown: {remaining:.0f}s"))
                    await asyncio.sleep(remaining)
                    self._consec_failures = 0
                    self._rl_cooldown_until = 0

                for attempt in range(1, retries + 2):
                    first_attempt = attempt == 1

                    # Circuit breaker: pause all workers after threshold failures
                    if self._consec_failures >= self._cb_threshold:
                        self._bus.emit(TrackSkipped(track=track, reason=f"circuit breaker: {self._cb_pause}s pause"))
                        await asyncio.sleep(self._cb_pause)
                        self._consec_failures = 0

                    try:
                        result = await asyncio.wait_for(
                            self._process_one(track, worker_id=wid), timeout=self._settings.track_timeout
                        )
                    except asyncio.TimeoutError:
                        self._consec_failures += 1
                        self._bus.emit(TrackFailed(track=track, error="timed out", stage="pipeline"))
                        result = TrackResult(track=track, success=False, error="timed out")

                    if result.success:
                        self._consec_failures = 0
                        await self._yt_sleep(worker_id=wid, track=track)
                        return result

                    # Track failed — determine if rate-limited and apply adaptive backoff
                    self._consec_failures += 1
                    rate_limited = self._is_rate_limited(result.error or "")
                    if rate_limited:
                        # Signal all workers to pause via global cooldown
                        rl_delay = self._retry_backoff(attempt, rate_limited=True)
                        self._rl_cooldown_until = time.monotonic() + rl_delay
                        self._bus.emit(WorkerStage(
                            worker_id=wid, track=track,
                            stage=f"rate-limited {rl_delay:.0f}s",
                        ))

                    if not first_attempt or rate_limited:
                        delay = self._retry_backoff(attempt, rate_limited=rate_limited)
                        stage = f"retry {attempt}/{retries}" if not rate_limited else f"rate-limited {delay:.0f}s"
                        self._bus.emit(WorkerStage(worker_id=wid, track=track, stage=stage))
                        await asyncio.sleep(delay)
                await self._yt_sleep(worker_id=wid, track=track)
                return result
            finally:
                self._bus.emit(WorkerFinished(worker_id=wid, track=track))
                await worker_pool.put(wid)

        return await asyncio.gather(*(_bounded(t) for t in tracks))

    async def _process_one(self, track: Track, worker_id: int = 0) -> TrackResult:
        if self._settings.skip_instrumental and is_instrumental(track.title):
            self._bus.emit(TrackSkipped(track=track, reason="instrumental"))
            return TrackResult(track=track, success=True, skipped_reason="instrumental")

        # Skip if manifest says already downloaded
        if self._manifest:
            for mt in self._manifest.tracks:
                if mt.id == track.id and mt.downloaded and mt.filepath:
                    stored = Path(mt.filepath)
                    if stored.exists():
                        self._bus.emit(TrackSkipped(track=track, reason="already downloaded"))
                        return TrackResult(track=track, success=True, skipped_reason="already downloaded", filepath=stored)

        if self._settings.metadata_source == "youtube" and (self._source_ctx is None or self._source_ctx.source_type != "playlist"):
            safe_title = sanitize_filename(track.title)
        else:
            width = max(2, len(str(self._total_tracks)))
            safe_title = sanitize_filename(f"{track.track_number:0{width}d} - {track.title}")

        lyrics_source: str | None = None
        self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="searching"))
        try:
            video_id = await self._audio.search(track)
        except Exception as exc:
            self._bus.emit(TrackFailed(track=track, error=str(exc), stage="audio"))
            return TrackResult(track=track, success=False, error=str(exc))

        if not video_id:
            self._bus.emit(TrackFailed(track=track, error="no match found", stage="audio"))
            return TrackResult(track=track, success=False, error="no match found")

        # Check video cache first (collab dedup)
        if not self._settings.dry_run:
            cached = find_live_path(video_id, self._settings.output_dir)
            if cached:
                actual_ext = cached.suffix.lstrip(".")
                final_dest = self._build_dest(safe_title, track, ext=actual_ext)
                if cached != final_dest:
                    final_dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(cached), str(final_dest))
                    register(video_id, final_dest, self._settings.output_dir)
                    self._bus.emit(AudioDownloaded(track=track, filepath=final_dest))

                    artist_dir = final_dest.parent.parent.parent
                    if not self._settings.audio_only:
                        self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="tagging"))
                        try:
                            await self._tagger.tag_file(final_dest, track)
                        except Exception as exc:
                            self._bus.emit(TrackFailed(track=track, error=str(exc), stage="tagging"))
                            final_dest.unlink(missing_ok=True)
                            return TrackResult(track=track, success=False, error=str(exc))
                        self._bus.emit(TagsApplied(track=track, filepath=final_dest))

                        if self._settings.enable_lyrics:
                            lyrics = track.lyrics
                            if lyrics is None and self._lyrics_chain is not None:
                                self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="lyrics"))
                                try:
                                    lyrics = await self._lyrics_chain.fetch(track)
                                except Exception as exc:
                                    self._bus.emit(TrackFailed(track=track, error=str(exc), stage="lyrics"))
                                    lyrics = None
                            if lyrics is not None:
                                track.lyrics = lyrics
                                if self._embedder.embed(final_dest, lyrics, mode=self._settings.lyrics_mode):
                                    lyrics_source = lyrics.source
                                self._bus.emit(LyricsFound(track=track, lyrics=lyrics))

                    _save_history_entry(artist_dir, video_id)
                    self._update_manifest(track, True, str(final_dest))
                    self._bus.emit(TrackCompleted(track=track, filepath=final_dest, had_lyrics=lyrics_source))
                    return TrackResult(track=track, success=True, filepath=final_dest, had_lyrics=lyrics_source)

        tmp_dest = Path("tmp") / f"{track.id}"

        if self._settings.dry_run:
            final_dest = self._build_dest(safe_title, track)
            self._bus.emit(TrackCompleted(track=track, filepath=final_dest))
            return TrackResult(track=track, success=True, filepath=final_dest)

        self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="downloading"))
        try:
            filepath = await self._audio.download(track, video_id, tmp_dest)
        except Exception as exc:
            self._bus.emit(TrackFailed(track=track, error=str(exc), stage="audio"))
            return TrackResult(track=track, success=False, error=str(exc))

        self._bus.emit(AudioDownloaded(track=track, filepath=filepath))
        register(video_id, filepath, self._settings.output_dir)

        # Remux .webm (Opus in WebM) → .ogg (lossless container swap)
        if filepath.suffix.lower() == ".webm" and self._settings.remux_to_ogg:
            self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="remuxing"))
            ogg_path = filepath.with_suffix(".ogg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(filepath), "-c:a", "copy", str(ogg_path)],
                    capture_output=True, timeout=60, check=True,
                )
                filepath.unlink()
                filepath = ogg_path
            except Exception:
                pass  # keep .webm if remux fails

        # Build final path with the actual file extension
        actual_ext = filepath.suffix.lstrip(".")
        final_dest = self._build_dest(safe_title, track, ext=actual_ext)
        artist_dir = final_dest.parent.parent.parent
        if self._settings.metadata_source == "youtube":
            artist_dir = self._settings.output_dir / "youtube-singles"

        if self._settings.audio_only:
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            _safe_replace(filepath, final_dest)
            _save_history_entry(artist_dir, video_id)
            self._update_manifest(track, True, str(final_dest))
            self._bus.emit(TrackCompleted(track=track, filepath=final_dest))
            return TrackResult(track=track, success=True, filepath=final_dest)

        self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="tagging"))
        try:
            await self._tagger.tag_file(filepath, track)
        except Exception as exc:
            self._bus.emit(TrackFailed(track=track, error=str(exc), stage="tagging"))
            filepath.unlink(missing_ok=True)
            return TrackResult(track=track, success=False, error=str(exc))

        self._bus.emit(TagsApplied(track=track, filepath=filepath))

        if self._settings.enable_lyrics:
            lyrics = track.lyrics  # use pre-collected lyrics if available
            if lyrics is None and self._lyrics_chain is not None:
                self._bus.emit(WorkerStage(worker_id=worker_id, track=track, stage="lyrics"))
                try:
                    lyrics = await self._lyrics_chain.fetch(track)
                except Exception as exc:
                    self._bus.emit(TrackFailed(track=track, error=str(exc), stage="lyrics"))
                    lyrics = None
            if lyrics is not None:
                track.lyrics = lyrics
                if self._embedder.embed(filepath, lyrics, mode=self._settings.lyrics_mode):
                    lyrics_source = lyrics.source
                self._bus.emit(LyricsFound(track=track, lyrics=lyrics))

        final_dest.parent.mkdir(parents=True, exist_ok=True)
        _safe_replace(filepath, final_dest)
        _save_history_entry(artist_dir, video_id)

        self._update_manifest(track, True, str(final_dest))

        self._bus.emit(TrackCompleted(track=track, filepath=final_dest, had_lyrics=lyrics_source))
        return TrackResult(track=track, success=True, filepath=final_dest, had_lyrics=lyrics_source)

    def _update_manifest(self, track: Track, downloaded: bool, filepath: str | None) -> None:
        if self._manifest is None:
            return
        for mt in self._manifest.tracks:
            if mt.id == track.id:
                mt.downloaded = downloaded
                if filepath:
                    mt.filepath = filepath
                break
        if self._source_ctx is not None:
            write_manifest(self._manifest, self._settings.output_dir, self._source_ctx.source_type or "track",
                           sanitize_filename(self._source_ctx.source_name) if self._source_ctx.source_name else None)


    def _single_base(self, artist_dir: str) -> Path:
        """Return the base path for a single: artist folder if it exists, else top-level singles/."""
        artist_path = self._settings.output_dir / "artists" / artist_dir
        if artist_path.exists():
            return artist_path / "singles"
        return self._settings.output_dir / "singles"

    def _single_path(self, artist_dir: str, safe_title: str, ext: str) -> Path:
        return self._single_base(artist_dir) / f"{safe_title}{ext}"


    @staticmethod
    def _single_name(track: Track, safe_title: str) -> str:
        """Build filename for a single: release year prefix instead of track number.

        Singles on Deezer all have track_number=1, so NN -  prefix is useless.
        Using the release year keeps them distinguishable at a glance.
        Falls back to safe_title if no release date is available.
        """
        if track.album.release_date:
            return sanitize_filename(f"{track.album.release_date.year} - {track.title}")
        return safe_title

    def _build_dest(self, safe_title: str, track: Track, ext: str | None = None) -> Path:
        """Build the final destination path based on source context and track metadata."""
        ext = f".{ext or self._settings.audio_format}"
        ctx = self._source_ctx

        # Playlist: drop into playlists/<name>/
        if ctx and ctx.source_type == "playlist":
            return (
                self._settings.output_dir
                / "playlists"
                / sanitize_filename(ctx.source_name)
                / f"{safe_title}{ext}"
            )

        # YouTube singles: separate folder, not mixed with Deezer singles
        if self._settings.metadata_source == "youtube":
            return (
                self._settings.output_dir
                / "youtube-singles"
                / f"{sanitize_filename(track.album.artist.name)} - {safe_title}{ext}"
            )

        # Determine the artist folder
        if ctx and ctx.source_type == "artist":
            artist_dir = sanitize_filename(ctx.source_name)
        else:
            artist_dir = sanitize_filename(track.album.artist.name)

        # Determine album type
        album_type = (track.album.album_type or "").lower()

        # Build album/year prefix
        album_prefix = ""
        if track.album.release_date:
            album_prefix = f"{track.album.release_date.year} - "
        album_dir = sanitize_filename(f"{album_prefix}{track.album.title}")

        if ctx and ctx.source_type == "track":
            # Single track URL — use year prefix instead of track number
            return self._single_path(artist_dir, self._single_name(track, safe_title), ext)

        if album_type == "single":
            # Singles use release year prefix instead of track number
            if ctx and ctx.source_type == "artist":
                base = self._settings.output_dir / "artists" / artist_dir / "singles"
            else:
                base = self._single_base(artist_dir)
            return base / f"{self._single_name(track, safe_title)}{ext}"

        if album_type == "ep":
            subdir = "eps"
        else:
            subdir = "albums"

        return (
            self._settings.output_dir
            / "artists"
            / artist_dir
            / subdir
            / album_dir
            / f"{safe_title}{ext}"
        )





def _safe_replace(src: Path, dst: Path) -> None:
    """Move a file, falling back to copy+delete across filesystem boundaries."""
    try:
        src.replace(dst)
    except OSError:
        shutil.move(str(src), str(dst))
