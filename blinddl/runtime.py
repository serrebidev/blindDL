# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Cross-platform runtime helpers for source and frozen builds."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


# Directories this process has already put on PATH. Every music and YouTube
# search asks whether Deno and Node have arrived yet, and each ask lands
# here; without this, PATH grew by seven entries per search until the
# environment handed to yt-dlp and ffmpeg no longer fitted in the 32,767
# characters Windows allows -- and every shutil.which in between had a
# longer list to walk.
_ON_PATH: set[str] = set()


def prepare_runtime_path() -> None:
    """Put bundled and package-manager-installed media tools on PATH.

    Safe to call as often as it is asked for: a directory is prepended once,
    and a tool installed later still joins the front of PATH the next time
    somebody looks for it.
    """
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
    if sys.platform == "win32":
        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", ""))
        candidates.extend([
            Path.home() / ".deno" / "bin",
            local_appdata / "Microsoft" / "WinGet" / "Links",
            local_appdata / "Microsoft" / "WindowsApps",
            program_files / "nodejs",
            program_files / "VideoLAN" / "VLC",
        ])
        package_root = local_appdata / "Microsoft" / "WinGet" / "Packages"
        package_tools = (
            ("DenoLand.Deno_*", "deno.exe"),
            ("Gyan.FFmpeg.Essentials_*", "ffmpeg.exe"),
            ("OpenJS.NodeJS.LTS_*", "node.exe"),
        )
        for package_pattern, executable in package_tools:
            # This walks a whole WinGet package tree -- thousands of entries
            # for FFmpeg or Node -- and it exists only to make one tool
            # findable. Once the tool can be found, there is nothing to look
            # for; a tool installed later still fails this test until the
            # walk has put it on PATH.
            if shutil.which(os.path.splitext(executable)[0]):
                continue
            matches = package_root.glob(f"{package_pattern}/**/{executable}")
            candidates.extend(path.parent for path in matches if path.is_file())
    else:
        candidates.extend([
            Path.home() / ".deno" / "bin",
            Path.home() / ".local" / "bin",
            Path("/usr/local/bin"),
        ])
        if sys.platform == "darwin":
            candidates.extend([
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/Applications/VLC.app/Contents/MacOS"),
            ])
    available: list[str] = []
    for path in candidates:
        entry = str(path)
        if entry in _ON_PATH or entry in available or not path.is_dir():
            continue
        available.append(entry)
    if available:
        _ON_PATH.update(available)
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
