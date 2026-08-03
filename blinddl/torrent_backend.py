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
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import requests

from .book_backend import HEADERS, format_size, safe_filename, score_match
from .runtime import open_file, open_magnet

SOURCE_PIRATEBAY = "The Pirate Bay"
SOURCE_EZTV = "EZTV"
SOURCE_NYAA = "Nyaa"
SOURCE_TORRENTS_CSV = "Torrents-CSV"
SOURCE_LIMETORRENTS = "LimeTorrents"
SOURCE_BITSEARCH = "BitSearch / SolidTorrents"
SOURCE_KNABEN = "Knaben"
ALL_SOURCES = [
    SOURCE_KNABEN,
    SOURCE_PIRATEBAY,
    SOURCE_EZTV,
    SOURCE_NYAA,
    SOURCE_TORRENTS_CSV,
    SOURCE_LIMETORRENTS,
    SOURCE_BITSEARCH,
]

PIRATEBAY_URL = "https://apibay.org/q.php"
EZTV_URL = "https://eztvx.to/api/get-torrents"
NYAA_URL = "https://nyaa.si/"
TORRENTS_CSV_URL = "https://torrents-csv.com/service/search"
LIMETORRENTS_URL = "https://www.limetorrents.lol/search/all"
# The origin. solidtorrents.to and bitsearch.to are both 301s onto it.
BITSEARCH_URL = "https://bitsearch.eu/api/v1/search"
KNABEN_URL = "https://api.knaben.org/v1"

SEARCH_TIMEOUT_S = 8.0
HTTP_TIMEOUT_S = 20
SEARCH_ROWS = 40
MAX_RESULTS_PER_SOURCE = 25
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
        return parsedate_to_datetime(text).timestamp()
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
        "age": "",
        "url": "",
    }
    item.update(extra)
    if item["size_bytes"] and not item["file_size"]:
        item["file_size"] = format_size(item["size_bytes"])
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


def fetch_torrent_file(item, out_dir, timeout=HTTP_TIMEOUT_S):
    """Save one result's .torrent file and return the path.

    Private trackers publish an authenticated .torrent rather than a magnet,
    and the URL only works for the account that was given it. Saving the file
    and opening that is what carries the tracker's passkey through to the
    client; handing the client the URL would not.
    """
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
    magnet = magnet_for(item)
    if magnet:
        open_magnet(magnet)
        return magnet
    if item.get("download_url") and out_dir:
        path = fetch_torrent_file(item, out_dir)
        open_file(path)
        return path
    raise RuntimeError("That result carries no magnet link or info hash.")


# -- The Pirate Bay ---------------------------------------------------------


def search_piratebay(query, timeout=HTTP_TIMEOUT_S):
    """Query the public apibay endpoint, which answers in plain JSON."""
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
            age=_age(doc.get("added")),
            url=f"https://thepiratebay.org/description.php?id={doc.get('id')}",
        ))
    return items


# -- EZTV -------------------------------------------------------------------


def search_eztv(query, timeout=HTTP_TIMEOUT_S):
    """EZTV indexes television only, and answers with a magnet per row.

    Its API filters by IMDb id rather than by text, so the query is matched
    against a page of recent releases. That is what the site can do without
    an account; _rank drops whatever does not answer the query.
    """
    response = _http().get(
        EZTV_URL, params={"limit": 100, "page": 1}, timeout=timeout)
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
            age=_age(doc.get("date_released_unix")),
            url=f"https://eztvx.to/ep/{doc.get('id')}/",
        ))
    return items


# -- Nyaa -------------------------------------------------------------------


def search_nyaa(query, timeout=HTTP_TIMEOUT_S):
    """Nyaa publishes its search as RSS, with the swarm counts in the feed."""
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
            url=(entry.findtext("guid") or "").strip(),
        ))
    return items


# -- Torrents-CSV -----------------------------------------------------------


def search_torrents_csv(query, timeout=HTTP_TIMEOUT_S):
    """A plain JSON index with no site to scrape and no rate limiting."""
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
            age=_age(doc.get("created_unix")),
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


def search_limetorrents(query, timeout=HTTP_TIMEOUT_S):
    """Scrape one LimeTorrents search page."""
    response = _http().get(f"{LIMETORRENTS_URL}/{quote(query)}/",
                           timeout=timeout)
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


def search_bitsearch(query, timeout=HTTP_TIMEOUT_S):
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
        params={"q": query, "sort": "seeders", "limit": 100},
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
            age=_age(_timestamp(doc.get("updatedAt"))),
            url=f"https://bitsearch.eu/torrent/{doc.get('id')}",
        ))
    return items


# -- Knaben ------------------------------------------------------------------


def search_knaben(query, timeout=HTTP_TIMEOUT_S):
    """Knaben is a meta-search over many indexers, answering as one JSON API.

    This is how blindDL covers 1337x: Knaben runs its own scraper against it
    and serves the cached rows, so the results arrive without the headless
    browser 1337x's own Cloudflare challenge would otherwise demand.
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
            age=_age(_timestamp(doc.get("date"))),
            url=str(doc.get("details") or ""),
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
    return _item(
        source, attrs.get("infohash"), title,
        magnet=magnet,
        # A private tracker hands out an authenticated .torrent rather than
        # a magnet, so the link is kept and fetched at download time.
        download_url="" if link.startswith("magnet:") else link,
        format=attrs.get("category_name") or "",
        seeders=seeders,
        leechers=_int(attrs.get("leechers")) or max(0, peers - seeders),
        size_bytes=_int(entry.findtext("size") or attrs.get("size")),
        age=_age(_pubdate(entry.findtext("pubDate"))),
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
        age=_age(_timestamp(doc.get("publishDate"))),
        url=str(doc.get("infoUrl") or "").strip(),
    )


def search_feed(query, feed, timeout=HTTP_TIMEOUT_S):
    """Search one user-added feed, whichever dialect it answers in.

    Torznab and Newznab are the same RSS shape and take t=search&q=. Prowlarr
    also offers its own JSON search at /api/v1/search, which takes query= and
    covers every tracker configured in it at once. Both sets of parameters go
    out together -- each tool ignores the ones it does not know -- and the
    reply is read as JSON or as RSS depending on what came back.
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


_SEARCHERS = {
    SOURCE_KNABEN: search_knaben,
    SOURCE_PIRATEBAY: search_piratebay,
    SOURCE_EZTV: search_eztv,
    SOURCE_NYAA: search_nyaa,
    SOURCE_TORRENTS_CSV: search_torrents_csv,
    SOURCE_LIMETORRENTS: search_limetorrents,
    SOURCE_BITSEARCH: search_bitsearch,
}
# Indexers that cannot be given the query, so an unmatched row is noise.
_STRICT_SOURCES = {SOURCE_EZTV}


# -- search ------------------------------------------------------------------


def _rank(items, query, strict=False):
    """Drop the noise, then put the best-seeded close matches first.

    A torrent nobody is seeding cannot be downloaded however well its name
    matches, so the swarm decides the order among results that answer the
    query -- which is the ordering every torrent site offers first.

    strict is for an indexer whose rows were never a reply to the query --
    EZTV can only hand back its latest releases -- where anything that does
    not match is simply the wrong programme, not a near miss worth showing.
    """
    seen = set()
    unique = []
    for item in items:
        key = item.get("infohash") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        item["score"] = score_match(query, item.get("title", ""))
        unique.append(item)
    kept = [item for item in unique if item["score"] >= MIN_MATCH_SCORE]
    if not kept and not strict:
        kept = unique
    indexed = sorted(
        enumerate(kept),
        key=lambda pair: (-pair[1]["seeders"], -pair[1]["score"], pair[0]))
    return [item for _index, item in indexed][:MAX_RESULTS_PER_SOURCE]


def search(query, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None, config=None):
    """Search the chosen indexers at once and return after timeout_s.

    Same contract as archive_backend.search: indexers run in parallel, the
    call returns at the deadline, and late ones still report through
    on_site(source, items).

    A source name that is not one of the built-in indexers is one of the
    user's own feeds, looked up in config.

    Returns (items, answered, asked).
    """
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
                rows = search_feed(query, by_feed[source])
            else:
                rows = _SEARCHERS[source](query)
            items = _rank(rows, query, strict=source in _STRICT_SOURCES)
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
