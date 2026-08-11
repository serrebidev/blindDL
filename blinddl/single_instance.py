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
ENDPOINT_FILE = "running-instance.json"


def endpoint_path() -> Path:
    return Path(app_data_dir()) / ENDPOINT_FILE


class RestoreServer:
    """Listen only on loopback and ask the GUI thread to restore its frame."""

    def __init__(self, on_restore, path: str | os.PathLike | None = None):
        self.on_restore = on_restore
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
                    message = connection.recv(64)
                except OSError:
                    continue
            if message == RESTORE_MESSAGE:
                self.on_restore()

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
    path: str | os.PathLike | None = None, timeout: float = 5.0
) -> bool:
    """Ask an existing instance to restore, retrying through startup races."""
    path = Path(path) if path is not None else endpoint_path()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            port = int(data["port"])
            with socket.create_connection(("127.0.0.1", port), timeout=0.4) as peer:
                peer.sendall(RESTORE_MESSAGE)
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
