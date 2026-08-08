"""Application bootstrap. Wires providers and services together based on
Settings, resolves the input URL/query into tracks, then runs the pipeline.
See ARCHITECTURE.md "Application Bootstrap Flow" for the sequence diagram
this mirrors.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from sideb.app.events_bus import EventBus
from sideb.app.pipeline import Pipeline, SourceContext, TrackResult
from sideb.utils.path import sanitize_filename
from sideb.config.settings import Settings
from sideb.models.events import (
    TrackCompleted,
    TrackQueued,
    WorkerFinished,
    WorkerStage,
    WorkerStarted,
)
from sideb.models.track import Track
from sideb.providers.audio.youtube import YouTubeAudio
from sideb.providers.lyrics.deezer import DeezerLyrics
from sideb.providers.lyrics.lrclib import LRCLIBLyrics
from sideb.providers.metadata.deezer import DeezerMetadata
from sideb.providers.metadata.youtube import YouTubeMusicMetadata
from sideb.services.embedder import Embedder
from sideb.services.lyrics_chain import LyricsChain
from sideb.services.manifest import (
    Manifest,
    write_manifest,
)
from sideb.services.tagger import Tagger
from sideb.services.version_filter import dedupe_by_isrc

_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/(\d+)",
    re.IGNORECASE,
)
_YT_RE = re.compile(
    r"(?:youtube\.com|music\.youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)


@dataclass
class RunSummary:
    total: int
    results: list[TrackResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[TrackResult]:
        return [r for r in self.results if r.success and not r.skipped_reason]

    @property
    def skipped(self) -> list[TrackResult]:
        return [r for r in self.results if r.skipped_reason]

    @property
    def failed(self) -> list[TrackResult]:
        return [r for r in self.results if not r.success]

    @property
    def with_lyrics(self) -> list[TrackResult]:
        return [r for r in self.succeeded if r.had_lyrics]  # truthy string


class Application:
    """Owns provider lifecycles for a single run of the pipeline."""

    def __init__(self, settings: Settings, event_bus: EventBus | None = None) -> None:
        self.settings = settings
        self.event_bus = event_bus or EventBus()

        self._deezer_provider = DeezerMetadata(user_agent=settings.user_agent, proxy=settings.proxy)
        self.metadata_provider = self._deezer_provider
        self._yt_provider = YouTubeMusicMetadata(
            deezer=self._deezer_provider,
            oauth_file=str(settings.ytmusic_oauth_file),
            duration_threshold=settings.yt_single_duration_threshold,
        )
        self.audio_provider = YouTubeAudio(
            cookies_file=settings.cookies_file,
            cookies_from_browser=settings.cookies_from_browser,
            isrc_tolerance=settings.isrc_duration_tolerance,
            title_tolerance=settings.title_duration_tolerance,
            channel_tolerance=settings.channel_duration_tolerance,
            proxy=settings.proxy,
            user_agent=settings.user_agent,
            concurrent_fragments=settings.concurrent_fragments,
        )
        self.tagger = Tagger(user_agent=settings.user_agent)
        self.embedder = Embedder()
        self.lyrics_chain = self._build_lyrics_chain() if settings.enable_lyrics else None

        self.pipeline = Pipeline(
            settings=settings,
            audio_provider=self.audio_provider,
            tagger=self.tagger,
            embedder=self.embedder,
            lyrics_chain=self.lyrics_chain,
            event_bus=self.event_bus,
        )

    def _build_lyrics_chain(self) -> LyricsChain:
        providers: list[DeezerLyrics | LRCLIBLyrics] = []
        if self.settings.deezer_arl:
            providers.append(DeezerLyrics(arl=self.settings.deezer_arl, user_agent=self.settings.user_agent, proxy=self.settings.proxy))
        providers.append(LRCLIBLyrics(user_agent=self.settings.user_agent, proxy=self.settings.proxy))
        return LyricsChain(providers)

    async def _detect_source(self, url_or_query: str) -> SourceContext:
        """Determine the source type/name/ID from the input URL or query."""
        # YouTube forced or auto-detect
        if self.settings.metadata_source == "youtube" or _YT_RE.search(url_or_query):
            from sideb.providers.metadata.youtube import (
                _PLAYLIST_ID_RE,
                _VIDEO_ID_RE,
            )
            pl_id = _PLAYLIST_ID_RE.search(url_or_query)
            if pl_id:
                try:
                    info = await self._yt_provider.get_playlist_info(pl_id.group(1))
                    name = info.get("title", "")
                except Exception:
                    name = ""
                return SourceContext(source_type="playlist", source_name=name)
            if _VIDEO_ID_RE.search(url_or_query) or "youtu.be/" in url_or_query:
                return SourceContext(source_type="track", source_name="YouTube Music")
            # Search query — detect source type later from resolved tracks
            return SourceContext(source_type=None, source_name=url_or_query)

        from sideb.utils.url_resolver import resolve_url

        resolved = await resolve_url(url_or_query)
        match = _URL_RE.search(resolved)
        if not match:
            return SourceContext(source_type=None, source_name=url_or_query)

        kind = match.group(1).lower()
        obj_id = match.group(2)

        if kind == "artist":
            try:
                info = await self._deezer_provider.get_artist_info(obj_id)
                name = info.get("name", "")
            except Exception:
                name = ""
            return SourceContext(source_type="artist", source_name=name, source_id=obj_id)
        if kind == "album":
            try:
                info = await self._deezer_provider.get_album_info(obj_id)
                name = info.get("artist", {}).get("name", "")
            except Exception:
                name = ""
            return SourceContext(source_type="album", source_name=name, source_id=obj_id)
        if kind == "playlist":
            try:
                info = await self._deezer_provider.get_playlist_info(obj_id)
                name = info.get("title", "")
            except Exception:
                name = ""
            return SourceContext(source_type="playlist", source_name=name, source_id=obj_id)
        if kind == "track":
            return SourceContext(source_type="track", source_name="", source_id=obj_id)
        return SourceContext(source_type=None)

    async def resolve(self, url_or_query: str, *, on_progress=None) -> list[Track]:
        provider: DeezerMetadata | YouTubeMusicMetadata
        if self.settings.metadata_source == "youtube":
            provider = self._yt_provider
        elif _YT_RE.search(url_or_query):
            provider = self._yt_provider
        else:
            provider = self.metadata_provider
        tracks = await provider.resolve_url(url_or_query, on_progress=on_progress)
        return dedupe_by_isrc(tracks, prefer_original_release=self.settings.prefer_original_release)

    async def resolve_artist_albums(
        self, album_ids: list[str], *, on_progress=None
    ) -> list[Track]:
        """Resolve tracks for specific albums of an artist (parallel fetch)."""
        sem = asyncio.Semaphore(5)
        tracks: list[Track] = []
        seen: set[str] = set()

        async def fetch(aid: str) -> list[Track]:
            async with sem:
                try:
                    return await self.metadata_provider.get_album(aid)
                except Exception:
                    return []

        results = await asyncio.gather(*(fetch(aid) for aid in album_ids))
        for album_tracks in results:
            for t in album_tracks:
                if t.id not in seen:
                    seen.add(t.id)
                    tracks.append(t)
        return dedupe_by_isrc(tracks, prefer_original_release=self.settings.prefer_original_release)

    async def run(self, url_or_query: str) -> RunSummary:
        source_ctx = await self._detect_source(url_or_query)
        tracks = await self.resolve(url_or_query)
        if not tracks:
            return RunSummary(total=0, results=[])
        # Fill source name from the first track if we only have an ID
        if not source_ctx.source_name and source_ctx.source_type in ("artist", "album", "track") and tracks:
            source_ctx.source_name = tracks[0].artist.name
        results = await self.pipeline.run(tracks, source_ctx=source_ctx)
        return RunSummary(total=len(tracks), results=results)

    async def collect(self, url_or_query: str, *, pre_collect: bool = False, on_progress=None) -> tuple[SourceContext, list[Track]]:
        """Phase 1: fetch Deezer metadata and return tracks without downloading.
        If pre_collect=True, also fetch lyrics for each track."""
        source_ctx = await self._detect_source(url_or_query)
        tracks = await self.resolve(url_or_query, on_progress=on_progress)
        if not source_ctx.source_name and source_ctx.source_type in ("artist", "album", "track") and tracks:
            source_ctx.source_name = tracks[0].artist.name

        if pre_collect and self.lyrics_chain is not None:
            total = len(tracks)
            sem = asyncio.Semaphore(self.settings.workers)
            worker_pool: asyncio.Queue[int] = asyncio.Queue()
            for wid in range(1, self.settings.workers + 1):
                await worker_pool.put(wid)

            for i, track in enumerate(tracks, start=1):
                self.event_bus.emit(TrackQueued(track=track, position=i, total=total))

            async def _fetch_lyrics(track: Track) -> None:
                wid = await worker_pool.get()
                self.event_bus.emit(WorkerStarted(worker_id=wid, track=track))
                async with sem:
                    self.event_bus.emit(WorkerStage(worker_id=wid, track=track, stage="lyrics"))
                    try:
                        assert self.lyrics_chain is not None
                        lyrics = await self.lyrics_chain.fetch(track)
                        if lyrics is not None:
                            track.lyrics = lyrics
                    except Exception:
                        pass
                self.event_bus.emit(WorkerFinished(worker_id=wid, track=track))
                self.event_bus.emit(TrackCompleted(track=track, filepath=Path(), had_lyrics=track.lyrics.source if track.lyrics else None))
                await worker_pool.put(wid)

            await asyncio.gather(*(_fetch_lyrics(t) for t in tracks))

        return source_ctx, tracks

    async def search_youtube_candidates(self, query: str, limit: int = 5) -> list[dict]:
        """Lightweight YouTube search for the interactive track picker."""
        return await self._yt_provider.search_candidates(query, limit=limit)

    async def resolve_youtube(self, tracks: list[Track], *, on_progress=None) -> dict[str, str | None]:
        """Phase 2: batch resolve YouTube URLs for tracks.

        Returns dict mapping track ID -> video_id (or None if not found).
        """
        results: dict[str, str | None] = {}
        total = len(tracks)
        for i, track in enumerate(tracks, start=1):
            if on_progress:
                on_progress(i, total, track)
            try:
                video_id = await self.audio_provider.search(track)
                results[track.id] = video_id
            except Exception:
                results[track.id] = None
        return results

    async def download_all(
        self,
        tracks: list[Track],
        youtube_ids: dict[str, str | None],
        source_ctx: SourceContext | None = None,
        manifest: Manifest | None = None,
    ) -> RunSummary:
        """Phase 3: download tracks that have YouTube URLs, update manifest."""
        downloadable = [t for t in tracks if youtube_ids.get(t.id)]
        if not downloadable:
            return RunSummary(total=0, results=[])
        results = await self.pipeline.run(downloadable, source_ctx=source_ctx, manifest=manifest)
        summary = RunSummary(total=len(downloadable), results=results)

        # Update manifest with download results (pipeline already does this per-track,
        # but ensure final flush for any that may have been missed)
        if manifest is not None and source_ctx is not None:
            od = self.settings.output_dir
            write_manifest(manifest, od, source_ctx.source_type or "track",
                           sanitize_filename(source_ctx.source_name) if source_ctx else None)
        return summary

    async def aclose(self) -> None:
        await self._deezer_provider.aclose()
        await self._yt_provider.aclose()
        await self.audio_provider.aclose()
        await self.tagger.aclose()
        if self.lyrics_chain is not None:
            await self.lyrics_chain.aclose()
