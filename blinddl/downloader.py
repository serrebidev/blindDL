# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Download queue: worker threads pulling items through the backends.

The queue itself is GUI-agnostic; it calls `notify(item)` whenever an item
changes state. The GUI passes a notify that marshals to the main thread
(wx.CallAfter). There is no cap on queue length; concurrency is bounded
only by the user's max_concurrent setting.
"""

import threading
import time
import uuid

from . import (
    adult_backend,
    deezer_backend,
    musicdl_backend,
    sideb_backend,
    ytdlp_backend,
)

STATUS_QUEUED = "Queued"
STATUS_DOWNLOADING = "Downloading"
STATUS_DONE = "Done"
STATUS_ERROR = "Error"
STATUS_CANCELLED = "Cancelled"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_DOWNLOADING)
FINISHED_STATUSES = (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)


def format_speed(bytes_per_second):
    if not bytes_per_second:
        return ""
    value = float(bytes_per_second)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


class DownloadItem:
    def __init__(self, title, kind, payload, audio_only=True, audio_format="mp3"):
        self.id = uuid.uuid4().hex
        self.title = title
        self.kind = kind  # "ytdlp", "musicdl", "sideb" or "adult"
        self.payload = payload  # URL string, or musicdl SongInfo
        self.audio_only = audio_only
        self.audio_format = audio_format
        self.status = STATUS_QUEUED
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.cancel_event = threading.Event()
        self._last_notify = 0.0

    def update_from_ytdlp(self, d):
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                self.percent = min(100.0, downloaded * 100.0 / total)
            self.speed = format_speed(d.get("speed"))
            eta = d.get("eta")
            self.eta = ytdlp_backend.format_duration(eta) if eta else ""


class DownloadQueue:
    def __init__(self, config, notify):
        self.config = config
        self.notify = notify
        self.items = []
        self._cond = threading.Condition()
        self._workers = []
        self._ensure_workers()

    # -- worker management ------------------------------------------------

    def _ensure_workers(self):
        wanted = max(1, int(self.config["max_concurrent"]))
        while len(self._workers) < wanted:
            t = threading.Thread(target=self._worker, daemon=True,
                                 name=f"blinddl-worker-{len(self._workers)}")
            t.start()
            self._workers.append(t)

    def set_concurrency(self, n):
        self.config["max_concurrent"] = n
        self._ensure_workers()

    # -- public API -------------------------------------------------------

    def add(self, item):
        with self._cond:
            self.items.append(item)
            self._cond.notify()
        self._notify(item)

    def add_ytdlp(self, url, title, audio_only=None):
        if audio_only is None:
            audio_only = self.config["audio_only"]
        item = DownloadItem(title=title, kind="ytdlp", payload=url,
                            audio_only=audio_only,
                            audio_format=self.config["audio_format"])
        self.add(item)
        return item

    def add_musicdl(self, song_info, title):
        item = DownloadItem(title=title, kind="musicdl", payload=song_info)
        self.add(item)
        return item

    def add_sideb(self, url, title):
        item = DownloadItem(title=title, kind="sideb", payload=url)
        self.add(item)
        return item

    def add_adult(self, payload, title):
        item = DownloadItem(title=title, kind="adult", payload=payload,
                            audio_only=False)
        self.add(item)
        return item

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is None:
            return
        item.cancel_event.set()
        if item.status == STATUS_QUEUED:
            item.status = STATUS_CANCELLED
            self._notify(item)

    def remove_finished(self):
        with self._cond:
            self.items = [i for i in self.items if i.status not in FINISHED_STATUSES]

    def counts(self):
        with self._cond:
            snapshot = list(self.items)
        active = sum(1 for i in snapshot if i.status == STATUS_DOWNLOADING)
        queued = sum(1 for i in snapshot if i.status == STATUS_QUEUED)
        done = sum(1 for i in snapshot if i.status == STATUS_DONE)
        failed = sum(1 for i in snapshot if i.status in (STATUS_ERROR, STATUS_CANCELLED))
        return active, queued, done, failed

    def _find(self, item_id):
        with self._cond:
            for item in self.items:
                if item.id == item_id:
                    return item
        return None

    # -- internals --------------------------------------------------------

    def _notify(self, item, throttle=False):
        if throttle:
            now = time.monotonic()
            if now - item._last_notify < 0.5 and item.status == STATUS_DOWNLOADING:
                return
            item._last_notify = now
        if self.notify is not None:
            self.notify(item)

    def _worker(self):
        while True:
            with self._cond:
                item = None
                while item is None:
                    for candidate in self.items:
                        if candidate.status == STATUS_QUEUED:
                            item = candidate
                            break
                    if item is None:
                        self._cond.wait()
                item.status = STATUS_DOWNLOADING
            self._notify(item)
            try:
                if item.kind == "ytdlp":
                    self._run_ytdlp(item)
                elif item.kind == "sideb":
                    self._run_sideb(item)
                elif item.kind == "adult":
                    self._run_adult(item)
                else:
                    self._run_musicdl(item)
                item.percent = 100.0
                item.status = STATUS_DONE
            except ytdlp_backend.DownloadCancelled:
                item.status = STATUS_CANCELLED
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                if item.cancel_event.is_set():
                    item.status = STATUS_CANCELLED
                else:
                    item.status = STATUS_ERROR
                    item.error = str(exc)
            item.speed = ""
            item.eta = ""
            self._notify(item)

    def _run_ytdlp(self, item):
        def progress(d):
            item.update_from_ytdlp(d)
            self._notify(item, throttle=True)

        ytdlp_backend.download(
            item.payload,
            self.config["download_dir"],
            audio_only=item.audio_only,
            audio_format=item.audio_format,
            progress_cb=progress,
            cancel_event=item.cancel_event,
        )

    def _run_musicdl(self, item):
        # musicdl exposes no progress callbacks; the item stays in the
        # indeterminate "Downloading" state until it returns.
        musicdl_backend.download(item.payload, self.config["download_dir"])

    def _run_sideb(self, item):
        # An ARL unlocks Deezer's original MP3 320/FLAC stream. Only fall back
        # to Side B's YouTube Music audio when the account cannot provide the
        # requested quality; authentication and other native errors must stay
        # visible so a broken ARL does not fail silently.
        if (self.config["deezer_arl"] or "").strip():
            try:
                self._run_deezer(item)
                return
            except deezer_backend.DeezerQualityError:
                pass

        # Imported here so a missing sideb install only fails Side B jobs.
        from sideb.models.events import TrackCompleted, WorkerStage

        def on_event(event):
            # Fires on sideb's event loop, inside this worker thread.
            if isinstance(event, WorkerStage):
                # No per-track percent exists; show the pipeline stage
                # (searching/downloading/tagging/lyrics) where speed goes.
                item.speed = event.stage
                self._notify(item, throttle=True)
            elif isinstance(event, TrackCompleted):
                item.percent = 100.0
                self._notify(item)

        sideb_backend.download(
            item.payload, self.config["download_dir"], self.config,
            event_cb=on_event)

    def _run_adult(self, item):
        started = time.monotonic()

        def progress(current, total=None):
            if isinstance(current, dict):
                item.update_from_ytdlp(current)
            else:
                downloaded = current or 0
                total_bytes = total or 0
                elapsed = max(time.monotonic() - started, 0.001)
                rate = downloaded / elapsed
                item.speed = format_speed(rate)
                if total_bytes:
                    item.percent = min(100.0, downloaded * 100.0 / total_bytes)
                    if rate:
                        item.eta = ytdlp_backend.format_duration(
                            max(total_bytes - downloaded, 0) / rate)
            self._notify(item, throttle=True)

        adult_backend.download(
            item.payload, self.config["download_dir"], progress_cb=progress,
            cancel_event=item.cancel_event)

    def _run_deezer(self, item):
        started = time.monotonic()

        def progress(downloaded, total):
            elapsed = max(time.monotonic() - started, 0.001)
            rate = downloaded / elapsed
            item.speed = format_speed(rate)
            if total:
                item.percent = min(100.0, downloaded * 100.0 / total)
                remaining = max(total - downloaded, 0)
                item.eta = ytdlp_backend.format_duration(remaining / rate)
            self._notify(item, throttle=True)

        deezer_backend.download(
            item.payload, self.config["download_dir"], self.config,
            progress_cb=progress, cancel_event=item.cancel_event)
