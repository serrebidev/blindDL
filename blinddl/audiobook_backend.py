# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Audiobook backend: free audiobooks from LibriVox and friends.

Built on LeMetadatarr/audiobooker, which gives one search API and one
AudioBook shape across LibriVox, LoyalBooks and half a dozen smaller free
audiobook sites, with its own fuzzy scoring and deduplication. blindDL adds
the Internet Archive's audio collections as a further source, because it
answers in about a second and covers LibriVox's whole catalog by full text
search -- LibriVox's own API only matches titles from the start of the
string, so "sherlock holmes" finds nothing there.

An audiobook is a set of chapter files, not one download. Queueing a book
fetches every chapter into a folder of its own, in order, with zero-padded
names so any player and any file list reads them in the right order.

audiobooker is imported inside the functions, so blindDL still starts when
it is not installed; the job then fails with a clear "No module named
'audiobooker'" instead of taking the app down.
"""

from __future__ import annotations

import os
import re
import threading
import time
from urllib.parse import quote, unquote, urlparse

import requests

from . import search_order
from .book_backend import (
    HEADERS,
    IA_ARCHIVE_SORTS,
    IA_DETAILS_URL,
    IA_DOWNLOAD_URL,
    IA_METADATA_URL,
    IA_SEARCH_URL,
    format_size,
    safe_filename,
    score_match,
)
from .search_order import ORDER_POPULAR, ORDER_RECENT, ORDER_RELEVANCE

SOURCE_ARCHIVE_AUDIO = "Internet Archive"
# audiobooker's own source names, as its scrapers report them.
SOURCE_LIBRIVOX = "Librivox"
SOURCE_LOYALBOOKS = "LoyalBooks"

# The Archive sorts its own collections; audiobooker's scrapers drive site
# search forms that offer nothing but their own relevance ranking, so every
# audiobooker source answers by best match whatever is asked.
ORDER_SUPPORT = {SOURCE_ARCHIVE_AUDIO: frozenset({ORDER_RECENT,
                                                  ORDER_POPULAR})}

# Finished audiobooks land in this subfolder of the download directory.
AUDIOBOOK_SUBFOLDER = "Audiobooks"

SEARCH_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 20
DOWNLOAD_TIMEOUT_S = 300
SEARCH_ROWS = 200
MAX_RESULTS_PER_SOURCE = 200
# audiobooker scores 0-1; below this a hit is a different book entirely.
MIN_SCORE = 0.45
# blindDL's own 0-100 lexical floor, applied to every source alike.
MIN_MATCH_SCORE = 35.0

# The Internet Archive collections that hold spoken-word books. Anything
# else under mediatype:(audio) is music, radio or podcasts.
IA_AUDIOBOOK_COLLECTIONS = ("librivoxaudio", "audio_bookspoetry")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav")

_session_lock = threading.Lock()
_session = None
_sources_lock = threading.Lock()
_source_classes = None


class AudiobookDownloadCancelled(Exception):
    """Raised when the user cancels an audiobook download."""


def _http():
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            _session = session
        return _session


# -- sources ---------------------------------------------------------------


def _audiobooker_classes():
    """audiobooker's scraper classes, by the name they report as `source`.

    Returns an empty mapping when audiobooker is not installed, so the
    Internet Archive source still works on its own.
    """
    global _source_classes
    with _sources_lock:
        if _source_classes is None:
            try:
                from audiobooker.search import ALL_SOURCES
            except Exception:  # noqa: BLE001 - optional dependency
                _source_classes = {}
            else:
                _source_classes = {cls.__name__: cls for cls in ALL_SOURCES}
        return _source_classes


def all_sources():
    """Every audiobook source blindDL can search, in reading order."""
    return [SOURCE_ARCHIVE_AUDIO] + sorted(_audiobooker_classes())


def source_label(source):
    """Human-facing name for an audiobook source."""
    if source == SOURCE_LIBRIVOX:
        return "LibriVox"
    if source == SOURCE_ARCHIVE_AUDIO:
        return "Internet Archive"
    # "StephenKingAudioBooks" -> "Stephen King Audio Books"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", source)


def sources_by_label():
    return sorted(all_sources(), key=lambda s: source_label(s).lower())


def enabled_sources(disabled):
    """The sources to search, given the user's switched-off list."""
    disabled = set(disabled or ())
    return [source for source in all_sources() if source not in disabled]


# -- normalized results ----------------------------------------------------


def _item(source, identifier, title, author, **extra):
    item = {
        "id": f"{source}:{identifier}",
        "kind": "audiobook",
        "title": str(title or "Unknown title").strip(),
        "artist": str(author or "").strip(),
        "author": str(author or "").strip(),
        "narrator": "",
        "source": source_label(source),
        "backend_source": source,
        "identifier": identifier,
        "year": "",
        "duration_s": 0,
        "file_size": "",
        "size_bytes": 0,
        "chapters": 0,
        "streams": [],
        "url": "",
        "format": "",
    }
    item.update(extra)
    if item["size_bytes"] and not item["file_size"]:
        item["file_size"] = format_size(item["size_bytes"])
    if not item["format"]:
        item["format"] = _stream_format(item["streams"])
    return item


def _stream_format(streams):
    """The file type the chapters will arrive as, named for a reader.

    download() falls back to .mp3 for a stream whose URL hides the
    extension, so the search row says the same thing the file will be.
    """
    for url in streams or ():
        name = unquote(os.path.basename(urlparse(str(url)).path))
        extension = os.path.splitext(name)[1].lower()
        if extension in AUDIO_EXTENSIONS:
            return extension.lstrip(".").upper()
    return "MP3"


def _name(person):
    first = getattr(person, "first_name", "") or ""
    last = getattr(person, "last_name", "") or ""
    return f"{first} {last}".strip()


def _from_audiobook(book):
    """Turn one audiobooker AudioBook into a blindDL result row."""
    authors = ", ".join(filter(None, (_name(a) for a in book.authors or ())))
    narrators = ", ".join(
        filter(None, (_name(n) for n in getattr(book, "narrators", None) or ())))
    streams = [str(url) for url in (book.streams or ()) if url]
    external = getattr(book, "external_ids", None) or {}
    identifier = (external.get("librivox_id") or
                  (streams[0] if streams else book.title))
    return _item(
        book.source or SOURCE_LIBRIVOX, str(identifier), book.title, authors,
        narrator=narrators,
        year=str(book.year or "") if book.year else "",
        duration_s=int(book.runtime or 0),
        chapters=len(getattr(book, "chapters", None) or streams),
        streams=streams,
        url=streams[0] if streams else "",
    )


# -- Internet Archive ------------------------------------------------------


def search_archive(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search the Internet Archive's spoken-word collections."""
    escaped = re.sub(r'["\\]', " ", query).strip()
    collections = " OR ".join(f"collection:({name})"
                              for name in IA_AUDIOBOOK_COLLECTIONS)
    response = _http().get(
        IA_SEARCH_URL,
        params={
            "q": f"({escaped}) AND mediatype:(audio) AND ({collections})",
            "fl[]": ["identifier", "title", "creator", "year", "item_size",
                     "downloads", "publicdate"],
            "rows": SEARCH_ROWS,
            "page": 1,
            "output": "json",
            "sort[]": IA_ARCHIVE_SORTS[search_order.normalize(order)],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    items = []
    for doc in response.json().get("response", {}).get("docs", []) or []:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        creator = doc.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(str(part) for part in creator if part)
        items.append(_item(
            SOURCE_ARCHIVE_AUDIO, identifier, doc.get("title"), creator,
            year=str(doc.get("year") or ""),
            size_bytes=int(doc.get("item_size") or 0),
            url=f"{IA_DETAILS_URL}/{quote(str(identifier))}",
        ))
    return items


def archive_streams(identifier, timeout=HTTP_TIMEOUT_S):
    """Return the chapter audio URLs of one Internet Archive item, in order.

    Every LibriVox recording is published at several bitrates; the 64 kbps
    set is the one the site itself offers as the download, and keeps a long
    book to a sane size.
    """
    response = _http().get(f"{IA_METADATA_URL}/{quote(str(identifier))}",
                           timeout=timeout)
    response.raise_for_status()
    files = response.json().get("files") or ()
    by_extension = {}
    for entry in files:
        name = str(entry.get("name") or "")
        extension = os.path.splitext(name.lower())[1]
        if extension not in AUDIO_EXTENSIONS:
            continue
        by_extension.setdefault(extension, []).append(entry)
    for extension in AUDIO_EXTENSIONS:
        entries = by_extension.get(extension)
        if not entries:
            continue
        entries.sort(key=lambda entry: str(entry.get("name") or "").lower())
        return [
            (f"{IA_DOWNLOAD_URL}/{quote(str(identifier))}/"
             f"{quote(str(entry.get('name')))}",
             str(entry.get("name")))
            for entry in entries
        ]
    raise RuntimeError("That Internet Archive item has no audio files.")


# -- audiobooker sources ---------------------------------------------------


def search_audiobooker(source, query, timeout=HTTP_TIMEOUT_S):
    """Search one audiobooker source, with its own scoring and dedup."""
    classes = _audiobooker_classes()
    scraper = classes.get(source)
    if scraper is None:
        return []
    # _parallel_search rather than the public search(), which does not expose
    # the relevance floor; without it LibriVox's API answers an unmatched
    # query with its default catalog listing.
    from audiobooker.search import _parallel_search

    items = []
    for book in _parallel_search("search", query, [scraper()],
                                 max_per_source=SEARCH_ROWS, timeout=timeout,
                                 min_score=MIN_SCORE):
        if not book.streams:
            continue
        items.append(_from_audiobook(book))
    return items


# -- search ----------------------------------------------------------------


def _rank(items, query, order=ORDER_RELEVANCE):
    """Drop the noise and put the best answers to *order* first.

    Under best match that is the closest title; under the other two it is
    whatever the source replied with, since the sort was asked of the source.
    """
    for item in items:
        item["score"] = score_match(query, item.get("title", ""),
                                    item.get("author", ""))
    kept = [item for item in items if item["score"] >= MIN_MATCH_SCORE]
    if not kept:
        kept = list(items)
    indexed = sorted(
        enumerate(kept),
        key=lambda pair: search_order.rank_key(
            order, pair[1]["score"], pair[0]))
    return [item for _index, item in indexed][:MAX_RESULTS_PER_SOURCE]


def supports_order(source, order):
    """Whether one audiobook source can answer *order* itself."""
    return search_order.supported(ORDER_SUPPORT, source, order)


def search(query, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None, order=ORDER_RELEVANCE):
    """Search the chosen audiobook sources at once and return after timeout_s.

    Same contract as musicdl_backend.search and book_backend.search: sources
    run in parallel, the call returns at the deadline, and sources that
    answer late still report through on_site(source, items). Only the
    Internet Archive can be given an *order*; supports_order says so.

    Returns (items, answered, asked).
    """
    order = search_order.normalize(order)
    wanted = [source for source in (sources or all_sources())
              if source == SOURCE_ARCHIVE_AUDIO or
              source in _audiobooker_classes()]
    found = {}
    found_lock = threading.Lock()

    def search_one(source):
        if stop is not None and stop.is_set():
            return
        native = order if supports_order(source, order) else ORDER_RELEVANCE
        try:
            if source == SOURCE_ARCHIVE_AUDIO:
                rows = search_archive(query, order=order)
            else:
                rows = search_audiobooker(source, query)
            # Ranking is the expensive half, and a search the user has
            # already replaced has nowhere to put the answer.
            if stop is not None and stop.is_set():
                return
            items = _rank(rows, query, native)
        except Exception:  # noqa: BLE001 - one bad site must not kill the rest
            items = []
        with found_lock:
            found[source] = items
        if on_site is not None and (stop is None or not stop.is_set()):
            try:
                on_site(source_label(source), items)
            except Exception:  # noqa: BLE001 - a bad callback is not the site's fault
                pass

    threads = []
    for source in wanted:
        thread = threading.Thread(target=search_one, args=(source,),
                                  name=f"audiobook-search-{source}",
                                  daemon=True)
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
    return (items,
            [source_label(s) for s in wanted if s in answered],
            [source_label(s) for s in wanted])


# -- preview and download --------------------------------------------------


def first_stream(item):
    """A single playable URL for the preview player, or "" if there is none."""
    streams = item.get("streams") or ()
    if streams:
        return str(streams[0])
    identifier = item.get("identifier")
    if item.get("backend_source") == SOURCE_ARCHIVE_AUDIO and identifier:
        chapters = archive_streams(identifier)
        return chapters[0][0] if chapters else ""
    return ""


def resolve_chapters(item):
    """Return [(url, file name)] for every chapter of one audiobook."""
    if item.get("backend_source") == SOURCE_ARCHIVE_AUDIO:
        return archive_streams(item["identifier"])
    chapters = []
    for index, url in enumerate(item.get("streams") or (), start=1):
        name = unquote(os.path.basename(urlparse(str(url)).path)) or ""
        extension = os.path.splitext(name)[1].lower()
        if extension not in AUDIO_EXTENSIONS:
            extension = ".mp3"
        chapters.append((str(url), f"{index:03d}{extension}"))
    if not chapters:
        raise RuntimeError("That audiobook has no audio files to download.")
    return chapters


def download(item, out_dir, progress_cb=None, cancel_event=None):
    """Download every chapter of one audiobook into a folder of its own.

    progress_cb(downloaded, total) reports across the whole book, so the
    Downloads tab shows one percentage for the book rather than per chapter.
    Chapters already on disk with the expected size are left alone, so a
    cancelled book resumes instead of starting over.
    """
    chapters = resolve_chapters(item)
    author = item.get("author") or ""
    stem = f"{item.get('title', 'audiobook')} - {author}" if author else \
        str(item.get("title", "audiobook"))
    folder = os.path.join(out_dir, AUDIOBOOK_SUBFOLDER, safe_filename(stem))
    os.makedirs(folder, exist_ok=True)

    expected_total = int(item.get("size_bytes") or 0)
    downloaded_total = 0
    width = max(2, len(str(len(chapters))))
    for index, (url, name) in enumerate(chapters, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise AudiobookDownloadCancelled()
        extension = os.path.splitext(name)[1].lower() or ".mp3"
        if extension not in AUDIO_EXTENSIONS:
            extension = ".mp3"
        path = os.path.join(folder,
                            f"{index:0{width}d} - {safe_filename(os.path.splitext(name)[0])}{extension}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            downloaded_total += os.path.getsize(path)
            if progress_cb is not None:
                progress_cb(downloaded_total, expected_total)
            continue
        partial = path + ".part"
        try:
            with _http().get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S,
                             allow_redirects=True) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise AudiobookDownloadCancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded_total += len(chunk)
                        if progress_cb is not None:
                            progress_cb(downloaded_total, expected_total)
            os.replace(partial, path)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise
    return folder
