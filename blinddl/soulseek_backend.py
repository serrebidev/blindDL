# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Persistent Soulseek search, transfer, and library-sharing backend.

aioslsk is asynchronous while blindDL's search and download workers are
ordinary threads.  One private asyncio loop owns the Soulseek client for the
whole application lifetime; the small public functions below safely bridge
those worker threads into that loop.
"""

from __future__ import annotations

import asyncio
import dbm
import functools
import json
import logging
import ntpath
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import app_data_dir

_IMPORT_ERROR: ImportError | None = None

try:
    from aioslsk.client import SoulSeekClient
    from aioslsk.commands import (
        GetUserStatsCommand,
        GetRoomListCommand,
        JoinRoomCommand,
        LeaveRoomCommand,
        PeerGetDirectoryContentCommand,
        PeerGetSharesCommand,
        PeerGetUserInfoCommand,
        PrivateMessageCommand,
        RoomMessageCommand,
    )
    from aioslsk.events import (
        FriendListChangedEvent,
        PrivateMessageEvent,
        RoomJoinedEvent,
        RoomLeftEvent,
        RoomListEvent,
        RoomMessageEvent,
        TransferAddedEvent,
        TransferProgressEvent,
        TransferRemovedEvent,
        UserStatusUpdateEvent,
    )
    from aioslsk.protocol.messages import (
        PeerTransferQueueFailed,
        PeerTransferReply,
    )
    from aioslsk.protocol.primitives import AttributeKey
    from aioslsk.settings import (
        CredentialsSettings,
        Settings,
        SharedDirectorySettingEntry,
    )
    from aioslsk.shares.cache import SharesShelveCache
    from aioslsk.shares.model import SharedDirectory
    from aioslsk.transfer.cache import TransferShelveCache
    from aioslsk.transfer.manager import TransferManager
    from aioslsk.transfer.model import FailReason, TransferDirection
    from aioslsk.transfer.state import TransferState
except ImportError as exc:  # pragma: no cover - dependency is included in releases
    _IMPORT_ERROR = exc
    SoulSeekClient = None


logger = logging.getLogger(__name__)
# Public searches routinely encounter peers behind closed ports, and replies
# can arrive after blindDL's search deadline. aioslsk logs both normal cases at
# WARNING; blindDL surfaces actionable connection and transfer failures itself.
logging.getLogger("aioslsk").setLevel(logging.ERROR)

SOURCE = "Soulseek"
SETTINGS_CHANGED_MESSAGE = "Soulseek settings changed during this transfer."


def _path_is_within(parent: str, candidate: str) -> bool:
    """Return whether candidate is inside parent, including parent itself."""
    try:
        return os.path.commonpath((candidate, parent)) == parent
    except ValueError:
        # Windows raises when the paths use different drives. They cannot have
        # a parent/child relationship, so this is a normal negative result.
        return False


def _shared_directory_is_parent_of(self, directory) -> bool:
    path = directory if isinstance(directory, str) else directory.absolute_path
    return _path_is_within(self.absolute_path, path)


def _shared_directory_is_child_of(self, directory) -> bool:
    path = directory if isinstance(directory, str) else directory.absolute_path
    return _path_is_within(path, self.absolute_path)


def _shared_directory_items_for(self, directory) -> set:
    return {
        item
        for item in self.items
        if _path_is_within(directory.absolute_path, item.get_absolute_path())
    }


def _install_aioslsk_cross_drive_fix() -> None:
    """Make aioslsk shared-folder comparisons safe across Windows drives."""
    if not available():
        return
    # aioslsk 1.6.3 lets ValueError from ntpath.commonpath escape from these
    # methods. Its share manager calls them while reconciling the old cache
    # with new settings, which prevents Soulseek from reconnecting when a
    # download or shared folder moves to another drive.
    SharedDirectory.is_parent_of = _shared_directory_is_parent_of
    SharedDirectory.is_child_of = _shared_directory_is_child_of
    SharedDirectory.get_items_for_directory = _shared_directory_items_for


# -- refusing uploads to users who share nothing ------------------------------

# How long a peer's shared-file count is trusted before it is looked up again.
# Long enough that a peer requesting a whole folder is only checked once, short
# enough that somebody who starts sharing is let in during the same session.
_LEECHER_CACHE_SECONDS = 600.0

# Set from the current settings every time the client is configured.
_leecher_guard: dict[str, Any] = {"enabled": True, "allowed": frozenset()}

# username (casefolded) -> (checked at, shared file count)
_leecher_counts: dict[str, tuple[float, int]] = {}


def _set_leecher_guard(snapshot: dict[str, Any]) -> None:
    """Track who may upload without sharing: friends and priority users."""
    allowed = {str(name).casefold() for name in snapshot.get("friends", [])}
    allowed |= {str(name).casefold() for name in snapshot.get("priority_users", [])}
    _leecher_guard["enabled"] = bool(snapshot.get("block_leechers", True))
    _leecher_guard["allowed"] = frozenset(allowed)


async def _shared_file_count(username: str) -> int | None:
    """Ask the server how many files a peer shares, or None if unknown."""
    key = username.casefold()
    cached = _leecher_counts.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _LEECHER_CACHE_SECONDS:
        return cached[1]
    client = _SERVICE._client
    if client is None:
        return None
    try:
        stats = await asyncio.wait_for(
            client(GetUserStatsCommand(username), response=True), timeout=15
        )
    except Exception:  # noqa: BLE001 - a lookup failure must not block uploads
        logger.debug("could not read Soulseek stats for %s", username, exc_info=True)
        return None
    count = int(getattr(stats, "shared_file_count", 0) or 0)
    _leecher_counts[key] = (now, count)
    return count


async def _refuses_upload(connection) -> bool:
    """True when this peer shares nothing and is not on the allowed list."""
    username = getattr(connection, "username", "") or ""
    if not username or not _leecher_guard["enabled"]:
        return False
    if username.casefold() in _leecher_guard["allowed"]:
        return False
    count = await _shared_file_count(username)
    # An unanswered lookup leaves the upload alone: a server hiccup must not
    # start refusing peers who do share.
    return count == 0


# functools.wraps carries aioslsk's _registered_message marker onto the
# wrapper, which is how its handlers are found when a manager is built.


def _guarded_queue_handler(original):
    """Refuse a queue request from a leecher before aioslsk queues it."""

    @functools.wraps(original)
    async def guarded_queue(self, message, connection):
        if await _refuses_upload(connection):
            connection.queue_message(
                PeerTransferQueueFailed.Request(
                    filename=message.filename,
                    reason=FailReason.FILE_NOT_SHARED,
                )
            )
            return None
        return await original(self, message, connection)

    guarded_queue._blinddl_guarded = True
    return guarded_queue


def _guarded_request_handler(original):
    """Refuse a direct upload request from a leecher.

    Peers may skip the queue request entirely, and the same message asks us to
    send a file or to receive one, so only uploads are judged here.
    """

    @functools.wraps(original)
    async def guarded_request(self, message, connection):
        if message.direction == TransferDirection.UPLOAD.value and await (
            _refuses_upload(connection)
        ):
            connection.queue_message(
                PeerTransferReply.Request(
                    ticket=message.ticket,
                    allowed=False,
                    reason=FailReason.FILE_NOT_SHARED,
                )
            )
            return None
        return await original(self, message, connection)

    guarded_request._blinddl_guarded = True
    return guarded_request


def _install_leecher_guard() -> None:
    """Refuse upload requests from peers who share no files of their own.

    aioslsk decides both at the queue request and at the transfer request, so
    both are wrapped. Its own refusal message is reused, which peers already
    understand as "you are not getting this file".
    """
    if not available():
        return
    if getattr(TransferManager._on_peer_transfer_queue, "_blinddl_guarded", False):
        return
    TransferManager._on_peer_transfer_queue = _guarded_queue_handler(
        TransferManager._on_peer_transfer_queue
    )
    TransferManager._on_peer_transfer_request = _guarded_request_handler(
        TransferManager._on_peer_transfer_request
    )


AUDIO_EXTENSIONS = frozenset(
    {
        "aac",
        "aiff",
        "alac",
        "ape",
        "flac",
        "m4a",
        "m4b",
        "mp3",
        "mpc",
        "ogg",
        "opus",
        "wav",
        "wma",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        "3gp",
        "avi",
        "flv",
        "m2ts",
        "m4v",
        "mkv",
        "mov",
        "mp4",
        "mpeg",
        "mpg",
        "ogv",
        "ts",
        "vob",
        "webm",
        "wmv",
    }
)
BOOK_EXTENSIONS = frozenset(
    {
        "azw",
        "azw3",
        "cb7",
        "cbr",
        "cbz",
        "djvu",
        "doc",
        "docx",
        "epub",
        "fb2",
        "html",
        "lit",
        "mobi",
        "odt",
        "pdf",
        "rtf",
        "txt",
    }
)
TORRENT_EXTENSIONS = frozenset({"torrent"})

MEDIA_EXTENSIONS = {
    "audio": AUDIO_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
    "book": BOOK_EXTENSIONS,
    "torrent": TORRENT_EXTENSIONS,
    "media": AUDIO_EXTENSIONS | VIDEO_EXTENSIONS,
}


class SoulseekError(RuntimeError):
    """A user-facing Soulseek connection, search, or transfer failure."""


class SoulseekDownloadCancelled(SoulseekError):
    """Raised when a blindDL cancellation aborts an aioslsk transfer."""


class SoulseekSettingsChanged(SoulseekError):
    """Raised when a client restart interrupts an in-flight transfer."""


class SoulseekTransientError(SoulseekError):
    """A peer- or network-side interruption that a retry may get past.

    Peers cancel uploads, drop their connection, or hit a disk error all the
    time; none of those means the file is gone. The download queue retries
    these a few times before giving up.
    """


# Reasons a peer sends that retrying the same peer cannot change. Everything
# else -- a cancelled upload, a peer read error, an unknown abort -- is worth
# asking for again.
_PERMANENT_REASONS = frozenset({"file not shared."})


def _transfer_failure(reason):
    """Build the exception for a transfer that stopped before completing."""
    text = str(reason or "").strip() or "Transfer failed"
    if text.casefold() in _PERMANENT_REASONS:
        return SoulseekError(text)
    return SoulseekTransientError(text)


def available() -> bool:
    return SoulSeekClient is not None


_install_aioslsk_cross_drive_fix()
_install_leecher_guard()


def _cache_dir(name: str = "soulseek") -> str:
    """Return a shelve cache directory private to this Python version.

    aioslsk stores its share index and transfer list with ``shelve``, which
    chooses a ``dbm`` backend from whatever the running interpreter offers.
    A frozen build embeds its own Python, so it is routinely a different
    version from the one a source checkout runs -- and 3.13 made
    ``dbm.sqlite3`` the default, which older interpreters cannot read at all.
    Sharing one directory therefore lets whichever ran first write a database
    the other reports as "db type could not be determined". Giving each
    version its own directory keeps them from ever meeting.
    """
    version = f"py{sys.version_info.major}{sys.version_info.minor}"
    path = os.path.join(app_data_dir(), f"{name}-{version}")
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        _migrate_cache(os.path.join(app_data_dir(), name), path)
    return path


def _migrate_cache(source: str, target: str) -> None:
    """Carry a pre-versioning cache forward when this Python can read it.

    Queued transfers live in that cache, so it is worth keeping. A database
    written by an incompatible interpreter is left where it is; abandoning it
    is exactly what fixes the failure this split exists to prevent.
    """
    index = os.path.join(source, SharesShelveCache.DEFAULT_FILENAME)
    try:
        backend = dbm.whichdb(index)
    except OSError:
        return
    # None means unreadable; "" means an unrecognized format. Either way the
    # files are not ours to reuse.
    if not backend:
        return
    try:
        __import__(backend)
    except ImportError:
        return
    try:
        for entry in os.scandir(source):
            if entry.is_file():
                shutil.copy2(entry.path, os.path.join(target, entry.name))
    except OSError:
        logger.exception("could not migrate the Soulseek cache from %s", source)


def runtime_probe() -> str:
    """Verify the complete backend really works in a frozen build."""
    if not available():
        detail = f" ({_IMPORT_ERROR})" if _IMPORT_ERROR else ""
        raise SoulseekError(
            "The bundled Soulseek backend could not be loaded" + detail + "."
        )
    settings = Settings(
        credentials=CredentialsSettings(username="blinddl-self-test", password="test")
    )
    cache_dir = _cache_dir("soulseek-self-test")
    shares_cache = SharesShelveCache(cache_dir)
    transfer_cache = TransferShelveCache(cache_dir)
    # Constructing these only records a directory name. aioslsk opens the
    # shelve later, inside client.start(), and shelve resolves its dbm backend
    # by importing a name held in a plain string list -- something PyInstaller
    # cannot see and so has historically left out of the build. Round-trip
    # both caches here so a missing backend fails the release instead of
    # surfacing as an empty Soulseek search for the user.
    try:
        shares_cache.write(shares_cache.read())
        transfer_cache.write(transfer_cache.read())
        backend = dbm.whichdb(
            os.path.join(cache_dir, SharesShelveCache.DEFAULT_FILENAME)
        )
    except dbm.error as exc:
        raise SoulseekError(
            f"The Soulseek cache could not be opened ({exc}); this build's "
            "dbm backends are incomplete."
        ) from exc
    if not backend:
        raise SoulseekError(
            "The Soulseek cache was written in a format this build cannot "
            "identify; its dbm backends are incomplete."
        )
    client = SoulSeekClient(
        settings,
        shares_cache=shares_cache,
        transfer_cache=transfer_cache,
    )
    if client is None:
        raise SoulseekError("The bundled Soulseek client could not be constructed.")
    return f"aioslsk backend and persistent caches ({backend})"


async def _verify_account_async(username: str, password: str, timeout: float):
    if not available():
        detail = f" ({_IMPORT_ERROR})" if _IMPORT_ERROR else ""
        raise SoulseekError(
            "The bundled Soulseek backend could not be loaded"
            + detail
            + ". Reinstall blindDL to restore Soulseek support."
        )
    settings = Settings(
        credentials=CredentialsSettings(username=username, password=password)
    )
    # Account checks must not scan or advertise the user's files. Soulseek
    # registers an unused username during the same login exchange used by an
    # existing account, so no separate registration protocol is necessary.
    settings.shares.scan_on_start = False
    settings.network.server.reconnect.auto = False
    settings.network.upnp.enabled = False
    # Choose disposable ports, avoiding collisions with blindDL's persistent
    # client (or another Soulseek client) during this short account check.
    ports = []
    while len(ports) < 2:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in ports:
            ports.append(port)
    settings.network.listening.port = ports[0]
    settings.network.listening.obfuscated_port = ports[1]
    client = SoulSeekClient(settings)
    try:
        await asyncio.wait_for(client.start(), timeout=timeout)
        await asyncio.wait_for(client.login(), timeout=timeout)
    except Exception as exc:
        raise SoulseekError(str(exc) or exc.__class__.__name__) from exc
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001 - preserve the useful login error
            logger.exception("failed to stop the Soulseek account check")


def verify_account(username: str, password: str, timeout: float = 30.0):
    """Sign in, or register an unused username, without sharing any files."""
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        raise SoulseekError("Enter a Soulseek username.")
    if not password:
        raise SoulseekError("Enter a Soulseek password.")
    asyncio.run(_verify_account_async(username, password, timeout))


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _config_snapshot(config) -> dict[str, Any]:
    """Copy only backend settings so later Config mutations cannot race us."""
    download_dir = str(config.get("download_dir", "") or "").strip()
    extra_folders = []
    seen = set()
    for value in config.get("soulseek_shared_folders", []) or []:
        path = str(value or "").strip()
        if not path:
            continue
        key = _normal_path(path)
        if key in seen:
            continue
        seen.add(key)
        extra_folders.append(os.path.abspath(path))
    rooms = []
    seen_rooms = set()
    for value in config.get("soulseek_rooms", []) or []:
        room = str(value or "").strip()
        key = room.casefold()
        if not room or key in seen_rooms:
            continue
        seen_rooms.add(key)
        rooms.append(room)
    private_rooms = []
    seen_private_rooms = set()
    for value in config.get("soulseek_private_rooms", []) or []:
        room = str(value or "").strip()
        key = room.casefold()
        if not room or key in seen_private_rooms:
            continue
        seen_private_rooms.add(key)
        private_rooms.append(room)
    friends = []
    seen_friends = set()
    for value in config.get("soulseek_friends", []) or []:
        username = str(value or "").strip()
        key = username.casefold()
        if not username or key in seen_friends:
            continue
        seen_friends.add(key)
        friends.append(username)
    priority_users = []
    seen_priority = set()
    for value in config.get("soulseek_priority_users", []) or []:
        username = str(value or "").strip()
        key = username.casefold()
        if not username or key in seen_priority:
            continue
        seen_priority.add(key)
        priority_users.append(username)
    return {
        "enabled": bool(config.get("soulseek_enabled", False)),
        "username": str(config.get("soulseek_username", "") or "").strip(),
        "password": str(config.get("soulseek_password", "") or ""),
        "description": str(config.get("soulseek_description", "") or "").strip(),
        "download_dir": os.path.abspath(download_dir) if download_dir else "",
        "share_library": bool(config.get("soulseek_share_library", True)),
        "shared_folders": extra_folders,
        "listen_port": int(config.get("soulseek_listen_port", 60000)),
        "obfuscated_port": int(config.get("soulseek_obfuscated_port", 60001)),
        "upnp": bool(config.get("soulseek_upnp", True)),
        "obfuscate": bool(config.get("soulseek_obfuscate", False)),
        "upload_slots": int(config.get("soulseek_upload_slots", 2)),
        "upload_kib": int(config.get("soulseek_max_upload_kib", 0)),
        "download_kib": int(config.get("soulseek_max_download_kib", 0)),
        "max_results": int(config.get("soulseek_max_results", 500)),
        "block_leechers": bool(config.get("soulseek_block_leechers", True)),
        "rooms": rooms,
        "private_rooms": private_rooms,
        "friends": friends,
        "priority_users": priority_users,
    }


def _signature(snapshot: dict[str, Any]) -> tuple:
    return tuple(
        (key, tuple(value) if isinstance(value, list) else value)
        for key, value in sorted(snapshot.items())
        # These are applied to a live client, so changing one must not force a
        # reconnect that would interrupt transfers.
        if key
        not in {
            "friends",
            "priority_users",
            "rooms",
            "private_rooms",
            "block_leechers",
        }
    )


def _build_settings(snapshot: dict[str, Any]):
    if not available():
        detail = f" ({_IMPORT_ERROR})" if _IMPORT_ERROR else ""
        raise SoulseekError(
            "The bundled Soulseek backend could not be loaded"
            + detail
            + ". Reinstall blindDL to restore Soulseek support."
        )
    if not snapshot["username"] or not snapshot["password"]:
        raise SoulseekError(
            "Enter a Soulseek username and password in Settings, Soulseek."
        )
    if not snapshot["download_dir"]:
        raise SoulseekError("Choose a download folder before enabling Soulseek.")

    os.makedirs(snapshot["download_dir"], exist_ok=True)
    folders = []
    seen = set()
    candidates = list(snapshot["shared_folders"])
    if snapshot["share_library"]:
        candidates.insert(0, snapshot["download_dir"])
    for path in candidates:
        absolute = os.path.abspath(path)
        key = _normal_path(absolute)
        if key in seen:
            continue
        seen.add(key)
        folders.append(SharedDirectorySettingEntry(path=absolute))

    settings = Settings(
        credentials=CredentialsSettings(
            username=snapshot["username"],
            password=snapshot["password"],
        )
    )
    settings.credentials.info.description = snapshot["description"] or None
    settings.shares.download = snapshot["download_dir"]
    settings.shares.directories = folders
    settings.shares.scan_on_start = True
    settings.network.server.reconnect.auto = True
    settings.network.listening.port = snapshot["listen_port"]
    settings.network.listening.obfuscated_port = snapshot["obfuscated_port"]
    settings.network.upnp.enabled = snapshot["upnp"]
    settings.network.peer.obfuscate = snapshot["obfuscate"]
    # Despite the historical ``kbps`` field name, aioslsk's limiter multiplies
    # these values by 1024 bytes, matching the KiB/s used in blindDL's UI.
    settings.network.limits.upload_speed_kbps = snapshot["upload_kib"]
    settings.network.limits.download_speed_kbps = snapshot["download_kib"]
    settings.transfers.limits.upload_slots = snapshot["upload_slots"]
    settings.transfers.report_interval = 0.25
    settings.rooms.favorites = set(snapshot["rooms"])
    settings.rooms.auto_join = True
    # aioslsk gives users in this set upload priority. Keep blindDL's
    # friend and explicit free-slot lists separate in its own configuration.
    settings.users.friends = set(snapshot["friends"]) | set(
        snapshot["priority_users"]
    )
    return settings


def _format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _format_speed(speed: int) -> str:
    return f"{_format_size(speed)}/s" if speed else ""


def _file_extension(file_data) -> str:
    extension = str(file_data.extension or "").strip().lstrip(".").lower()
    if not extension:
        extension = Path(ntpath.basename(file_data.filename)).suffix.lstrip(".").lower()
    return extension


def _result_item(result, file_data) -> dict[str, Any]:
    attributes = file_data.get_attribute_map()
    duration = attributes.get(AttributeKey.DURATION)
    bitrate = attributes.get(AttributeKey.BITRATE)
    sample_rate = attributes.get(AttributeKey.SAMPLE_RATE)
    bit_depth = attributes.get(AttributeKey.BIT_DEPTH)
    extension = _file_extension(file_data)

    quality = extension.upper()
    details = []
    if bitrate:
        details.append(f"{bitrate} kbps")
    if bit_depth:
        details.append(f"{bit_depth}-bit")
    if sample_rate:
        details.append(f"{sample_rate / 1000:g} kHz")
    if details:
        quality = f"{quality}, {', '.join(details)}"

    availability = "free slot" if result.has_free_slots else "queued"
    if result.queue_size:
        availability += f", {result.queue_size} waiting"
    speed = _format_speed(result.avg_speed)
    if speed:
        availability += f", {speed} average"

    username = str(result.username)
    filename = str(file_data.filename)
    return {
        "title": ntpath.basename(filename) or filename,
        "kind": "soulseek",
        "source": f"{SOURCE}, {availability}",
        "availability": availability,
        "artist": username,
        "author": username,
        "creator": username,
        "uploader": username,
        "username": username,
        "remote_path": filename,
        "folder": ntpath.dirname(filename),
        "format": quality,
        "extension": extension,
        "duration_s": int(duration) if duration else None,
        "size_bytes": int(file_data.filesize or 0),
        "file_size": _format_size(file_data.filesize),
        "has_free_slots": bool(result.has_free_slots),
        "average_speed": int(result.avg_speed or 0),
        "queue_size": int(result.queue_size or 0),
        # Torrent result columns still convey something meaningful: one peer
        # owns the .torrent file, and its remote queue is the waiting count.
        "seeders": 1 if result.has_free_slots else 0,
        "leechers": int(result.queue_size or 0),
        "age": "",
        "year": "",
    }


class _Service:
    def __init__(self, history_path=None):
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._async_lock = None
        self._client = None
        self._active_signature = None
        self._failed_signature = None
        self._failure: Exception | None = None
        self._rescan_task = None
        self._username = ""
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._listeners_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._rooms: dict[str, dict[str, Any]] = {}
        self._room_messages: list[dict[str, Any]] = []
        self._private_messages: list[dict[str, Any]] = []
        self._friends: dict[str, dict[str, str]] = {}
        self._configured_friends: set[str] = set()
        self._uploads: list[dict[str, Any]] = []
        self._history_path = os.fspath(history_path) if history_path else None
        self._load_history()

    @staticmethod
    def _clean_history_rows(rows, kind):
        if not isinstance(rows, list):
            return []
        required = ("timestamp", "message")
        cleaned = []
        for row in rows[-2000:]:
            if not isinstance(row, dict) or any(key not in row for key in required):
                continue
            if kind == "room" and not all(key in row for key in ("room", "user")):
                continue
            if kind == "private" and "user" not in row:
                continue
            try:
                timestamp = int(row["timestamp"])
            except (TypeError, ValueError):
                continue
            cleaned_row = {
                "timestamp": timestamp,
                "user": str(row.get("user") or ""),
                "message": str(row.get("message") or ""),
                "outgoing": bool(row.get("outgoing", False)),
            }
            if kind == "room":
                cleaned_row["room"] = str(row.get("room") or "")
            cleaned.append(cleaned_row)
        return cleaned

    def _load_history(self):
        if not self._history_path:
            return
        try:
            with open(self._history_path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(document, dict):
            return
        self._room_messages = self._clean_history_rows(
            document.get("room_messages"), "room"
        )
        self._private_messages = self._clean_history_rows(
            document.get("private_messages"), "private"
        )

    def _save_history(self):
        if not self._history_path:
            return
        with self._state_lock:
            document = {
                "version": 1,
                "room_messages": list(self._room_messages),
                "private_messages": list(self._private_messages),
            }
        temporary = self._history_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._history_path)), exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._history_path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def add_listener(self, listener: Callable[[dict[str, Any]], None]):
        with self._listeners_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[dict[str, Any]], None]):
        with self._listeners_lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def _emit(self, event: dict[str, Any]):
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(dict(event))
            except Exception:  # noqa: BLE001 - one UI listener cannot stop chat
                logger.exception("Soulseek event listener failed")

    @staticmethod
    def _room_data(room) -> dict[str, Any]:
        return {
            "name": str(room.name),
            "private": bool(room.private),
            "joined": bool(room.joined),
            "user_count": int(room.user_count or len(room.users)),
        }

    def _set_rooms(self, rooms):
        room_data = [self._room_data(room) for room in rooms]
        with self._state_lock:
            self._rooms = {room["name"].casefold(): room for room in room_data}
        self._emit({"type": "rooms", "rooms": self.rooms_snapshot()})

    def _update_room(self, room):
        data = self._room_data(room)
        with self._state_lock:
            self._rooms[data["name"].casefold()] = data
        self._emit({"type": "rooms", "rooms": self.rooms_snapshot()})

    def rooms_snapshot(self):
        with self._state_lock:
            rooms = [dict(room) for room in self._rooms.values()]
        return sorted(
            rooms,
            key=lambda room: (
                not room["joined"],
                -room["user_count"],
                room["name"].casefold(),
            ),
        )

    def room_messages_snapshot(self):
        with self._state_lock:
            return [dict(message) for message in self._room_messages]

    def private_messages_snapshot(self):
        with self._state_lock:
            return [dict(message) for message in self._private_messages]

    @staticmethod
    def _status_name(user) -> str:
        status = getattr(user, "status", None)
        name = getattr(status, "name", "unknown")
        return str(name).replace("_", " ").title()

    def _set_friends(self, usernames, client=None):
        self._configured_friends = {str(name).casefold() for name in usernames}
        friends = {}
        for username in usernames:
            user = client.users.get_user_object(username) if client else None
            friends[username.casefold()] = {
                "username": username,
                "status": self._status_name(user) if user else "Unknown",
            }
        with self._state_lock:
            self._friends = friends
        self._emit({"type": "friends", "friends": self.friends_snapshot()})

    def friends_snapshot(self):
        with self._state_lock:
            friends = [dict(friend) for friend in self._friends.values()]
        return sorted(
            friends,
            key=lambda friend: (
                friend["status"] == "Offline",
                friend["status"] == "Unknown",
                friend["username"].casefold(),
            ),
        )

    @staticmethod
    def _transfer_key(transfer) -> str:
        return "{}\0{}".format(
            str(transfer.username).casefold(),
            str(transfer.remote_path).casefold(),
        )

    def _upload_data(self, transfer) -> dict[str, Any]:
        snapshot = transfer.take_progress_snapshot()
        state = getattr(snapshot.state, "name", str(snapshot.state))
        total = int(transfer.filesize or 0)
        uploaded = int(snapshot.bytes_transfered or 0)
        speed = float(snapshot.speed or 0)
        return {
            "key": self._transfer_key(transfer),
            "title": ntpath.basename(str(transfer.remote_path))
            or str(transfer.remote_path),
            "service": SOURCE,
            "peer": str(transfer.username),
            "status": state.replace("_", " ").title(),
            "uploaded": uploaded,
            "total": total,
            "percent": min(100.0, uploaded * 100.0 / total) if total else 0.0,
            "speed": speed,
            "active": state
            not in {"COMPLETE", "ABORTED", "FAILED", "INCOMPLETE"},
            "error": str(snapshot.fail_reason or snapshot.abort_reason or ""),
            "started_at": snapshot.start_time,
            "completed_at": snapshot.complete_time,
        }

    def _publish_uploads(self, client=None):
        client = client or self._client
        if client is None:
            return
        rows = [
            self._upload_data(transfer)
            for transfer in client.transfers.get_uploads()
        ]
        rows.sort(
            key=lambda row: (
                not row["active"],
                -(row["started_at"] or row["completed_at"] or 0),
                row["title"].casefold(),
            )
        )
        with self._state_lock:
            self._uploads = rows[:2000]
        self._emit({"type": "uploads", "uploads": self.uploads_snapshot()})

    def uploads_snapshot(self):
        with self._state_lock:
            return [dict(upload) for upload in self._uploads]

    def _append_room_message(self, message):
        data = {
            "timestamp": int(message.timestamp),
            "room": str(message.room.name),
            "user": str(message.user.name),
            "message": str(message.message),
            "outgoing": str(message.user.name).casefold() == self._username.casefold(),
        }
        with self._state_lock:
            self._room_messages.append(data)
            del self._room_messages[:-2000]
        self._save_history()
        self._emit({"type": "room_message", "message": dict(data)})

    def _append_private_message(self, data):
        with self._state_lock:
            self._private_messages.append(data)
            del self._private_messages[:-2000]
        self._save_history()
        self._emit({"type": "private_message", "message": dict(data)})

    def _on_room_list(self, event):
        self._set_rooms(event.rooms)

    def _on_room_joined(self, event):
        self._update_room(event.room)

    def _on_room_left(self, event):
        self._update_room(event.room)

    def _on_room_message(self, event):
        self._append_room_message(event.message)

    def _on_private_message(self, event):
        message = event.message
        self._append_private_message(
            {
                "timestamp": int(message.timestamp),
                "user": str(message.user.name),
                "message": str(message.message),
                "outgoing": False,
            }
        )

    def _on_friend_list_changed(self, event):
        client = self._client
        with self._state_lock:
            for username in event.removed:
                self._friends.pop(username.casefold(), None)
            for username in event.added:
                if username.casefold() not in self._configured_friends:
                    continue
                user = client.users.get_user_object(username) if client else None
                self._friends[username.casefold()] = {
                    "username": username,
                    "status": self._status_name(user) if user else "Unknown",
                }
        self._emit({"type": "friends", "friends": self.friends_snapshot()})

    def _on_user_status(self, event):
        username = str(event.current.name)
        with self._state_lock:
            friend = self._friends.get(username.casefold())
            if friend is None:
                return
            friend["status"] = self._status_name(event.current)
        self._emit({"type": "friends", "friends": self.friends_snapshot()})

    def _on_transfer_changed(self, event):
        transfer = getattr(event, "transfer", None)
        if transfer is None or transfer.is_upload():
            self._publish_uploads()

    def _on_transfer_progress(self, event):
        if any(transfer.is_upload() for transfer, _before, _after in event.updates):
            self._publish_uploads()

    def _register_events(self, client):
        client.events.register(RoomListEvent, self._on_room_list)
        client.events.register(RoomJoinedEvent, self._on_room_joined)
        client.events.register(RoomLeftEvent, self._on_room_left)
        client.events.register(RoomMessageEvent, self._on_room_message)
        client.events.register(PrivateMessageEvent, self._on_private_message)
        client.events.register(FriendListChangedEvent, self._on_friend_list_changed)
        client.events.register(UserStatusUpdateEvent, self._on_user_status)
        client.events.register(TransferAddedEvent, self._on_transfer_changed)
        client.events.register(TransferProgressEvent, self._on_transfer_progress)
        client.events.register(TransferRemovedEvent, self._on_transfer_changed)

    def _ensure_loop(self):
        if self._thread and self._thread.is_alive():
            return
        self._loop_ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="blinddl-soulseek"
        )
        self._thread.start()
        self._loop_ready.wait(5)
        if self._loop is None:
            raise SoulseekError("Could not start the Soulseek background service.")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._async_lock = asyncio.Lock()
        self._loop_ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _submit(self, coroutine):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def _stop_client(self):
        client, self._client = self._client, None
        self._active_signature = None
        self._username = ""
        if client is not None:
            await client.stop()
            self._publish_uploads(client)

    async def _configure(self, snapshot: dict[str, Any]):
        signature = _signature(snapshot)
        # Applied outside the client: the guard refuses upload requests before
        # aioslsk queues them, whether or not the client is rebuilt below.
        _set_leecher_guard(snapshot)
        async with self._async_lock:
            if not snapshot["enabled"]:
                await self._stop_client()
                self._failed_signature = None
                self._failure = None
                return None
            if self._client is not None and signature == self._active_signature:
                self._client.settings.users.friends = set(snapshot["friends"]) | set(
                    snapshot.get("priority_users", [])
                )
                self._client.settings.rooms.favorites = set(snapshot["rooms"])
                self._set_friends(snapshot["friends"], self._client)
                return self._client
            if signature == self._failed_signature and self._failure is not None:
                raise self._failure

            await self._stop_client()
            settings = _build_settings(snapshot)
            cache_dir = _cache_dir()
            client = SoulSeekClient(
                settings,
                shares_cache=SharesShelveCache(cache_dir),
                transfer_cache=TransferShelveCache(cache_dir),
            )
            self._register_events(client)
            try:
                await client.start()
                await client.login()
            except Exception as exc:
                try:
                    await client.stop()
                except Exception:  # noqa: BLE001 - preserve the login error
                    logger.exception("failed to stop an unsuccessful Soulseek client")
                error = SoulseekError(str(exc) or exc.__class__.__name__)
                self._failed_signature = signature
                self._failure = error
                raise error from exc

            self._client = client
            self._username = snapshot["username"]
            self._set_friends(snapshot["friends"], client)
            self._publish_uploads(client)
            self._active_signature = signature
            self._failed_signature = None
            self._failure = None
            return client

    def configure(self, config, timeout: float = 30.0):
        snapshot = _config_snapshot(config)
        return self._submit(self._configure(snapshot)).result(timeout=timeout)

    async def _refresh_rooms(self, snapshot):
        client = await self._configure(snapshot)
        rooms = await client(GetRoomListCommand(), response=True)
        self._set_rooms(rooms)
        return self.rooms_snapshot()

    def refresh_rooms(self, config, timeout: float = 30.0):
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._refresh_rooms(snapshot)).result(timeout=timeout)

    async def _join_room(self, snapshot, room, private):
        client = await self._configure(snapshot)
        joined_room = await client(
            JoinRoomCommand(room, private=private), response=True
        )
        self._update_room(joined_room)
        return room

    def join_room(self, room, config, private=False, timeout: float = 30.0):
        room = str(room or "").strip()
        if not room:
            raise SoulseekError("Enter a room name.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._join_room(snapshot, room, private)).result(
            timeout=timeout
        )

    async def _leave_room(self, snapshot, room):
        client = await self._configure(snapshot)
        left_room = await client(LeaveRoomCommand(room), response=True)
        self._update_room(left_room)
        return room

    def leave_room(self, room, config, timeout: float = 30.0):
        room = str(room or "").strip()
        if not room:
            raise SoulseekError("Enter a room name.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._leave_room(snapshot, room)).result(timeout=timeout)

    async def _send_room_message(self, snapshot, room, message):
        client = await self._configure(snapshot)
        await client(RoomMessageCommand(room, message))

    def send_room_message(self, room, message, config, timeout: float = 30.0):
        room = str(room or "").strip()
        message = str(message or "").strip()
        if not room:
            raise SoulseekError("Join or select a room first.")
        if not message:
            raise SoulseekError("Enter a message.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        self._submit(self._send_room_message(snapshot, room, message)).result(
            timeout=timeout
        )

    async def _send_private_message(self, snapshot, username, message):
        client = await self._configure(snapshot)
        await client(PrivateMessageCommand(username, message))
        data = {
            "timestamp": int(time.time()),
            "user": username,
            "message": message,
            "outgoing": True,
        }
        self._append_private_message(data)
        return data

    def send_private_message(self, username, message, config, timeout: float = 30.0):
        username = str(username or "").strip()
        message = str(message or "").strip()
        if not username:
            raise SoulseekError("Enter a Soulseek username.")
        if not message:
            raise SoulseekError("Enter a message.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(
            self._send_private_message(snapshot, username, message)
        ).result(timeout=timeout)

    async def _change_friend(self, snapshot, username, add):
        client = await self._configure(snapshot)
        priority_names = {
            value.casefold() for value in snapshot.get("priority_users", [])
        }
        friends = {
            value
            for value in client.settings.users.friends
            if value.casefold() not in priority_names
        }
        if add:
            friends.add(username)
        else:
            friends = {
                friend for friend in friends if friend.casefold() != username.casefold()
            }
        client.settings.users.friends = friends | set(
            snapshot.get("priority_users", [])
        )
        updated = dict(snapshot)
        updated["friends"] = sorted(friends, key=str.casefold)
        self._active_signature = _signature(updated)
        self._set_friends(updated["friends"], client)
        return self.friends_snapshot()

    async def _change_priority(self, snapshot, username, add):
        client = await self._configure(snapshot)
        priority = set(snapshot.get("priority_users", []))
        if add:
            priority.add(username)
        else:
            priority = {
                value for value in priority
                if value.casefold() != username.casefold()
            }
        client.settings.users.friends = set(snapshot["friends"]) | priority
        updated = dict(snapshot)
        updated["priority_users"] = sorted(priority, key=str.casefold)
        self._active_signature = _signature(updated)
        return updated["priority_users"]

    def change_priority(self, username, config, add, timeout: float = 30.0):
        username = str(username or "").strip()
        if not username:
            raise SoulseekError("Enter a Soulseek username.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(
            self._change_priority(snapshot, username, add)
        ).result(timeout=timeout)

    @staticmethod
    def _file_item(username, directory, file_data, locked=False):
        filename = str(file_data.filename)
        remote_path = (
            filename
            if ntpath.dirname(filename)
            else ntpath.join(str(directory), filename)
        )
        extension = _file_extension(file_data)
        return {
            "title": ntpath.basename(remote_path) or remote_path,
            "kind": "soulseek",
            "source": SOURCE,
            "username": str(username),
            "artist": str(username),
            "remote_path": remote_path,
            "folder": ntpath.dirname(remote_path),
            "format": extension.upper(),
            "extension": extension,
            "size_bytes": int(file_data.filesize or 0),
            "file_size": _format_size(file_data.filesize),
            "locked": bool(locked),
        }

    async def _browse_user(self, snapshot, username):
        client = await self._configure(snapshot)
        public, locked = await client(
            PeerGetSharesCommand(username), response=True
        )
        directories = []
        for is_locked, entries in ((False, public), (True, locked)):
            for directory in entries:
                name = str(directory.name)
                directories.append(
                    {
                        "name": name,
                        "locked": is_locked,
                        "files": [
                            self._file_item(username, name, file_data, is_locked)
                            for file_data in directory.files
                        ],
                    }
                )
        directories.sort(key=lambda row: row["name"].casefold())
        return directories

    def browse_user(self, username, config, timeout: float = 60.0):
        username = str(username or "").strip()
        if not username:
            raise SoulseekError("Enter a Soulseek username.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._browse_user(snapshot, username)).result(
            timeout=timeout
        )

    async def _browse_directory(self, snapshot, username, directory):
        client = await self._configure(snapshot)
        entries = await client(
            PeerGetDirectoryContentCommand(username, directory), response=True
        )
        return [
            {
                "name": str(entry.name),
                "locked": False,
                "files": [
                    self._file_item(username, entry.name, file_data)
                    for file_data in entry.files
                ],
            }
            for entry in entries
        ]

    def browse_directory(
        self, username, directory, config, timeout: float = 60.0
    ):
        username = str(username or "").strip()
        directory = str(directory or "").strip()
        if not username or not directory:
            raise SoulseekError("Choose a Soulseek user and folder.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(
            self._browse_directory(snapshot, username, directory)
        ).result(timeout=timeout)

    async def _user_profile(self, snapshot, username):
        client = await self._configure(snapshot)
        info, stats = await asyncio.gather(
            client(PeerGetUserInfoCommand(username), response=True),
            client(GetUserStatsCommand(username), response=True),
        )
        user = client.users.get_user_object(username)
        permissions = getattr(info.upload_permissions, "value", None)
        return {
            "username": username,
            "description": str(info.description or ""),
            "picture": info.picture,
            "status": self._status_name(user),
            "has_slots_free": bool(info.has_slots_free),
            "upload_slots": int(info.upload_slots or 0),
            "queue_length": int(info.queue_length or 0),
            "upload_permissions": str(permissions or "Unknown").replace("_", " ").title(),
            "average_speed": int(stats.avg_speed or 0),
            "uploads": int(stats.uploads or 0),
            "shared_files": int(stats.shared_file_count or 0),
            "shared_folders": int(stats.shared_folder_count or 0),
        }

    def user_profile(self, username, config, timeout: float = 45.0):
        username = str(username or "").strip()
        if not username:
            raise SoulseekError("Enter a Soulseek username.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._user_profile(snapshot, username)).result(
            timeout=timeout
        )

    def change_friend(self, username, config, add, timeout: float = 30.0):
        username = str(username or "").strip()
        if not username:
            raise SoulseekError("Enter a Soulseek username.")
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(self._change_friend(snapshot, username, add)).result(
            timeout=timeout
        )

    async def _stop_upload(self, key):
        client = self._client
        if client is None:
            raise SoulseekError("Soulseek is not connected.")
        transfer = next(
            (
                item
                for item in client.transfers.get_uploads()
                if self._transfer_key(item) == key
            ),
            None,
        )
        if transfer is None:
            return False
        await client.transfers.abort(transfer)
        self._publish_uploads(client)
        return True

    def stop_upload(self, key, timeout: float = 30.0):
        return self._submit(self._stop_upload(str(key))).result(timeout=timeout)

    async def _search(self, snapshot, query, media_kind, timeout_s, stop_event=None,
                      on_batch=None):
        client = await self._configure(snapshot)
        request = await client.searches.search(query)
        extensions = MEDIA_EXTENSIONS.get(media_kind, MEDIA_EXTENSIONS["media"])

        def sort_key(item):
            return (
                not item["has_free_slots"],
                item["queue_size"],
                -item["average_speed"],
                item["title"].casefold(),
            )

        if on_batch is None:
            deadline = asyncio.get_running_loop().time() + max(0.25, float(timeout_s))
            try:
                while asyncio.get_running_loop().time() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        break
                    await asyncio.sleep(0.1)
                items = [
                    _result_item(result, file_data)
                    for result in request.results
                    for file_data in result.shared_items
                    if _file_extension(file_data) in extensions
                ]
                items.sort(key=sort_key)
                return items[: max(1, snapshot["max_results"])]
            finally:
                try:
                    client.searches.remove_request(request)
                except KeyError:
                    pass

        # Streaming search: hand each new result over as the peer answers,
        # on this service thread, until the caller stops the search. The
        # caller folds the batches into the UI a few at a time, so a long
        # Soulseek search never dumps thousands of rows on a screen reader
        # in one go.
        seen = set()
        try:
            while stop_event is None or not stop_event.is_set():
                batch = []
                for result in request.results:
                    for file_data in result.shared_items:
                        if _file_extension(file_data) not in extensions:
                            continue
                        item = _result_item(result, file_data)
                        key = (
                            item["username"].casefold(),
                            item["remote_path"].casefold(),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        batch.append(item)
                if batch:
                    batch.sort(key=sort_key)
                    on_batch(batch)
                await asyncio.sleep(0.25)
            return []
        finally:
            try:
                client.searches.remove_request(request)
            except KeyError:
                pass

    def search(self, query, config, media_kind, timeout_s, stop_event=None,
               on_batch=None):
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            return []
        if on_batch is not None:
            # A streaming search runs until the caller stops it.
            return self._submit(
                self._search(
                    snapshot, query, media_kind, 0.0, stop_event, on_batch=on_batch
                )
            ).result()
        wait = max(10.0, float(timeout_s) + 30.0)
        return self._submit(
            self._search(snapshot, query, media_kind, timeout_s, stop_event)
        ).result(timeout=wait)

    async def _download(self, snapshot, item, progress_cb, cancel_event):
        client = await self._configure(snapshot)
        transfer = await client.transfers.download(
            str(item["username"]), str(item["remote_path"])
        )
        target_relative = str(item.get("target_relative_path") or "").strip()
        if target_relative and transfer.local_path is None:
            safe_parts = []
            for raw_part in target_relative.replace("/", "\\").split("\\"):
                part = raw_part.strip().strip(".")
                if not part or part == "..":
                    continue
                part = "".join(
                    "_" if char in '<>:"/\\|?*' else char for char in part
                ).rstrip(" .")
                if part:
                    safe_parts.append(part)
            if safe_parts:
                candidate = os.path.abspath(
                    os.path.join(snapshot["download_dir"], *safe_parts)
                )
                root = os.path.abspath(snapshot["download_dir"])
                if os.path.commonpath((root, candidate)) == root:
                    transfer.local_path = candidate
        while True:
            if client is not self._client:
                raise SoulseekSettingsChanged(SETTINGS_CHANGED_MESSAGE)
            if cancel_event is not None and cancel_event.is_set():
                try:
                    await client.transfers.abort(transfer)
                finally:
                    raise SoulseekDownloadCancelled()

            snapshot_now = transfer.take_progress_snapshot()
            state = snapshot_now.state
            total = int(transfer.filesize or item.get("size_bytes") or 0)
            done = int(snapshot_now.bytes_transfered or 0)
            speed = float(snapshot_now.speed or 0)
            eta = (max(total - done, 0) / speed) if total and speed else None
            if progress_cb is not None:
                progress_cb(
                    {
                        "downloaded": done,
                        "total": total,
                        "speed": speed,
                        "eta": eta,
                        "state": state.name.replace("_", " ").title(),
                        "queue_position": transfer.place_in_queue,
                        "local_path": transfer.local_path,
                    }
                )

            if state == TransferState.COMPLETE:
                return transfer.local_path
            if state == TransferState.FAILED:
                reason = (
                    snapshot_now.fail_reason
                    or transfer.fail_reason
                    or "Transfer failed"
                )
                raise _transfer_failure(reason)
            if state == TransferState.ABORTED:
                if cancel_event is not None and cancel_event.is_set():
                    raise SoulseekDownloadCancelled()
                reason = (
                    snapshot_now.abort_reason
                    or transfer.abort_reason
                    or "Transfer aborted"
                )
                raise _transfer_failure(reason)
            await asyncio.sleep(0.25)

    def download(self, item, config, progress_cb=None, cancel_event=None):
        snapshot = _config_snapshot(config)
        if not snapshot["enabled"]:
            raise SoulseekError("Soulseek is disabled in Settings.")
        return self._submit(
            self._download(snapshot, item, progress_cb, cancel_event)
        ).result()

    async def _delayed_rescan(self):
        try:
            await asyncio.sleep(2)
            client = self._client
            if client is not None:
                await client.shares.scan()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background sharing is best effort
            logger.exception("Soulseek library rescan failed")

    def schedule_rescan(self):
        if self._loop is None or self._client is None:
            return

        def schedule():
            if self._rescan_task is not None and not self._rescan_task.done():
                self._rescan_task.cancel()
            self._rescan_task = self._loop.create_task(self._delayed_rescan())

        self._loop.call_soon_threadsafe(schedule)

    async def _shutdown(self):
        if self._rescan_task is not None:
            self._rescan_task.cancel()
            await asyncio.gather(self._rescan_task, return_exceptions=True)
            self._rescan_task = None
        await self._stop_client()

    def shutdown(self):
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            self._submit(self._shutdown()).result(timeout=15)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            self._loop = None
            self._thread = None


_SERVICE = _Service(os.path.join(app_data_dir(), "soulseek-history.json"))


def configure(config, timeout: float = 30.0):
    return _SERVICE.configure(config, timeout=timeout)


def search(query, config, media_kind, timeout_s, stop_event=None, on_batch=None):
    return _SERVICE.search(
        query, config, media_kind, timeout_s, stop_event, on_batch=on_batch
    )


def download(item, config, progress_cb: Callable | None = None, cancel_event=None):
    return _SERVICE.download(item, config, progress_cb, cancel_event)


def schedule_rescan():
    _SERVICE.schedule_rescan()


def add_listener(listener):
    _SERVICE.add_listener(listener)


def remove_listener(listener):
    _SERVICE.remove_listener(listener)


def rooms_snapshot():
    return _SERVICE.rooms_snapshot()


def room_messages_snapshot():
    return _SERVICE.room_messages_snapshot()


def private_messages_snapshot():
    return _SERVICE.private_messages_snapshot()


def friends_snapshot():
    return _SERVICE.friends_snapshot()


def uploads_snapshot():
    return _SERVICE.uploads_snapshot()


def refresh_rooms(config, timeout: float = 30.0):
    return _SERVICE.refresh_rooms(config, timeout=timeout)


def join_room(room, config, private=False, timeout: float = 30.0):
    return _SERVICE.join_room(room, config, private=private, timeout=timeout)


def leave_room(room, config, timeout: float = 30.0):
    return _SERVICE.leave_room(room, config, timeout=timeout)


def send_room_message(room, message, config, timeout: float = 30.0):
    return _SERVICE.send_room_message(room, message, config, timeout=timeout)


def send_private_message(username, message, config, timeout: float = 30.0):
    return _SERVICE.send_private_message(username, message, config, timeout=timeout)


def add_friend(username, config, timeout: float = 30.0):
    return _SERVICE.change_friend(username, config, True, timeout=timeout)


def remove_friend(username, config, timeout: float = 30.0):
    return _SERVICE.change_friend(username, config, False, timeout=timeout)


def give_free_slot(username, config, timeout: float = 30.0):
    return _SERVICE.change_priority(username, config, True, timeout=timeout)


def remove_free_slot(username, config, timeout: float = 30.0):
    return _SERVICE.change_priority(username, config, False, timeout=timeout)


def browse_user(username, config, timeout: float = 60.0):
    return _SERVICE.browse_user(username, config, timeout=timeout)


def browse_directory(username, directory, config, timeout: float = 60.0):
    return _SERVICE.browse_directory(
        username, directory, config, timeout=timeout
    )


def user_profile(username, config, timeout: float = 45.0):
    return _SERVICE.user_profile(username, config, timeout=timeout)


def stop_upload(key, timeout: float = 30.0):
    return _SERVICE.stop_upload(key, timeout=timeout)


def shutdown():
    _SERVICE.shutdown()
