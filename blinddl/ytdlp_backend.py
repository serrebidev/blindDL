# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""yt-dlp backend: URL inspection, search, and downloading.

All functions are synchronous and meant to be called from worker threads.
Progress is reported through plain callbacks so the GUI layer can marshal
them onto the main thread.
"""

import os

import yt_dlp


class DownloadCancelled(Exception):
    """Raised inside progress hooks when the user cancels a download."""


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _entry_to_item(entry):
    url = entry.get("webpage_url") or entry.get("url") or ""
    # Flat playlist entries sometimes only carry a bare id.
    if url and not url.startswith("http") and entry.get("ie_key") == "Youtube":
        url = f"https://www.youtube.com/watch?v={url}"
    return {
        "id": str(entry.get("id") or url),
        "title": entry.get("title") or "Unknown title",
        "url": url,
        "duration": entry.get("duration"),
        "uploader": entry.get("uploader") or entry.get("channel") or "",
    }


def extract_flat(url):
    """Inspect a URL without downloading.

    Returns (items, title): items is a list of normalized dicts (one entry
    for a single video, many for a playlist/channel), title is the name of
    the video/playlist/channel itself.
    """
    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError(f"Could not extract any information from: {url}")
    title = info.get("title") or url
    entries = info.get("entries")
    if entries is not None:
        items = [_entry_to_item(e) for e in entries if e]
    else:
        items = [_entry_to_item(info)]
    items = [i for i in items if i["url"]]
    return items, title


def search(query, count=20):
    """Search YouTube via yt-dlp's ytsearch extractor."""
    items, _ = extract_flat(f"ytsearch{count}:{query}")
    return items


def download(url, out_dir, audio_only=True, audio_format="mp3",
             progress_cb=None, cancel_event=None):
    """Download one URL. progress_cb receives yt-dlp progress dicts."""
    os.makedirs(out_dir, exist_ok=True)

    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if progress_cb is not None:
            progress_cb(d)

    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "continuedl": True,
        "windowsfilenames": True,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
        }]
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        raise
