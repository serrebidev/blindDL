"""Central URL resolver. Follows redirects for short-link domains and can
resolve YouTube @handle URLs to channel IDs.
"""

from __future__ import annotations

import re

_SHORT_DOMAINS = ("deezer.page.link", "dzr.page.link", "link.deezer.com")

_YT_CHANNEL_RE = re.compile(
    r"(?:youtube\.com|music\.youtube\.com)/(channel/|@)([^/?\s]+)",
)


def resolve_url_sync(url: str) -> str:
    if not any(d in url for d in _SHORT_DOMAINS):
        return url
    import httpx
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=10)
        return str(resp.url)
    except Exception:
        return url


async def resolve_url(url: str) -> str:
    if not any(d in url for d in _SHORT_DOMAINS):
        return url
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, follow_redirects=True, timeout=10)
            return str(resp.url)
    except Exception:
        return url


def resolve_yt_channel_id(url: str) -> str | None:
    """Extract a YouTube channel ID from *url*, following /@handle redirects.

    Supports both formats:
      https://music.youtube.com/channel/UCxxxxx
      https://music.youtube.com/@handle
    """
    m = _YT_CHANNEL_RE.search(url)
    if not m:
        return None
    if m.group(1) == "channel/":
        return m.group(2)
    # @handle format — follow redirect to resolve to /channel/ID
    import httpx
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        m2 = _YT_CHANNEL_RE.search(str(resp.url))
        if m2 and m2.group(1) == "channel/":
            return m2.group(2)
    except Exception:
        pass
    return None
