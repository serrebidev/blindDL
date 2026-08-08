"""Renders pipeline events as a live rich progress display.

Layout is unchanged from the original design (see docs/rich-ui-guide.md):
a `Live` + `Group` of plain `Text` renderables, never `rich.progress.Progress`
— that's what keeps the worker lines and the progress line from jittering
against each other. This module only changes how those lines look, not the
event-driven architecture around them.
"""

from __future__ import annotations

import re
import time

from rich.console import Group
from rich.live import Live
from rich.text import Text

from sideb.app.events_bus import EventBus
from sideb.cli.theme import BAR_EMPTY, BAR_FULL, RESULT_STYLE, RESULT_SYMBOLS, STAGE_SYMBOLS, WORKER_COLORS
from sideb.models.events import (
    PipelineEvent,
    TrackCompleted,
    TrackFailed,
    TrackQueued,
    TrackSkipped,
    WorkerFinished,
    WorkerStage,
    WorkerStarted,
)
from sideb.utils.console import console

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_BAR_WIDTH = 24
_STAGE_COL = max(len(s) for s in STAGE_SYMBOLS) + 2  # symbol + space + word
_TITLE_COL = 42


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _elapsed_str(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _bar(done: int, total: int, width: int = _BAR_WIDTH) -> str:
    if total <= 0:
        return BAR_EMPTY * width
    filled = round(width * min(done, total) / total)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


class ProgressRenderer:
    """Subscribes to the EventBus and drives a live rich progress display."""

    def __init__(self, event_bus: EventBus, *, quiet: bool = False) -> None:
        self._quiet = quiet
        self._worker_stages: dict[int, tuple[str, str]] = {}
        self._completed: list[str] = []
        self._start_time = time.monotonic()
        self._completed_count = 0
        self._total = 0
        self._live: Live | None = None
        self._event_bus = event_bus
        self._listener = self._on_event
        event_bus.subscribe(self._listener)

    def __enter__(self) -> "ProgressRenderer":
        self._live = Live(
            self._build_renderable(),
            console=console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._live:
            self._live.update(self._build_renderable())
            self._live.__exit__(exc_type, exc_value, traceback)
        self._event_bus.unsubscribe(self._listener)

    def _build_renderable(self) -> Group:
        parts: list[Text] = []

        if self._worker_stages:
            for wid in sorted(self._worker_stages):
                title, stage = self._worker_stages[wid]
                color = WORKER_COLORS[(wid - 1) % len(WORKER_COLORS)]
                sym = next((v for k, v in STAGE_SYMBOLS.items() if stage.startswith(k)), "?")
                stage_label = f"{sym} {stage}".ljust(_STAGE_COL)
                shown_title = title if len(title) <= _TITLE_COL else title[: _TITLE_COL - 1] + "\u2026"
                parts.append(
                    Text.assemble(
                        ("  ", ""),
                        (f"W{wid}".ljust(3), f"bold {color}"),
                        (" ", ""),
                        (stage_label, color),
                        ("  ", ""),
                        (shown_title, "" if stage != "downloading" else "bold"),
                    )
                )
            parts.append(Text(""))

        elapsed = time.monotonic() - self._start_time
        pct = int(100 * self._completed_count / self._total) if self._total else 0
        bar = _bar(self._completed_count, self._total)
        bar_color = "green" if self._total and self._completed_count >= self._total else "cyan"

        progress_line = Text.assemble(
            ("  ", ""),
            (bar, bar_color),
            (f"  {pct:3d}%", "bold" if pct else "dim"),
            ("   ", ""),
            (f"{self._completed_count}/{self._total}", "bold"),
            (" tracks   ", "dim"),
            (_elapsed_str(elapsed), "dim"),
        )
        parts.append(progress_line)
        parts.append(Text(""))

        return Group(*parts)

    def refresh(self) -> None:
        if self._live:
            self._live.update(self._build_renderable())

    def _on_event(self, event: PipelineEvent) -> None:
        if isinstance(event, TrackQueued):
            self._total = event.total
        elif isinstance(event, WorkerStarted):
            self._worker_stages[event.worker_id] = (event.track.title, "searching")
            self.refresh()
        elif isinstance(event, WorkerStage):
            self._worker_stages[event.worker_id] = (event.track.title, event.stage)
            self.refresh()
        elif isinstance(event, WorkerFinished):
            self._worker_stages.pop(event.worker_id, None)
            self.refresh()
        elif isinstance(event, (TrackCompleted, TrackSkipped, TrackFailed)):
            self._completed_count += 1
            self.refresh()
            if not self._quiet:
                self._log_result(event)

    def _log_result(self, event: PipelineEvent) -> None:
        if isinstance(event, TrackCompleted):
            line = Text(f"  {RESULT_SYMBOLS['ok']} {event.track.title}")
            line.stylize(RESULT_STYLE["ok"])
            if event.had_lyrics:
                colors = {"deezer": "#a238ff", "lrclib": "cyan"}
                c = colors.get(event.had_lyrics, "dim")
                ly = event.track.lyrics
                if ly:
                    parts = []
                    if ly.word_synced:
                        parts.append("word")
                    if ly.synced:
                        parts.append("synced")
                    if not parts and ly.plain:
                        parts.append("plain")
                    suffix = "+".join(parts) if parts else event.had_lyrics
                    line.append(f"  (+{suffix})", style=c)
                else:
                    line.append(f"  (+{event.had_lyrics})", style=c)
            console.print(line)
        elif isinstance(event, TrackSkipped):
            style = RESULT_STYLE["skip"]
            console.print(f"  [{style}]{RESULT_SYMBOLS['skip']} {event.track.title} \u2014 {event.reason}[/{style}]")
        elif isinstance(event, TrackFailed):
            style = RESULT_STYLE["fail"]
            console.print(f"  [{style}]{RESULT_SYMBOLS['fail']} {event.track.title} \u2014 {_strip_ansi(event.error)}[/{style}]")
