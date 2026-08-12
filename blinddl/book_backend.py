# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Book backend: search and download free ebooks from open-access libraries.

Built on the approach of joaorbarros/book-finder: ask Open Library for the
reference metadata, ask the Internet Archive for the actual scans, and rank
what comes back by lexical similarity rather than trusting either site's own
relevance order. blindDL adds Project Gutenberg and Standard Ebooks, whose
hand-made EPUB and plain-text editions read far better under a screen reader
than a scanned PDF ever will, and drops book-finder's pypdf/rapidfuzz
dependencies -- the matching is difflib, and integrity checking is done on
the file's magic bytes.

Every source is a public, key-less API serving public-domain or open-access
material. Internet Archive items that are lending-only ("access-restricted")
are filtered out: their files answer 401, so listing them would only offer
the user downloads that cannot happen.

Search mirrors musicdl_backend.search: sources run in parallel, the call
returns at the deadline, and sites that answer late still report through
on_site.
"""

from __future__ import annotations

import os
import re
import threading
import time
import defusedxml.ElementTree as ET
from difflib import SequenceMatcher
from urllib.parse import quote

import requests

from . import annas_backend, search_order
from .annas_backend import SOURCE_ANNAS
from .search_order import (
    ORDER_POPULAR,
    ORDER_RECENT,
    ORDER_RELEVANCE,
)

SOURCE_ARCHIVE = "Internet Archive"
SOURCE_OPENLIBRARY = "Open Library"
SOURCE_GUTENBERG = "Project Gutenberg"
SOURCE_STANDARD = "Standard Ebooks"
ALL_SOURCES = [
    SOURCE_ARCHIVE,
    SOURCE_OPENLIBRARY,
    SOURCE_GUTENBERG,
    SOURCE_STANDARD,
    SOURCE_ANNAS,
]

# Finished books land in this subfolder of the download directory, so a book
# never lands loose among the user's music.
BOOK_SUBFOLDER = "Books"

# Per-search wall clock budget, matching the music search. Sources that answer
# later are not thrown away; they arrive through on_site.
SEARCH_TIMEOUT_S = 5.0
# Hard socket timeout, so an abandoned search thread dies instead of hanging
# on a slow host for the rest of the session.
HTTP_TIMEOUT_S = 20
# Results requested per source before filtering and ranking.
SEARCH_ROWS = 200
# Ranked results kept per source. A broad list so a search returns plenty.
MAX_RESULTS_PER_SOURCE = 200
# Below this similarity a hit is noise rather than a different edition.
MIN_SCORE = 35.0

USER_AGENT = ("blindDL/1.0 (accessible downloader; "
              "https://github.com/serrebidev/blindDL)")
HEADERS = {"User-Agent": USER_AGENT}

IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata"
IA_DOWNLOAD_URL = "https://archive.org/download"
IA_DETAILS_URL = "https://archive.org/details"
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_URL = "https://openlibrary.org"
GUTENDEX_URL = "https://gutendex.com/books"
STANDARD_EBOOKS_FEED = "https://standardebooks.org/feeds/opds/all"

# How the Internet Archive is asked to order any of its collections. Shared
# with archive_backend and torrent_backend, which query the same endpoint.
# publicdate is when the item reached the Archive, which is what "newest"
# means for a library of scans -- the item's own `date` field is the year the
# work was made, so a 1940s radio show would sort as ancient however recently
# it was uploaded.
IA_ARCHIVE_SORTS = {
    ORDER_RELEVANCE: "downloads desc",
    ORDER_POPULAR: "downloads desc",
    ORDER_RECENT: "publicdate desc",
}

# Open Library sorts editions rather than works: `editions` is how many
# printings a title has, which is the closest thing it publishes to how
# widely read something is.
OPENLIBRARY_SORTS = {ORDER_RECENT: "new", ORDER_POPULAR: "editions"}
# Gutendex's default is already download count; `descending` orders by
# Gutenberg id, so the most recently transcribed books come first.
GUTENDEX_SORTS = {ORDER_RECENT: "descending", ORDER_POPULAR: "popular"}

# Which libraries can answer which order themselves. Standard Ebooks' OPDS
# search feed takes no sort, and Anna's Archive offers newest/oldest but
# nothing resembling a popularity figure.
ORDER_SUPPORT = {
    SOURCE_ARCHIVE: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    SOURCE_OPENLIBRARY: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    SOURCE_GUTENBERG: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    SOURCE_STANDARD: frozenset(),
    SOURCE_ANNAS: frozenset({ORDER_RECENT}),
}


def supports_order(source, order):
    """Whether one library can answer *order* itself."""
    return search_order.supported(ORDER_SUPPORT, source, order)

# Preference order for the file offered to the user. EPUB is reflowable and
# reads properly in every screen-reader-friendly reader; plain text always
# works; a scanned PDF is the last resort.
FORMAT_EPUB = "EPUB"
FORMAT_TEXT = "Text"
FORMAT_PDF = "PDF"
FORMAT_KINDLE = "Kindle"
FORMAT_PREFERENCE = [FORMAT_EPUB, FORMAT_TEXT, FORMAT_PDF, FORMAT_KINDLE]

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_OPDS_ACQUISITION = "http://opds-spec.org/acquisition"

_session_lock = threading.Lock()
_session = None


class BookDownloadCancelled(Exception):
    """Raised when the user cancels a book download."""


def _http():
    """One shared, thread-safe requests Session for all book traffic."""
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            _session = session
        return _session


# -- naming and formatting ------------------------------------------------


def source_label(source):
    """Human-facing name for a book source (already readable)."""
    return source


def sources_by_label():
    """Every book source, ordered the way a list should read."""
    return sorted(ALL_SOURCES, key=str.lower)


def enabled_sources(disabled):
    """The sources to search, given the user's switched-off list."""
    disabled = set(disabled or ())
    return [source for source in ALL_SOURCES if source not in disabled]


def format_size(size):
    """Bytes as a short human string; empty when the size is unknown."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _best_format(formats):
    """Pick the most readable format out of those a book offers."""
    available = {f for f in formats if f}
    for candidate in FORMAT_PREFERENCE:
        if candidate in available:
            return candidate
    return ""


def safe_filename(name):
    """A file name that survives Windows, macOS and Linux alike."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name or "book"))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "book")[:120]


# -- lexical matching (book-finder's ranking, without rapidfuzz) ----------


def _normalize(text):
    if isinstance(text, (list, tuple)):
        text = ", ".join(str(part) for part in text if part)
    if not text:
        return ""
    return re.sub(r"[^\w\s]", " ", str(text).casefold()).strip()


def _tokens(text):
    return [token for token in _normalize(text).split() if token]


def _token_sort_ratio(left, right):
    """Order-insensitive similarity, 0-100, on the standard library only."""
    left_tokens = " ".join(sorted(_tokens(left)))
    right_tokens = " ".join(sorted(_tokens(right)))
    if not left_tokens or not right_tokens:
        return 0.0
    return SequenceMatcher(None, left_tokens, right_tokens).ratio() * 100.0


def score_match(query, title, author=""):
    """How well one candidate answers the user's query, 0-100.

    The search box takes a single string, so "moby dick melville" has to score
    well against a title that carries only "Moby-Dick; or, The Whale". Every
    query word appearing somewhere in the title or the author is treated as a
    strong match, which is what a reader means by "found it".
    """
    title_score = _token_sort_ratio(query, title)
    combined_score = _token_sort_ratio(query, f"{title} {author}")
    score = max(title_score, combined_score)
    query_tokens = set(_tokens(query))
    if query_tokens and query_tokens <= set(_tokens(f"{title} {author}")):
        # Every word asked for is here, so this is the book -- but keep the
        # title similarity in the number, or every edition ties and the
        # source's own ordering is lost.
        score = max(score, 70.0 + 0.3 * title_score)
    return round(score, 2)


def _rank(items, query, order=ORDER_RELEVANCE):
    """Drop the noise and put the best answers to *order* first.

    The score is a filter under every order -- a book that does not answer
    the query is not the newest edition of it either -- but it only decides
    the sequence under best match. Newest and most popular were asked of the
    library itself, so its reply is kept in the order it arrived in.
    """
    for item in items:
        item["score"] = score_match(query, item.get("title", ""),
                                    item.get("author", ""))
    kept = [item for item in items if item["score"] >= MIN_SCORE]
    # An exhausted filter means nothing matched well; showing the site's own
    # best guesses beats showing an empty list.
    if not kept:
        kept = list(items)
    indexed = sorted(
        enumerate(kept),
        key=lambda pair: search_order.rank_key(
            order, pair[1]["score"], pair[0]))
    return [item for _index, item in indexed][:MAX_RESULTS_PER_SOURCE]


def _item(source, identifier, title, author, **extra):
    """One normalized result row, shaped like every other blindDL result."""
    item = {
        "id": f"{source}:{identifier}",
        "kind": "book",
        "title": str(title or "Unknown title").strip(),
        # The results list shows authors in its artist column, so books fill
        # both names and stay sortable with everything else.
        "artist": str(author or "").strip(),
        "author": str(author or "").strip(),
        "source": source,
        "identifier": identifier,
        "year": "",
        "format": "",
        "file_size": "",
        "size_bytes": 0,
        "url": "",
        "download_url": "",
    }
    item.update(extra)
    if item["size_bytes"] and not item["file_size"]:
        item["file_size"] = format_size(item["size_bytes"])
    return item


# -- Internet Archive ------------------------------------------------------

# Names and formats that cannot be opened without a DRM licence.
_IA_ENCRYPTED = re.compile(r"(lcp|acs|encrypted)", re.IGNORECASE)
_IA_FORMAT_MAP = (
    (FORMAT_EPUB, ("epub",)),
    (FORMAT_TEXT, ("djvutxt", "text", "ocr search text")),
    (FORMAT_PDF, ("pdf",)),
)


def _ia_formats(formats):
    """Map the Internet Archive's format names onto blindDL's four."""
    found = set()
    for raw in formats or ():
        name = str(raw)
        if _IA_ENCRYPTED.search(name):
            continue
        lowered = name.casefold()
        for label, needles in _IA_FORMAT_MAP:
            if any(needle in lowered for needle in needles):
                found.add(label)
    return found


def _ia_query(query, rows, timeout, order=ORDER_RELEVANCE):
    response = _http().get(
        IA_SEARCH_URL,
        params={
            "q": query,
            "fl[]": ["identifier", "title", "creator", "year", "format",
                     "item_size", "access-restricted-item", "publicdate"],
            "rows": rows,
            "page": 1,
            "output": "json",
            # Popular editions first by default: the Archive's own relevance
            # order buries readable scans under duplicated uploads.
            "sort[]": IA_ARCHIVE_SORTS[search_order.normalize(order)],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("response", {}).get("docs", []) or []


def search_archive(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search archive.org's text collection for downloadable books."""
    escaped = re.sub(r'["\\]', " ", query).strip()
    docs = _ia_query(f'title:("{escaped}") AND mediatype:(texts)',
                     SEARCH_ROWS, timeout, order)
    if len(docs) < 5:
        # A title-only phrase misses "the hobbit tolkien", where half the
        # query is the author. Fall back to the plain term search.
        seen = {doc.get("identifier") for doc in docs}
        for doc in _ia_query(f"({escaped}) AND mediatype:(texts)",
                             SEARCH_ROWS, timeout, order):
            if doc.get("identifier") not in seen:
                docs.append(doc)

    items = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        # Lending-only items answer 401 on every file. Never offer them.
        if str(doc.get("access-restricted-item", "")).lower() == "true":
            continue
        formats = _ia_formats(doc.get("format"))
        if not formats:
            continue
        creator = doc.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(str(part) for part in creator if part)
        items.append(_item(
            SOURCE_ARCHIVE, identifier, doc.get("title"), creator,
            year=str(doc.get("year") or ""),
            format=_best_format(formats),
            # item_size covers the whole item -- page scans included -- so it
            # would misreport a 2 MB EPUB as a gigabyte. The real file size
            # is known once the download resolves.
            url=f"{IA_DETAILS_URL}/{quote(str(identifier))}",
        ))
    return items


def _ia_file_score(entry):
    """Rank one Archive file: readable formats first, DRM never."""
    name = str(entry.get("name") or "")
    lowered = name.casefold()
    if _IA_ENCRYPTED.search(name) or _IA_ENCRYPTED.search(
            str(entry.get("format") or "")):
        return None
    if lowered.endswith(".epub"):
        return 0, FORMAT_EPUB
    if lowered.endswith("_djvu.txt") or lowered.endswith(".txt"):
        return 1, FORMAT_TEXT
    if lowered.endswith(".pdf"):
        return 2, FORMAT_PDF
    return None


def resolve_archive_file(identifier, timeout=HTTP_TIMEOUT_S):
    """Return (download URL, file name, format, size) for an Archive item."""
    response = _http().get(f"{IA_METADATA_URL}/{quote(str(identifier))}",
                           timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    metadata = payload.get("metadata") or {}
    if str(metadata.get("access-restricted-item", "")).lower() == "true":
        raise RuntimeError(
            "This Internet Archive item is lending-only and cannot be "
            "downloaded.")
    best = None
    for entry in payload.get("files") or ():
        rank = _ia_file_score(entry)
        if rank is None:
            continue
        candidate = (rank[0], len(str(entry.get("name"))), entry, rank[1])
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        raise RuntimeError("No downloadable book file in that Archive item.")
    _rank_index, _length, entry, book_format = best
    name = str(entry.get("name"))
    url = (f"{IA_DOWNLOAD_URL}/{quote(str(identifier))}/"
           f"{quote(name)}")
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return url, name, book_format, size


# -- Open Library ----------------------------------------------------------


def search_openlibrary(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search Open Library for editions whose full text is public.

    Open Library is book-finder's "answer key" -- correct title, author, year
    and page count -- and its public-access editions point at an Internet
    Archive identifier, so the rows it contributes are downloadable too.
    """
    params = {
        "q": query,
        "limit": SEARCH_ROWS,
        "fields": ("key,title,author_name,first_publish_year,"
                   "number_of_pages_median,ebook_access,ia"),
    }
    sort = OPENLIBRARY_SORTS.get(search_order.normalize(order))
    if sort:
        params["sort"] = sort
    response = _http().get(
        OPENLIBRARY_SEARCH_URL,
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    items = []
    for doc in response.json().get("docs", []) or []:
        # "public" is the only access level whose files are downloadable;
        # borrowable and print-disabled editions answer 401.
        if str(doc.get("ebook_access") or "").lower() != "public":
            continue
        identifiers = doc.get("ia") or []
        if not identifiers:
            continue
        identifier = identifiers[0]
        authors = doc.get("author_name") or []
        items.append(_item(
            SOURCE_OPENLIBRARY, identifier, doc.get("title"),
            ", ".join(str(author) for author in authors),
            year=str(doc.get("first_publish_year") or ""),
            pages=doc.get("number_of_pages_median") or 0,
            url=f"{OPENLIBRARY_URL}{doc.get('key', '')}",
        ))
    return items


# -- Project Gutenberg (Gutendex) -----------------------------------------

_GUTENBERG_FORMATS = (
    (FORMAT_EPUB, ("application/epub+zip",)),
    (FORMAT_TEXT, ("text/plain; charset=utf-8", "text/plain; charset=us-ascii",
                   "text/plain")),
    (FORMAT_KINDLE, ("application/x-mobipocket-ebook",)),
)


def _gutenberg_download(formats):
    """Pick the most readable non-archive download Gutenberg offers."""
    for label, media_types in _GUTENBERG_FORMATS:
        for media_type in media_types:
            url = formats.get(media_type)
            # .zip entries hold the same book, but a reader cannot open them.
            if url and not str(url).lower().endswith(".zip"):
                return str(url), label
    return "", ""


def search_gutenberg(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search Project Gutenberg's ~75,000 public-domain books via Gutendex."""
    params = {"search": query}
    sort = GUTENDEX_SORTS.get(search_order.normalize(order))
    if sort:
        params["sort"] = sort
    response = _http().get(GUTENDEX_URL, params=params, timeout=timeout)
    response.raise_for_status()
    items = []
    for book in response.json().get("results", []) or []:
        formats = book.get("formats") or {}
        url, book_format = _gutenberg_download(formats)
        if not url:
            continue
        authors = [author.get("name", "") for author in book.get("authors")
                   or ()]
        book_id = book.get("id")
        items.append(_item(
            SOURCE_GUTENBERG, str(book_id), book.get("title"),
            ", ".join(name for name in authors if name),
            format=book_format,
            download_url=url,
            url=f"https://www.gutenberg.org/ebooks/{book_id}",
        ))
    return items


# -- Standard Ebooks (OPDS) ------------------------------------------------


def _standard_entry(entry):
    title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
    author = ""
    author_node = entry.find(f"{_ATOM_NS}author")
    if author_node is not None:
        author = (author_node.findtext(f"{_ATOM_NS}name") or "").strip()
    page_url = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
    year = ""
    published = entry.findtext(f"{_ATOM_NS}published") or ""
    if len(published) >= 4 and published[:4].isdigit():
        year = published[:4]

    download_url = ""
    size = 0
    for link in entry.findall(f"{_ATOM_NS}link"):
        rel = link.get("rel") or ""
        if not rel.startswith(_OPDS_ACQUISITION):
            continue
        if link.get("type") != "application/epub+zip":
            continue
        # "Recommended compatible epub" comes first and is the one to take;
        # the advanced build targets modern readers only.
        download_url = link.get("href") or ""
        try:
            size = int(link.get("length") or 0)
        except (TypeError, ValueError):
            size = 0
        break
    if not (title and download_url):
        return None
    return _item(
        SOURCE_STANDARD, page_url or title, title, author,
        year=year, format=FORMAT_EPUB, size_bytes=size,
        download_url=download_url, url=page_url,
    )


def search_standard_ebooks(query, timeout=HTTP_TIMEOUT_S,
                           order=ORDER_RELEVANCE):
    """Search Standard Ebooks' hand-typeset public-domain EPUB catalog.

    The unfiltered OPDS feed is patron-only (401), but the same feed with a
    query is public, so blindDL never asks for the bulk catalog. That feed
    takes no sort of its own, so *order* is accepted and ignored here --
    ORDER_SUPPORT says as much, and the search reports it.
    """
    response = _http().get(STANDARD_EBOOKS_FEED,
                           params={"query": query, "per-page": SEARCH_ROWS},
                           timeout=timeout)
    response.raise_for_status()
    try:
        feed = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError(f"Standard Ebooks returned no usable feed: {exc}")
    items = []
    for entry in feed.findall(f"{_ATOM_NS}entry"):
        item = _standard_entry(entry)
        if item is not None:
            items.append(item)
    return items


# -- Anna's Archive --------------------------------------------------------


def search_annas(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search Anna's Archive's index of the shadow libraries.

    Rows carry the record's MD5; the file itself is resolved at download
    time through annas_backend's cascade.
    """
    items = []
    order = search_order.normalize(order)
    rows = annas_backend.search(query, timeout=timeout, order=order)
    if order != ORDER_RECENT:
        # Records held by LibGen are the ones a non-member can actually
        # download, so they lead; the rest stay listed, and say so if they
        # are picked. Under newest-first that reshuffle would undo the sort
        # the site was just asked for, so the site's order is left alone.
        rows.sort(key=lambda row: not row.get("on_libgen"))
    for row in rows:
        items.append(_item(
            SOURCE_ANNAS, row["md5"], row["title"], row["author"],
            year=row.get("year", ""),
            format=row.get("format", ""),
            size_bytes=row.get("size_bytes", 0),
            url=row.get("url", ""),
            md5=row["md5"],
        ))
    return items


# -- search ----------------------------------------------------------------

_SEARCHERS = {
    SOURCE_ARCHIVE: search_archive,
    SOURCE_OPENLIBRARY: search_openlibrary,
    SOURCE_GUTENBERG: search_gutenberg,
    SOURCE_STANDARD: search_standard_ebooks,
    SOURCE_ANNAS: search_annas,
}


def search(query, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None, order=ORDER_RELEVANCE):
    """Search the chosen book sources at once and return after timeout_s.

    Same contract as musicdl_backend.search. sources is a list of book source
    names; None means every source. Sites still working when the budget runs
    out are not waited for, but they are not thrown away either: on_site
    (source, items) fires for every source that answers, late ones included.
    Set the `stop` event to silence a superseded search.

    *order* is one of search_order's constants and goes out with the query.
    A library that cannot sort that way answers by its own best match;
    supports_order says which will, so a caller can tell the user.

    Returns (items, answered, asked).
    """
    order = search_order.normalize(order)
    wanted = [source for source in (sources or ALL_SOURCES)
              if source in _SEARCHERS]
    found = {}
    found_lock = threading.Lock()

    def search_one(source):
        if stop is not None and stop.is_set():
            return
        try:
            searcher = _SEARCHERS[source]
            try:
                rows = searcher(query, order=order)
            except TypeError as exc:
                # Keep test doubles and third-party source extensions written
                # for the pre-order call shape working. Do not swallow a
                # TypeError raised inside a current searcher.
                if "unexpected keyword argument 'order'" not in str(exc):
                    raise
                rows = searcher(query)
            items = _rank(rows, query,
                          order if supports_order(source, order)
                          else ORDER_RELEVANCE)
        except Exception:  # noqa: BLE001 - one bad site must not kill the rest
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
                                  name=f"book-search-{source}", daemon=True)
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


# -- download --------------------------------------------------------------

_EXTENSIONS = {
    FORMAT_EPUB: ".epub",
    FORMAT_TEXT: ".txt",
    FORMAT_PDF: ".pdf",
    FORMAT_KINDLE: ".azw3",
}
_KNOWN_EXTENSIONS = (".epub", ".pdf", ".txt", ".azw3", ".mobi", ".fb2",
                     ".djvu", ".cbz", ".cbr", ".rtf", ".doc", ".docx",
                     ".htm", ".html")
# The first bytes a real book file starts with. A site answering with an
# error page or a login wall fails this instead of landing in the library.
_MAGIC = {
    ".epub": b"PK\x03\x04",
    ".docx": b"PK\x03\x04",
    ".pdf": b"%PDF",
}
# FB2 books are XML, so only real markup documents are rejected here.
_HTML_MAGIC = (b"<!doctype html", b"<html")


def _resolve(item, config=None):
    """Return (url, format, expected size) for one search result."""
    url = item.get("download_url") or ""
    if url:
        return url, item.get("format") or "", int(item.get("size_bytes") or 0)
    if item.get("source") == SOURCE_ANNAS:
        # Anna's Archive indexes files rather than serving them; the cascade
        # decides whether the bytes come from a membership or from LibGen.
        key = (config or {}).get("annas_archive_key", "") if config else ""
        url = annas_backend.resolve_download(item["md5"], member_key=key)
        return url, item.get("format") or "", int(item.get("size_bytes") or 0)
    identifier = item.get("identifier")
    if not identifier:
        raise RuntimeError("That result carries no downloadable file.")
    # Archive and Open Library rows only know an item identifier at search
    # time; the file list costs a request, so it is fetched once, here.
    url, _name, book_format, size = resolve_archive_file(identifier)
    return url, book_format, size


def _extension(url, book_format):
    for known in _KNOWN_EXTENSIONS:
        if url.lower().split("?")[0].endswith(known):
            return known
    known = _EXTENSIONS.get(book_format)
    if known:
        return known
    # Anna's Archive names formats the way readers do: MOBI, FB2, DJVU...
    if book_format and book_format.isalnum():
        return f".{book_format.lower()}"
    return ".epub"


def _verify(path, extension):
    """Reject an error page or a truncated file before it reaches the shelf."""
    expected = _MAGIC.get(extension)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError as exc:
        raise RuntimeError(f"The downloaded book could not be read: {exc}")
    if size == 0:
        raise RuntimeError("The download was empty.")
    if expected and not head.startswith(expected):
        raise RuntimeError(
            "The download was not a book file. The source may have answered "
            "with an error page.")
    if not expected and head.lstrip().lower().startswith(_HTML_MAGIC):
        raise RuntimeError(
            "The download was a web page, not a book. The mirror may be "
            "asking for a browser check.")


def _open_stream(url, source):
    """Start the download, through whichever client that source needs."""
    if source == SOURCE_ANNAS:
        # LibGen and Anna's Archive both fingerprint their clients.
        return annas_backend.open_stream(url)
    return _http().get(url, stream=True, timeout=HTTP_TIMEOUT_S,
                       allow_redirects=True)


def download(item, out_dir, config=None, progress_cb=None, cancel_event=None):
    """Download one book result and return the finished file's path.

    progress_cb(downloaded, total) fires while the file streams, matching the
    other backends. Books go into a Books subfolder of the download
    directory. A partial or non-book download is deleted rather than left
    behind for the library to list.
    """
    url, book_format, expected_size = _resolve(item, config)
    extension = _extension(url, book_format)

    folder = os.path.join(out_dir, BOOK_SUBFOLDER)
    os.makedirs(folder, exist_ok=True)
    author = item.get("author") or item.get("artist") or ""
    stem = f"{item.get('title', 'book')} - {author}" if author else \
        str(item.get("title", "book"))
    path = os.path.join(folder, safe_filename(stem) + extension)
    partial = path + ".part"

    try:
        response = _open_stream(url, item.get("source"))
        try:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or
                        expected_size or 0)
            downloaded = 0
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise BookDownloadCancelled()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)
        finally:
            response.close()
        _verify(partial, extension)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return path
