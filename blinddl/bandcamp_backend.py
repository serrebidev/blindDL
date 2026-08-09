# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Bandcamp backend: search via the fuzzysearch API, download via yt-dlp.

Bandcamp's internal autocomplete API (used by their mobile app) returns
albums, tracks, and artists without authentication.  Album/track detail
and download are handled by yt-dlp's Bandcamp extractor.
"""

import requests

from . import ytdlp_backend

_API_BASE = "https://bandcamp.com/api"
_SEARCH_SOURCE = "Bandcamp"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 "
               "Safari/537.36")
HTTP_TIMEOUT_S = 15


def _api_get(path, params=None):
    resp = requests.get(
        f"{_API_BASE}{path}", params=params or {},
        headers={"User-Agent": _USER_AGENT}, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def _item_from_result(result):
    """Convert a fuzzysearch result to a normalised item dict."""
    rtype = result.get("type", "")
    title = result.get("name", "Unknown")
    url = result.get("url", "")
    item = {
        "id": f"bandcamp:{result['id']}",
        "kind": "bandcamp",
        "title": title,
        "artist": "",
        "source": _SEARCH_SOURCE,
        "duration_s": 0,
        "url": url,
    }
    if rtype == "t":
        # Tracks may carry a band_name.  The full metadata arrives
        # when the album URL is resolved through yt-dlp.
        item["artist"] = result.get("band_name", "")
        item["format"] = "MP3"
    elif rtype == "a":
        item["format"] = "Album"
    elif rtype == "b":
        item["format"] = "Artist"
    return item


def search(query, config=None):
    """Search Bandcamp via the fuzzysearch API.  Returns normalised items."""
    try:
        data = _api_get(
            "/fuzzysearch/1/app_autocomplete", {"q": query})
    except Exception:
        return []
    return [_item_from_result(r) for r in data.get("results", [])
            if r.get("type") in ("a", "t")]


def extract_flat(url, config=None):
    """Resolve a Bandcamp album/track URL to (items, title) via yt-dlp.

    Same contract as ytdlp_backend.extract_flat.
    """
    return ytdlp_backend.extract_flat(url)


def download(url, out_dir, config=None, progress_cb=None, cancel_event=None):
    """Download a Bandcamp album/track via yt-dlp."""
    audio_only = True  # Bandcamp is music
    audio_format = (config or {}).get("audio_format", "mp3")
    return ytdlp_backend.download(
        url, out_dir, audio_only=audio_only,
        audio_format=audio_format,
        progress_cb=progress_cb, cancel_event=cancel_event)
