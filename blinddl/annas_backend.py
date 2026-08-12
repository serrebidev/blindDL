# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Anna's Archive: search the shadow-library index, download via the cascade.

Built on the approach of yakeworld/doi-fetch, which treats Anna's Archive as
a discovery service rather than a download server: the site's own free
"slow download" sits behind DDoS-Guard and answers 403 to any client that is
not a real browser, so doi-fetch uses Anna's Archive to find a file's MD5 and
then fetches the bytes from LibGen. blindDL does the same, in three steps:

    1. search Anna's Archive           -> MD5, title, author, format, size
    2. member API, if a key is set     -> a direct download URL
    3. LibGen mirrors, keyed by MD5    -> ads.php, then its keyed get.php link

Step 2 is Anna's Archive's documented, stable JSON API and needs a paid
membership key; blindDL leaves it empty by default and step 3 handles
everyone else. Nothing here defeats a paywall or a bot check: when both
paths fail the user is told to open the record page (Ctrl+C copies its URL)
and use the site's own slow download in a browser.

curl_cffi does the talking because both sites sit behind Cloudflare-style
fingerprint checks that plain requests cannot pass. It is already a blindDL
dependency for the adult providers.
"""

from __future__ import annotations

import html
import re

from .search_order import ORDER_RECENT, normalize as _normalize_order

SOURCE_ANNAS = "Anna's Archive"

# The site's own search sorts. It publishes newest, oldest, largest and
# smallest -- and no popularity figure at all, so "most popular" has nothing
# here to ask for and falls back to the site's relevance ranking.
SEARCH_SORTS = {ORDER_RECENT: "newest"}

# Anna's Archive rotates domains as they are seized. Tried in order; the
# first that answers is remembered for the rest of the session.
DOMAINS = (
    "annas-archive.gl",
    "annas-archive.pk",
    "annas-archive.gd",
)
# LibGen mirrors serve the same MD5-addressed catalog behind different names.
LIBGEN_MIRRORS = (
    "https://libgen.li",
    "https://libgen.vg",
    "https://libgen.bz",
)
# The fingerprint curl_cffi presents. Both sites reject anything that does
# not look like a current browser.
IMPERSONATE = "chrome"
HTTP_TIMEOUT_S = 25
DOWNLOAD_TIMEOUT_S = 300
SEARCH_ROWS = 40

_TITLE_RE = re.compile(
    r'<a href="/md5/([0-9a-f]{32})"[^>]*text-lg[^>]*>(.*?)</a>', re.DOTALL)
_AUTHOR_RE = re.compile(r'icon-\[mdi--user-edit\][^>]*></span>\s*([^<]+)</a>')
_PUBLISHER_RE = re.compile(r'icon-\[mdi--company\][^>]*></span>\s*([^<]+)</a>')
# "English [en] · EPUB · 1.7MB · 2012 · Book (fiction)"
_META_RE = re.compile(
    r'font-semibold text-sm leading-\[1\.2\] mt-2">([^<]+)')
_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE)
# "🚀/lgli/upload/zlib" -- which shadow libraries hold this file. Records
# backed by LibGen are the ones a non-member can actually download.
_MIRRORS_RE = re.compile(r"\U0001F680([/\w]+)")
LIBGEN_COLLECTIONS = ("lgli", "lgrs")
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_FORMAT_RE = re.compile(
    r"\b(EPUB|PDF|MOBI|AZW3|DJVU|CBZ|CBR|FB2|TXT|RTF|DOC|DOCX)\b",
    re.IGNORECASE)
_LIBGEN_KEY_RE = re.compile(
    r'href="(get\.php\?md5=[0-9a-f]{32}&(?:amp;)?key=[A-Za-z0-9]+)"')
_TAG_RE = re.compile(r"<[^>]+>")

_working_domain = None


class AnnasUnavailable(RuntimeError):
    """Every Anna's Archive mirror refused or failed to answer."""


def _requests():
    """curl_cffi's requests, imported late so a missing install is survivable."""
    from curl_cffi import requests as curl_requests

    return curl_requests


def _get(url, **kwargs):
    kwargs.setdefault("impersonate", IMPERSONATE)
    kwargs.setdefault("timeout", HTTP_TIMEOUT_S)
    return _requests().get(url, **kwargs)


_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+);")


def _clean(text):
    """Strip markup and entities, including the site's double-escaped titles."""
    cleaned = html.unescape(_TAG_RE.sub("", text or ""))
    if _ENTITY_RE.search(cleaned):
        cleaned = html.unescape(cleaned)
    return cleaned.strip()


def _parse_size(text):
    match = _SIZE_RE.search(text or "")
    if not match:
        return 0
    scale = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
    try:
        return int(float(match.group(1)) * scale[match.group(2).lower()])
    except (KeyError, ValueError):
        return 0


def _parse_rows(body, domain):
    """Pull one result row per record out of a search page."""
    rows = []
    matches = list(_TITLE_RE.finditer(body))
    for index, match in enumerate(matches):
        md5 = match.group(1)
        title = _clean(match.group(2))
        if not title:
            continue
        end = (matches[index + 1].start() if index + 1 < len(matches)
               else min(len(body), match.end() + 4000))
        block = body[match.end():end]
        author = ""
        author_match = _AUTHOR_RE.search(block)
        if author_match:
            author = _clean(author_match.group(1))
        publisher = ""
        publisher_match = _PUBLISHER_RE.search(block)
        if publisher_match:
            publisher = _clean(publisher_match.group(1))
        meta = ""
        meta_match = _META_RE.search(block)
        if meta_match:
            meta = _clean(meta_match.group(1))
        format_match = _FORMAT_RE.search(meta)
        year_match = _YEAR_RE.search(meta) or _YEAR_RE.search(publisher)
        mirrors_match = _MIRRORS_RE.search(meta)
        mirrors = [part for part in
                   (mirrors_match.group(1) if mirrors_match else "").split("/")
                   if part]
        rows.append({
            "md5": md5,
            "title": title,
            "author": author,
            "format": format_match.group(1).upper() if format_match else "",
            "size_bytes": _parse_size(meta),
            "year": year_match.group(1) if year_match else "",
            "url": f"https://{domain}/md5/{md5}",
            "mirrors": mirrors,
            "on_libgen": any(name in LIBGEN_COLLECTIONS for name in mirrors),
        })
    return rows


def search(query, timeout=HTTP_TIMEOUT_S, order=None):
    """Search every Anna's Archive mirror until one answers with results."""
    global _working_domain
    domains = list(DOMAINS)
    if _working_domain in domains:
        domains.remove(_working_domain)
        domains.insert(0, _working_domain)

    params = {"q": query, "display": ""}
    sort = SEARCH_SORTS.get(_normalize_order(order))
    if sort:
        params["sort"] = sort

    last_error = None
    for domain in domains:
        try:
            response = _get(f"https://{domain}/search", params=params,
                            timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last_error = exc
            continue
        if response.status_code != 200:
            last_error = RuntimeError(f"{domain} answered {response.status_code}")
            continue
        rows = _parse_rows(response.text, domain)
        if rows:
            _working_domain = domain
            return rows[:SEARCH_ROWS]
        last_error = last_error or RuntimeError(f"{domain} returned no results")
    if last_error is not None and not isinstance(last_error, RuntimeError):
        raise AnnasUnavailable(str(last_error))
    return []


# -- download resolution ---------------------------------------------------


def _member_download_url(md5, key, timeout=HTTP_TIMEOUT_S):
    """Ask Anna's Archive's member API where the file lives.

    Documented endpoint; answers 401 without a membership key, which is the
    normal case and simply moves the caller on to LibGen.
    """
    domains = ([_working_domain] if _working_domain else []) + list(DOMAINS)
    for domain in domains:
        try:
            response = _get(f"https://{domain}/dyn/api/fast_download.json",
                            params={"md5": md5, "key": key}, timeout=timeout)
        except Exception:  # noqa: BLE001 - try the next mirror
            continue
        if response.status_code not in (200, 204):
            continue
        try:
            url = (response.json() or {}).get("download_url")
        except ValueError:
            continue
        if url:
            return str(url)
    return ""


def libgen_download_url(md5, timeout=HTTP_TIMEOUT_S):
    """Resolve an MD5 to LibGen's keyed, single-use download link."""
    for mirror in LIBGEN_MIRRORS:
        try:
            response = _get(f"{mirror}/ads.php", params={"md5": md5},
                            timeout=timeout)
        except Exception:  # noqa: BLE001 - try the next mirror
            continue
        if response.status_code != 200:
            continue
        match = _LIBGEN_KEY_RE.search(response.text)
        if match:
            return f"{mirror}/{html.unescape(match.group(1))}"
    return ""


def resolve_download(md5, member_key=""):
    """Return a URL the file can actually be fetched from.

    Anna's Archive membership first when the user has a key, then LibGen --
    doi-fetch's cascade, in the same order and for the same reason.
    """
    if (member_key or "").strip():
        url = _member_download_url(md5, member_key.strip())
        if url:
            return url
    url = libgen_download_url(md5)
    if url:
        return url
    raise RuntimeError(
        "No mirror would serve that file. Copy the result's URL with "
        "Control C and use the slow download on the Anna's Archive page, or "
        "add a membership key in Settings for direct downloads.")


def open_stream(url, timeout=DOWNLOAD_TIMEOUT_S):
    """Start a streamed GET for a resolved download URL."""
    return _get(url, timeout=timeout, allow_redirects=True,
                stream=True)
