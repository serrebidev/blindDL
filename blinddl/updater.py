# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Keeps blindDL's runtime dependencies up to date.

Covers everything the app relies on:
- Python packages (pip): yt-dlp, musicdl, wxPython, python-vlc, and the rest of
  requirements.txt.
- Deno: the JavaScript runtime yt-dlp needs for YouTube extraction.
- ffmpeg: needed for audio extraction and video merging.

Deno and ffmpeg are upgraded through winget when available. All functions
are synchronous and intended for worker threads; progress goes to a log
callback. Nothing here runs at import time.
"""

import os
import shutil
import subprocess
import sys
import time

PIP_PACKAGES = ["musicdl", "wxPython", "python-vlc"]
# yt-dlp tracks the nightly builds (pip pre-releases), so it upgrades with
# --pre in its own command instead of waiting for stable releases.
PRE_PACKAGES = ["yt-dlp"]
# sideb is not on PyPI, so it is installed and refreshed from git. pip
# cannot tell whether a git package is stale, so it is reinstalled on
# every update check (cheap, and only when already installed).
GIT_PACKAGES = {
    "sideb": "git+https://github.com/mosaddiqdev/sideb",
    # Adult providers are refreshed when present; frozen builds update them
    # together with the application release.
    "aebndl": "git+https://github.com/hyper440/aebn-vod-downloader",
    "eaf_base_api": "git+https://github.com/EchterAlsFake/eaf_base_api",
    "unofficial-api-for-beeg": "git+https://github.com/EchterAlsFake/unofficial-api-for-beeg",
    "unofficial-api-for-eporner": "git+https://github.com/EchterAlsFake/unofficial-api-for-eporner",
    "unofficial-api-for-hqporner": "git+https://github.com/EchterAlsFake/unofficial-api-for-hqporner",
    "unofficial-api-for-missav": "git+https://github.com/EchterAlsFake/unofficial-api-for-missav",
    "porngo_api": "git+https://github.com/EchterAlsFake/unofficial-api-for-porngo",
    "unofficial-api-for-pornhub": "git+https://github.com/EchterAlsFake/unofficial-api-for-pornhub",
    "unofficial-api-for-porntrex": "git+https://github.com/EchterAlsFake/unofficial-api-for-porntrex",
    "unofficial-api-for-redtube": "git+https://github.com/EchterAlsFake/unofficial-api-for-redtube",
    "Sex_API": "git+https://github.com/EchterAlsFake/unofficial-api-for-sex.com",
    "unofficial-api-for-spankbang": "git+https://github.com/EchterAlsFake/unofficial-api-for-spankbang",
    "unofficial-api-for-thumbzilla": "git+https://github.com/EchterAlsFake/unofficial-api-for-thumbzilla",
    "unofficial-api-for-tube8": "git+https://github.com/EchterAlsFake/unofficial-api-for-tube8",
    "unofficial-api-for-xfreehd": "git+https://github.com/EchterAlsFake/unofficial-api-for-xfreehd",
    "unofficial-api-for-xhamster": "git+https://github.com/EchterAlsFake/unofficial-api-for-xhamster",
    "unofficial-api-for-xnxx": "git+https://github.com/EchterAlsFake/unofficial-api-for-xnxx",
    "unofficial-api-for-xvideos": "git+https://github.com/EchterAlsFake/unofficial-api-for-xvideos",
    "unofficial-api-for-youporn": "git+https://github.com/EchterAlsFake/unofficial-api-for-youporn",
}
WINGET_PACKAGES = {
    "DenoLand.Deno": "Deno (JavaScript runtime for yt-dlp/YouTube)",
    "Gyan.FFmpeg.Essentials": "ffmpeg (audio/video conversion)",
    "yt-dlp.FFmpeg": "ffmpeg (audio/video conversion)",
}

CREATE_NO_WINDOW = 0x08000000


def _subprocess_options():
    """Return subprocess flags that exist on the current operating system."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def _run(cmd, log, timeout=1800):
    log(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", **_subprocess_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  failed to run: {exc}")
        return False
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.strip().splitlines()[-15:]:
        log(f"  {line}")
    return proc.returncode == 0


def update_pip_packages(log):
    """Upgrade the Python dependencies. Returns True if anything changed."""
    if getattr(sys, "frozen", False):
        log("Bundled Python packages update with blindDL releases.")
        return False
    before = _installed_versions()
    ok = _run([sys.executable, "-m", "pip", "install", "--upgrade",
               "--quiet", *PIP_PACKAGES], log)
    ok = _run([sys.executable, "-m", "pip", "install", "--upgrade",
               "--quiet", "--pre", *PRE_PACKAGES], log) and ok
    installed = _installed_versions()
    for name, url in GIT_PACKAGES.items():
        if name in installed:
            ok = _run([sys.executable, "-m", "pip", "install", "--upgrade",
                       "--quiet", url], log) and ok
    after = _installed_versions()
    changed = [p for p in (*PIP_PACKAGES, *PRE_PACKAGES, *GIT_PACKAGES)
               if before.get(p) != after.get(p)]
    if changed:
        log("Updated: " + ", ".join(
            f"{p} {before.get(p) or '?'} -> {after.get(p) or '?'}" for p in changed))
        log("Restart blindDL to use the new versions.")
    elif ok:
        log("All Python packages are already up to date.")
    return bool(changed)


def _installed_versions():
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show",
             *PIP_PACKAGES, *PRE_PACKAGES, *GIT_PACKAGES],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace", **_subprocess_options(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    versions = {}
    name = None
    for line in proc.stdout.splitlines():
        if line.startswith("Name: "):
            name = line[6:].strip()
        elif line.startswith("Version: ") and name:
            versions[name] = line[9:].strip()
    return versions


def update_winget_packages(log):
    """Upgrade external tools through the platform package manager."""
    if sys.platform == "win32":
        if shutil.which("winget") is None:
            log("winget not found; skipping Deno/ffmpeg updates.")
            return
        for package_id, description in WINGET_PACKAGES.items():
            log(f"Checking {description}...")
            _run([
                "winget", "upgrade", "--id", package_id, "--exact",
                "--silent", "--accept-package-agreements",
                "--accept-source-agreements", "--disable-interactivity",
            ], log)
    elif sys.platform == "darwin" and shutil.which("brew"):
        _run(["brew", "upgrade", "deno", "ffmpeg"], log)
    else:
        log("Deno and ffmpeg are managed by the installed package on this system.")


def ensure_deno(log):
    """Make sure Deno is installed at all (yt-dlp needs it for YouTube)."""
    if shutil.which("deno") is not None:
        return True
    log("Deno is not installed; installing it now (required by yt-dlp for YouTube)...")
    if sys.platform == "win32" and shutil.which("winget"):
        return _run([
            "winget", "install", "--id", "DenoLand.Deno", "--exact",
            "--silent", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity",
        ], log)
    if sys.platform == "darwin" and shutil.which("brew"):
        return _run(["brew", "install", "deno"], log)
    log("Deno is unavailable; install it from https://deno.com/runtime")
    return False


def run_full_update(log, include_winget=True):
    """Update everything. log(str) receives human-readable progress lines."""
    started = time.time()
    log("Checking for updates...")
    changed = update_pip_packages(log)
    if include_winget:
        update_winget_packages(log)
    log(f"Update check finished in {time.time() - started:.0f} seconds.")
    return changed
