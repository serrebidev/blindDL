# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Apple Music backend: download via gamdl (optional dependency).

gamdl (Glomatico's Apple Music Downloader) downloads AAC/M4A from Apple
Music using browser cookies.  Install it separately:

    pip install gamdl

Then place your Apple Music cookies file (Netscape format) somewhere and
set the path in Settings.  The backend is skipped when gamdl is not
installed or no cookies file is configured.
"""

import os
import re
import subprocess

_SEARCH_SOURCE = "Apple Music"
_APPLE_URL_RE = re.compile(
    r"music\.apple\.com/(?:[a-z]{2}/)?(?:album|playlist|artist|song)/",
    re.IGNORECASE)


def is_apple_music_url(url):
    return bool(_APPLE_URL_RE.search(url))


def search(query, config=None, order=None):
    """Apple Music search is not available without the MusicKit API.
    When gamdl is installed, users can paste Apple Music URLs directly.
    """
    return []


def extract_flat(url, config=None):
    """Apple Music URLs are resolved by gamdl at download time.
    Return a single placeholder item so the URL panel can proceed.
    """
    if not is_apple_music_url(url):
        raise RuntimeError(f"Not an Apple Music URL: {url}")
    return [{
        "id": f"applemusic:{url}",
        "kind": "applemusic",
        "title": url.rsplit("/", 1)[-1] if "/" in url else url,
        "artist": "",
        "source": _SEARCH_SOURCE,
        "duration_s": 0,
        "url": url,
    }], "Apple Music"


def _find_gamdl():
    """Return the gamdl executable path, or None."""
    import shutil
    path = shutil.which("gamdl")
    if path:
        return path
    # Try the common pipx / user install locations.
    import sys
    script_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
    candidate = os.path.join(script_dir, "gamdl.exe"
                             if sys.platform == "win32" else "gamdl")
    if os.path.isfile(candidate):
        return candidate
    return None


def download(url, out_dir, config=None, progress_cb=None, cancel_event=None):
    """Download an Apple Music URL via gamdl."""
    gamdl = _find_gamdl()
    if not gamdl:
        raise RuntimeError(
            "gamdl is not installed.  Install it with:\n"
            "    pip install gamdl\n"
            "Then place your Apple Music cookies file and set the path in "
            "Settings.")
    cookies_path = (config or {}).get("apple_music_cookies", "")
    if not cookies_path or not os.path.isfile(cookies_path):
        raise RuntimeError(
            "No Apple Music cookies file configured.  Export your browser "
            "cookies in Netscape format while logged in at music.apple.com, "
            "then set the path in Settings.")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        gamdl, url,
        "--output-path", out_dir,
        "--cookies-path", cookies_path,
        "--no-synced-lyrics",
        "--no-config-file",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       timeout=600)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gamdl failed:\n{e.stderr or e.stdout or 'Unknown error'}") from e
    except FileNotFoundError:
        raise RuntimeError("gamdl executable not found.") from None


def download_track(url, out_dir, config=None, progress_cb=None,
                   cancel_event=None):
    """Alias for download."""
    return download(url, out_dir, config, progress_cb, cancel_event)
