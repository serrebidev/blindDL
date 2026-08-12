# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Install the Deno runtime a release build bundles, and survive a flaky CDN.

denoland/setup-deno pulls its zip from GitHub's release downloads and gives up
after three attempts inside about thirty seconds. When that CDN answered 503
during the 0.14.2 release, both Linux jobs died before installing anything, and
because the publish job needs every platform, the whole release never shipped.

This installer fetches the same binary from Deno's own CDN instead, so a GitHub
release-download outage no longer stops a release, and it retries for minutes
rather than seconds so a short blip on either host is ridden out.
"""

from __future__ import annotations

import os
import platform
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CHANNEL_URL = "https://dl.deno.land/release-latest.txt"
RELEASE_URL = "https://dl.deno.land/release/{version}/{asset}.zip"

# The CDN answers 403 to urllib's default "Python-urllib/3.x", so introduce
# ourselves the way any other download client would.
USER_AGENT = "blindDL-release-build"

# The workflows asked setup-deno for "v2.x", so a jump to Deno 3 is a decision
# for a person to make rather than something a release quietly picks up.
WANTED_MAJOR = 2

ATTEMPTS = 6
BACKOFF_SECONDS = 15

TARGETS = {
    ("windows", "amd64"): "deno-x86_64-pc-windows-msvc",
    ("windows", "arm64"): "deno-aarch64-pc-windows-msvc",
    ("linux", "amd64"): "deno-x86_64-unknown-linux-gnu",
    ("linux", "arm64"): "deno-aarch64-unknown-linux-gnu",
    ("darwin", "amd64"): "deno-x86_64-apple-darwin",
    ("darwin", "arm64"): "deno-aarch64-apple-darwin",
}

ARCHES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def target_asset() -> str:
    """Name the Deno build this machine needs."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = ARCHES.get(machine)
    if arch is None:
        raise SystemExit(f"unsupported CPU architecture for Deno: {machine}")
    asset = TARGETS.get((system, arch))
    if asset is None:
        raise SystemExit(f"unsupported platform for Deno: {system} {arch}")
    return asset


def fetch(url: str, what: str) -> bytes:
    """Read a URL, retrying the failures that a retry can actually clear."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            # A wrong URL or a missing asset answers 4xx and will answer 4xx
            # every time, so say so now instead of sleeping for minutes first.
            last = str(error)
            if error.code not in (408, 429) and 400 <= error.code < 500:
                raise SystemExit(f"could not {what}: {last}") from error
            print(f"{what} failed on attempt {attempt}/{ATTEMPTS}: {last}")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            # A 503, a socket hang up and a stalled read all look different
            # here, and all of them are worth another try.
            last = str(error)
            print(f"{what} failed on attempt {attempt}/{ATTEMPTS}: {last}")
        if attempt < ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * attempt)
    raise SystemExit(f"could not {what} after {ATTEMPTS} attempts: {last}")


def wanted_version() -> str:
    """Pick the Deno version to install."""
    pinned = os.environ.get("BLINDDL_DENO_VERSION", "").strip()
    if pinned:
        return pinned if pinned.startswith("v") else f"v{pinned}"

    version = fetch(CHANNEL_URL, "read the Deno release channel").decode().strip()
    major = version.lstrip("v").split(".")[0]
    if major != str(WANTED_MAJOR):
        raise SystemExit(
            f"Deno's stable channel is now {version}, not {WANTED_MAJOR}.x. "
            "Check that yt-dlp still works on it, then set BLINDDL_DENO_VERSION "
            f"or raise WANTED_MAJOR in {Path(__file__).name}."
        )
    return version


def main() -> int:
    asset = target_asset()
    version = wanted_version()
    destination = Path(os.environ.get("BLINDDL_DENO_DIR") or Path.home() / ".deno" / "bin")
    destination.mkdir(parents=True, exist_ok=True)

    archive = destination / "deno-download.zip"
    url = RELEASE_URL.format(version=version, asset=asset)
    print(f"Installing Deno {version} from {url}")
    archive.write_bytes(fetch(url, f"download Deno {version}"))

    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if Path(name).name in ("deno", "deno.exe")]
        if not names:
            raise SystemExit(f"no deno binary inside {url}")
        for name in names:
            binary = destination / Path(name).name
            binary.write_bytes(bundle.read(name))
            # Zip files carry no Unix permission bits through this path, so the
            # binary arrives unexecutable on Linux and macOS unless we say so.
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    archive.unlink()

    # Later steps in the job find deno through PATH, exactly as they did when
    # setup-deno put it there.
    path_file = os.environ.get("GITHUB_PATH")
    if path_file:
        with open(path_file, "a", encoding="utf-8") as handle:
            handle.write(f"{destination}\n")
    print(f"Deno {version} installed in {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
