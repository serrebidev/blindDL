# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Download queue: worker threads pulling items through the backends.

The queue itself is GUI-agnostic; it calls `notify(item)` whenever an item
changes state. The GUI passes a notify that marshals to the main thread
(wx.CallAfter). There is no cap on queue length; concurrency is bounded
only by the user's max_concurrent setting.
"""

import base64
import enum
import json
import os
import threading
import time
import uuid

from . import (
    adult_backend,
    applemusic_backend,
    archive_backend,
    audiobook_backend,
    book_backend,
    deezer_backend,
    musicdl_backend,
    sideb_backend,
    soulseek_backend,
    torrent_backend,
    torrent_engine,
    ytdlp_backend,
)
from .config import app_data_dir

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
    def __init__(self, title, kind, payload, audio_only=True,
                 audio_format="mp3", video_format="mp4"):
        self.id = uuid.uuid4().hex
        self.title = title
        # "ytdlp", "musicdl", "sideb", "adult", "book", "audiobook",
        # "archive", "soulseek" or "torrent"
        self.kind = kind
        self.payload = payload  # URL string, musicdl SongInfo, or result dict
        self.audio_only = audio_only
        self.audio_format = audio_format
        self.video_format = video_format
        self.status = STATUS_QUEUED
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.error = ""
        # Finished in-app torrents continue uploading after their download
        # row says Done.  Persisting this flag lets the engine reattach them
        # from libtorrent's resume data after a restart.
        self.seeding = False
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
    STATE_VERSION = 1

    def __init__(self, config, notify, state_path=None, start_workers=True):
        self.config = config
        self.notify = notify
        self.items = []
        self._cond = threading.Condition()
        self._workers = []
        self._torrent_workers = []
        self._wanted_workers = 0
        self._wanted_torrent_workers = 0
        self._shutting_down = False
        self._save_lock = threading.Lock()
        self._state_path = (
            os.path.join(app_data_dir(), "downloads.json")
            if state_path is None
            else os.fspath(state_path) if state_path else None
        )
        self._load_state()
        if start_workers:
            self.start()

    def start(self):
        with self._cond:
            self._ensure_workers()
            self._cond.notify_all()

    # -- worker management ------------------------------------------------

    def _ensure_workers(self):
        """Grow both worker pools to match the current settings.

        Torrents get a pool of their own because they are slow in a way
        nothing else here is: one can hold a thread for hours while it seeds
        out of a thin swarm. Sharing the ordinary pool would let a handful of
        them starve every other download in the queue.
        """
        wanted = max(1, int(self.config["max_concurrent"]))
        self._wanted_workers = wanted
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        while len(self._workers) < wanted:
            index = len(self._workers)
            t = threading.Thread(target=self._worker, daemon=True,
                                 args=(False, index),
                                 name=f"blinddl-worker-{index}")
            t.start()
            self._workers.append(t)

        # With the engine off a torrent is an instant hand-off, so two
        # threads are plenty; with it on, one per simultaneous torrent.
        torrents = 2
        if self.config.get("torrent_engine"):
            torrents = max(2, int(self.config.get("torrent_max_active", 3)))
        self._wanted_torrent_workers = torrents
        self._torrent_workers = [
            worker for worker in self._torrent_workers if worker.is_alive()
        ]
        while len(self._torrent_workers) < torrents:
            index = len(self._torrent_workers)
            t = threading.Thread(target=self._worker, daemon=True,
                                 args=(True, index),
                                 name=f"blinddl-torrent-{index}")
            t.start()
            self._torrent_workers.append(t)

    def set_concurrency(self, n):
        self.config["max_concurrent"] = n
        with self._cond:
            self._ensure_workers()
            # Workers above a reduced limit wake, see that they are no longer
            # wanted, and exit instead of lingering for the process lifetime.
            self._cond.notify_all()

    # -- public API -------------------------------------------------------

    def add(self, item):
        with self._cond:
            self.items.append(item)
            # Two pools wait on this condition and only one of them can take
            # a given item, so every waiter has to look.
            self._cond.notify_all()
        self._save_state()
        self._notify(item)

    def add_ytdlp(self, url, title, audio_only=None):
        if audio_only is None:
            audio_only = self.config["audio_only"]
        item = DownloadItem(title=title, kind="ytdlp", payload=url,
                            audio_only=audio_only,
                            audio_format=self.config["audio_format"],
                            video_format=self.config["video_format"])
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

    def add_applemusic(self, url, title):
        item = DownloadItem(title=title, kind="applemusic", payload=url)
        self.add(item)
        return item

    def add_adult(self, payload, title):
        item = DownloadItem(title=title, kind="adult", payload=payload,
                            audio_only=False,
                            video_format=self.config["video_format"])
        self.add(item)
        return item

    def add_book(self, payload, title):
        item = DownloadItem(title=title, kind="book", payload=payload,
                            audio_only=False)
        self.add(item)
        return item

    def add_audiobook(self, payload, title):
        item = DownloadItem(title=title, kind="audiobook", payload=payload)
        self.add(item)
        return item

    def add_torrent(self, payload, title):
        item = DownloadItem(title=title, kind="torrent", payload=payload,
                            audio_only=False)
        self.add(item)
        return item

    def add_archive(self, payload, title):
        item = DownloadItem(title=title, kind="archive", payload=payload,
                            audio_only=not payload.get("video"))
        self.add(item)
        return item

    def add_soulseek(self, payload, title):
        item = DownloadItem(title=title, kind="soulseek", payload=payload)
        self.add(item)
        return item

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is None:
            return
        item.cancel_event.set()
        if item.status == STATUS_QUEUED:
            item.status = STATUS_CANCELLED
            self._save_state()
            self._notify(item)

    def remove_finished(self):
        with self._cond:
            # A completed torrent can still be an active upload. Keep its row
            # (and therefore its restart manifest) until seeding is stopped.
            self.items = [
                item for item in self.items
                if item.status not in FINISHED_STATUSES or item.seeding
            ]
        self._save_state()

    def mark_torrent_stopped(self, key, title=""):
        """Remember that a completed torrent must not seed after restart."""
        key = str(key or "").casefold()
        title = str(title or "").casefold()
        changed = False
        with self._cond:
            for item in self.items:
                if item.kind != "torrent" or not item.seeding:
                    continue
                payload = item.payload if isinstance(item.payload, dict) else {}
                infohash = str(payload.get("infohash") or "").casefold()
                if infohash == key or (
                    not infohash and title and item.title.casefold() == title
                ):
                    item.seeding = False
                    changed = True
        if changed:
            self._save_state()
        return changed

    def shutdown(self):
        """Atomically save queue state before transfer engines are stopped."""
        active = {
            str(key).casefold(): str(title).casefold()
            for key, title, _ratio, _rate in torrent_engine.seeding()
        }
        with self._cond:
            for item in self.items:
                if item.kind != "torrent" or item.status != STATUS_DONE:
                    continue
                payload = item.payload if isinstance(item.payload, dict) else {}
                infohash = str(payload.get("infohash") or "").casefold()
                item.seeding = (
                    infohash in active
                    if infohash
                    else item.title.casefold() in active.values()
                )
            self._save_state(force=True)
            self._shutting_down = True

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

    # -- durable state ----------------------------------------------------

    @staticmethod
    def _json_safe(value):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, bytes):
            return {"__blinddl_bytes__": base64.b64encode(value).decode("ascii")}
        if isinstance(value, bytearray):
            return {
                "__blinddl_bytes__": base64.b64encode(bytes(value)).decode("ascii")
            }
        if isinstance(value, enum.Enum):
            return DownloadQueue._json_safe(value.value)
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, dict):
            return {str(key): DownloadQueue._json_safe(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [DownloadQueue._json_safe(item) for item in value]
        raise TypeError(f"unsupported queue value: {type(value).__name__}")

    @staticmethod
    def _json_restore(value):
        if isinstance(value, list):
            return [DownloadQueue._json_restore(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"__blinddl_bytes__"}:
                return base64.b64decode(value["__blinddl_bytes__"], validate=True)
            return {key: DownloadQueue._json_restore(item)
                    for key, item in value.items()}
        return value

    @classmethod
    def _payload_record(cls, item):
        payload = item.payload
        payload_type = "json"
        if item.kind == "musicdl" and hasattr(payload, "todict"):
            payload_type = "musicdl-song"
            payload = payload.todict()
        return payload_type, cls._json_safe(payload)

    @classmethod
    def _item_record(cls, item):
        record = {
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "audio_only": item.audio_only,
            "audio_format": item.audio_format,
            "video_format": item.video_format,
            "status": item.status,
            "percent": item.percent,
            "error": item.error,
            "seeding": item.seeding,
        }
        try:
            payload_type, payload = cls._payload_record(item)
            record.update({"payload_type": payload_type, "payload": payload})
        except (TypeError, ValueError) as exc:
            record.update({
                "payload_type": "unavailable",
                "payload": None,
                "payload_error": str(exc),
            })
        return record

    @classmethod
    def _item_from_record(cls, record, resume_seeds):
        if not isinstance(record, dict):
            raise ValueError("queue item is not an object")
        payload_type = record.get("payload_type", "json")
        payload = cls._json_restore(record.get("payload"))
        if payload_type == "musicdl-song":
            from musicdl.modules.utils.data import SongInfo

            payload = SongInfo.fromdict(payload)
        item = DownloadItem(
            str(record.get("title") or "Recovered download"),
            str(record.get("kind") or "unknown"),
            payload,
            audio_only=bool(record.get("audio_only", True)),
            audio_format=str(record.get("audio_format") or "mp3"),
            video_format=str(record.get("video_format") or "mp4"),
        )
        item.id = str(record.get("id") or uuid.uuid4().hex)
        item.percent = max(0.0, min(100.0, float(record.get("percent") or 0)))
        item.error = str(record.get("error") or "")
        item.seeding = bool(record.get("seeding", False))
        status = str(record.get("status") or STATUS_QUEUED)
        if payload_type == "unavailable":
            item.status = STATUS_ERROR
            item.error = "Could not restore this download: " + str(
                record.get("payload_error") or "unsupported saved data"
            )
            item.seeding = False
        elif status in ACTIVE_STATUSES:
            item.status = STATUS_QUEUED
        elif status == STATUS_DONE and item.kind == "torrent" and item.seeding \
                and resume_seeds:
            item.status = STATUS_QUEUED
            item.percent = 100.0
        elif status in FINISHED_STATUSES:
            item.status = status
        else:
            item.status = STATUS_ERROR
            item.error = f"Could not restore unknown download status: {status}"
            item.seeding = False
        return item

    def _load_state(self):
        if not self._state_path:
            return
        try:
            with open(self._state_path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        records = document.get("items", []) if isinstance(document, dict) else []
        resume_seeds = bool(
            self.config.get("torrent_engine") and torrent_engine.available()
        )
        for record in records:
            try:
                self.items.append(self._item_from_record(record, resume_seeds))
            except (ImportError, OSError, TypeError, ValueError) as exc:
                item = DownloadItem("Recovered download", "unknown", None)
                item.status = STATUS_ERROR
                item.error = f"Could not restore saved download: {exc}"
                self.items.append(item)

    def _save_state(self, force=False):
        if not self._state_path or (self._shutting_down and not force):
            return
        with self._cond:
            document = {
                "version": self.STATE_VERSION,
                "items": [self._item_record(item) for item in self.items],
            }
        parent = os.path.dirname(os.path.abspath(self._state_path))
        temporary = self._state_path + ".tmp"
        with self._save_lock:
            try:
                os.makedirs(parent, exist_ok=True)
                with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(document, handle, indent=2, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, self._state_path)
            except OSError:
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    # -- internals --------------------------------------------------------

    def _notify(self, item, throttle=False):
        if throttle:
            now = time.monotonic()
            if now - item._last_notify < 0.5 and item.status == STATUS_DOWNLOADING:
                return
            item._last_notify = now
        if self.notify is not None:
            self.notify(item)

    def _worker(self, torrents_only=False, worker_index=0):
        while True:
            with self._cond:
                item = None
                while item is None:
                    wanted = (
                        self._wanted_torrent_workers
                        if torrents_only
                        else self._wanted_workers
                    )
                    if worker_index >= wanted:
                        return
                    for candidate in self.items:
                        if candidate.status != STATUS_QUEUED:
                            continue
                        if (candidate.kind == "torrent") != torrents_only:
                            continue
                        item = candidate
                        break
                    if item is None:
                        self._cond.wait()
                item.status = STATUS_DOWNLOADING
            self._save_state()
            self._notify(item)
            try:
                if item.kind == "ytdlp":
                    self._run_ytdlp(item)
                elif item.kind == "sideb":
                    self._run_sideb(item)
                elif item.kind == "adult":
                    self._run_adult(item)
                elif item.kind == "book":
                    self._run_book(item)
                elif item.kind == "audiobook":
                    self._run_audiobook(item)
                elif item.kind == "archive":
                    self._run_archive(item)
                elif item.kind == "soulseek":
                    self._run_soulseek(item)
                elif item.kind == "torrent":
                    self._run_torrent(item)
                elif item.kind == "applemusic":
                    self._run_applemusic(item)
                else:
                    self._run_musicdl(item)
                item.percent = 100.0
                item.status = STATUS_DONE
                # Every completed blindDL download lives in the default
                # library, which is a Soulseek share by default. Debouncing in
                # the backend turns a finished batch into one scan.
                soulseek_backend.schedule_rescan()
            except (ytdlp_backend.DownloadCancelled,
                    book_backend.BookDownloadCancelled,
                    audiobook_backend.AudiobookDownloadCancelled,
                    archive_backend.ArchiveDownloadCancelled,
                    soulseek_backend.SoulseekDownloadCancelled,
                    torrent_engine.TorrentDownloadCancelled):
                item.status = STATUS_CANCELLED
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                if item.cancel_event.is_set():
                    item.status = STATUS_CANCELLED
                else:
                    item.status = STATUS_ERROR
                    item.error = str(exc)
            item.speed = ""
            item.eta = ""
            self._save_state()
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
            video_format=item.video_format,
            progress_cb=progress,
            cancel_event=item.cancel_event,
            cookies_from_browser=self.config["cookies_from_browser"],
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

    def _run_applemusic(self, item):
        applemusic_backend.download(
            item.payload, self.config["download_dir"], self.config,
            cancel_event=item.cancel_event)

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
            cancel_event=item.cancel_event, video_format=item.video_format)

    def _run_book(self, item):
        started = time.monotonic()

        def progress(downloaded, total):
            elapsed = max(time.monotonic() - started, 0.001)
            rate = downloaded / elapsed
            item.speed = format_speed(rate)
            if total:
                item.percent = min(100.0, downloaded * 100.0 / total)
                if rate:
                    item.eta = ytdlp_backend.format_duration(
                        max(total - downloaded, 0) / rate)
            self._notify(item, throttle=True)

        book_backend.download(
            item.payload, self.config["download_dir"], self.config,
            progress_cb=progress, cancel_event=item.cancel_event)

    def _run_audiobook(self, item):
        started = time.monotonic()

        def progress(downloaded, total):
            elapsed = max(time.monotonic() - started, 0.001)
            rate = downloaded / elapsed
            item.speed = format_speed(rate)
            if total:
                item.percent = min(100.0, downloaded * 100.0 / total)
                if rate:
                    item.eta = ytdlp_backend.format_duration(
                        max(total - downloaded, 0) / rate)
            self._notify(item, throttle=True)

        audiobook_backend.download(
            item.payload, self.config["download_dir"], progress_cb=progress,
            cancel_event=item.cancel_event)

    def _run_archive(self, item):
        started = time.monotonic()

        def progress(downloaded, total):
            elapsed = max(time.monotonic() - started, 0.001)
            rate = downloaded / elapsed
            item.speed = format_speed(rate)
            if total:
                item.percent = min(100.0, downloaded * 100.0 / total)
                if rate:
                    item.eta = ytdlp_backend.format_duration(
                        max(total - downloaded, 0) / rate)
            self._notify(item, throttle=True)

        archive_backend.download(
            item.payload, self.config["download_dir"], progress_cb=progress,
            cancel_event=item.cancel_event)

    def _run_soulseek(self, item):
        def progress(info):
            downloaded = info.get("downloaded") or 0
            total = info.get("total") or 0
            if total:
                item.percent = min(100.0, downloaded * 100.0 / total)
            speed = info.get("speed") or 0
            state = info.get("state") or ""
            queue_position = info.get("queue_position")
            if speed:
                item.speed = format_speed(speed)
            elif queue_position is not None:
                item.speed = f"Soulseek queue position {queue_position}"
            else:
                item.speed = state
            eta = info.get("eta")
            item.eta = ytdlp_backend.format_duration(eta) if eta else ""
            self._notify(item, throttle=True)

        soulseek_backend.download(
            item.payload, self.config, progress_cb=progress,
            cancel_event=item.cancel_event)

    def _run_torrent(self, item):
        if self.config.get("torrent_engine") and torrent_engine.available():
            self._run_torrent_engine(item)
            return
        # With the engine off, blindDL does not move torrent bytes; the user's
        # own client does. Handing over the magnet is the whole job, so this
        # finishes at once and the queue row records that the link went out.
        item.speed = "Opening torrent client"
        self._notify(item)
        # out_dir is where a private tracker's .torrent lands when the row
        # carries no magnet; a magnet never touches the disk.
        torrent_backend.hand_off(item.payload, self.config["download_dir"])
        item.speed = ""

    def _run_torrent_engine(self, item):
        """Download a torrent inside blindDL, with real progress on the row."""
        def progress(info):
            item.percent = info["percent"]
            state = info["state"]
            if state:
                # No byte rate to show yet -- say what the engine is doing
                # instead, so the row is never silently blank.
                item.speed = state
                item.eta = ""
            else:
                rate = format_speed(info["rate"])
                swarm = f"{info['seeds']} seeds, {info['peers']} peers"
                item.speed = f"{rate}, {swarm}" if rate else swarm
                item.eta = (ytdlp_backend.format_duration(info["eta"])
                            if info["eta"] else "")
            self._notify(item, throttle=True)

        item.speed = "Starting torrent"
        self._notify(item)
        torrent_engine.download(
            item.payload, self.config["download_dir"], self.config,
            progress_cb=progress, cancel_event=item.cancel_event)
        item.seeding = True

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
