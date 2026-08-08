"""Events emitted onto the pipeline's event bus as each track moves through
the pipeline. Observers (rich progress bars, loggers, JSON writers) subscribe
to these without the pipeline knowing anything about how they're rendered."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sideb.models.track import Lyrics, Track


@dataclass(slots=True)
class PipelineEvent:
    pass


@dataclass(slots=True)
class TrackQueued(PipelineEvent):
    track: Track
    position: int
    total: int


@dataclass(slots=True)
class MetadataFetched(PipelineEvent):
    track: Track


@dataclass(slots=True)
class AudioSearchStarted(PipelineEvent):
    track: Track


@dataclass(slots=True)
class AudioDownloadProgress(PipelineEvent):
    track: Track
    progress: float  # 0.0 - 1.0
    speed: str = ""


@dataclass(slots=True)
class AudioDownloaded(PipelineEvent):
    track: Track
    filepath: Path


@dataclass(slots=True)
class TagsApplied(PipelineEvent):
    track: Track
    filepath: Path


@dataclass(slots=True)
class LyricsFound(PipelineEvent):
    track: Track
    lyrics: Lyrics


@dataclass(slots=True)
class TrackSkipped(PipelineEvent):
    track: Track
    reason: str  # e.g. "instrumental"


@dataclass(slots=True)
class TrackCompleted(PipelineEvent):
    track: Track
    filepath: Path
    had_lyrics: str | None = None  # lyrics source: "deezer", "lrclib", or None


@dataclass(slots=True)
class TrackFailed(PipelineEvent):
    track: Track
    error: str
    stage: str  # "metadata" | "audio" | "tagging" | "lyrics"


@dataclass(slots=True)
class WorkerStarted(PipelineEvent):
    worker_id: int
    track: Track


@dataclass(slots=True)
class WorkerFinished(PipelineEvent):
    worker_id: int
    track: Track


@dataclass(slots=True)
class WorkerStage(PipelineEvent):
    worker_id: int
    track: Track
    stage: str  # "searching" | "downloading" | "tagging" | "lyrics" | "remuxing" | "sleep" | "cooldown" | "rate-limited Ns" | "retry N/M"
