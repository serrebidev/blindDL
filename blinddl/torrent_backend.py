# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Torrent search across public indexers, handed off to the user's client.

Several projects pointed the way here. sayem314/torrent-indexer supplied the
list of indexers worth asking and the idea of one normalized result whatever
the site; WhitlockXD/moviepilot-custom singled out the public Pirate Bay and
BitSearch endpoints; Wizzel-F50/Limetorrents-addon the LimeTorrents search
path; SimoneFelici/bt1337xearch the category and seeder ordering a torrent
list needs; focarica/AutoTorrent the plain "search, pick, fetch" shape; and
araidz/Trawl the two that matter most, Knaben and SolidTorrents.

Knaben earns its place at the front of the list: it is a meta-search over
many indexers at once, 1337x among them, and it answers as one JSON API.
That is how 1337x results reach this module at all -- bt1337xearch drives a
headless browser through Scrapling to answer 1337x's Cloudflare challenge
directly, which is a very large dependency for one site.

The other thing not copied is the download engine. AutoTorrent links
libtorrent, Trawl and PyFlow-Omni spawn aria2, Eskoxx/Freedom streams
through webtorrent-cli. A chosen torrent is instead handed to whatever
BitTorrent client the user already has -- which is where their downloads,
disks and seeding rules live anyway.

Nothing is downloaded by this module. It searches, and it opens a magnet.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
import defusedxml.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from . import search_order
from .book_backend import (
    HEADERS,
    IA_ARCHIVE_SORTS,
    format_size,
    safe_filename,
    score_match,
)
from .runtime import open_file, open_magnet
from .search_order import ORDER_POPULAR, ORDER_RECENT, ORDER_RELEVANCE

SOURCE_PIRATEBAY = "The Pirate Bay"
SOURCE_EZTV = "EZTV"
SOURCE_NYAA = "Nyaa"
SOURCE_TORRENTS_CSV = "Torrents-CSV"
SOURCE_LIMETORRENTS = "LimeTorrents"
SOURCE_BITSEARCH = "BitSearch / SolidTorrents"
SOURCE_KNABEN = "Knaben"
SOURCE_ARCHIVE = "Internet Archive"
# eBookelo and Audiobook Bay publish no magnets in their search pages: the
# hash is only revealed on each book's own page, so the row keeps the page
# URL and the magnet is resolved from it when the user actually downloads.
SOURCE_EBOOKELO = "eBookelo"
SOURCE_AUDIOBOOKBAY = "Audiobook Bay"
ALL_SOURCES = [
    SOURCE_KNABEN,
    SOURCE_PIRATEBAY,
    SOURCE_EZTV,
    SOURCE_NYAA,
    SOURCE_TORRENTS_CSV,
    SOURCE_LIMETORRENTS,
    SOURCE_BITSEARCH,
    SOURCE_ARCHIVE,
    SOURCE_EBOOKELO,
    SOURCE_AUDIOBOOKBAY,
]

# Which indexers can be asked for an order, and which are simply already in
# one. Several of these publish exactly one ordering and no way to change it,
# so they are listed for the order they already answer in rather than left
# out: asking apibay for the most popular torrent is answering the question,
# even though no parameter goes out.
#
# Knaben is the one that looks like it should and does not. Its order_by
# field replaces the search rather than sorting it -- asking for seeders
# returns the most-seeded torrents on the whole site, not the ones that match
# -- so it is left on relevance under every order and _rank arranges its rows.
# Torznab and Newznab define no sort at all, so the user's own feeds are in
# the same position.
ORDER_SUPPORT = {
    SOURCE_KNABEN: frozenset(),
    SOURCE_PIRATEBAY: frozenset({ORDER_POPULAR}),
    SOURCE_EZTV: frozenset({ORDER_RECENT}),
    SOURCE_NYAA: frozenset({ORDER_RECENT}),
    SOURCE_TORRENTS_CSV: frozenset({ORDER_POPULAR}),
    SOURCE_LIMETORRENTS: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    SOURCE_BITSEARCH: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    SOURCE_ARCHIVE: frozenset({ORDER_RECENT, ORDER_POPULAR}),
    # Book and audiobook sites answer their own relevance order and nothing
    # else; _rank arranges their rows from there.
    SOURCE_EBOOKELO: frozenset(),
    SOURCE_AUDIOBOOKBAY: frozenset(),
}

# LimeTorrents puts its sort in the path: /search/all/<query>/<sort>/<page>/.
_LIMETORRENTS_SORTS = {ORDER_RECENT: "date", ORDER_POPULAR: "seeds"}
# BitSearch takes it as a query parameter, and already defaults to seeders.
_BITSEARCH_SORTS = {ORDER_RELEVANCE: "seeders", ORDER_POPULAR: "seeders",
                    ORDER_RECENT: "date"}

PIRATEBAY_URL = "https://apibay.org/q.php"
EZTV_URL = "https://eztvx.to/api/get-torrents"
# EZTV's API is keyed on IMDb ids, so a title is resolved to one first.
# TVmaze covers television only and needs no key, which suits both ends.
TVMAZE_URL = "https://api.tvmaze.com/singlesearch/shows"
NYAA_URL = "https://nyaa.si/"
TORRENTS_CSV_URL = "https://torrents-csv.com/service/search"
LIMETORRENTS_URL = "https://www.limetorrents.lol/search/all"
# The origin. solidtorrents.to and bitsearch.to are both 301s onto it.
BITSEARCH_URL = "https://bitsearch.eu/api/v1/search"
KNABEN_URL = "https://api.knaben.org/v1"
# eBookelo is a Spanish-language book site that indexes books in many
# languages (each row says which), and serves every book as a torrent.
EBOOKELO_URL = "https://ww2.ebookelo.com"
# Audiobook Bay is the public torrent index for audiobooks; the hash is
# published on each book's detail page, never in the search listing.
AUDIOBOOKBAY_URL = "https://audiobookbay.lu"
# The Archive publishes every public item as a torrent as well, and indexes
# that file as a format of its own, so asking for the format is what keeps
# the results to items BitTorrent can actually fetch.
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_TORRENT_URL = "https://archive.org/download"
ARCHIVE_DETAILS_URL = "https://archive.org/details"

SEARCH_TIMEOUT_S = 8.0
HTTP_TIMEOUT_S = 20
SEARCH_ROWS = 200
MAX_RESULTS_PER_SOURCE = 200
MIN_MATCH_SCORE = 30.0

# Open trackers added to a magnet built from a bare info hash. Without them a
# client has only the DHT to go on, and a cold start can take minutes.
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
)

# The Pirate Bay's numeric categories, by leading digit.
_PIRATEBAY_CATEGORIES = {
    "1": "Audio",
    "2": "Video",
    "3": "Applications",
    "4": "Games",
    "5": "Other",
    "6": "Other",
}

_HASH_RE = re.compile(r"\b([0-9a-fA-F]{40})\b")
_TAG_RE = re.compile(r"<[^>]+>")
_NYAA_NS = "{https://nyaa.si/xmlns/nyaa}"

_session_lock = threading.Lock()
_session = None


def _http():
    """One shared, thread-safe requests Session for all torrent traffic."""
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.headers.update(HEADERS)
            _session = session
        return _session


# -- user-added feeds --------------------------------------------------------

# A feed is one Torznab or Newznab endpoint the user added: a Prowlarr or
# Jackett instance, or any tracker that publishes the same API directly.
# Private trackers work through these, and only through these -- the login,
# the cookie and the passkey stay in the tool that already holds them, and
# blindDL never asks for a tracker password.


def feeds(config):
    """The user's own indexer feeds, as normalized dicts.

    Anything without both a name and a URL is skipped rather than searched:
    a half-filled row would otherwise fail on every search.
    """
    rows = []
    seen = set()
    for entry in (config or {}).get("torznab_feeds") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name or not url or name in seen:
            continue
        seen.add(name)
        rows.append({
            "name": name,
            "url": url,
            "api_key": str(entry.get("api_key") or "").strip(),
        })
    return rows


def feed_named(config, name):
    """One feed by its name, or None."""
    return next((feed for feed in feeds(config) if feed["name"] == name), None)


# -- naming ----------------------------------------------------------------


def source_label(source):
    """Human-facing name for an indexer (already readable)."""
    return source


def all_sources(config=None):
    """Every indexer available: the built-in ones plus the user's feeds."""
    return list(ALL_SOURCES) + [feed["name"] for feed in feeds(config)]


def sources_by_label(config=None):
    """Every indexer, ordered the way a list should read."""
    return sorted(all_sources(config), key=str.lower)


def enabled_sources(disabled, config=None):
    """The indexers to search, given the user's switched-off list."""
    disabled = set(disabled or ())
    return [source for source in all_sources(config)
            if source not in disabled]


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _timestamp(value):
    """Seconds since the epoch from an ISO date, or 0 when unreadable.

    Knaben and SolidTorrents both date their rows in ISO 8601 rather than
    with the unix times the other indexers use.
    """
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return 0
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0


def _pubdate(value):
    """Seconds since the epoch from an RSS pubDate, or 0 when unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        # parsedate_to_datetime drops the timezone, but an RSS pubDate is
        # always GMT; interpret it as UTC so the epoch (and the "age" and
        # recent-sort that derive from it) is not shifted by the local offset.
        return (
            parsedate_to_datetime(text)
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError):
        return _timestamp(text)


def _age(unix_time):
    """How long ago something was posted, said the way a person says it."""
    seconds = time.time() - float(unix_time or 0)
    if not unix_time or seconds < 0:
        return ""
    for limit, divisor, word in (
            (3600, 60, "minute"), (86400, 3600, "hour"),
            (2592000, 86400, "day"), (31536000, 2592000, "month")):
        if seconds < limit:
            count = max(1, int(seconds // divisor))
            return f"{count} {word}{'s' if count != 1 else ''} ago"
    count = max(1, int(seconds // 31536000))
    return f"{count} year{'s' if count != 1 else ''} ago"


def _text(value):
    """Markup and entities out of a scraped fragment.

    Tags become spaces rather than vanishing, so two words either side of
    one do not run together; the runs that leaves are collapsed here, since
    a screen reader should read a title the way it is written.
    """
    stripped = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return re.sub(r"\s+", " ", stripped).strip()


def _item(source, infohash, title, **extra):
    """One normalized result row, shaped like every other blindDL result."""
    infohash = str(infohash or "").strip().lower()
    item = {
        "id": f"{source}:{infohash or title}",
        "kind": "torrent",
        "title": str(title or "Untitled").strip(),
        # The results list shows uploaders in its artist column.
        "artist": "",
        "uploader": "",
        "source": source,
        "infohash": infohash,
        "magnet": "",
        # A private tracker's authenticated .torrent, when it offers no
        # magnet. Fetched only when the user actually downloads the row.
        "download_url": "",
        "format": "",
        "seeders": 0,
        "leechers": 0,
        "size_bytes": 0,
        "file_size": "",
        # When the row was posted, as seconds since the epoch, and the same
        # moment said the way a person says it. The number is what a newest
        # -first sort needs; the words are what the results list reads out.
        # 0 means the indexer did not say, which is not the same as "old".
        "posted": 0,
        "age": "",
        "url": "",
    }
    item.update(extra)
    if item["size_bytes"] and not item["file_size"]:
        item["file_size"] = format_size(item["size_bytes"])
    if item["posted"] and not item["age"]:
        item["age"] = _age(item["posted"])
    if not item["magnet"] and item["infohash"] and not item["download_url"]:
        item["magnet"] = magnet_for(item)
    if not item["artist"]:
        item["artist"] = item["uploader"]
    return item


# -- magnets ---------------------------------------------------------------


def magnet_for(item):
    """The magnet link for one result, built from its hash when needed.

    Indexers that publish a magnet of their own keep it -- it carries the
    trackers that site's swarm actually uses. The rest get the info hash plus
    the open trackers above.

    A row that came with its own .torrent gets no magnet at all. Those are
    private trackers: the torrent is flagged private, so DHT and peer
    exchange are off and only the tracker's own announce URL -- carrying the
    account's passkey, and present only inside that file -- can find the
    swarm. Bolting the open trackers above onto a private info hash would
    not connect to anything, and announcing a private torrent to public
    trackers is what gets an account banned from one.
    """
    existing = str(item.get("magnet") or "").strip()
    if existing.startswith("magnet:"):
        return existing
    if str(item.get("download_url") or "").strip():
        return ""
    infohash = str(item.get("infohash") or "").strip().lower()
    if not infohash:
        return ""
    query = [("dn", str(item.get("title") or "").strip())]
    query.extend(("tr", tracker) for tracker in TRACKERS)
    return f"magnet:?xt=urn:btih:{infohash}&{urlencode(query)}"


def resolve_magnet(item, timeout=HTTP_TIMEOUT_S):
    """The magnet that starts *item*, fetching it from the source if needed.

    Most indexers publish the hash with the row, so magnet_for answers at
    once. eBookelo and Audiobook Bay only reveal it on the book's own page,
    so the magnet is resolved from that page the first time the row is
    actually downloaded. Returns "" when the row has no hash anywhere.
    """
    magnet = magnet_for(item)
    if magnet:
        return magnet
    source = str(item.get("source") or "")
    try:
        if source == SOURCE_EBOOKELO:
            return _ebookelo_magnet(item, timeout=timeout)
        if source == SOURCE_AUDIOBOOKBAY:
            return _audiobookbay_magnet(item, timeout=timeout)
    except Exception:  # noqa: BLE001 - a dead site must not kill the queue row
        return ""
    return ""


def fetch_torrent_file(item, out_dir, timeout=HTTP_TIMEOUT_S):
    """Save one result's .torrent file and return the path.

    Private trackers publish an authenticated .torrent rather than a magnet,
    and the URL only works for the account that was given it. Saving the file
    and opening that is what carries the tracker's passkey through to the
    client; handing the client the URL would not.
    """
    # A torrent opened from the file manager is already on this disk. There
    # is nothing to fetch, and fetching is not merely wasteful: the file the
    # user picked is the only copy that carries a private tracker's passkey.
    local = str(item.get("torrent_path") or "").strip()
    if local and os.path.isfile(local):
        return local
    url = str(item.get("download_url") or "").strip()
    if not url:
        raise RuntimeError("That result carries no torrent file to fetch.")
    os.makedirs(out_dir, exist_ok=True)
    response = _http().get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    body = response.content
    # Some trackers answer a spent or unauthorised link with an HTML page and
    # a 200. A torrent file is bencoded and always starts with a dictionary.
    if not body.startswith(b"d"):
        raise RuntimeError(
            "That tracker did not return a torrent file. The link may have "
            "expired, or the feed's API key may be wrong.")
    path = os.path.join(out_dir, safe_filename(item.get("title")) + ".torrent")
    with open(path, "wb") as handle:
        handle.write(body)
    return path


def hand_off(item, out_dir=None):
    """Open one result in whatever BitTorrent client the user has set up.

    Prefers a magnet, which needs nothing fetched. Falls back to downloading
    the tracker's own .torrent and opening that, which is the only thing that
    works for a private tracker. Returns what was opened.
    """
    magnet = resolve_magnet(item)
    if magnet:
        open_magnet(magnet)
        return magnet
    if item.get("download_url") and out_dir:
        path = fetch_torrent_file(item, out_dir)
        open_file(path)
        return path
    if item.get("torrent_path") and os.path.isfile(item["torrent_path"]):
        open_file(item["torrent_path"])
        return item["torrent_path"]
    raise RuntimeError("That result carries no magnet link or info hash.")


def is_torrent_link(text):
    """Whether *text* is a magnet link or the path of a torrent file."""
    candidate = str(text or "").strip().strip('"')
    if candidate.lower().startswith("magnet:"):
        return True
    return (candidate.lower().endswith(".torrent")
            and os.path.isfile(candidate))


def item_from_link(link):
    """A queue payload for a magnet link or a .torrent file on disk.

    This is what a torrent handed to blindDL from outside becomes -- opened
    from a file manager, or clicked in a browser -- so that it joins the
    download queue as the same kind of row a search result does, and every
    part of blindDL downstream of the queue treats it identically.
    """
    candidate = str(link or "").strip().strip('"')
    if candidate.lower().startswith("magnet:"):
        query = parse_qs(urlparse(candidate).query)
        # A magnet names itself in dn=, though it is not obliged to. Falling
        # back to the info hash beats a row called "Untitled": it is what
        # the swarm knows the torrent by, and it is what the row will be
        # replaced with once metadata arrives anyway.
        name = (query.get("dn") or [""])[0].strip()
        infohash = ""
        for urn in query.get("xt") or []:
            if str(urn).lower().startswith("urn:btih:"):
                infohash = str(urn)[len("urn:btih:"):].strip().lower()
                break
        item = _item("Magnet link", infohash, name or infohash or "Torrent")
        item["magnet"] = candidate
        return item
    path = os.path.abspath(candidate)
    if not os.path.isfile(path):
        raise RuntimeError(f"There is no torrent file at {candidate}.")
    item = _item("Torrent file", "", os.path.splitext(
        os.path.basename(path))[0])
    # Kept rather than fetched: the file is already on this disk, and it is
    # the only copy that carries a private tracker's passkey.
    item["torrent_path"] = path
    return item


# -- The Pirate Bay ---------------------------------------------------------


def search_piratebay(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Query the public apibay endpoint, which answers in plain JSON.

    apibay takes no sort and always replies best-seeded first, which is the
    answer to "most popular" already; *order* is accepted for one uniform
    call shape and needs nothing sent.
    """
    response = _http().get(PIRATEBAY_URL, params={"q": query, "cat": "0"},
                           timeout=timeout)
    response.raise_for_status()
    items = []
    for doc in response.json() or ():
        infohash = str(doc.get("info_hash") or "")
        # An empty search answers with one placeholder row rather than [].
        if not infohash or set(infohash) == {"0"}:
            continue
        category = str(doc.get("category") or "")
        items.append(_item(
            SOURCE_PIRATEBAY, infohash, doc.get("name"),
            uploader=str(doc.get("username") or ""),
            format=_PIRATEBAY_CATEGORIES.get(category[:1], ""),
            seeders=_int(doc.get("seeders")),
            leechers=_int(doc.get("leechers")),
            size_bytes=_int(doc.get("size")),
            posted=_int(doc.get("added")),
            url=f"https://thepiratebay.org/description.php?id={doc.get('id')}",
        ))
    return items


# -- EZTV -------------------------------------------------------------------


def imdb_id_for(query, timeout=HTTP_TIMEOUT_S):
    """The IMDb id of the programme a query names, or "".

    TVmaze answers this without a key and only about television, which is
    exactly the question being asked -- a film title finds nothing here, and
    EZTV would have nothing for it either.
    """
    try:
        response = _http().get(TVMAZE_URL, params={"q": query},
                               timeout=timeout)
        if response.status_code != 200:
            return ""
        imdb = (response.json().get("externals") or {}).get("imdb")
    except Exception:  # noqa: BLE001 - a name lookup must not fail the search
        return ""
    return str(imdb or "").strip()


def search_eztv(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """EZTV indexes television only, and answers with a magnet per row.

    Its API is keyed on IMDb ids and cannot be given text at all, which is
    why every other EZTV tool asks its user to supply the id by hand. The
    programme is looked up by name first instead, so a search for "the
    office" reaches the whole run of it rather than whatever happens to be
    on the front page.

    Without an id -- a film, or a show TVmaze does not know -- the recent
    releases are scanned instead, and _rank drops what does not match.

    The API has no sort: it answers newest episode first, which is the whole
    of what it can be asked for. That is why it is listed as answering
    "most recent" and nothing else.
    """
    imdb = imdb_id_for(query, timeout=timeout)
    params = {"limit": "100", "page": "1"}
    if imdb:
        # The API wants the bare number; the tt prefix returns nothing.
        params["imdb_id"] = imdb[2:] if imdb.lower().startswith("tt") else imdb
    response = _http().get(EZTV_URL, params=params, timeout=timeout)
    response.raise_for_status()
    items = []
    for doc in response.json().get("torrents") or ():
        items.append(_item(
            SOURCE_EZTV, doc.get("hash"),
            doc.get("title") or doc.get("filename"),
            magnet=str(doc.get("magnet_url") or ""),
            format="Video",
            seeders=_int(doc.get("seeds")),
            leechers=_int(doc.get("peers")),
            size_bytes=_int(doc.get("size_bytes")),
            posted=_int(doc.get("date_released_unix")),
            url=f"https://eztvx.to/ep/{doc.get('id')}/",
        ))
    return items


# -- Nyaa -------------------------------------------------------------------


def search_nyaa(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Nyaa publishes its search as RSS, with the swarm counts in the feed.

    The feed ignores the sort parameters the website itself takes and always
    answers newest first, so it is listed as answering "most recent" and its
    rows are left in the order they arrive under that order.
    """
    response = _http().get(
        NYAA_URL, params={"page": "rss", "q": query}, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for entry in root.iterfind("./channel/item"):
        title = (entry.findtext("title") or "").strip()
        if not title:
            continue
        size = (entry.findtext(f"{_NYAA_NS}size") or "").strip()
        items.append(_item(
            SOURCE_NYAA, entry.findtext(f"{_NYAA_NS}infoHash"), title,
            format=(entry.findtext(f"{_NYAA_NS}category") or "").strip(),
            seeders=_int(entry.findtext(f"{_NYAA_NS}seeders")),
            leechers=_int(entry.findtext(f"{_NYAA_NS}leechers")),
            # Nyaa states the size itself; there is no byte count to convert.
            file_size=size.replace("GiB", "GB").replace("MiB", "MB"),
            posted=int(_pubdate(entry.findtext("pubDate"))),
            url=(entry.findtext("guid") or "").strip(),
        ))
    return items


# -- Torrents-CSV -----------------------------------------------------------


def search_torrents_csv(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """A plain JSON index with no site to scrape and no rate limiting.

    It takes no sort either, and answers best-seeded first -- so like apibay
    it is already the answer to "most popular" and nothing else.
    """
    response = _http().get(TORRENTS_CSV_URL,
                           params={"q": query, "size": SEARCH_ROWS},
                           timeout=timeout)
    response.raise_for_status()
    items = []
    for doc in response.json().get("torrents") or ():
        items.append(_item(
            SOURCE_TORRENTS_CSV, doc.get("infohash"), doc.get("name"),
            seeders=_int(doc.get("seeders")),
            leechers=_int(doc.get("leechers")),
            size_bytes=_int(doc.get("size_bytes")),
            posted=_int(doc.get("created_unix")),
        ))
    return items


# -- LimeTorrents -----------------------------------------------------------

# Its results are an old-style table: the info hash is in the .torrent link
# that opens each row, and the four numbers follow in fixed order.
_LIME_ROW_RE = re.compile(
    r'<td class="tdleft">(?P<name>.*?)</td>\s*'
    r'<td class="tdnormal">(?P<age>.*?)</td>\s*'
    r'<td class="tdnormal">(?P<size>.*?)</td>\s*'
    r'<td class="tdseed">(?P<seeds>.*?)</td>\s*'
    r'<td class="tdleech">(?P<leech>.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)


def search_limetorrents(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Scrape one LimeTorrents search page.

    Its sort is a path segment rather than a parameter: /search/all/<query>/
    takes an optional /<sort>/<page>/ after it, where the sort is `seeds` or
    `date`. Without one the site uses its own relevance ranking.
    """
    sort = _LIMETORRENTS_SORTS.get(search_order.normalize(order))
    path = (f"{LIMETORRENTS_URL}/{quote(query)}/{sort}/1/" if sort
            else f"{LIMETORRENTS_URL}/{quote(query)}/")
    response = _http().get(path, timeout=timeout)
    response.raise_for_status()
    items = []
    for match in _LIME_ROW_RE.finditer(response.text):
        cell = match.group("name")
        found = _HASH_RE.search(cell)
        if not found:
            continue
        # The row's own page link is the second anchor; the first is the
        # .torrent file on itorrents.
        links = re.findall(r'href="([^"]+)"', cell)
        page = next((link for link in links if link.endswith(".html")), "")
        # The Added cell carries the category too, as "1 Year+ - in Movies".
        # Split rather than drop it: that is the only thing LimeTorrents says
        # about what a row actually is.
        age, _, category = _text(match.group("age")).partition(" - in ")
        items.append(_item(
            SOURCE_LIMETORRENTS, found.group(1), _text(cell),
            format=category.strip(),
            seeders=_int(_text(match.group("seeds")).replace(",", "")),
            leechers=_int(_text(match.group("leech")).replace(",", "")),
            file_size=_text(match.group("size")),
            age=age.strip(),
            url=f"https://www.limetorrents.lol{page}" if page else "",
        ))
    return items


# -- BitSearch / SolidTorrents ----------------------------------------------


def search_bitsearch(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """One index, reached through its JSON API.

    BitSearch and SolidTorrents are the same service: the same torrent ids
    and the same info hashes come back from both, and solidtorrents.to
    redirects onto bitsearch.to, which redirects onto bitsearch.eu. They are
    one source here rather than two, or every result would be listed twice.

    The API is asked directly at the origin, both to skip that redirect pair
    and because it answers with a hundred rows where either website shows
    twenty. It publishes no magnet, so the info hash builds one.
    """
    response = _http().get(
        BITSEARCH_URL,
        params={
            "q": query,
            "sort": _BITSEARCH_SORTS[search_order.normalize(order)],
            "limit": 100,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    items = []
    for doc in response.json().get("results") or ():
        items.append(_item(
            SOURCE_BITSEARCH, doc.get("infohash"), doc.get("title"),
            seeders=_int(doc.get("seeders")),
            leechers=_int(doc.get("leechers")),
            size_bytes=_int(doc.get("size")),
            # updatedAt is when the row was last re-scraped rather than when
            # the torrent was posted, which is all this API carries.
            posted=int(_timestamp(doc.get("updatedAt"))),
            url=f"https://bitsearch.eu/torrent/{doc.get('id')}",
        ))
    return items


# -- Knaben ------------------------------------------------------------------


def search_knaben(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Knaben is a meta-search over many indexers, answering as one JSON API.

    This is how blindDL covers 1337x: Knaben runs its own scraper against it
    and serves the cached rows, so the results arrive without the headless
    browser 1337x's own Cloudflare challenge would otherwise demand.

    *order* is deliberately not forwarded; see the note on order_by below.
    """
    response = _http().post(
        KNABEN_URL,
        json={
            "search_type": "score",
            "search_field": "title",
            "query": query,
            # No order_by on purpose. Knaben sorts by whichever field is
            # named instead of by relevance, so asking for seeders returns
            # the most-seeded torrents on the site rather than the ones that
            # answer the query. Relevance order comes back by default, and
            # _rank puts the swarm counts in charge from there -- so a wide
            # page is fetched to give it enough matches to choose from.
            "size": 100,
            # Rows its own scanners flagged, and adult material, which has
            # its own engines in blindDL and is off unless asked for.
            "hide_unsafe": True,
            "hide_xxx": True,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    items = []
    for doc in response.json().get("hits") or ():
        seeders = _int(doc.get("seeders"))
        items.append(_item(
            SOURCE_KNABEN, doc.get("hash"), doc.get("title"),
            magnet=str(doc.get("magnetUrl") or ""),
            format=str(doc.get("category") or ""),
            # Knaben names the indexer each row came from; that is more use
            # than repeating "Knaben" on every line.
            uploader=str(doc.get("tracker") or ""),
            seeders=seeders,
            leechers=max(0, _int(doc.get("peers")) - seeders),
            size_bytes=_int(doc.get("bytes")),
            posted=int(_timestamp(doc.get("date"))),
            url=str(doc.get("details") or ""),
        ))
    return items



# -- eBookelo ---------------------------------------------------------------

# One search-result card: title, author, language, and the link to the book
# page. The language is the flag's alt text on the site, and is what the
# results list needs -- a Spanish-language site indexing books from everywhere
# must not hide which language each copy is in.
_EBOOKELO_CARD_RE = re.compile(
    r'<div class="bookCard">(.*?)</div>\s*</div>', re.DOTALL)
_EBOOKELO_LINK_RE = re.compile(
    r'href="/ebook/(\d+)/([^"]*)"', re.DOTALL)
_EBOOKELO_TITLE_RE = re.compile(r'<h3 class="title">(.*?)</h3>', re.DOTALL)
_EBOOKELO_AUTHOR_RE = re.compile(r'<span class="autor">(.*?)</span>',
                                 re.DOTALL)
_EBOOKELO_LANG_RE = re.compile(
    r'<span class="flag ([a-z]+)"></span>\s*<span>(.*?)</span>',
    re.DOTALL)

# The site writes languages in Spanish; the results list should say them the
# way the reader is thinking. Unknown codes keep the site's own word.
_EBOOKELO_LANGUAGES = {
    "espa": "Spanish", "ingl": "English", "fran": "French",
    "alem": "German", "ital": "Italian", "port": "Portuguese",
    "rus": "Russian", "japa": "Japanese", "chin": "Chinese",
    "arab": "Arabic", "hola": "Dutch", "suec": "Swedish",
    "noru": "Norwegian", "dane": "Danish", "finn": "Finnish",
    "pol": "Polish", "chec": "Czech", "huna": "Hungarian",
    "grie": "Greek", "turc": "Turkish", "kore": "Korean",
    "lati": "Latin", "cata": "Catalan", "gallego": "Galician",
    "vasc": "Basque", "eusk": "Basque", "persa": "Persian",
    "hind": "Hindi", "indones": "Indonesian", "ucra": "Ukrainian",
}


def _ebookelo_language(flag):
    """The language one eBookelo flag stands for, or "" when unknown."""
    return _EBOOKELO_LANGUAGES.get(str(flag or "").casefold(), "")


def _ebookelo_magnet(item, timeout=HTTP_TIMEOUT_S):
    """The magnet of one eBookelo book, from its download page.

    eBookelo's search and book pages carry no magnet at all; the download
    page for a chosen format answers with a page whose hidden field holds the
    magnet (and whose script redirects a browser to it). That page is what
    this resolves, once, when the user actually picks the book.
    """
    book_id = str(item.get("identifier") or "").strip()
    if not book_id:
        return ""
    response = _http().get(f"{EBOOKELO_URL}/download/{book_id}/magnet",
                           timeout=timeout)
    response.raise_for_status()
    found = re.search(r'name="magnet"\s+value="([^"]*)"', response.text)
    if not found:
        return ""
    return html.unescape(found.group(1)).strip()


def search_ebookelo(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search eBookelo's book index, which serves every book as a torrent.

    The search page is server-rendered -- no JavaScript needed -- and answers
    one page of results with title, author and language for each book. The
    magnet is not there; it lives on each book's own download page, resolved
    when the row is downloaded (see _ebookelo_magnet).
    """
    path = "/".join(part for part in (EBOOKELO_URL, "search",
                                      quote(query), "page", "1") if part)
    response = _http().get(path, timeout=timeout)
    response.raise_for_status()
    items = []
    for card in _EBOOKELO_CARD_RE.finditer(response.text):
        body = card.group(1)
        link = _EBOOKELO_LINK_RE.search(body)
        if not link:
            continue
        book_id, slug = link.group(1), link.group(2)
        title_match = _EBOOKELO_TITLE_RE.search(body)
        if not title_match:
            continue
        title = _text(title_match.group(1))
        author_match = _EBOOKELO_AUTHOR_RE.search(body)
        author = _text(author_match.group(1)) if author_match else ""
        lang_match = _EBOOKELO_LANG_RE.search(body)
        language = ""
        if lang_match:
            language = _ebookelo_language(lang_match.group(1))
            if not language:
                language = _text(lang_match.group(2))
        items.append(_item(
            SOURCE_EBOOKELO, "", title,
            uploader=author,
            format=language or "",
            identifier=book_id,
            url=f"{EBOOKELO_URL}/ebook/{book_id}/{slug}",
            download_url=f"{EBOOKELO_URL}/download/{book_id}/magnet",
        ))
    return items


# -- Audiobook Bay ----------------------------------------------------------

_AUDIOBOOKBAY_TITLE_RE = re.compile(
    r'<h2><a href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
_AUDIOBOOKBAY_LANG_RE = re.compile(r"Language:\s*([^<]*)")
_AUDIOBOOKBAY_SIZE_RE = re.compile(
    r"File Size:\s*<span[^>]*>([\d.]+)</span>\s*(MB|GB|KB)s?", re.I)
_AUDIOBOOKBAY_FORMAT_RE = re.compile(
    r"Format:\s*<span[^>]*>([^<]*)</span>", re.I)

# The detail page names its hash and trackers in table cells, in the order
# the torrent itself lists them -- hash first, then each tracker.
_AUDIOBOOKBAY_HASH_RE = re.compile(
    r"<td>Info Hash:</td>\s*<td>\s*([0-9a-fA-F]{40})\s*</td>")
_AUDIOBOOKBAY_TRACKER_RE = re.compile(
    r"<td>Tracker:</td>\s*<td>([^<]*)</td>")


def _size_to_bytes(value, unit):
    """One "292.27 MB"-style figure as bytes, or 0 when unreadable."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return 0
    multiplier = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}.get(
        str(unit).strip().upper(), 0)
    return int(number * multiplier)


def _audiobookbay_magnet(item, timeout=HTTP_TIMEOUT_S):
    """Build the magnet of one Audiobook Bay row from its detail page.

    The search listing carries no hash; each book's page names it in a table
    next to the trackers that book's swarm uses. Those two, plus the open
    trackers blindDL adds to every bare hash, make the magnet.
    """
    url = str(item.get("url") or "").strip()
    if not url.startswith("http"):
        url = f"{AUDIOBOOKBAY_URL}{url}"
    response = _http().get(url, timeout=timeout)
    response.raise_for_status()
    text = response.text
    found = _AUDIOBOOKBAY_HASH_RE.search(text)
    if not found:
        return ""
    infohash = found.group(1).lower()
    trackers = [match.group(1).strip()
                for match in _AUDIOBOOKBAY_TRACKER_RE.finditer(text)]
    query = [("dn", str(item.get("title") or "").strip())]
    for tracker in trackers:
        query.append(("tr", tracker))
    for tracker in TRACKERS:
        if tracker not in trackers:
            query.append(("tr", tracker))
    return f"magnet:?xt=urn:btih:{infohash}&{urlencode(query)}"


def search_audiobookbay(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search Audiobook Bay's public index of torrented audiobooks.

    The listing is a WordPress search: one page of posts, each carrying the
    title, language, format and file size. The hash is only on the book's own
    page, resolved at download time (see _audiobookbay_magnet).
    """
    response = _http().get(AUDIOBOOKBAY_URL, params={"s": query},
                           timeout=timeout)
    response.raise_for_status()
    items = []
    # Each post is one <div class="post"> block. Splitting on the opening tag
    # is safer than a closing-tag pattern: the posts contain nested divs, and
    # the first closing tag that looks right would cut a post in half.
    for body in response.text.split('<div class="post">')[1:]:
        title_match = _AUDIOBOOKBAY_TITLE_RE.search(body)
        if not title_match:
            continue
        link = title_match.group(1)
        title = _text(title_match.group(2))
        if not link.startswith("http"):
            link = f"{AUDIOBOOKBAY_URL}{link}"
        lang_match = _AUDIOBOOKBAY_LANG_RE.search(body)
        language = _text(lang_match.group(1)) if lang_match else ""
        size_match = _AUDIOBOOKBAY_SIZE_RE.search(body)
        size_bytes = 0
        if size_match:
            size_bytes = _size_to_bytes(size_match.group(1),
                                        size_match.group(2))
        fmt_match = _AUDIOBOOKBAY_FORMAT_RE.search(body)
        audiobook_format = _text(fmt_match.group(1)) if fmt_match else ""
        # The language is the thing that decides between two copies of the
        # same book, so it leads the format column.
        listed = " · ".join(part for part in (language, audiobook_format)
                            if part)
        items.append(_item(
            SOURCE_AUDIOBOOKBAY, "", title,
            format=listed,
            size_bytes=size_bytes,
            url=link,
            download_url=link,
        ))
    return items


# -- user feeds: Torznab, Newznab and Prowlarr's own API ---------------------

_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
_NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"


def _attrs(entry):
    """The torznab:attr / newznab:attr pairs of one feed item, as a dict."""
    found = {}
    for namespace in (_TORZNAB_NS, _NEWZNAB_NS):
        for node in entry.iterfind(f"{namespace}attr"):
            name = str(node.get("name") or "").strip().lower()
            if name:
                found[name] = str(node.get("value") or "").strip()
    return found


def _from_torznab(source, entry):
    """One Torznab RSS item as a blindDL result."""
    attrs = _attrs(entry)
    title = (entry.findtext("title") or "").strip()
    if not title:
        return None
    link = (entry.findtext("link") or "").strip()
    enclosure = entry.find("enclosure")
    if enclosure is not None and not link:
        link = str(enclosure.get("url") or "").strip()
    magnet = attrs.get("magneturl") or ""
    if not magnet and link.startswith("magnet:"):
        magnet, link = link, ""
    seeders = _int(attrs.get("seeders"))
    # Torznab reports total peers; leechers are what is left after seeders.
    peers = _int(attrs.get("peers"))
    raw_leechers = attrs.get("leechers")
    leechers = (
        _int(raw_leechers)
        if raw_leechers is not None and str(raw_leechers).strip()
        else max(0, peers - seeders)
    )
    return _item(
        source, attrs.get("infohash"), title,
        magnet=magnet,
        # A private tracker hands out an authenticated .torrent rather than
        # a magnet, so the link is kept and fetched at download time.
        download_url="" if link.startswith("magnet:") else link,
        format=attrs.get("category_name") or "",
        seeders=seeders,
        leechers=leechers,
        size_bytes=_int(entry.findtext("size") or attrs.get("size")),
        posted=int(_pubdate(entry.findtext("pubDate"))),
        url=(entry.findtext("comments") or entry.findtext("guid") or "").strip(),
    )


def _from_prowlarr(source, doc):
    """One row of Prowlarr's own JSON search as a blindDL result."""
    if str(doc.get("protocol") or "torrent").lower() != "torrent":
        return None  # a Usenet release is not something blindDL can open
    categories = doc.get("categories") or ()
    category = ", ".join(
        str(entry.get("name")) for entry in categories
        if isinstance(entry, dict) and entry.get("name"))
    link = str(doc.get("downloadUrl") or "").strip()
    return _item(
        source, doc.get("infoHash"), doc.get("title"),
        magnet=str(doc.get("magnetUrl") or ""),
        download_url="" if link.startswith("magnet:") else link,
        format=category,
        # Prowlarr says which of the user's trackers each row came from,
        # which is the useful thing to show rather than repeating the feed.
        uploader=str(doc.get("indexer") or ""),
        seeders=_int(doc.get("seeders")),
        leechers=_int(doc.get("leechers")),
        size_bytes=_int(doc.get("size")),
        posted=int(_timestamp(doc.get("publishDate"))),
        url=str(doc.get("infoUrl") or "").strip(),
    )


def search_feed(query, feed, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Search one user-added feed, whichever dialect it answers in.

    Torznab and Newznab are the same RSS shape and take t=search&q=. Prowlarr
    also offers its own JSON search at /api/v1/search, which takes query= and
    covers every tracker configured in it at once. Both sets of parameters go
    out together -- each tool ignores the ones it does not know -- and the
    reply is read as JSON or as RSS depending on what came back.

    Neither specification defines a sort, so *order* is accepted and not
    sent; a feed's rows are put in the asked-for order by _rank instead,
    which every one of them carries a seeder count and a date for.
    """
    params = {"t": "search", "q": query, "query": query, "limit": SEARCH_ROWS}
    headers = {}
    if feed.get("api_key"):
        params["apikey"] = feed["api_key"]
        # Prowlarr prefers the header; Torznab endpoints want the parameter.
        headers["X-Api-Key"] = feed["api_key"]
    response = _http().get(feed["url"], params=params, headers=headers,
                           timeout=timeout)
    response.raise_for_status()
    source = feed["name"]
    body = response.text.lstrip()
    if body.startswith(("{", "[")):
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else payload
        items = [_from_prowlarr(source, doc) for doc in rows or ()
                 if isinstance(doc, dict)]
    else:
        root = ET.fromstring(response.content)
        items = [_from_torznab(source, entry)
                 for entry in root.iterfind("./channel/item")]
    return [item for item in items if item is not None]


# -- Internet Archive -------------------------------------------------------


def search_archive(query, timeout=HTTP_TIMEOUT_S, order=ORDER_RELEVANCE):
    """Public-domain and openly licensed media, as torrents.

    These have no swarm worth speaking of and do not need one: every Archive
    torrent is webseeded by the Archive itself, so it fetches at full speed
    with no peers at all. The search API reports no swarm figures, and that
    permanent seed is what the single seeder here stands for -- without it
    these rows would sort below every dead torrent on a public tracker.

    One torrent covers a whole item, so a show with many episodes arrives in
    one piece rather than as a row per file.
    """
    # Every one of these closes or escapes the wrong thing in the Archive's
    # query language, and a malformed query comes back as an empty result
    # set rather than an error -- a search that silently finds nothing.
    escaped = re.sub(r'["\\()]', " ", query).strip()
    response = _http().get(
        ARCHIVE_SEARCH_URL,
        params={
            "q": f'({escaped}) AND format:("Archive BitTorrent")',
            "fl[]": ["identifier", "title", "creator", "item_size",
                     "publicdate"],
            "rows": SEARCH_ROWS,
            "page": 1,
            "output": "json",
            "sort[]": IA_ARCHIVE_SORTS[search_order.normalize(order)],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    items = []
    for doc in response.json().get("response", {}).get("docs", []) or ():
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            continue
        creator = doc.get("creator")
        if isinstance(creator, list):
            creator = creator[0] if creator else ""
        name = quote(identifier)
        items.append(_item(
            SOURCE_ARCHIVE, "", doc.get("title") or identifier,
            uploader=str(creator or ""),
            seeders=1,
            size_bytes=_int(doc.get("item_size")),
            posted=int(_timestamp(doc.get("publicdate"))),
            download_url=(f"{ARCHIVE_TORRENT_URL}/{name}/"
                          f"{name}_archive.torrent"),
            url=f"{ARCHIVE_DETAILS_URL}/{name}",
        ))
    return items


_SEARCHERS = {
    SOURCE_KNABEN: search_knaben,
    SOURCE_PIRATEBAY: search_piratebay,
    SOURCE_EZTV: search_eztv,
    SOURCE_NYAA: search_nyaa,
    SOURCE_TORRENTS_CSV: search_torrents_csv,
    SOURCE_LIMETORRENTS: search_limetorrents,
    SOURCE_BITSEARCH: search_bitsearch,
    SOURCE_ARCHIVE: search_archive,
    SOURCE_EBOOKELO: search_ebookelo,
    SOURCE_AUDIOBOOKBAY: search_audiobookbay,
}
# Indexers that cannot be given the query, so an unmatched row is noise.
_STRICT_SOURCES = {SOURCE_EZTV}


# -- search ------------------------------------------------------------------


def _rank(items, query, strict=False, order=ORDER_RELEVANCE):
    """Drop the noise, then put the rows in the order that was asked for.

    A torrent nobody is seeding cannot be downloaded however well its name
    matches, so under best match the swarm decides the order among results
    that answer the query -- which is the ordering every torrent site offers
    first. Most popular is the same ordering, said out loud.

    Most recent orders by when the row was posted. An indexer that says
    nothing about when a torrent appeared sorts last rather than first: an
    unknown date is not a new one.

    strict is for an indexer whose rows were never a reply to the query --
    EZTV can only hand back its latest releases -- where anything that does
    not match is simply the wrong programme, not a near miss worth showing.
    """
    order = search_order.normalize(order)
    seen = set()
    unique = []
    for item in items:
        # Sources without an infohash (eBookelo, Audiobook Bay, Archive) are
        # keyed by their identity rather than title alone, so two different
        # releases that happen to share a title are not collapsed into one.
        key = item.get("infohash") or (
            item.get("source"), item.get("title"),
            item.get("magnet") or item.get("download_url") or item.get("url"),
        )
        if key in seen:
            continue
        seen.add(key)
        item["score"] = score_match(query, item.get("title", ""))
        unique.append(item)
    kept = [item for item in unique if item["score"] >= MIN_MATCH_SCORE]
    if not kept and not strict:
        kept = unique

    newest_first = order == ORDER_RECENT
    native_order = (
        order != ORDER_RELEVANCE
        and bool(unique)
        and supports_order(unique[0].get("source", ""), order)
    )

    def key(pair):
        index, item = pair
        if native_order:
            # The provider already answered the requested question. This is
            # especially important for Archive popularity, which is download
            # count rather than the live swarm size shown by other indexers.
            return False, 0, 0, index
        posted = int(item.get("posted") or 0)
        if newest_first:
            return (posted == 0, -posted, -item["score"], index)
        return (False, -item["seeders"], -item["score"], index)

    return [item for _index, item in sorted(enumerate(kept), key=key)][
        :MAX_RESULTS_PER_SOURCE]


def supports_order(source, order, config=None):
    """Whether one indexer can answer *order* itself.

    A name that is not a built-in indexer is one of the user's own feeds,
    and Torznab defines no sort, so those never can.
    """
    return search_order.supported(ORDER_SUPPORT, source, order)


def search(query, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None, config=None, order=ORDER_RELEVANCE):
    """Search the chosen indexers at once and return after timeout_s.

    Same contract as archive_backend.search: indexers run in parallel, the
    call returns at the deadline, and late ones still report through
    on_site(source, items).

    A source name that is not one of the built-in indexers is one of the
    user's own feeds, looked up in config.

    *order* goes out to the indexers that take a sort. The rest are sorted
    here instead -- every torrent row carries a seeder count, and most carry
    a date -- so a torrent search answers all three orders whatever the
    indexer offers, which is not true of the other engines.

    Returns (items, answered, asked).
    """
    order = search_order.normalize(order)
    by_feed = {feed["name"]: feed for feed in feeds(config)}
    wanted = [source for source in (sources or all_sources(config))
              if source in _SEARCHERS or source in by_feed]
    found = {}
    found_lock = threading.Lock()

    def search_one(source):
        if stop is not None and stop.is_set():
            return
        try:
            if source in by_feed:
                rows = search_feed(query, by_feed[source], order=order)
            else:
                rows = _SEARCHERS[source](query, order=order)
            items = _rank(rows, query, strict=source in _STRICT_SOURCES,
                          order=order)
        except Exception:  # noqa: BLE001 - one bad indexer must not kill the rest
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
                                  name=f"torrent-search-{source}", daemon=True)
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
