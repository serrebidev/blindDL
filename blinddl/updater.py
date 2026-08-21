# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Keeps blindDL's runtime dependencies up to date.

Covers everything the app relies on:
- Python packages (pip): yt-dlp, wxPython, python-vlc, ytmusicapi,
  and the rest of requirements.txt. Side B is not among them -- it is
  vendored in ./sideb and travels with blindDL's own releases.
- Deno, FFmpeg/FFprobe, Node.js and VLC: installed through WinGet, Homebrew,
  or the Linux system package manager.

Frozen releases install large native tools in the background instead of
duplicating them in every BlindDL update. All functions are synchronous and
intended for worker threads; progress goes to a log callback. Nothing here runs
at import time.
"""

import os
import hashlib
import json
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__
from .config import app_data_dir

# ytmusicapi is here because Side B is vendored: it breaks whenever YouTube
# Music changes, and there is no upstream release to pull the fix from.
# musicdl and Side B are vendored and update with blindDL itself.
PIP_PACKAGES = ["wxPython", "python-vlc", "ytmusicapi", "cryptography"]
# Upgraded only when already present, like the git packages below: libtorrent
# is the optional in-app torrent engine, and a user who has never turned it
# on should not have it installed behind their back.
OPTIONAL_PACKAGES = ["libtorrent"]
# yt-dlp tracks the nightly builds (pip pre-releases), so it upgrades with
# --pre in its own command instead of waiting for stable releases.
PRE_PACKAGES = ["yt-dlp"]
# sideb is not on PyPI, so it is installed and refreshed from git. pip
# cannot tell whether a git package is stale, so it is reinstalled on
# every update check (cheap, and only when already installed).
GIT_PACKAGES = {
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
    "DenoLand.Deno": (
        "Deno (JavaScript runtime for yt-dlp/YouTube)", ("deno",)),
    "Gyan.FFmpeg.Essentials": (
        "FFmpeg (audio/video conversion)", ("ffmpeg", "ffprobe")),
    "OpenJS.NodeJS.LTS": (
        "Node.js LTS (music-source JavaScript)", ("node",)),
    "VideoLAN.VLC": (
        "VLC media player (audio preview)", ("vlc",)),
}
HOMEBREW_PACKAGES = {
    "DenoLand.Deno": ("deno", False),
    "Gyan.FFmpeg.Essentials": ("ffmpeg", False),
    "OpenJS.NodeJS.LTS": ("node", False),
    "VideoLAN.VLC": ("vlc", True),
}
LINUX_PACKAGES = {
    "apt-get": {
        "Gyan.FFmpeg.Essentials": "ffmpeg",
        "OpenJS.NodeJS.LTS": "nodejs",
        "VideoLAN.VLC": "vlc",
    },
    "dnf": {
        "Gyan.FFmpeg.Essentials": "ffmpeg",
        "OpenJS.NodeJS.LTS": "nodejs",
        "VideoLAN.VLC": "vlc",
    },
    "pacman": {
        "Gyan.FFmpeg.Essentials": "ffmpeg",
        "OpenJS.NodeJS.LTS": "nodejs",
        "VideoLAN.VLC": "vlc",
    },
    "zypper": {
        "Gyan.FFmpeg.Essentials": "ffmpeg",
        "OpenJS.NodeJS.LTS": "nodejs",
        "VideoLAN.VLC": "vlc",
    },
}
DENO_CHANNEL_URL = "https://dl.deno.land/release-latest.txt"
DENO_RELEASE_URL = "https://dl.deno.land/release/{version}/{asset}.zip"
DENO_TARGETS = {
    ("windows", "amd64"): "deno-x86_64-pc-windows-msvc",
    ("windows", "arm64"): "deno-aarch64-pc-windows-msvc",
    ("linux", "amd64"): "deno-x86_64-unknown-linux-gnu",
    ("linux", "arm64"): "deno-aarch64-unknown-linux-gnu",
    ("darwin", "amd64"): "deno-x86_64-apple-darwin",
    ("darwin", "arm64"): "deno-aarch64-apple-darwin",
}
_external_tools_lock = threading.Lock()
# Packages this process has already tried to install. Cleared by an update
# check, which is the user asking for another go.
_install_attempted: set[str] = set()

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
# Without this the helper is a child of blindDL for as long as it runs,
# and a job object that kills its processes on close takes the helper
# down with the blindDL that started it -- an update that does nothing
# at all, with nothing to show for it.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
RELEASE_API_URL = "https://api.github.com/repos/serrebidev/blindDL/releases/latest"
# Written by the Windows helper scripts once they have finished, win or lose,
# and read on the next start. An update that dies between two processes has
# nowhere else to say so, which is how a silent failure used to look like
# nothing at all having happened.
UPDATE_RESULT_NAME = "last-update-result.json"
UPDATE_USER_AGENT = f"blindDL/{__version__}"
DOWNLOAD_BLOCK = 256 * 1024
# Download progress is spoken, not drawn, so it is reported in coarse steps:
# a screen reader reading every percent of a hundred-megabyte package would
# say nothing else until it finished.
PROGRESS_PERCENT_STEP = 10
# What a server that sends no Content-Length gets instead of percentages.
PROGRESS_BYTES_STEP = 16 * 1024 * 1024
WINDOWS_UPDATE_LOG_NAME = "windows-update-helper.log"


class UpdateError(RuntimeError):
    """An application update could not be checked, verified, or started."""


@dataclass(frozen=True)
class AppUpdate:
    """One newer GitHub release and its package for this installation."""

    version: str
    page_url: str
    package_name: str
    package_url: str
    checksum_name: str
    checksum_url: str


def _subprocess_options() -> dict[str, Any]:
    """Return subprocess flags that exist on the current operating system."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def _version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", str(value))[:3]]
    return tuple((parts + [0, 0, 0])[:3])


def _release_platform():
    system = {"win32": "windows", "darwin": "macos"}.get(
        sys.platform, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return system, arch


def _is_debian_family():
    return Path("/etc/debian_version").is_file() or shutil.which("dpkg") is not None


def _windows_installed_build():
    """True when the running executable is owned by the Inno installer."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg  # noqa: PLC0415 - Windows-only standard library
    except ImportError:
        return False
    subkey = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        r"\{656F03B0-B9A0-5C26-8F6C-68577B4F9D7D}_is1"
    )
    views = (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
    executable_dir = Path(sys.executable).resolve().parent
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                    location, _kind = winreg.QueryValueEx(key, "InstallLocation")
            except OSError:
                continue
            try:
                if Path(location).resolve() == executable_dir:
                    return True
            except OSError:
                continue
    return False


def _asset_map(release):
    return {
        str(asset.get("name") or ""): str(asset.get("browser_download_url") or "")
        for asset in release.get("assets", [])
        if asset.get("name") and asset.get("browser_download_url")
    }


def _select_update(release):
    version = str(release.get("tag_name") or "").lstrip("vV")
    if not version:
        raise UpdateError("The latest GitHub release has no version tag.")
    if _version_tuple(version) <= _version_tuple(__version__):
        return None

    system, arch = _release_platform()
    assets = _asset_map(release)
    if system == "windows":
        installed = _windows_installed_build()
        extension = "exe" if installed else "zip"
        # Windows releases are currently x64. Windows 11 on ARM runs them
        # through its x64 compatibility layer, so an ARM machine must use the
        # package the running application was built from instead of looking
        # for a windows-arm64 asset that does not exist.
        if arch == "arm64" and not any(
            name.endswith(f"windows-arm64.{extension}") for name in assets
        ):
            arch = "x64"
        suffix = f"windows-{arch}.{extension}"
    elif system == "macos":
        suffix = f"macos-{arch}.dmg"
    else:
        deb_arch = "arm64" if arch == "arm64" else "amd64"
        deb_suffix = f"_{deb_arch}.deb"
        has_deb = any(name.endswith(deb_suffix) for name in assets)
        suffix = (deb_suffix if _is_debian_family() and has_deb
                  else f"linux-{arch}.tar.gz")

    package_name = next((name for name in assets if name.endswith(suffix)), "")
    checksum_name = f"SHA256SUMS-{system}-{arch}.txt"
    if not package_name or checksum_name not in assets:
        raise UpdateError(
            f"blindDL {version} has no complete package for {system} {arch}."
        )
    return AppUpdate(
        version=version,
        page_url=str(release.get("html_url") or ""),
        package_name=package_name,
        package_url=assets[package_name],
        checksum_name=checksum_name,
        checksum_url=assets[checksum_name],
    )


def _open_url(url, timeout=30):
    if urlparse(str(url)).scheme.casefold() != "https":
        raise UpdateError("The update server returned a non-HTTPS URL.")
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": UPDATE_USER_AGENT,
    })
    try:
        response = urlopen(request, timeout=timeout)  # nosec B310
    except (HTTPError, URLError, OSError) as exc:
        raise UpdateError(f"Could not reach the update server: {exc}") from exc
    final_url = getattr(response, "geturl", lambda: url)()
    if urlparse(str(final_url)).scheme.casefold() != "https":
        response.close()
        raise UpdateError("The update server redirected to a non-HTTPS URL.")
    return response


def check_for_app_update(log=lambda _line: None):
    """Return the newest applicable release, or None when already current."""
    log(f"Checking for a BlindDL update (current version {__version__})...")
    try:
        with _open_url(RELEASE_API_URL) as response:
            release = json.load(response)
    except (ValueError, TypeError) as exc:
        raise UpdateError("The update server returned invalid release data.") from exc
    update = _select_update(release)
    if update is None:
        log(f"BlindDL {__version__} is up to date.")
    else:
        log(f"BlindDL {update.version} is available.")
    return update


def _format_size(size):
    """Spoken size of a download: '9.4 MB', '112 MB'."""
    megabytes = float(size) / (1024 * 1024)
    return f"{megabytes:.1f} MB" if megabytes < 10 else f"{megabytes:.0f} MB"


def _content_length(response):
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    try:
        return max(0, int(headers.get("Content-Length") or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _progress_reporter(label, report, step=PROGRESS_PERCENT_STEP):
    """Return a _download callback that reports whole steps of progress.

    A download with nothing to say about itself is the one thing an update
    cannot be: there is no window to look at, so silence and a stall are
    indistinguishable. Each *step* percent is reported once, and a server
    that sends no Content-Length gets megabytes instead of percentages.
    """
    state = {"percent": step, "bytes": PROGRESS_BYTES_STEP}

    def on_progress(done, total):
        if total > 0:
            percent = min(100, int(done * 100 // total))
            if percent < state["percent"]:
                return
            state["percent"] = percent - percent % step + step
            report(f"{label}: {percent} percent of {_format_size(total)}.")
        elif done >= state["bytes"]:
            state["bytes"] = done - done % PROGRESS_BYTES_STEP + PROGRESS_BYTES_STEP
            report(f"{label}: {_format_size(done)} downloaded.")

    return on_progress


def _download(url, destination, digest=None, on_progress=None):
    hasher = hashlib.sha256() if digest is not None else None
    with _open_url(url, timeout=120) as response, open(destination, "wb") as output:
        total = _content_length(response) if on_progress is not None else 0
        done = 0
        while True:
            block = response.read(DOWNLOAD_BLOCK)
            if not block:
                break
            output.write(block)
            done += len(block)
            if hasher is not None:
                hasher.update(block)
            if on_progress is not None:
                on_progress(done, total)
    return hasher.hexdigest() if hasher is not None else ""


def _file_digest(path):
    """SHA-256 of a file already on disk, or "" when it cannot be read."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(DOWNLOAD_BLOCK), b""):
                hasher.update(block)
    except OSError:
        return ""
    return hasher.hexdigest()


def _prune_old_updates(keep):
    """Delete the staging folders of every version except *keep*.

    Each one holds the release package and the tree unpacked from it, so a
    machine that updates often was quietly giving up gigabytes to versions
    it had already moved past.
    """
    for folder in keep.parent.glob("v*"):
        if folder == keep or not folder.is_dir():
            continue
        shutil.rmtree(folder, ignore_errors=True)


def download_app_update(update, log=lambda _line: None, progress=None):
    """Download *update* and verify it against the release checksum file.

    *progress* receives the download-progress lines as they happen; the
    callers speak them. Without one they join the rest of the log, which
    is read but never spoken.
    """
    update_dir = Path(app_data_dir()) / "updates" / f"v{update.version}"
    update_dir.mkdir(parents=True, exist_ok=True)
    checksums_path = update_dir / update.checksum_name
    package_path = update_dir / Path(update.package_name).name
    log(f"Downloading checksum: {update.checksum_name}")
    _download(update.checksum_url, checksums_path)
    expected = ""
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        pieces = line.split(None, 1)
        if len(pieces) == 2 and pieces[1].lstrip("*") == update.package_name:
            expected = pieces[0].lower()
            break
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("The release checksum does not list this package.")
    # A staged update that never installed leaves its package behind. It is
    # the same hundred-odd megabytes the server would send again, and the
    # checksum above is enough to prove it is the right one.
    if package_path.is_file() and _file_digest(package_path) == expected:
        log(f"{update.package_name} was already downloaded and still matches.")
        _prune_old_updates(update_dir)
        return package_path
    log(f"Downloading {update.package_name}...")
    partial = package_path.with_name(package_path.name + ".part")
    try:
        actual = _download(
            update.package_url, partial, digest=True,
            on_progress=_progress_reporter(
                f"blindDL {update.version}",
                progress if progress is not None else log,
            ),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    if actual.lower() != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError(
            "The downloaded update failed its SHA-256 check and was deleted."
        )
    partial.replace(package_path)
    log("The update package passed its SHA-256 integrity check.")
    _prune_old_updates(update_dir)
    return package_path


def _safe_extract_zip(archive, destination):
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise UpdateError("The portable update contains an unsafe path.")
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                raise UpdateError("The portable update contains an unsafe link.")
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output)


# The Windows update helper, which outlives blindDL itself: it waits for
# blindDL to close, replaces the files it was running from, and starts it
# again. It is the helper BlindRSS uses, and it is a batch file for the
# reason BlindRSS made it one -- the work is done by robocopy, which moves a
# folder's *contents* and so never has to rename the folder.
#
# That distinction is the whole fix. A sync client, an open Explorer window,
# or a search indexer holds a *directory* open without holding any file
# inside it, and a rename of that directory then fails for as long as they
# are watching it -- which, for the folder blindDL lives in, is always.
# Three releases running tried to make the rename work: wait longer for
# blindDL to let go, retry the rename for a minute, start the helper from
# somewhere else. The folder was never the thing that had to move.
#
# blindDL is still what reports the outcome, so the helper writes the same
# last-update-result.json the PowerShell helpers wrote, and keeps its log
# only when there is something in it worth reading.
_WINDOWS_HELPER = r"""@echo off
rem blindDL writes this file as it starts an update and then exits. It runs
rem from %TEMP%, never from the folder it replaces: Windows holds a directory
rem open for whichever process has it as its current one, so a helper started
rem in place arrives already blocking the only job it came to do.
setlocal enabledelayedexpansion

set "MODE=%~1"
set "BLINDDL_PID=%~2"
set "INSTALL_DIR=%~3"
set "SOURCE=%~4"
set "BLINDDL_RESULT=%~5"
set "BLINDDL_VERSION=%~6"
set "BLINDDL_LOG=%~7"
set "PS=%~8"

if "%PS%"=="" set "PS=powershell.exe"
set "EXE_NAME=blindDL.exe"
set "EXE=%INSTALL_DIR%\%EXE_NAME%"
set "BACKUP_DIR="
set "DETAIL="
rem Kept beside the result file blindDL already knows how to find, so the
rem list of files an update had to leave behind needs no argument of its own.
for %%D in ("%BLINDDL_RESULT%") do set "LEFTOVERS=%%~dpDleftover-files.txt"

call :main >> "%BLINDDL_LOG%" 2>&1
set "RC=%ERRORLEVEL%"
rem The log is kept only when it has something to explain. blindDL names it
rem to the user when an update fails, so one left behind by an update that
rem worked would send them off to read about a failure that never happened.
if "%RC%"=="0" del /f /q "%BLINDDL_LOG%" >nul 2>nul
rem blindDL is started out here rather than inside :main, and after that
rem delete rather than before it: a process started while the log is being
rem written to inherits the handle it is written through, and blindDL runs
rem for hours. The log of a perfectly good update would have been undeletable
rem for as long as the blindDL it produced was running.
call :restart
if not "%RC%"=="0" exit /b %RC%
rem End the batch context before deleting this copy of the helper, so cmd
rem does not go looking for its next line in a file that is gone. It costs
rem the exit code, which is why it happens only once there is nothing left
rem to report: a helper that failed keeps both its log and itself.
(goto) 2>nul & del /f /q "%~f0" >nul 2>nul
exit /b 0

:main
echo [blindDL update] mode %MODE%, version %BLINDDL_VERSION%
echo [blindDL update] install "%INSTALL_DIR%"
echo [blindDL update] source "%SOURCE%"
if exist "%TEMP%\." (
    pushd "%TEMP%" >nul 2>nul
) else (
    if exist "%SystemRoot%\." pushd "%SystemRoot%" >nul 2>nul
)

if "%MODE%"=="" goto :arguments_missing
if "%INSTALL_DIR%"=="" goto :arguments_missing
if "%SOURCE%"=="" goto :arguments_missing
if "%BLINDDL_RESULT%"=="" goto :arguments_missing

call :wait_for_exit
if errorlevel 1 (
    set "DETAIL=blindDL was still running when the update tried to start"
    goto :fail
)

if /I "%MODE%"=="installed" goto :installed_update

:portable_update
if not exist "%SOURCE%\%EXE_NAME%" (
    set "DETAIL=the staged update does not contain blindDL.exe"
    goto :fail
)
rem A wait, not a gate. A file still held after this is not a reason to
rem abandon the update: the moves below are the real test of whether it can
rem be replaced, and they go on without a file that nothing needs. This once
rem failed the update outright, which meant one file open in a sync client
rem stopped a release from installing before a single thing had been tried.
call :wait_for_unlock

set "BACKUP_DIR=%INSTALL_DIR%.blinddl-update-backup-%RANDOM%%RANDOM%"
if exist "%BACKUP_DIR%\." rmdir /s /q "%BACKUP_DIR%" >nul 2>nul

rem A blindDL that has just closed can leave one of its own DLLs held for a
rem second or two by a virus scanner or the search indexer, and robocopy
rem /MOVE will copy such a file but fail to delete the original. /R retries
rem the copy, not the delete, so the whole move is repeated, waiting a
rem little longer after each go: most holds are over within seconds.
set "ATTEMPT=0"
:drain_attempt
set /a ATTEMPT+=1
echo [blindDL update] Moving the old blindDL files aside, attempt !ATTEMPT!
robocopy "%INSTALL_DIR%" "%BACKUP_DIR%" /E /MOVE /R:10 /W:2 /NFL /NDL /NJH /NJS /NP
if errorlevel 8 (
    set "DETAIL=the old blindDL files could not be moved aside"
    goto :rollback
)
call :verify_drained
if not errorlevel 1 goto :drained
if !ATTEMPT! lss 6 (
    "%PS%" -NoProfile -InputFormat None -Command "Start-Sleep -Seconds !ATTEMPT!" >nul 2>nul
    goto :drain_attempt
)

rem Some hold outlasts the waiting. It is not blindDL -- that has been gone
rem since :wait_for_exit -- but something watching the folder: a sync client
rem hashing a hundred-odd megabytes it has just seen change, a scanner
rem reading the same, the indexer. A reader that opens a file without
rem sharing deletion blocks every way of shifting it: it cannot be deleted,
rem and it cannot be renamed out of the way either. Waiting is the only
rem thing that works on it, and a sync client given a folder this size can
rem outlast any wait worth making somebody sit through.
rem
rem So the update goes on without it. What is left behind is a file of the
rem old version's that the new one does not need -- the new files are about
rem to land on top of the ones that share their names, and a stale extra is
rem loaded by nothing. Compare that against the alternative: rolling back a
rem working update, every time, on any machine that syncs its folder. Where
rem the leftover is a file the new release *does* need, the move below fails
rem on it and that is still a rollback, which is the case that warrants one.
rem The paths are written down so the last of them can be cleared once the
rem hold is over, here if it ends quickly and by blindDL itself if not.
echo [blindDL update] Some old files would not move; going on without them
call :note_leftovers
:drained

echo [blindDL update] Putting the new blindDL files in place
robocopy "%SOURCE%" "%INSTALL_DIR%" /E /MOVE /R:10 /W:2 /NFL /NDL /NJH /NJS /NP
if errorlevel 8 (
    set "DETAIL=the new blindDL files could not be put in place"
    goto :rollback
)
if not exist "%EXE%" (
    set "DETAIL=the new blindDL folder arrived without blindDL.exe"
    goto :rollback
)

call :restore_extras
call :verify_version
if errorlevel 1 (
    set "DETAIL=the folder does not hold blindDL %BLINDDL_VERSION% after the update"
    goto :rollback
)

call :clear_leftovers
rmdir /s /q "%BACKUP_DIR%" >nul 2>nul
if exist "%BACKUP_DIR%\." echo [blindDL update] The previous version is still on disk at "%BACKUP_DIR%"
call :save 1 ""
exit /b 0

:installed_update
if not exist "%SOURCE%" (
    set "DETAIL=the downloaded blindDL installer is missing"
    goto :fail
)
call :wait_for_unlock
echo [blindDL update] Running the blindDL installer
rem Called, not started. "start /wait" hands the program to a second cmd, and
rem a second cmd given a script rather than a program opens a window that
rem stays open -- an update that waits on a console nobody can see. call
rem waits for either kind and hands back its exit code.
call "%SOURCE%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /DIR="%INSTALL_DIR%"
if errorlevel 1 (
    set "DETAIL=the blindDL installer did not finish successfully"
    goto :fail
)
if not exist "%EXE%" (
    set "DETAIL=the installer finished but blindDL.exe is gone"
    goto :fail
)
call :verify_version
if errorlevel 1 (
    set "DETAIL=blindDL %BLINDDL_VERSION% is not the version that is now installed"
    goto :fail
)
call :save 1 ""
exit /b 0

:rollback
echo [blindDL update] Putting the previous blindDL back
if not "%BACKUP_DIR%"=="" if exist "%BACKUP_DIR%\." (
    rem Copied back, not moved. Whatever refused the move in the first place
    rem must not be given the chance to consume the only copy of the version
    rem that was working.
    robocopy "%BACKUP_DIR%" "%INSTALL_DIR%" /E /R:5 /W:2 /NFL /NDL /NJH /NJS /NP
    if errorlevel 8 echo [blindDL update] The previous blindDL could not be put back. It is at "%BACKUP_DIR%"
)
goto :fail

:arguments_missing
set "DETAIL=the update helper was started without the information it needs"
goto :fail

:fail
if "%DETAIL%"=="" set "DETAIL=the update did not finish"
echo [blindDL update] %DETAIL%
rem A rollback puts the old version back whole, so nothing in the folder is
rem left over from anything -- the note would only send blindDL deleting
rem files of the version it is still running.
if not "%LEFTOVERS%"=="" del /f /q "%LEFTOVERS%" >nul 2>nul
call :save 0 "%DETAIL%"
exit /b 1

rem Waiting on blindDL's own process id is not enough. blindDL starts helpers
rem of its own out of the same folder, and any one of them still running
rem holds files this update has to replace, so everything running from the
rem install folder is waited for, asked to close, and finally stopped.
:wait_for_exit
echo [blindDL update] Waiting for blindDL to close
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $install=([IO.Path]::GetFullPath([string]$env:INSTALL_DIR)).TrimEnd('\')+'\'; function Owned { $items=@(Get-Process | Where-Object { try { ([IO.Path]::GetFullPath([string]$_.Path)).StartsWith($install,[StringComparison]::OrdinalIgnoreCase) } catch { $false } }); $id=0; if ([int]::TryParse([string]$env:BLINDDL_PID,[ref]$id)) { $known=Get-Process -Id $id -ErrorAction SilentlyContinue; if ($known) { $items+=$known } }; @($items | Sort-Object Id -Unique) };function Gone([int]$s) { $end=(Get-Date).AddSeconds($s); while ((Get-Date) -lt $end) { if (@(Owned).Count -eq 0) { return $true }; Start-Sleep -Milliseconds 400 }; return (@(Owned).Count -eq 0) }; if (-not (Gone 30)) { foreach ($p in Owned) { try { $null=$p.CloseMainWindow() } catch { } } }; if (-not (Gone 15)) { foreach ($p in Owned) { Write-Host ('Stopping ' + $p.ProcessName + ' ' + $p.Id); Stop-Process -Id $p.Id -Force } }; if (-not (Gone 15)) { Write-Host ('Still running from the blindDL folder: ' + ((Owned | ForEach-Object { $_.ProcessName + ' ' + $_.Id }) -join ', ')); exit 1 }; Start-Sleep -Milliseconds 1500; exit 0"
exit /b %ERRORLEVEL%

rem Windows keeps an executable mapped for a moment after the process that
rem ran it exits, and a virus scanner reading a freshly closed program holds
rem it for longer than that. The files are what have to be free, so the files
rem are what get asked.
:wait_for_unlock
echo [blindDL update] Waiting for the blindDL files to be released
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $install=[string]$env:INSTALL_DIR; $paths=@(Join-Path $install ([string]$env:EXE_NAME)); $inner=Join-Path $install '_internal'; if (Test-Path -LiteralPath $inner) { $paths += @(Get-ChildItem -LiteralPath $inner -File -Filter *.dll | ForEach-Object FullName) }; $held=@(); foreach ($path in $paths) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }; $ok=$false; for ($i=0; $i -lt 20 -and -not $ok; $i++) { try { $handle=[IO.File]::Open($path,'Open','ReadWrite','None'); $handle.Close(); $ok=$true } catch [System.UnauthorizedAccessException] { $ok=$true } catch { Start-Sleep -Milliseconds 500 } }; if (-not $ok) { $held += $path } }; if ($held.Count -gt 0) { Write-Host 'Still held:'; $held | ForEach-Object { Write-Host $_ }; exit 1 }; exit 0"
exit /b %ERRORLEVEL%

:verify_drained
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $install=[string]$env:INSTALL_DIR; $left=@(Get-ChildItem -LiteralPath $install -File -Recurse -Force | Select-Object -First 5); if ($left.Count -gt 0) { Write-Host 'Files left in the blindDL folder:'; $left | ForEach-Object { Write-Host $_.FullName }; exit 1 }; exit 0"
exit /b %ERRORLEVEL%

rem Writes down every file the moves could not shift, so that whoever gets
rem the chance next can finish the job.
:note_leftovers
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $install=[string]$env:INSTALL_DIR; $left=@(Get-ChildItem -LiteralPath $install -File -Recurse -Force | ForEach-Object { $_.FullName }); if ($left.Count -eq 0) { exit 0 }; Write-Host 'Left where they are for now:'; $left | ForEach-Object { Write-Host $_ }; $note=[string]$env:LEFTOVERS; $folder=Split-Path -Parent $note; if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }; Set-Content -LiteralPath $note -Value $left -Encoding UTF8; exit 0"
exit /b 0

rem One more go at the noted files, now that the update itself is done and
rem some seconds have passed. What still will not go stays on the list for
rem blindDL to sweep at its next start; an emptied list is removed, because
rem a list of nothing would have blindDL looking every time.
:clear_leftovers
if "%LEFTOVERS%"=="" exit /b 0
if not exist "%LEFTOVERS%" exit /b 0
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $note=[string]$env:LEFTOVERS; $rest=@(); foreach ($file in @(Get-Content -LiteralPath $note)) { if (-not $file) { continue }; if (-not (Test-Path -LiteralPath $file)) { continue }; try { Remove-Item -LiteralPath $file -Force -ErrorAction Stop } catch { $rest += $file } }; if ($rest.Count -gt 0) { Set-Content -LiteralPath $note -Value $rest -Encoding UTF8; Write-Host ('Still there, left for blindDL: ' + $rest.Count) } else { Remove-Item -LiteralPath $note -Force }; exit 0"
exit /b 0

rem Anything the folder held that the release does not ship is the user's own
rem -- a tool dropped in beside blindDL, a file saved there -- and comes back.
rem Only what sat at the top level: _internal belongs to the release, and
rem merging the old one into the new is how a blindDL that was replaced still
rem starts as the version it was.
:restore_extras
if not exist "%BACKUP_DIR%\." exit /b 0
for /f "delims=" %%I in ('dir /b /a "%BACKUP_DIR%" 2^>nul') do (
    if not exist "%INSTALL_DIR%\%%I" (
        if exist "%BACKUP_DIR%\%%I\" (
            robocopy "%BACKUP_DIR%\%%I" "%INSTALL_DIR%\%%I" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
        ) else (
            copy /y "%BACKUP_DIR%\%%I" "%INSTALL_DIR%\%%I" >nul
        )
        echo [blindDL update] Kept %%I from the old folder
    )
)
exit /b 0

:verify_version
"%PS%" -NoProfile -InputFormat None -Command "$ErrorActionPreference='SilentlyContinue'; $exe=Join-Path ([string]$env:INSTALL_DIR) ([string]$env:EXE_NAME); $found=''; try { $found=[string](Get-Item -LiteralPath $exe).VersionInfo.FileVersion } catch { }; if (-not $found) { Write-Host 'The new blindDL.exe has no readable version information.'; exit 1 }; function Key([string]$t) { $n=@([regex]::Matches($t,'\d+') | ForEach-Object { [int]$_.Value }); $k=@(0,0,0); for ($i=0; $i -lt 3 -and $i -lt $n.Count; $i++) { $k[$i]=$n[$i] }; return ($k -join '.') }; if ((Key $found) -ne (Key ([string]$env:BLINDDL_VERSION))) { Write-Host ('The folder holds blindDL ' + $found + ', not ' + [string]$env:BLINDDL_VERSION); exit 1 }; exit 0"
exit /b %ERRORLEVEL%

:save
set "BLINDDL_OK=%~1"
set "BLINDDL_DETAIL=%~2"
"%PS%" -NoProfile -InputFormat None -Command "$path=[string]$env:BLINDDL_RESULT; $folder=Split-Path -Parent $path; if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }; [ordered]@{ status='complete'; ok=($env:BLINDDL_OK -eq '1'); version=[string]$env:BLINDDL_VERSION; detail=[string]$env:BLINDDL_DETAIL; log=[string]$env:BLINDDL_LOG } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $path -Encoding UTF8"
exit /b 0

rem Reached from the epilogue, after the log has been closed and possibly
rem deleted, so the one line this can have to say goes to the log by name.
rem It writes the log back into existence when it has to, which is right:
rem there is something to read in it again.
:restart
if not exist "%EXE%" (
    echo [blindDL update] blindDL.exe is missing, so blindDL could not be started again >> "%BLINDDL_LOG%"
    exit /b 1
)
start "" /d "%INSTALL_DIR%" "%EXE%"
exit /b 0
"""


# Where the helper writes the files it could not shift. A sibling of the
# result file, which is how the batch finds it without another argument.
LEFTOVERS_NAME = "leftover-files.txt"


def _leftovers_path():
    return Path(app_data_dir()) / "updates" / LEFTOVERS_NAME


def _within(folder, path):
    """Whether *path* is the folder itself or something inside it."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return resolved == folder or folder in resolved.parents


def sweep_replaced_files(folder=None):
    """Delete the old files an update could not move at the time.

    Rather than abandon a working update over a file a sync client or a
    scanner happened to have open, the helper leaves that file where it is,
    writes down where, and carries on -- a stale file beside the new ones is
    loaded by nothing, and an update that rolled back helps no one. Whatever
    held it has long since let go by the time blindDL next starts, which is
    when this runs. Returns how many went.

    Only files inside *folder* -- the one blindDL is running from -- are
    touched. The note outlives the folder it was written for: copy a
    portable blindDL somewhere else and the copy reads a list of paths in an
    installation it has nothing to do with. Those are dropped rather than
    kept, since no later run will be any better placed to remove them.
    """
    note = _leftovers_path()
    try:
        # PowerShell's UTF8 writes a byte order mark; utf-8-sig reads the
        # file with or without one.
        listed = note.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    root = Path(folder) if folder else Path(sys.executable).resolve().parent
    root = Path(root).resolve()
    swept = 0
    remaining = []
    for line in listed:
        stale = line.strip()
        if not stale or not _within(root, stale):
            continue
        try:
            path = Path(stale)
            if not path.is_file():
                continue
            path.unlink()
            swept += 1
        except OSError:
            remaining.append(stale)  # still held; try again next time
    try:
        if remaining:
            note.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            note.unlink(missing_ok=True)
    except OSError:
        pass
    return swept


def _update_result_path():
    return Path(app_data_dir()) / "updates" / UPDATE_RESULT_NAME


def _write_update_result(result):
    """Atomically leave a result for the next BlindDL process to read."""
    path = _update_result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(result), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(
            f"Could not prepare the update status file in {path.parent}: {exc}"
        ) from exc


def _forget_update_result():
    try:
        _update_result_path().unlink()
    except OSError:
        pass


def take_update_result(forget=True):
    """Return what the last update helper recorded.

    Manual checks consume the record before deliberately retrying. Automatic
    checks leave failures in place so the same broken install is not started
    again every time BlindDL restarts.
    """
    path = _update_result_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if forget:
        _forget_update_result()
    try:
        result = json.loads(raw)
    except ValueError:
        if not forget:
            _forget_update_result()
        return None
    if not isinstance(result, dict):
        if not forget:
            _forget_update_result()
        return None
    return result


def _discard_staged_update(version):
    """Delete the staging folder of an update that has already taken.

    It holds the release package and the tree unpacked from it -- a hundred
    and thirty megabytes, twice over on the way in. _prune_old_updates keeps
    the newest, which is the right answer while an update is still pending
    and the wrong one afterwards: a machine that was up to date went on
    holding the whole of the version it was already running, until some
    later release came along to displace it.
    """
    if not version:
        return
    folder = Path(app_data_dir()) / "updates" / f"v{version}"
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def last_update_failure(forget=True):
    """One spoken sentence when the previous update did not take, else None."""
    result = take_update_result(forget=forget)
    if result is None:
        return None
    version = str(result.get("version") or "").strip()
    if result.get("ok"):
        # Only what this blindDL is now running: an "ok" for anything else
        # is not this install's, and the package may still be wanted.
        if _version_tuple(version) == _version_tuple(__version__):
            _discard_staged_update(version)
        if not forget:
            _forget_update_result()
        return None
    if (
        result.get("status") == "pending"
        and _version_tuple(version) == _version_tuple(__version__)
    ):
        # The helper's final status write was lost, but the executable itself
        # proves the requested version is now running.
        _discard_staged_update(version)
        if not forget:
            _forget_update_result()
        return None
    detail = str(result.get("detail") or "").strip()
    if result.get("status") == "pending" and not detail:
        detail = (
            "the Windows update helper did not report finishing. The verified "
            "package is still downloaded, so try the update again"
        )
    log_path = str(result.get("log") or "").strip()
    try:
        log_has_content = bool(log_path and Path(log_path).stat().st_size)
    except OSError:
        log_has_content = False
    if detail and log_has_content:
        detail += f". Technical details are in {log_path}"
    head = (f"blindDL {version} did not install" if version
            else "The last blindDL update did not install")
    return f"{head}: {detail}" if detail else f"{head}."


def _helper_cwd():
    """A directory the update helper can run in without holding it open.

    A child process inherits blindDL's working directory, and a portable
    blindDL is started in the very folder an update has to replace. Windows
    holds a directory open for as long as it is some process's current one,
    so the helper arrived already blocking the rename it was there to make.
    """
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or ""
    if root and Path(root).is_dir():
        return root
    return tempfile.gettempdir()


def _write_helper(path, script):
    # cmd.exe reads a .bat byte by byte in the console code page and takes a
    # UTF-8 BOM for part of the first command, so the file is plain ASCII with
    # Windows line endings. Every path it works on reaches it as an argument,
    # never as text inside the file, so nothing is lost by that.
    path.write_text(script, encoding="ascii", newline="\r\n")


def _stage_windows_helper():
    """Write the helper somewhere nothing it deletes can contain it.

    Not beside the staged update: the helper's last act is to hand blindDL a
    folder it can delete, and a batch file cannot delete the folder it is
    running from. A uniquely named copy in the temp folder can delete itself
    on the way out, which is what the last line of it does.
    """
    handle, name = tempfile.mkstemp(
        prefix="blindDL-update-helper-", suffix=".bat")
    os.close(handle)
    helper = Path(name)
    _write_helper(helper, _WINDOWS_HELPER)
    return helper


def _find_windows_shell():
    """Return the command processor that runs the post-exit helper."""
    candidates = [os.environ.get("COMSPEC") or ""]
    windows = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or ""
    if windows:
        candidates.append(str(Path(windows) / "System32" / "cmd.exe"))
    candidates.append(shutil.which("cmd.exe") or "")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    # Unit tests exercise Windows selection on non-Windows builders with the
    # process launch mocked. A real Windows run must resolve an actual shell.
    if os.name != "nt":
        return "cmd.exe"
    raise UpdateError(
        "The Windows command processor is unavailable, so BlindDL cannot "
        "finish the update."
    )


def _find_windows_powershell():
    """Return a PowerShell host that can run the post-exit helper."""
    candidates = []
    windows = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or ""
    if windows:
        candidates.append(
            Path(windows) / "System32" / "WindowsPowerShell" / "v1.0"
            / "powershell.exe"
        )
    candidates.extend(
        Path(found) for found in (
            shutil.which("powershell.exe"), shutil.which("pwsh.exe")
        ) if found
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # Unit tests exercise Windows selection on non-Windows builders with the
    # process launch mocked. A real Windows run must resolve an actual host.
    if os.name != "nt":
        return "powershell.exe"
    raise UpdateError(
        "Windows PowerShell is unavailable, so BlindDL cannot finish the update."
    )


def _windows_update_hosts():
    """The two programs the helper needs, resolved while BlindDL is still up.

    Whichever of them is missing, the answer has to arrive before BlindDL
    closes: afterwards there is nothing left to say it to.
    """
    return _find_windows_shell(), _find_windows_powershell()


def _portable_update_needs_elevation(target):
    """Test the parent operations required to replace a portable folder."""
    if os.name != "nt":
        return False
    parent = target.parent
    probe = moved = None
    try:
        probe = Path(tempfile.mkdtemp(prefix=".blinddl-update-access-", dir=parent))
        moved = probe.with_name(probe.name + "-moved")
        probe.rename(moved)
        moved.rmdir()
        return False
    except PermissionError:
        if str(parent).startswith(("\\\\", "//")):
            raise UpdateError(
                f"The portable BlindDL folder cannot be replaced on this network "
                f"location: {target}. Move it to a writable local folder and try again."
            )
        return True
    except OSError as exc:
        raise UpdateError(
            f"The portable BlindDL folder cannot be replaced in {parent}: {exc}"
        ) from exc
    finally:
        for leftover in (moved, probe):
            if leftover is not None and leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)


def _start_elevated_windows_helper(shell, arguments):
    """Start *shell* with UAC and fail without closing on cancellation."""
    import ctypes  # noqa: PLC0415 - Windows-only standard library

    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None, "runas", shell, subprocess.list2cmdline(arguments),
        _helper_cwd(), 0,
    )
    code = int(result or 0)
    if code <= 32:
        raise UpdateError(
            "Administrator permission was not granted, so the portable BlindDL "
            "folder was left unchanged."
        )


def _start_windows_helper(command):
    """Start the helper so BlindDL's own exit cannot take it down with it."""
    options = {
        "cwd": _helper_cwd(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        subprocess.Popen(command, **options)
        return
    flags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
             | CREATE_BREAKAWAY_FROM_JOB)
    try:
        subprocess.Popen(command, creationflags=flags, **options)
    except OSError:
        # A job object can forbid breakaway outright, and asking for it there
        # fails the launch rather than the flag. Better a helper that shares
        # BlindDL's fate than no helper at all.
        subprocess.Popen(
            command, creationflags=flags & ~CREATE_BREAKAWAY_FROM_JOB,
            **options,
        )


def _launch_windows_helper(mode, install_dir, source, version, *,
                           elevated=False):
    """Write the helper, start it, and leave a note if it never reports back.

    The helper's arguments are positional and in this order: mode, BlindDL's
    process id, the folder BlindDL runs from, the staged folder or installer,
    the result file, the version being installed, the log, and the PowerShell
    to use for the few things a batch file cannot do itself.
    """
    shell, powershell = _windows_update_hosts()
    log_path = Path(app_data_dir()) / "updates" / WINDOWS_UPDATE_LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    helper = _stage_windows_helper()
    arguments = [
        mode, str(os.getpid()), str(install_dir), str(source),
        str(_update_result_path()), str(version), str(log_path),
        str(powershell),
    ]
    _write_update_result({
        "status": "pending",
        "ok": False,
        "version": str(version),
        "detail": "",
        "log": str(log_path),
    })
    command = [shell, "/d", "/c", str(helper), *arguments]
    try:
        if elevated:
            _start_elevated_windows_helper(
                shell, ["/d", "/c", str(helper), *arguments])
        else:
            _start_windows_helper(command)
    except (OSError, UpdateError) as exc:
        _write_update_result({
            "status": "complete",
            "ok": False,
            "version": str(version),
            "detail": f"the Windows update helper could not start: {exc}",
            "log": str(log_path),
        })
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"The Windows update helper could not start: {exc}") from exc
    return helper


def _portable_windows_update(package_path, version):
    target = Path(sys.executable).resolve().parent
    if not (target / "blindDL.exe").is_file():
        raise UpdateError("The current portable BlindDL folder is not valid.")
    elevated = _portable_update_needs_elevation(target)
    update_root = package_path.parent / "portable"
    if update_root.exists():
        shutil.rmtree(update_root)
    update_root.mkdir()
    _safe_extract_zip(package_path, update_root)
    source = update_root / "blindDL"
    if not (source / "blindDL.exe").is_file():
        raise UpdateError("The portable update does not contain blindDL.exe.")
    _launch_windows_helper(
        "portable", target, source, version, elevated=elevated)
    return True


def install_app_update(update, package_path, log=lambda _line: None):
    """Launch or stage the platform updater. True means BlindDL should exit."""
    package_path = Path(package_path)
    if not package_path.is_file():
        raise UpdateError(f"The downloaded update is missing: {package_path}")
    suffixes = "".join(package_path.suffixes).lower()
    if sys.platform == "win32":
        if suffixes.endswith(".zip"):
            log("Staging the portable update; BlindDL will restart itself.")
            return _portable_windows_update(package_path, update.version)
        if not suffixes.endswith(".exe"):
            raise UpdateError(
                f"Cannot install this Windows update package: {package_path.name}"
            )
        log("Staging the silent BlindDL installer; BlindDL will restart itself.")
        target = Path(sys.executable).resolve().parent
        _launch_windows_helper(
            "installed", target, package_path, update.version)
        return True
    if sys.platform == "darwin":
        log("Opening the update disk image. Replace BlindDL in Applications.")
        subprocess.Popen(["open", str(package_path)])
        return False
    if suffixes.endswith(".deb"):
        if shutil.which("pkexec"):
            log("Starting the system package installer...")
            # apt-get takes package names, not paths, and reads anything with
            # a slash in it as a local file to install (resolving dependencies
            # for it) rather than answering "Unable to locate package". The
            # slash has to come from an absolute path: pkexec runs its program
            # in the target user's home directory, so "./name.deb" would be
            # looked for in root's home, where it is not.
            subprocess.Popen(
                ["pkexec", "apt-get", "install", "-y",
                 str(package_path.resolve())],
            )
            return True
        log("Opening the package in your system installer...")
        subprocess.Popen(["xdg-open", str(package_path)])
        return False
    if suffixes.endswith(".tar.gz"):
        stage = package_path.parent / "linux-portable"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        with tarfile.open(package_path, "r:gz") as package:
            package.extractall(stage, filter="data")
        installer = next(stage.glob("*/install.sh"), None)
        if installer is None:
            raise UpdateError("The Linux update does not contain install.sh.")
        log("Starting the BlindDL user installer...")
        # blindDL closes as soon as this returns True, so the installer is the
        # only thing left to bring it back; the same script run by hand from a
        # terminal does not, and says what to run instead.
        environment = dict(os.environ, BLINDDL_RESTART="1")
        subprocess.Popen(
            ["sh", str(installer)], cwd=installer.parent, env=environment
        )
        return True
    raise UpdateError(f"Cannot install update package: {package_path.name}")


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


def _find_winget():
    found = shutil.which("winget")
    if found:
        return found
    if sys.platform == "win32":
        candidate = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft" / "WindowsApps" / "winget.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return None


def _find_brew():
    found = shutil.which("brew")
    if found:
        return found
    for candidate in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if candidate.is_file():
            return str(candidate)
    return None


def _find_linux_package_manager():
    for manager in LINUX_PACKAGES:
        found = shutil.which(manager)
        if found:
            return manager, found
    return None, None


def _linux_elevation(log):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo:
        try:
            probe = subprocess.run(
                [sudo, "-n", "true"], capture_output=True, timeout=10,
                **_subprocess_options(),
            )
            if probe.returncode == 0:
                return [sudo, "-n"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    pkexec = shutil.which("pkexec")
    if pkexec:
        log("The operating system may request permission to install media tools.")
        return [pkexec]
    log("No non-interactive administrator helper is available.")
    return None


def _install_deno_user(log):
    """Install Deno under the current user's home without Python or admin rights."""
    arches = {
        "amd64": "amd64", "x86_64": "amd64", "x64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
    }
    system = platform.system().lower()
    arch = arches.get(platform.machine().lower())
    asset = DENO_TARGETS.get((system, arch))
    if not asset:
        log(f"No automatic Deno package exists for {system} {platform.machine()}.")
        return False
    try:
        with _open_url(DENO_CHANNEL_URL, timeout=120) as response:
            version = response.read().decode("utf-8").strip()
        if version.lstrip("v").split(".")[0] != "2":
            log(f"Deno's release channel returned unsupported version {version}.")
            return False
        url = DENO_RELEASE_URL.format(version=version, asset=asset)
        destination = Path.home() / ".deno" / "bin"
        destination.mkdir(parents=True, exist_ok=True)
        archive = destination / "deno-download.zip"
        log(f"Installing Deno {version} for the current user...")
        with _open_url(url, timeout=180) as response:
            archive.write_bytes(response.read())
        with zipfile.ZipFile(archive) as bundle:
            name = next(
                (item for item in bundle.namelist()
                 if Path(item).name in {"deno", "deno.exe"}),
                None,
            )
            if name is None:
                raise UpdateError("The Deno archive did not contain its executable.")
            binary = destination / Path(name).name
            binary.write_bytes(bundle.read(name))
            binary.chmod(
                binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        archive.unlink(missing_ok=True)
        from .runtime import prepare_runtime_path
        prepare_runtime_path()
        return _tool_available("deno")
    except (OSError, UpdateError, URLError, zipfile.BadZipFile) as exc:
        log(f"Could not install Deno for the current user: {exc}")
        return False


def _tool_available(tool):
    from .runtime import prepare_runtime_path

    prepare_runtime_path()
    if tool != "vlc":
        return shutil.which(tool) is not None
    if shutil.which("vlc") is not None:
        return True
    if sys.platform == "win32":
        roots = [
            Path(os.environ.get(name, "")) / "VideoLAN" / "VLC"
            for name in ("ProgramFiles", "ProgramFiles(x86)")
        ]
        return any((root / "libvlc.dll").is_file() for root in roots)
    if sys.platform == "darwin":
        return any(path.is_file() for path in (
            Path("/Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib"),
            Path.home() / "Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib",
        ))
    try:
        import ctypes.util
        return ctypes.util.find_library("vlc") is not None
    except (ImportError, OSError):
        return False


def missing_external_tools(package_ids=None):
    """Return native package IDs whose executable payload is unavailable."""
    wanted = package_ids or WINGET_PACKAGES
    return [
        package_id for package_id in wanted
        if not all(_tool_available(tool) for tool in WINGET_PACKAGES[package_id][1])
    ]


def describe_external_tools(package_ids):
    """Human names for *package_ids*, in the order they were given."""
    return [
        WINGET_PACKAGES[package_id][0] for package_id in package_ids
        if package_id in WINGET_PACKAGES
    ]


def ensure_external_tools(log, package_ids=None, progress=None):
    """Install missing native runtimes with the current OS package manager.

    *log* receives everything, including the package manager's own output.
    *progress* receives only the short sentences worth showing and speaking
    -- one as each tool starts, one as it finishes -- because a package
    manager's output is not something a screen reader can be asked to sit
    through while VLC installs.
    """
    wanted = tuple(package_ids or WINGET_PACKAGES)
    say = progress if progress is not None else (lambda _line: None)

    def announce_result(package_id):
        # Nobody is listening without a progress callback, and the check
        # costs a PATH walk and a handful of stat calls per package.
        if progress is None:
            return
        description = WINGET_PACKAGES[package_id][0]
        if missing_external_tools((package_id,)):
            say(f"{description} could not be installed.")
        else:
            say(f"{description} installed.")

    with _external_tools_lock:
        missing = missing_external_tools(wanted)
        if not missing:
            return True
        # A package manager that could not install Deno once will not manage
        # it because the user searched again, and every music search asks.
        # Without this, a machine with no WinGet spawned two installers per
        # search, each one a heavy process that resolves a manifest over the
        # network before failing the same way.
        missing = [item for item in missing if item not in _install_attempted]
        if not missing:
            return False
        _install_attempted.update(missing)
        ok = True
        if sys.platform == "win32":
            winget = _find_winget()
            if winget is None:
                log("WinGet is unavailable; required download tools could not be installed.")
                say("WinGet is unavailable, so these tools could not be installed.")
                return False
            for package_id in missing:
                description = WINGET_PACKAGES[package_id][0]
                log(f"Installing {description}...")
                say(f"Installing {description}. This can take a few minutes.")
                ok = _run([
                    winget, "install", "--id", package_id, "--exact",
                    "--source", "winget", "--silent",
                    "--accept-package-agreements", "--accept-source-agreements",
                    "--disable-interactivity",
                ], log) and ok
                announce_result(package_id)
        elif sys.platform == "darwin":
            brew = _find_brew()
            for package_id in missing:
                description = WINGET_PACKAGES[package_id][0]
                package, is_cask = HOMEBREW_PACKAGES[package_id]
                log(f"Installing {description}...")
                say(f"Installing {description}. This can take a few minutes.")
                if brew:
                    command = [brew, "install"]
                    if is_cask:
                        command.append("--cask")
                    ok = _run([*command, package], log) and ok
                elif package_id == "DenoLand.Deno":
                    ok = _install_deno_user(log) and ok
                else:
                    log("Homebrew is unavailable; this native tool could not be installed.")
                    ok = False
                announce_result(package_id)
        elif sys.platform.startswith("linux"):
            if "DenoLand.Deno" in missing:
                say("Installing Deno. This can take a few minutes.")
                ok = _install_deno_user(log) and ok
                announce_result("DenoLand.Deno")
            native = [item for item in missing if item != "DenoLand.Deno"]
            if native:
                say("Installing " + ", ".join(describe_external_tools(native))
                    + ". This can take a few minutes.")
                manager, executable = _find_linux_package_manager()
                elevation = _linux_elevation(log)
                if not manager or not executable or elevation is None:
                    log("A supported Linux package manager is unavailable.")
                    say("No supported Linux package manager is available.")
                    ok = False
                else:
                    packages = list(dict.fromkeys(
                        LINUX_PACKAGES[manager][item] for item in native
                    ))
                    if manager == "apt-get":
                        ok = _run([*elevation, executable, "update"], log) and ok
                        command = [*elevation, executable, "install", "-y",
                                   "--no-install-recommends", *packages]
                    elif manager == "dnf":
                        command = [*elevation, executable, "install", "-y", *packages]
                    elif manager == "pacman":
                        command = [*elevation, executable, "-S", "--needed",
                                   "--noconfirm", *packages]
                    else:
                        command = [*elevation, executable, "--non-interactive",
                                   "install", *packages]
                    ok = _run(command, log) and ok
                for package_id in native:
                    announce_result(package_id)
        else:
            log(f"Automatic native-tool installation is unsupported on {sys.platform}.")
            return False
        still_missing = missing_external_tools(wanted)
        if still_missing:
            log("Still missing: " + ", ".join(
                WINGET_PACKAGES[package_id][0] for package_id in still_missing
            ))
            return False
        return ok


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
    present = [name for name in OPTIONAL_PACKAGES if name in installed]
    if present:
        ok = _run([sys.executable, "-m", "pip", "install", "--upgrade",
                   "--quiet", *present], log) and ok
    after = _installed_versions()
    changed = [p for p in (*PIP_PACKAGES, *PRE_PACKAGES, *GIT_PACKAGES,
                           *OPTIONAL_PACKAGES)
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
             *PIP_PACKAGES, *PRE_PACKAGES, *GIT_PACKAGES,
             *OPTIONAL_PACKAGES],
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
    # An update check is the user asking for another go at whatever would
    # not install, so a failed attempt stops standing in the way here.
    _install_attempted.clear()
    if sys.platform == "win32":
        if not ensure_external_tools(log):
            return
        winget = _find_winget()
        if winget is None:
            log("WinGet not found; skipping external-tool updates.")
            return
        for package_id, (description, _tools) in WINGET_PACKAGES.items():
            log(f"Checking {description}...")
            _run([
                winget, "upgrade", "--id", package_id, "--exact",
                "--source", "winget",
                "--silent", "--accept-package-agreements",
                "--accept-source-agreements", "--disable-interactivity",
            ], log)
    elif sys.platform == "darwin" and _find_brew():
        brew = _find_brew()
        _run([brew, "upgrade", "deno", "ffmpeg", "node"], log)
        _run([brew, "upgrade", "--cask", "vlc"], log)
    elif sys.platform.startswith("linux"):
        ensure_external_tools(log)
        deno = shutil.which("deno")
        if deno:
            _run([deno, "upgrade"], log)
    else:
        log("Native tools are managed by the installed package on this system.")


def ensure_deno(log):
    """Make sure Deno is installed at all (yt-dlp needs it for YouTube)."""
    if _tool_available("deno"):
        return True
    return ensure_external_tools(log, ("DenoLand.Deno",))


def run_full_update(log, include_winget=True):
    """Update everything. log(str) receives human-readable progress lines."""
    started = time.time()
    log("Checking for updates...")
    changed = update_pip_packages(log)
    if include_winget:
        update_winget_packages(log)
    log(f"Update check finished in {time.time() - started:.0f} seconds.")
    return changed
