# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Mixcloud backend: search via the public API, download via yt-dlp.

Mixcloud is where DJ sets, radio shows and long-form mixes live -- hours of
continuous audio that none of the track-shaped services carry. Its public
API needs no key and no sign-in, so search is always available; yt-dlp's
Mixcloud extractor handles playback and downloading.

What comes back is a *cloudcast*: one show, usually an hour or more, with a
host rather than an artist. That shape is why the duration column matters
here more than anywhere else -- it is the difference between a three-minute
edit and a four-hour set.
"""

import requests

from . import search_order, ytdlp_backend

_API_URL = "https://api.mixcloud.com"
SEARCH_SOURCE = "Mixcloud"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 "
               "Safari/537.36")
HTTP_TIMEOUT_S = 20
# The API caps a page at 100 and pages by offset, so the 200-result floor
# every other blindDL provider answers with takes at least two round trips.
# Pages overlap -- the same show can come back on two of them -- so the cap
# is deliberately higher than the count divided by the page size.
SEARCH_PAGE = 100
SEARCH_COUNT = 200
MAX_SEARCH_PAGES = 6

# Mixcloud's search endpoint takes a query and nothing else -- no sort, no
# date range. Asking for "most recent" therefore gets best match, and the
# search announcement says so rather than reordering a page of best matches
# and presenting it as the newest of anything.
ORDER_SUPPORT = {}


def supports_order(order):
    """Whether Mixcloud can answer *order* itself. Only best match."""
    return search_order.supported(ORDER_SUPPORT, SEARCH_SOURCE, order)


def _item(cloudcast):
    """One search result, in the shape the results list reads."""
    user = cloudcast.get("user") or {}
    key = str(cloudcast.get("key") or cloudcast.get("url") or "")
    try:
        duration = int(cloudcast.get("audio_length") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "id": f"mixcloud:{key}",
        "kind": "mixcloud",
        "title": str(cloudcast.get("name") or "Unknown show"),
        # The host, not an artist: a mix is credited to whoever put it
        # together, and that is the name worth reading in this column.
        "artist": str(user.get("name") or user.get("username") or ""),
        "source": SEARCH_SOURCE,
        "duration_s": duration,
        "format": "Mix",
        "url": str(cloudcast.get("url") or ""),
    }


def search(query, config=None, order=search_order.ORDER_RELEVANCE,
           count=SEARCH_COUNT):
    """Search Mixcloud's cloudcasts. Returns normalized items.

    *order* is accepted and ignored -- see ORDER_SUPPORT. A page that fails
    part-way keeps whatever the earlier pages already returned, because half
    an answer is worth more than an error nobody can act on.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _USER_AGENT
    items = []
    seen = set()
    try:
        for page in range(MAX_SEARCH_PAGES):
            # Paged by an offset blindDL works out rather than by the
            # ``paging.next`` link the API publishes: that link is missing
            # from the first page of plenty of queries -- "deep house" is
            # one -- even though the offset behind it answers perfectly.
            # Following it alone capped those searches at a single page.
            response = session.get(
                f"{_API_URL}/search/",
                params={
                    "q": query,
                    "type": "cloudcast",
                    "limit": SEARCH_PAGE,
                    "offset": page * SEARCH_PAGE,
                },
                timeout=HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            batch = response.json().get("data") or []
            if not batch:
                break
            fresh = 0
            for cloudcast in batch:
                item = _item(cloudcast)
                if not item["url"] or item["id"] in seen:
                    continue
                seen.add(item["id"])
                items.append(item)
                fresh += 1
            # Three ways this is the last page worth asking for: the
            # catalogue ran short of a full one, everything on it was
            # already listed (consecutive pages overlap), or there are
            # enough rows to answer with.
            if len(batch) < SEARCH_PAGE or not fresh or len(items) >= count:
                break
    except Exception:  # noqa: BLE001 - a partial answer still beats none
        pass
    finally:
        session.close()
    return items[:count]


def extract_flat(url, config=None):
    """Resolve a Mixcloud URL to (items, title) via yt-dlp.

    Same contract as ytdlp_backend.extract_flat.
    """
    return ytdlp_backend.extract_flat(url)


def download(url, out_dir, config=None, progress_cb=None, cancel_event=None):
    """Download one Mixcloud show via yt-dlp."""
    audio_format = (config or {}).get("audio_format", "mp3")
    return ytdlp_backend.download(
        url, out_dir, audio_only=True, audio_format=audio_format,
        progress_cb=progress_cb, cancel_event=cancel_event)
