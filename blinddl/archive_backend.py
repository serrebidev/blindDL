# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Internet Archive media: old-time radio, music, concerts, movies and TV.

Two projects shaped this one. codebox/old-time-radio showed that the
Archive's `oldtimeradio` and `radioprograms` collections are a complete
classic-radio station once you read each show item's file list through the
metadata API and take the MP3s in order. tkem/mopidy-internetarchive
supplied the rest of the shape: the advanced-search endpoint with an
explicit field list, browsing by collection, sorting by downloads, and a
format preference list so a player is handed the format it wants rather
than whatever the item happens to list first.

blindDL keeps both ideas and drops the streaming server. Each category here
is one Internet Archive query; a result is one item, and an item is often a
whole show with hundreds of episodes, so the file list is resolved when the
user picks it and the episodes are offered as separate downloads.

Everything is public domain or openly licensed material published by the
Archive itself; no key, account or scraping is involved.
"""

from __future__ import annotations

import os
import re
import threading
import time
from urllib.parse import quote

import requests

from .book_backend import (
    HEADERS,
    IA_DETAILS_URL,
    IA_DOWNLOAD_URL,
    IA_METADATA_URL,
    IA_SEARCH_URL,
    format_size,
    safe_filename,
    score_match,
)

CATEGORY_OTR = "Old-time radio"
CATEGORY_CONCERTS = "Live concerts"
CATEGORY_MUSIC = "Music & audio"
CATEGORY_MOVIES = "Movies"
CATEGORY_CLASSIC_TV = "Classic TV"
CATEGORY_TV_NEWS = "TV & news"

# Each category is one advanced-search query. mediatype:(collection) is
# excluded everywhere: those rows are sub-collections, not something that can
# be played or downloaded.
_NOT_COLLECTION = "NOT mediatype:(collection)"
CATEGORY_QUERIES = {
    CATEGORY_OTR: (
        "mediatype:(audio) AND (collection:(oldtimeradio) OR "
        f"collection:(radioprograms)) AND {_NOT_COLLECTION}"),
    CATEGORY_CONCERTS: f"mediatype:(etree) AND {_NOT_COLLECTION}",
    CATEGORY_MUSIC: (
        "mediatype:(audio) AND NOT collection:(librivoxaudio) AND NOT "
        "collection:(oldtimeradio) AND NOT collection:(radioprograms) AND "
        f"NOT collection:(audio_bookspoetry) AND {_NOT_COLLECTION}"),
    CATEGORY_MOVIES: (
        "mediatype:(movies) AND NOT collection:(tvarchive) AND NOT "
        f"collection:(classic_tv) AND {_NOT_COLLECTION}"),
    CATEGORY_CLASSIC_TV: (
        f"mediatype:(movies) AND collection:(classic_tv) AND {_NOT_COLLECTION}"),
    CATEGORY_TV_NEWS: (
        f"mediatype:(movies) AND collection:(tvarchive) AND {_NOT_COLLECTION}"),
}
AUDIO_CATEGORIES = [CATEGORY_OTR, CATEGORY_CONCERTS, CATEGORY_MUSIC]
VIDEO_CATEGORIES = [CATEGORY_MOVIES, CATEGORY_CLASSIC_TV, CATEGORY_TV_NEWS]
ALL_SOURCES = AUDIO_CATEGORIES + VIDEO_CATEGORIES

SEARCH_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 20
DOWNLOAD_TIMEOUT_S = 600
SEARCH_ROWS = 40
MAX_RESULTS_PER_SOURCE = 25
MIN_MATCH_SCORE = 30.0

# Playable formats, best first. The Archive publishes the same recording
# several times over; these are the ones a player handles without help.
AUDIO_PREFERENCE = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav")
VIDEO_PREFERENCE = (".mp4", ".m4v", ".webm", ".ogv", ".mkv", ".avi", ".mpeg",
                    ".mpg")
# Derivatives the Archive generates for its own player; never the main file.
_SKIP_NAMES = re.compile(r"(_thumb|_itemimage|__ia_thumb|_512kb\.mp4$)",
                         re.IGNORECASE)

_session_lock = threading.Lock()
_session = None


class ArchiveDownloadCancelled(Exception):
    """Raised when the user cancels an Internet Archive download."""


def _http():
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            _session = session
        return _session


def source_label(source):
    """Human-facing name for a category."""
    return source


def sources_by_label():
    return list(ALL_SOURCES)


def enabled_sources(disabled, categories=None):
    """The categories to search, given the user's switched-off list."""
    disabled = set(disabled or ())
    return [source for source in (categories or ALL_SOURCES)
            if source not in disabled]


def is_video_category(source):
    return source in VIDEO_CATEGORIES


# -- search ----------------------------------------------------------------


def _item(source, doc):
    identifier = doc.get("identifier")
    creator = doc.get("creator")
    if isinstance(creator, list):
        creator = ", ".join(str(part) for part in creator if part)
    size = int(doc.get("item_size") or 0)
    return {
        "id": f"archive:{identifier}",
        "kind": "archive",
        "title": str(doc.get("title") or identifier or "Untitled").strip(),
        # The results list shows creators in its artist column.
        "artist": str(creator or "").strip(),
        "creator": str(creator or "").strip(),
        "source": source,
        "identifier": identifier,
        "video": is_video_category(source),
        "year": str(doc.get("year") or ""),
        "size_bytes": size,
        "file_size": format_size(size),
        "url": f"{IA_DETAILS_URL}/{quote(str(identifier))}",
    }


def search_category(source, query, timeout=HTTP_TIMEOUT_S):
    """Run one category's Internet Archive query."""
    escaped = re.sub(r'["\\]', " ", query).strip()
    response = _http().get(
        IA_SEARCH_URL,
        params={
            "q": f"({escaped}) AND {CATEGORY_QUERIES[source]}",
            "fl[]": ["identifier", "title", "creator", "year", "item_size",
                     "downloads", "mediatype"],
            "rows": SEARCH_ROWS,
            "page": 1,
            "output": "json",
            # What the Archive's own visitors actually watch and listen to;
            # its plain relevance order surfaces a lot of duplicate uploads.
            "sort[]": "downloads desc",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return [_item(source, doc)
            for doc in response.json().get("response", {}).get("docs", []) or []
            if doc.get("identifier")]


def _rank(items, query):
    for item in items:
        item["score"] = score_match(query, item.get("title", ""),
                                    item.get("creator", ""))
    kept = [item for item in items if item["score"] >= MIN_MATCH_SCORE]
    if not kept:
        kept = list(items)
    indexed = sorted(enumerate(kept),
                     key=lambda pair: (-pair[1]["score"], pair[0]))
    return [item for _index, item in indexed][:MAX_RESULTS_PER_SOURCE]


def search(query, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None):
    """Search the chosen Archive categories at once and return after timeout_s.

    Same contract as musicdl_backend.search: categories run in parallel, the
    call returns at the deadline, and late categories still report through
    on_site(source, items).

    Returns (items, answered, asked).
    """
    wanted = [source for source in (sources or ALL_SOURCES)
              if source in CATEGORY_QUERIES]
    found = {}
    found_lock = threading.Lock()

    def search_one(source):
        if stop is not None and stop.is_set():
            return
        try:
            items = _rank(search_category(source, query), query)
        except Exception:  # noqa: BLE001 - one bad category must not kill the rest
            items = []
        with found_lock:
            found[source] = items
        if on_site is not None and (stop is None or not stop.is_set()):
            try:
                on_site(source, items)
            except Exception:  # noqa: BLE001 - a bad callback is not the site's fault
                pass

    threads = []
    for source in wanted:
        thread = threading.Thread(target=search_one, args=(source,),
                                  name=f"archive-search-{source}", daemon=True)
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + timeout_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    with found_lock:
        answered = dict(found)

    items = []
    for source in wanted:
        items.extend(answered.get(source, ()))
    return items, [s for s in wanted if s in answered], list(wanted)


# -- item files ------------------------------------------------------------


def item_files(identifier, video=False, timeout=HTTP_TIMEOUT_S):
    """Return the playable files of one item as normalized rows.

    One item can be a single film or a radio series with hundreds of
    episodes, so the caller decides whether to queue everything or ask.
    Only the best available format is returned, never the Archive's other
    derivatives of the same recording.
    """
    response = _http().get(f"{IA_METADATA_URL}/{quote(str(identifier))}",
                           timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    preference = VIDEO_PREFERENCE if video else AUDIO_PREFERENCE
    by_extension = {}
    for entry in payload.get("files") or ():
        name = str(entry.get("name") or "")
        if not name or _SKIP_NAMES.search(name):
            continue
        extension = os.path.splitext(name.lower())[1]
        if extension in preference:
            by_extension.setdefault(extension, []).append(entry)

    for extension in preference:
        entries = by_extension.get(extension)
        if not entries:
            continue
        entries.sort(key=lambda entry: str(entry.get("name") or "").lower())
        rows = []
        for entry in entries:
            name = str(entry.get("name"))
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            rows.append({
                "kind": "archive",
                "title": str(entry.get("title") or
                             os.path.splitext(os.path.basename(name))[0]),
                "artist": str(entry.get("creator") or ""),
                "file_name": name,
                "identifier": identifier,
                "video": video,
                "duration_s": _duration(entry.get("length")),
                "size_bytes": size,
                "file_size": format_size(size),
                "direct_url": (f"{IA_DOWNLOAD_URL}/{quote(str(identifier))}/"
                               f"{quote(name)}"),
                "url": f"{IA_DETAILS_URL}/{quote(str(identifier))}",
            })
        return rows
    raise RuntimeError("That Internet Archive item has no playable files.")


def _duration(length):
    """The Archive reports length as seconds or as mm:ss / hh:mm:ss."""
    if length in (None, ""):
        return None
    text = str(length)
    try:
        if ":" not in text:
            return float(text)
    except ValueError:
        return None
    seconds = 0.0
    try:
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    return seconds


def first_stream(item):
    """A playable URL for the preview player."""
    direct = item.get("direct_url")
    if direct:
        return str(direct)
    files = item_files(item["identifier"], video=bool(item.get("video")))
    return files[0]["direct_url"] if files else ""


# -- download --------------------------------------------------------------


def download(item, out_dir, progress_cb=None, cancel_event=None):
    """Download one Archive file, or a whole item, into a folder of its own.

    A row from item_files carries direct_url and downloads as itself. A whole
    item downloads every playable file it has, numbered so the folder reads
    in order.
    """
    folder_name = safe_filename(item.get("collection_title") or
                                item.get("title") or item.get("identifier"))
    if item.get("direct_url"):
        files = [item]
    else:
        files = item_files(item["identifier"], video=bool(item.get("video")))
    single = len(files) == 1

    folder = out_dir if single else os.path.join(out_dir, folder_name)
    os.makedirs(folder, exist_ok=True)

    total_expected = sum(int(entry.get("size_bytes") or 0) for entry in files)
    downloaded_total = 0
    width = max(2, len(str(len(files))))
    last_path = folder
    for index, entry in enumerate(files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise ArchiveDownloadCancelled()
        name = str(entry.get("file_name") or entry.get("title") or "file")
        extension = os.path.splitext(name)[1].lower()
        if not extension:
            extension = ".mp4" if entry.get("video") else ".mp3"
        stem = safe_filename(os.path.splitext(os.path.basename(name))[0])
        path = os.path.join(
            folder, stem + extension if single
            else f"{index:0{width}d} - {stem}{extension}")
        last_path = path
        if os.path.exists(path) and os.path.getsize(path) > 0:
            downloaded_total += os.path.getsize(path)
            if progress_cb is not None:
                progress_cb(downloaded_total, total_expected)
            continue
        partial = path + ".part"
        try:
            with _http().get(entry["direct_url"], stream=True,
                             timeout=DOWNLOAD_TIMEOUT_S,
                             allow_redirects=True) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise ArchiveDownloadCancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded_total += len(chunk)
                        if progress_cb is not None:
                            progress_cb(downloaded_total, total_expected)
            os.replace(partial, path)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise
    return last_path if single else folder
