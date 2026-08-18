# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Local restore signal used by blindDL's single-instance launcher."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

from .config import app_data_dir


RESTORE_MESSAGE = b"restore\n"
OPEN_PREFIX = b"open "
ENDPOINT_FILE = "running-instance.json"
# A magnet link carrying a display name and a dozen trackers runs to well
# over a kilobyte, so the read is bounded generously rather than tightly --
# but it is still bounded, because the socket faces the loopback interface
# and an unbounded read is an unbounded allocation.
MAX_MESSAGE_BYTES = 16384


def open_message(link: str) -> bytes:
    """The wire form of "open this link". JSON, so a link keeps its bytes.

    A magnet can hold anything a URL can, newlines included once it has been
    through a file manager, and the message is newline-terminated.
    """
    return OPEN_PREFIX + json.dumps(link).encode("utf-8") + b"\n"


def endpoint_path() -> Path:
    return Path(app_data_dir()) / ENDPOINT_FILE


class RestoreServer:
    """Listen only on loopback and ask the GUI thread to restore its frame."""

    def __init__(self, on_restore, path: str | os.PathLike | None = None,
                 on_open=None):
        self.on_restore = on_restore
        # Called with a magnet link or torrent path when a second launch was
        # a file manager opening one. None means this build has nowhere to
        # put it, and such a message is answered by restoring the window --
        # which is at least the half of it the user can see.
        self.on_open = on_open
        self.path = Path(path) if path is not None else endpoint_path()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = 0

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.5)
        self._socket = listener
        self.port = int(listener.getsockname()[1])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"port": self.port, "pid": os.getpid()}),
                encoding="utf-8",
            )
        except OSError:
            listener.close()
            self._socket = None
            raise
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="blinddl-instance-restore",
        )
        self._thread.start()
        return self

    def _run(self):
        listener = self._socket
        while not self._stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    message = self._read_message(connection)
                except OSError:
                    continue
            self._dispatch(message)

    @staticmethod
    def _read_message(connection) -> bytes:
        """One newline-terminated message, never more than the cap."""
        connection.settimeout(2.0)
        chunks: list[bytes] = []
        total = 0
        while total < MAX_MESSAGE_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).split(b"\n", 1)[0] + b"\n"

    def _dispatch(self, message: bytes) -> None:
        if message == RESTORE_MESSAGE:
            self.on_restore()
            return
        if not message.startswith(OPEN_PREFIX):
            return
        try:
            link = json.loads(message[len(OPEN_PREFIX):].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(link, str) or not link:
            return
        # Whoever asked also wants to see it happen, so the window comes
        # back either way -- and if this build cannot open links, coming
        # back is the whole of what it can do.
        self.on_restore()
        if self.on_open is not None:
            self.on_open(link)

    def stop(self):
        self._stop.set()
        listener, self._socket = self._socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("port", 0)) == self.port:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass


def notify_existing(
    path: str | os.PathLike | None = None, timeout: float = 5.0,
    link: str | None = None,
) -> bool:
    """Ask an existing instance to restore, retrying through startup races.

    With *link*, the running instance is asked to open a magnet or torrent
    as well. That is what makes a file association work at all: Windows
    starts a second blindDL for the file it was told to open, and the queue
    it belongs in lives in the first one.
    """
    path = Path(path) if path is not None else endpoint_path()
    message = RESTORE_MESSAGE if not link else open_message(link)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            port = int(data["port"])
            with socket.create_connection(("127.0.0.1", port), timeout=0.4) as peer:
                peer.sendall(message)
            return True
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            OverflowError,
            json.JSONDecodeError,
        ):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
