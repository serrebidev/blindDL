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


def _subprocess_options():
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


# Both Windows helpers outlive blindDL itself: they wait for it to close,
# change the files it was running from, and start it again. Everything they
# share lives here -- above all Save, which records the outcome whether the
# update took or not. A helper that wrote a log only on failure left a failed
# update indistinguishable from one that never ran, which is exactly what a
# self-update that "does nothing, with no error" is.
_HELPER_COMMON = r"""
$ErrorActionPreference = 'Stop'
$Steps = New-Object System.Collections.ArrayList

# A process holds its own working directory open, and a portable blindDL
# starts in the folder it lives in -- the folder this script has to rename.
# The helper inherits that directory when blindDL starts it, so the rename
# used to fail against nothing but the script performing it: "the process
# cannot access the file because it is being used by another process", every
# time, on a machine where nothing else was wrong. blindDL starts the helper
# elsewhere now; this is the same move made from inside, for a helper that
# was started some other way.
$Elsewhere = if ($env:SystemRoot) { $env:SystemRoot }
             else { [System.IO.Path]::GetTempPath() }
Set-Location -LiteralPath $Elsewhere
[System.IO.Directory]::SetCurrentDirectory($Elsewhere)

function Note([string]$Text) { [void]$Steps.Add($Text) }

function Save([bool]$Ok, [string]$Detail) {
  try {
    $folder = Split-Path -Parent $Result
    if (-not (Test-Path -LiteralPath $folder)) {
      New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    [ordered]@{
      status = 'complete'
      ok = $Ok
      version = $Version
      detail = $Detail
      steps = @($Steps)
      log = $Log
    } |
      ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Result -Encoding UTF8
  } catch { }
}

function Get-Reason($Record) {
  $problem = $Record.Exception
  while ($problem.InnerException) { $problem = $problem.InnerException }
  return $problem.Message
}

function Get-VersionKey([string]$Text) {
  $found = @([regex]::Matches($Text, '\d+') | ForEach-Object { [int]$_.Value })
  $key = @(0, 0, 0)
  for ($i = 0; $i -lt 3 -and $i -lt $found.Count; $i++) { $key[$i] = $found[$i] }
  return ($key -join '.')
}

function Read-InstalledVersion([string]$Path) {
  try { return [string](Get-Item -LiteralPath $Path).VersionInfo.FileVersion }
  catch { return '' }
}

# Waiting on the process id alone is not enough. Windows keeps blindDL.exe
# mapped for a moment after it exits, and an antivirus scan of a freshly
# closed executable holds it for longer than that, so the first write could
# fail against a program that had already gone. The file is what has to be
# free, so the file is what gets asked.
function Wait-ForRelease([string]$Path) {
  $deadline = (Get-Date).AddMinutes(5)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $BlindDLPid -ErrorAction SilentlyContinue)) {
      try {
        $handle = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $handle.Close()
        return $true
      } catch [System.UnauthorizedAccessException] {
        # An installed blindDL lives under Program Files, where this script
        # runs without the rights to open anything for writing. The question
        # cannot be answered there, and asking it forever meant an installed
        # blindDL waited out the full five minutes and then reported itself
        # still running. The process is gone; the elevated installer that
        # follows is the one equipped to deal with a file still in use.
        return $true
      } catch { }
    }
    Start-Sleep -Milliseconds 250
  }
  return $false
}
"""


# The portable update swaps whole folders instead of copying the new files
# over the old ones. Copy-Item merges, and a merge keeps every file the new
# release dropped: after a Python upgrade the folder holds both runtimes and
# the extension modules of both, which is how a portable blindDL can be
# replaced and still start as the version it was.
_PORTABLE_HELPER = (
    "param([int]$BlindDLPid, [string]$Source, [string]$Target,\n"
    "      [string]$Result, [string]$Version, [string]$Log)\n"
    + _HELPER_COMMON
    + r"""
$Exe = Join-Path $Target 'blindDL.exe'
$Backup = $Target + '.blinddl-update-backup-' + [guid]::NewGuid().ToString('N')

function Restart-BlindDL {
  if (-not (Test-Path -LiteralPath $Exe)) {
    throw 'blindDL.exe is missing, so BlindDL could not be restarted.'
  }
  Start-Process -FilePath $Exe -WorkingDirectory $Target
}

function Move-Folder([string]$From, [string]$To) {
  [System.IO.Directory]::Move($From, $To)
}

# A virus scanner reading the executable blindDL just closed, or an Explorer
# window left sitting in the folder, holds it for a few seconds. Giving up on
# the first refusal turns a moment's contention into an update that never
# happens, so the folder is asked for repeatedly before the answer is taken
# as final. Only used where both ends are on the same volume: a cross-volume
# move fails the same way every time, and has a copy waiting for it instead.
function Move-FolderSoon([string]$From, [string]$To) {
  $deadline = (Get-Date).AddSeconds(60)
  while ($true) {
    try { Move-Folder $From $To; return }
    catch [System.UnauthorizedAccessException] { throw }
    catch {
      if ((Get-Date) -ge $deadline) { throw }
      Start-Sleep -Milliseconds 500
    }
  }
}

function Undo-Swap {
  try {
    if (Test-Path -LiteralPath $Target) {
      Remove-Item -LiteralPath $Target -Recurse -Force
    }
    if (Test-Path -LiteralPath $Backup) {
      Move-FolderSoon $Backup $Target
      Note 'Put the previous blindDL back.'
    }
  } catch {
    Note ('The previous blindDL could not be put back: ' + (Get-Reason $_))
  }
}

if (-not (Wait-ForRelease $Exe)) {
  Save $false 'blindDL was still holding its own files five minutes after it closed.'
  Restart-BlindDL
  exit 1
}

try {
  Move-FolderSoon $Target $Backup
  Note 'Moved the old blindDL folder aside.'
} catch {
  Save $false ('The old blindDL folder is in use and could not be replaced: ' +
    (Get-Reason $_))
  Restart-BlindDL
  exit 1
}

try {
  try {
    # A rename when the staged folder shares a volume with the install,
    # a copy when it does not.
    Move-Folder $Source $Target
  } catch {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item `
      -Destination $Target -Recurse -Force
  }
  if (-not (Test-Path -LiteralPath $Exe)) {
    throw 'The new blindDL folder arrived without blindDL.exe.'
  }
  Note 'Put the new blindDL folder in place.'
  # Anything the folder held that the release does not ship is the user's own
  # -- a tool dropped in beside blindDL, a file saved there -- and comes back.
  Get-ChildItem -LiteralPath $Backup -Force | ForEach-Object {
    $kept = Join-Path $Target $_.Name
    if (-not (Test-Path -LiteralPath $kept)) {
      Copy-Item -LiteralPath $_.FullName -Destination $kept -Recurse -Force
      Note ('Kept ' + $_.Name + ' from the old folder.')
    }
  }
  $installed = Read-InstalledVersion $Exe
  if (-not $installed) {
    throw 'The new blindDL.exe has no readable version information.'
  }
  if ((Get-VersionKey $installed) -ne (Get-VersionKey $Version)) {
    throw ('The folder now holds blindDL ' + $installed + ', not ' + $Version + '.')
  }
} catch {
  $why = Get-Reason $_
  Undo-Swap
  Save $false $why
  Restart-BlindDL
  exit 1
}

try {
  Remove-Item -LiteralPath $Backup -Recurse -Force
} catch {
  Note 'The previous version is still on disk beside the new one.'
}
Save $true ''
try {
  Restart-BlindDL
} catch {
  Save $false ('blindDL was updated but could not be restarted: ' + (Get-Reason $_))
  exit 1
}
""")


# The installed build hands the work to Inno Setup, so what is left to get
# right is waiting for the old blindDL to let go, noticing a non-zero exit
# code, and confirming that the version on disk actually moved.
_INSTALLED_HELPER = (
    "param([int]$BlindDLPid, [string]$Installer, [string]$Target,\n"
    "      [string]$Result, [string]$Version, [string]$Log)\n"
    + _HELPER_COMMON
    + r"""
if (-not (Wait-ForRelease $Target)) {
  Save $false 'blindDL was still running five minutes after it closed.'
  if (Test-Path -LiteralPath $Target) { Start-Process -FilePath $Target }
  exit 1
}

try {
  $Run = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT',
    '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
  if ($Run.ExitCode -ne 0) {
    throw ('The blindDL installer stopped with exit code ' + $Run.ExitCode + '.')
  }
  if (-not (Test-Path -LiteralPath $Target)) {
    throw 'The installer finished but blindDL.exe is gone.'
  }
  $installed = Read-InstalledVersion $Target
  if (-not $installed) {
    throw 'The installed blindDL.exe has no readable version information.'
  }
  if ((Get-VersionKey $installed) -ne (Get-VersionKey $Version)) {
    throw ('blindDL ' + $installed + ' is still installed, not ' + $Version + '.')
  }
  Save $true ''
} catch {
  Save $false (Get-Reason $_)
}
try {
  Start-Process -FilePath $Target
} catch {
  Save $false ('The update finished but blindDL could not be restarted: ' +
    (Get-Reason $_))
  exit 1
}
""")


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
    # Windows PowerShell reads a .ps1 in the active code page unless the file
    # says otherwise, so the BOM is what keeps the script's own text intact.
    path.write_text(script, encoding="utf-8-sig")


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


def _validate_windows_helper(powershell, script):
    """Prove the selected host starts and parses *script* before BlindDL exits."""
    if os.name != "nt":
        return
    command = [
        powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", "[void][ScriptBlock]::Create([Console]::In.ReadToEnd())",
    ]
    try:
        completed = subprocess.run(
            command, input=script, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", **_subprocess_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"Windows PowerShell could not start: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown parse error").strip()
        raise UpdateError(f"The Windows update helper is invalid: {detail}")


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


def _start_elevated_windows_helper(powershell, arguments):
    """Start *powershell* with UAC and fail without closing on cancellation."""
    import ctypes  # noqa: PLC0415 - Windows-only standard library

    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None, "runas", powershell, subprocess.list2cmdline(arguments),
        _helper_cwd(), 0,
    )
    code = int(result or 0)
    if code <= 32:
        raise UpdateError(
            "Administrator permission was not granted, so the portable BlindDL "
            "folder was left unchanged."
        )


def _launch_windows_helper(helper, script, arguments, version, *, elevated=False):
    """Validate and launch a helper, leaving a diagnostic if it disappears."""
    powershell = _find_windows_powershell()
    _validate_windows_helper(powershell, script)
    log_path = helper.parent / WINDOWS_UPDATE_LOG_NAME
    _write_update_result({
        "status": "pending",
        "ok": False,
        "version": str(version),
        "detail": "",
        "log": str(log_path),
    })
    command_arguments = [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(helper), *arguments, "-Log", str(log_path),
    ]
    try:
        if elevated:
            _start_elevated_windows_helper(powershell, command_arguments)
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "wb") as output:
                subprocess.Popen(
                    [powershell, *command_arguments], cwd=_helper_cwd(),
                    stdout=output, stderr=subprocess.STDOUT,
                    **_subprocess_options(),
                )
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
    helper = package_path.parent / "finish-portable-update.ps1"
    _write_helper(helper, _PORTABLE_HELPER)
    arguments = [
        "-BlindDLPid", str(os.getpid()),
        "-Source", str(source), "-Target", str(target),
        "-Result", str(_update_result_path()), "-Version", str(version),
    ]
    _launch_windows_helper(
        helper, _PORTABLE_HELPER, arguments, version, elevated=elevated
    )
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
        helper = package_path.parent / "finish-installed-update.ps1"
        target = Path(sys.executable).resolve()
        _write_helper(helper, _INSTALLED_HELPER)
        arguments = [
            "-BlindDLPid", str(os.getpid()),
            "-Installer", str(package_path), "-Target", str(target),
            "-Result", str(_update_result_path()),
            "-Version", str(update.version),
        ]
        _launch_windows_helper(
            helper, _INSTALLED_HELPER, arguments, update.version
        )
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
