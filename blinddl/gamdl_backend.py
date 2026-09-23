# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Optional integration with an externally installed gamdl.

gamdl's current PlayReady dependency pins an older cryptography line than
blindDL intentionally ships. Running gamdl as its own executable keeps those
dependency graphs isolated while still letting blindDL use the current gamdl
Apple Music downloader when it is installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

MIN_VERSION = (3, 9, 1)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _candidate_paths():
    explicit = os.environ.get("BLINDDL_GAMDL", "").strip()
    if explicit:
        yield explicit

    found = shutil.which("gamdl")
    if found:
        yield found

    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            yield os.path.join(local, "gamdl-venv", "Scripts", "gamdl.exe")
        roaming = os.environ.get("APPDATA", "")
        if roaming:
            scripts = Path(roaming) / "Python"
            if scripts.is_dir():
                for child in sorted(scripts.glob("Python*/Scripts/gamdl.exe"), reverse=True):
                    yield str(child)
    else:
        yield str(Path.home() / ".local" / "bin" / "gamdl")
        yield str(Path.home() / ".local" / "pipx" / "venvs" / "gamdl" / "bin" / "gamdl")


def _version(executable):
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search((result.stdout or "") + "\n" + (result.stderr or ""))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def find_gamdl():
    """Return a current gamdl executable, or None when none is usable."""
    seen = set()
    for candidate in _candidate_paths():
        path = os.path.abspath(os.path.expanduser(candidate))
        key = os.path.normcase(path)
        if key in seen or not os.path.isfile(path):
            continue
        seen.add(key)
        version = _version(path)
        if version and version >= MIN_VERSION:
            return path
    return None


def available():
    return find_gamdl() is not None


def download(url, out_dir, config=None, cancel_event=None):
    """Download an Apple Music URL through gamdl.

    The caller owns catalogue/search behaviour. This function only hands the
    URL to a current external gamdl process and keeps its dependency
    environment separate from blindDL's.
    """
    executable = find_gamdl()
    if not executable:
        raise RuntimeError(
            "gamdl 3.9.1 or newer is not installed. Install gamdl separately "
            "or set BLINDDL_GAMDL to its executable."
        )

    cookies = str((config or {}).get("apple_music_cookies") or "").strip()
    if not cookies or not os.path.isfile(cookies):
        raise RuntimeError(
            "No Apple Music cookies file configured. Export your browser "
            "cookies while logged in at music.apple.com (Settings, Accounts, "
            "Apple Music)."
        )

    os.makedirs(out_dir, exist_ok=True)
    command = [
        executable,
        "-n",
        "--no-exceptions",
        "--log-level",
        "WARNING",
        "--cookies-path",
        cookies,
        "--output-path",
        os.path.abspath(out_dir),
        url,
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    log_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="blinddl-gamdl-", suffix=".log", delete=False
        ) as log_file:
            log_path = log_file.name
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )

        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise RuntimeError("Download cancelled")
            time.sleep(0.1)

        if process.returncode:
            detail = ""
            if log_path:
                try:
                    detail = Path(log_path).read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    pass
            tail = detail[-1200:] if detail else "gamdl exited without an error message."
            raise RuntimeError(f"gamdl could not download the Apple Music URL: {tail}")

        return os.path.abspath(out_dir)
    finally:
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass
