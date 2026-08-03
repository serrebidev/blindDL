# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Cross-platform runtime helpers for source and frozen builds."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def prepare_runtime_path() -> None:
    """Put optional Deno/FFmpeg binaries bundled by PyInstaller on PATH."""
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "tools")

    executable_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            executable_dir / "tools",
            executable_dir.parent / "Resources" / "tools",
        ]
    )
    available = [str(path) for path in candidates if path.is_dir()]
    if available:
        os.environ["PATH"] = os.pathsep.join(available + [os.environ.get("PATH", "")])


def _open(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    command = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_folder(path: str) -> None:
    """Open *path* in the platform's file manager."""
    _open(path)


def open_file(path: str) -> None:
    """Open *path* in whatever application the user has set for its type.

    Books are handed to the reader the user already knows -- their browser,
    Adobe Reader, Calibre, or an NVDA add-on -- rather than to a viewer
    blindDL would have to grow.
    """
    _open(path)


def open_magnet(magnet: str) -> None:
    """Hand a magnet link to the user's default BitTorrent client.

    Same reasoning as open_file: the client the user already runs knows
    where their downloads go and how they seed, so blindDL passes the link
    to the registered magnet: handler rather than moving the bytes itself.
    """
    if not str(magnet or "").startswith("magnet:"):
        raise RuntimeError("That is not a magnet link.")
    _open(magnet)
