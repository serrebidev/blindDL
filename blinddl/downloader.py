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
from contextlib import contextmanager
import enum
import json
import os
import re
import shutil
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
    updater,
    ytdlp_backend,
)
from .config import app_data_dir

STATUS_QUEUED = "Queued"
STATUS_DOWNLOADING = "Downloading"
STATUS_PAUSED = "Paused"
STATUS_DONE = "Done"
STATUS_ERROR = "Error"
STATUS_CANCELLED = "Cancelled"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_PAUSED)
FINISHED_STATUSES = (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)

PAUSABLE_KINDS = frozenset({
    "adult", "applemusic", "archive", "audiobook", "book", "soulseek",
    "torrent", "ytdlp",
})

ADD_QUEUED = "queued"
ADD_RESUMED = "resumed"
ADD_SKIPPED = "skipped"
ADD_ALREADY_ACTIVE = "already-active"

# Soulseek peers cancel uploads and drop connections all the time. A transfer
# interrupted that way is re-queued a few times, with a growing pause between
# attempts, before the row is finally reported as failed.
SOULSEEK_RETRY_ATTEMPTS = 5
SOULSEEK_RETRY_BASE_DELAY = 2.0
SOULSEEK_RETRY_MAX_DELAY = 30.0
# A settings change re-issues the transfer against a restarted client, which
# should settle on the first try. If it never settles, giving up beats a
# tight reconnect loop.
SOULSEEK_SETTINGS_RETRY_ATTEMPTS = 3


def addition_summary(items, titles=()):
    """Describe what happened when one or more selections were added."""
    items = list(items)
    titles = list(titles)
    actions = [getattr(item, "add_action", ADD_QUEUED) for item in items]
    if not actions:
        return "Nothing was added."
    if len(actions) == 1:
        title = getattr(items[0], "title", "") if items[0] is not None else ""
        title = title or (titles[0] if titles else "download")
        return {
            ADD_QUEUED: f"Queued: {title}",
            ADD_RESUMED: f"Resumed: {title}",
            ADD_SKIPPED: f"Already downloaded; skipped: {title}",
            ADD_ALREADY_ACTIVE: f"Already queued or downloading: {title}",
        }.get(actions[0], f"Queued: {title}")

    if len(set(actions)) == 1:
        count = len(actions)
        return {
            ADD_QUEUED: f"Queued {count} downloads.",
            ADD_RESUMED: f"Resumed {count} downloads.",
            ADD_SKIPPED: f"Skipped {count} already-downloaded files.",
            ADD_ALREADY_ACTIVE: (
                f"{count} downloads were already queued or downloading."
            ),
        }.get(actions[0], f"Queued {count} downloads.")

    labels = (
        (ADD_QUEUED, "queued"),
        (ADD_RESUMED, "resumed"),
        (ADD_SKIPPED, "already downloaded and skipped"),
        (ADD_ALREADY_ACTIVE, "already active"),
    )
    parts = [f"{actions.count(action)} {label}"
             for action, label in labels if action in actions]
    return ", ".join(parts).capitalize() + "."


def format_speed(bytes_per_second):
    if not bytes_per_second:
        return ""
    value = float(bytes_per_second)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def safe_folder(name):
    """A folder name for a playlist, channel, artist or archive item.

    The title comes from the site, so it can hold anything a path cannot.
    An empty result means the download lands straight in the download
    folder, exactly as a single video always has.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120]


class DownloadItem:
    def __init__(self, title, kind, payload, audio_only=True,
                 audio_format="mp3", video_format="mp4", folder=""):
        self.id = uuid.uuid4().hex
        self.title = title
        # Subfolder of the download folder this item belongs in: the name of
        # the playlist, channel, artist or archive item it came from. Empty
        # for a download that was asked for on its own.
        self.folder = safe_folder(folder)
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
        self.result_path = ""
        # Finished in-app torrents continue uploading after their download
        # row says Done.  Persisting this flag lets the engine reattach them
        # from libtorrent's resume data after a restart.
        self.seeding = False
        self.cancel_event = threading.Event()
        # Torrent cancellation may delete partial data according to Settings;
        # pausing must always preserve it, so this intent is separate.
        self.pause_event = threading.Event()
        self.pause_requested = False
        self._last_notify = 0.0
        # Transient result of the most recent attempt to add this item.  It is
        # intentionally not persisted; callers can use it to announce whether
        # a selection was queued, resumed, or was already present.
        self.add_action = ADD_QUEUED

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
        self._batch_depth = 0
        self._batched_items = []
        self._known_statuses = {}
        # Download key -> the rows carrying it, so adding a row is a lookup
        # instead of a re-keying of everything already queued.
        self._key_index = {}
        self._status_counts = {
            STATUS_DOWNLOADING: 0,
            STATUS_QUEUED: 0,
            STATUS_PAUSED: 0,
            STATUS_DONE: 0,
            STATUS_ERROR: 0,
            STATUS_CANCELLED: 0,
        }
        self._state_path = (
            os.path.join(app_data_dir(), "downloads.json")
            if state_path is None
            else os.fspath(state_path) if state_path else None
        )
        self._load_state()
        with self._cond:
            self._rebuild_counts_locked()
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

    @staticmethod
    def _payload_value(payload, name, default=None):
        if isinstance(payload, dict):
            return payload.get(name, default)
        return getattr(payload, name, default)

    @classmethod
    def _download_key(cls, item):
        """Return the stable identity of the file represented by *item*.

        Search rows contain volatile details such as peer counts, file sizes,
        and signed URLs.  Comparing their complete payload would therefore
        miss the same file after a fresh search.  Each backend's durable
        identifier is used instead, together with output settings that can
        legitimately produce a different file -- including the folder, since
        the same track in two playlists is two files in two places.
        """
        return (str(getattr(item, "folder", "") or ""), *cls._payload_key(item))

    @classmethod
    def _payload_key(cls, item):
        payload = item.payload
        kind = item.kind

        if kind == "soulseek":
            username = str(cls._payload_value(payload, "username") or "")
            remote_path = str(
                cls._payload_value(payload, "remote_path") or ""
            ).replace("/", "\\")
            relative_path = str(
                cls._payload_value(payload, "target_relative_path") or ""
            ).replace("/", "\\")
            if username and remote_path:
                return (kind, username.casefold(), remote_path.casefold(),
                        relative_path.casefold())

        if kind == "torrent":
            infohash = str(cls._payload_value(payload, "infohash") or "").strip()
            if infohash:
                return (kind, "infohash", infohash.casefold())
            for name in ("download_url", "magnet", "id"):
                value = str(cls._payload_value(payload, name) or "").strip()
                if value:
                    return (kind, name, value)

        if kind == "archive":
            identifier = str(
                cls._payload_value(payload, "identifier") or ""
            ).strip()
            file_name = str(
                cls._payload_value(payload, "file_name") or ""
            ).strip()
            if identifier:
                return (kind, identifier, file_name,
                        bool(cls._payload_value(payload, "video", False)))

        if kind == "book":
            source = str(cls._payload_value(payload, "source") or "")
            identifier = str(
                cls._payload_value(payload, "identifier") or ""
            ).strip()
            location = identifier or str(
                cls._payload_value(payload, "download_url") or ""
            ).strip()
            if location:
                return (kind, source, location,
                        str(cls._payload_value(payload, "format") or ""))

        if kind == "audiobook":
            source = str(
                cls._payload_value(payload, "backend_source") or ""
            )
            identifier = str(
                cls._payload_value(payload, "identifier") or ""
            ).strip()
            if identifier:
                return (kind, source, identifier)
            streams = cls._payload_value(payload, "streams") or ()
            if streams:
                return (kind, source, tuple(str(stream) for stream in streams))

        if kind == "musicdl":
            source = str(cls._payload_value(payload, "source") or "")
            identifier = str(
                cls._payload_value(payload, "identifier") or ""
            ).strip()
            if identifier:
                return (kind, source, identifier)
            location = cls._payload_value(payload, "download_url")
            if isinstance(location, str) and location.strip():
                return (kind, source, location.strip())
            title = str(cls._payload_value(payload, "song_name") or "")
            singers = str(cls._payload_value(payload, "singers") or "")
            album = str(cls._payload_value(payload, "album") or "")
            if title:
                return (kind, source, title.casefold(), singers.casefold(),
                        album.casefold())

        if kind == "adult":
            location = str(cls._payload_value(payload, "url") or "").strip()
            if location:
                return (kind, location, item.video_format)

        if isinstance(payload, str):
            variant = (
                item.audio_only,
                item.audio_format if item.audio_only else item.video_format,
            ) if kind == "ytdlp" else ()
            return (kind, payload.strip(), variant)

        # Unknown and future backends still get exact-payload de-duplication
        # when their payload can be represented by the durable JSON format.
        try:
            payload_key = json.dumps(
                cls._json_safe(payload), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return (kind, "object", id(payload))
        return (kind, payload_key)

    def add(self, item):
        """Queue *item*, de-duplicating completed and resumable downloads."""
        changed = False
        with self._cond:
            key = self._download_key(item)
            # Looked up, not scanned. Re-keying every queued row for every
            # addition -- and a key can be a full JSON dump of the payload --
            # made queueing a playlist quadratic, on the thread drawing the
            # list, with the workers' own condition held throughout.
            matches = list(self._key_index.get(key, ()))
            existing = next(
                (candidate for candidate in matches
                 if candidate.status in ACTIVE_STATUSES),
                None,
            )
            if existing is None:
                existing = next(
                    (candidate for candidate in matches
                     if candidate.status == STATUS_DONE),
                    None,
                )
            if existing is None and matches:
                existing = matches[-1]

            if existing is not None:
                if existing.status == STATUS_DONE:
                    existing.add_action = ADD_SKIPPED
                    return existing
                if existing.status in ACTIVE_STATUSES:
                    existing.add_action = ADD_ALREADY_ACTIVE
                    return existing

                # A cancelled or failed row is the queue's knowledge of the
                # partial transfer. Reuse it so yt-dlp, Soulseek, libtorrent,
                # and chapter-based backends can continue their own partials.
                existing.title = item.title
                existing.payload = item.payload
                existing.folder = item.folder
                existing.audio_only = item.audio_only
                existing.audio_format = item.audio_format
                existing.video_format = item.video_format
                existing.status = STATUS_QUEUED
                existing.error = ""
                existing.speed = ""
                existing.eta = ""
                existing.seeding = False
                existing.cancel_event.clear()
                existing.pause_requested = False
                existing.add_action = ADD_RESUMED
                item = existing
                changed = True
            else:
                item.add_action = ADD_QUEUED
                self.items.append(item)
                self._key_index.setdefault(key, []).append(item)
                changed = True

            if self._batch_depth:
                self._batched_items.append(item)
                return item
            # Two pools wait on this condition and only one of them can take
            # a given item, so every waiter has to look.
            self._cond.notify_all()
        if changed:
            self._save_state()
            self._notify(item)
        return item

    @contextmanager
    def batch_additions(self):
        """Persist and wake workers once for a group of queue additions.

        GUI actions can add hundreds of files at once. Holding the queue's
        re-entrant condition across the group keeps workers from observing a
        half-built batch, while the outermost context performs just one JSON
        serialization and fsync.
        """
        items = []
        outermost = False
        self._cond.acquire()
        try:
            outermost = self._batch_depth == 0
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                if outermost:
                    items = self._batched_items
                    self._batched_items = []
                    if items:
                        self._cond.notify_all()
        finally:
            self._cond.release()
            if outermost and items:
                self._save_state()
                for item in items:
                    self._notify(item)

    def add_ytdlp(self, url, title, audio_only=None, folder=""):
        if audio_only is None:
            audio_only = self.config["audio_only"]
        item = DownloadItem(title=title, kind="ytdlp", payload=url,
                            audio_only=audio_only,
                            audio_format=self.config["audio_format"],
                            video_format=self.config["video_format"],
                            folder=folder)
        return self.add(item)

    def add_musicdl(self, song_info, title, folder=""):
        item = DownloadItem(title=title, kind="musicdl", payload=song_info,
                            folder=folder)
        return self.add(item)

    def add_sideb(self, url, title, folder=""):
        item = DownloadItem(title=title, kind="sideb", payload=url,
                            folder=folder)
        return self.add(item)

    def add_applemusic(self, url, title, folder=""):
        item = DownloadItem(title=title, kind="applemusic", payload=url,
                            folder=folder)
        return self.add(item)

    def add_adult(self, payload, title, folder=""):
        item = DownloadItem(title=title, kind="adult", payload=payload,
                            audio_only=False,
                            video_format=self.config["video_format"],
                            folder=folder)
        return self.add(item)

    def add_book(self, payload, title):
        item = DownloadItem(title=title, kind="book", payload=payload,
                            audio_only=False)
        return self.add(item)

    def add_audiobook(self, payload, title):
        item = DownloadItem(title=title, kind="audiobook", payload=payload)
        return self.add(item)

    def add_torrent(self, payload, title):
        item = DownloadItem(title=title, kind="torrent", payload=payload,
                            audio_only=False)
        return self.add(item)

    def add_archive(self, payload, title):
        item = DownloadItem(title=title, kind="archive", payload=payload,
                            audio_only=not payload.get("video"))
        return self.add(item)

    def add_soulseek(self, payload, title):
        item = DownloadItem(title=title, kind="soulseek", payload=payload)
        return self.add(item)

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is None:
            return
        item.cancel_event.set()
        item.pause_requested = False
        item.pause_event.clear()
        with self._cond:
            if item.status in (STATUS_QUEUED, STATUS_PAUSED):
                item.status = STATUS_CANCELLED
            else:
                return
        self._save_state()
        self._notify(item)

    def pause(self, item_id):
        """Pause a queued or cancellable running download for later resume."""
        item = self._find(item_id)
        if item is None or item.status not in (STATUS_QUEUED, STATUS_DOWNLOADING):
            return False
        if item.status == STATUS_DOWNLOADING and item.kind not in PAUSABLE_KINDS:
            return False
        with self._cond:
            # Recheck under the worker's lock so a queued item cannot start
            # between the eligibility check and this transition.
            if item.status not in (STATUS_QUEUED, STATUS_DOWNLOADING):
                return False
            item.pause_requested = True
            item.pause_event.set()
            item.cancel_event.set()
            queued = item.status == STATUS_QUEUED
            if queued:
                item.status = STATUS_PAUSED
        if queued:
            self._save_state()
            self._notify(item)
        return True

    def resume(self, item_id):
        """Resume a paused download, preserving backend partial data."""
        item = self._find(item_id)
        if item is None or item.status != STATUS_PAUSED:
            return False
        with self._cond:
            item.pause_requested = False
            item.pause_event.clear()
            item.cancel_event.clear()
            item.status = STATUS_QUEUED
            item.error = ""
            self._cond.notify_all()
        self._save_state()
        self._notify(item)
        return True

    def retry(self, item_id):
        """Restart a finished download, preserving reusable partial data."""
        item = self._find(item_id)
        if item is None or item.status not in FINISHED_STATUSES or item.seeding:
            return False
        with self._cond:
            item.pause_requested = False
            item.pause_event.clear()
            item.cancel_event.clear()
            item.status = STATUS_QUEUED
            item.error = ""
            item.speed = ""
            item.eta = ""
            item.percent = 0.0
            self._cond.notify_all()
        self._save_state()
        self._notify(item)
        return True

    def remove(self, item_id, delete_data=False):
        """Remove a non-running queue row and optionally its known result."""
        item = self._find(item_id)
        if item is None or item.status == STATUS_DOWNLOADING or item.seeding:
            return False
        if delete_data and not self._delete_result(item):
            return False
        with self._cond:
            try:
                self.items.remove(item)
            except ValueError:
                return False
            self._rebuild_counts_locked()
        self._save_state()
        return True

    def can_delete_data(self, item):
        return bool(self._safe_result_path(item))

    def _safe_result_path(self, item):
        value = str(getattr(item, "result_path", "") or "").strip()
        if not value:
            return ""
        target = os.path.abspath(value)
        roots = []
        download_root = str(self.config.get("download_dir") or "").strip()
        if download_root:
            roots.append(os.path.abspath(download_root))
        torrent_root = str(self.config.get("torrent_dir") or "").strip()
        if torrent_root:
            roots.append(os.path.abspath(torrent_root))
        for root in roots:
            try:
                if (root and target != root
                        and os.path.commonpath((root, target)) == root):
                    return target
            except ValueError:
                continue
        return ""

    def _delete_result(self, item):
        target = self._safe_result_path(item)
        if not target:
            return False
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def remove_finished(self):
        with self._cond:
            # A completed torrent can still be an active upload. Keep its row
            # (and therefore its restart manifest) until seeding is stopped.
            self.items = [
                item for item in self.items
                if item.status not in FINISHED_STATUSES or item.seeding
            ]
            self._rebuild_counts_locked()
        self._save_state()

    def remove_completed(self):
        """Drop the downloads that finished cleanly, keeping the rest.

        This is what the automatic clear-out in Settings uses. A download
        that failed or was cancelled stays: its row is the only place the
        error can be read, and a list that empties itself after a failure
        would hide the one thing worth knowing about.
        """
        with self._cond:
            self.items = [
                item for item in self.items
                if item.status != STATUS_DONE or item.seeding
            ]
            self._rebuild_counts_locked()
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
            return (
                self._status_counts[STATUS_DOWNLOADING],
                self._status_counts[STATUS_QUEUED]
                + self._status_counts[STATUS_PAUSED],
                self._status_counts[STATUS_DONE],
                self._status_counts[STATUS_ERROR]
                + self._status_counts[STATUS_CANCELLED],
            )

    def _rebuild_counts_locked(self):
        self._known_statuses.clear()
        self._key_index.clear()
        for status in self._status_counts:
            self._status_counts[status] = 0
        for item in self.items:
            self._known_statuses[item.id] = item.status
            self._key_index.setdefault(
                self._download_key(item), []).append(item)
            if item.status in self._status_counts:
                self._status_counts[item.status] += 1

    def _record_status_locked(self, item):
        previous = self._known_statuses.get(item.id)
        if previous == item.status:
            return
        if previous in self._status_counts:
            self._status_counts[previous] -= 1
        self._known_statuses[item.id] = item.status
        if item.status in self._status_counts:
            self._status_counts[item.status] += 1

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
            "folder": item.folder,
            "audio_only": item.audio_only,
            "audio_format": item.audio_format,
            "video_format": item.video_format,
            "status": item.status,
            "percent": item.percent,
            "error": item.error,
            "result_path": item.result_path,
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
            folder=str(record.get("folder") or ""),
        )
        item.id = str(record.get("id") or uuid.uuid4().hex)
        item.percent = max(0.0, min(100.0, float(record.get("percent") or 0)))
        item.error = str(record.get("error") or "")
        item.result_path = str(record.get("result_path") or "")
        item.seeding = bool(record.get("seeding", False))
        status = str(record.get("status") or STATUS_QUEUED)
        if payload_type == "unavailable":
            item.status = STATUS_ERROR
            item.error = "Could not restore this download: " + str(
                record.get("payload_error") or "unsupported saved data"
            )
            item.seeding = False
        elif (
            status == STATUS_ERROR
            and item.kind == "soulseek"
            and item.error.startswith(soulseek_backend.SETTINGS_CHANGED_MESSAGE)
        ):
            # Releases before automatic reconnect told the user to queue this
            # transfer again. Upgrade that saved error row on the next launch.
            item.status = STATUS_QUEUED
            item.error = ""
        elif status == STATUS_PAUSED:
            item.status = STATUS_PAUSED
            item.pause_requested = True
            item.pause_event.set()
            item.cancel_event.set()
        elif status in (STATUS_QUEUED, STATUS_DOWNLOADING):
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
        with self._cond:
            self._record_status_locked(item)
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
                self._ensure_download_tools(item)
                if item.kind == "ytdlp":
                    result = self._run_ytdlp(item)
                elif item.kind == "sideb":
                    result = self._run_sideb(item)
                elif item.kind == "adult":
                    result = self._run_adult(item)
                elif item.kind == "book":
                    result = self._run_book(item)
                elif item.kind == "audiobook":
                    result = self._run_audiobook(item)
                elif item.kind == "archive":
                    result = self._run_archive(item)
                elif item.kind == "soulseek":
                    result = self._run_soulseek(item)
                elif item.kind == "torrent":
                    result = self._run_torrent(item)
                elif item.kind == "applemusic":
                    result = self._run_applemusic(item)
                else:
                    result = self._run_musicdl(item)
                if isinstance(result, (str, os.PathLike)):
                    item.result_path = os.fspath(result)
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
                item.status = (STATUS_PAUSED if item.pause_requested
                               else STATUS_CANCELLED)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                if item.pause_requested:
                    item.status = STATUS_PAUSED
                elif item.cancel_event.is_set():
                    item.status = STATUS_CANCELLED
                else:
                    item.status = STATUS_ERROR
                    item.error = str(exc)
            item.speed = ""
            item.eta = ""
            if item.status == STATUS_PAUSED:
                item.cancel_event.set()
            self._save_state()
            self._notify(item)

    @staticmethod
    def _ensure_download_tools(item):
        """Wait in the queue worker if first-run WinGet setup is still active."""
        packages = {
            "ytdlp": ("DenoLand.Deno", "Gyan.FFmpeg.Essentials"),
            "sideb": (
                "DenoLand.Deno", "Gyan.FFmpeg.Essentials",
                "OpenJS.NodeJS.LTS",
            ),
            "adult": ("DenoLand.Deno", "Gyan.FFmpeg.Essentials"),
            "applemusic": ("Gyan.FFmpeg.Essentials",),
            "musicdl": ("Gyan.FFmpeg.Essentials", "OpenJS.NodeJS.LTS"),
        }.get(item.kind)
        if packages and not updater.ensure_external_tools(
            lambda _line: None, packages
        ):
            raise RuntimeError(
                "Required media tools could not be installed through WinGet. "
                "Open Help, Check for updates, then try again."
            )

    def _out_dir(self, item):
        """Where this item's files go: its own folder when it came from one.

        A playlist, channel, artist page or archive item downloads into a
        folder named after it, so its files arrive together instead of
        being mixed through everything else in the download folder.
        """
        base = self.config["download_dir"]
        folder = getattr(item, "folder", "")
        if not folder:
            return base
        path = os.path.join(base, folder)
        os.makedirs(path, exist_ok=True)
        return path

    def _run_ytdlp(self, item):
        def progress(d):
            item.update_from_ytdlp(d)
            self._notify(item, throttle=True)

        return ytdlp_backend.download(
            item.payload,
            self._out_dir(item),
            audio_only=item.audio_only,
            audio_format=item.audio_format,
            video_format=item.video_format,
            progress_cb=progress,
            cancel_event=item.cancel_event,
            cookies_from_browser=self.config["cookies_from_browser"],
            cookies_file=self.config.get("cookies_file"),
        )

    def _run_musicdl(self, item):
        # musicdl exposes no progress callbacks; the item stays in the
        # indeterminate "Downloading" state until it returns.
        downloaded = musicdl_backend.download(
            item.payload, self._out_dir(item),
            online_lookup=bool(self.config["music_metadata_lookup"]))
        if isinstance(downloaded, (list, tuple)) and len(downloaded) == 1:
            path = getattr(downloaded[0], "save_path", "")
            if path:
                return os.fspath(path)
        return ""

    def _run_sideb(self, item):
        # An ARL unlocks Deezer's original MP3 320/FLAC stream. Only fall back
        # to Side B's YouTube Music audio when the account cannot provide the
        # requested quality; authentication and other native errors must stay
        # visible so a broken ARL does not fail silently.
        if (self.config["deezer_arl"] or "").strip():
            try:
                return self._run_deezer(item)
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

        return sideb_backend.download(
            item.payload, self._out_dir(item), self.config,
            event_cb=on_event)

    def _run_applemusic(self, item):
        return applemusic_backend.download(
            item.payload, self._out_dir(item), self.config,
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

        return adult_backend.download(
            item.payload, self._out_dir(item), progress_cb=progress,
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

        return book_backend.download(
            item.payload, self._out_dir(item), self.config,
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

        return audiobook_backend.download(
            item.payload, self._out_dir(item), progress_cb=progress,
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

        return archive_backend.download(
            item.payload, self._out_dir(item), progress_cb=progress,
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

        attempt = 0
        settings_attempts = 0
        while True:
            try:
                result = soulseek_backend.download(
                    item.payload, self.config, progress_cb=progress,
                    cancel_event=item.cancel_event)
                return result
            except soulseek_backend.SoulseekSettingsChanged:
                if item.cancel_event.is_set():
                    raise soulseek_backend.SoulseekDownloadCancelled() from None
                settings_attempts += 1
                if settings_attempts > SOULSEEK_SETTINGS_RETRY_ATTEMPTS:
                    raise soulseek_backend.SoulseekError(
                        "Soulseek settings kept changing; the transfer could "
                        "not be reconnected."
                    ) from None
                # Re-issuing the same peer/path lets aioslsk resume its cached
                # partial transfer after the settings-driven client restart.
                item.speed = "Soulseek settings changed; reconnecting automatically"
                item.eta = ""
                item.error = ""
                self._notify(item)
                if item.cancel_event.wait(1.0):
                    raise soulseek_backend.SoulseekDownloadCancelled() from None
            except soulseek_backend.SoulseekTransientError as exc:
                if item.cancel_event.is_set():
                    raise soulseek_backend.SoulseekDownloadCancelled() from None
                attempt += 1
                if attempt > SOULSEEK_RETRY_ATTEMPTS:
                    raise soulseek_backend.SoulseekError(
                        f"{exc} (retried {SOULSEEK_RETRY_ATTEMPTS} times)"
                    ) from exc
                delay = min(
                    SOULSEEK_RETRY_MAX_DELAY,
                    SOULSEEK_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                )
                item.speed = (
                    f"Peer interrupted; retrying in {delay:g}s "
                    f"({attempt}/{SOULSEEK_RETRY_ATTEMPTS})"
                )
                item.eta = ""
                item.error = ""
                self._notify(item)
                if item.cancel_event.wait(delay):
                    raise soulseek_backend.SoulseekDownloadCancelled() from None

    def _run_torrent(self, item):
        if self.config.get("torrent_engine") and torrent_engine.available():
            return self._run_torrent_engine(item)
        # With the engine off, blindDL does not move torrent bytes; the user's
        # own client does. Handing over the magnet is the whole job, so this
        # finishes at once and the queue row records that the link went out.
        item.speed = "Opening torrent client"
        self._notify(item)
        # out_dir is where a private tracker's .torrent lands when the row
        # carries no magnet; a magnet never touches the disk.
        torrent_backend.hand_off(item.payload, self._out_dir(item))
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
        result = torrent_engine.download(
            item.payload, self._out_dir(item), self.config,
            progress_cb=progress, cancel_event=item.cancel_event,
            keep_partial_event=item.pause_event)
        item.seeding = True
        return result

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

        return deezer_backend.download(
            item.payload, self._out_dir(item), self.config,
            progress_cb=progress, cancel_event=item.cancel_event)
