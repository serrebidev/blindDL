# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Discover complete podcast histories from live and archived RSS feeds.

Podcast feeds commonly expose only their newest episodes.  The Wayback
Machine often has older versions of the same XML document, so reading every
unique version can reconstruct years of episodes without relying on a proxy
service.  Feed redirects, ``itunes:new-feed-url`` and Atom self/next links are
followed as a bounded graph so a show that moved hosts can be reconstructed as
one list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
import defusedxml.ElementTree as DefusedET

from .config import app_data_dir


APPLE_SEARCH_URL = "https://itunes.apple.com/search"
APPLE_LOOKUP_URL = "https://itunes.apple.com/lookup"
GPODDER_SEARCH_URL = "https://gpodder.net/search.json"
FYYD_SEARCH_URL = "https://api.fyyd.de/0.2/search/podcast"
PODVERSE_SEARCH_URL = "https://api.podverse.fm/api/v1/podcast"
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY_PREFIX = "https://web.archive.org/web/"
DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_SNAPSHOTS = 5000
DEFAULT_MAX_FEED_URLS = 50
DEFAULT_WORKERS = 3
USER_AGENT = "blindDL podcast archiver"

_APPLE_PODCAST_ID_RE = re.compile(r"/id(\d+)(?:[/?#]|$)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_TRACKING_QUERY_NAMES = {
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
    "fbclid", "gclid", "si",
}

_PODCAST_FEED_HOST_LABELS = {"feed", "feeds", "podcast", "podcasts", "rss"}
_PODCAST_FEED_PATH_PARTS = {"feed", "feeds", "podcast", "podcasts", "rss"}


class PodcastArchiveCancelled(RuntimeError):
    """Raised when the user stops an archive scan."""


@dataclass(frozen=True)
class Capture:
    timestamp: str
    original: str
    digest: str = ""

    @property
    def replay_url(self):
        """The raw archived response, without Wayback rewriting its links."""
        return f"{WAYBACK_REPLAY_PREFIX}{self.timestamp}id_/{self.original}"


@dataclass
class FeedDocument:
    title: str
    feed_url: str
    episodes: list[dict]
    related_urls: list[str] = field(default_factory=list)


@dataclass
class PodcastArchive:
    title: str
    feed_url: str
    episodes: list[dict]
    feed_urls: list[str]
    snapshots_found: int = 0
    snapshots_loaded: int = 0
    warnings: list[str] = field(default_factory=list)


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1].split(":", 1)[-1].casefold()


def _children(parent, name):
    wanted = name.casefold()
    return [child for child in list(parent)
            if _local_name(child.tag) == wanted]


def _first_child(parent, *names):
    wanted = {name.casefold() for name in names}
    return next((child for child in list(parent)
                 if _local_name(child.tag) in wanted), None)


def _node_text(node):
    if node is None:
        return ""
    return _SPACE_RE.sub(" ", "".join(node.itertext())).strip()


def _child_text(parent, *names):
    return _node_text(_first_child(parent, *names))


def _http_url(value, base=""):
    value = str(value or "").strip()
    if not value:
        return ""
    resolved = urljoin(base, value)
    return resolved if urlparse(resolved).scheme.casefold() in ("http", "https") else ""


def _normalized_feed_url(value):
    url = _http_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(),
                       parsed.path or "/", "", parsed.query, ""))


def _normalized_media_url(value):
    parsed = urlparse(str(value or ""))
    query = [
        (name, val) for name, val in parse_qsl(parsed.query, keep_blank_values=True)
        if name.casefold() not in _TRACKING_QUERY_NAMES
    ]
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(),
                       parsed.path, "", urlencode(sorted(query)), ""))


def is_apple_podcast_url(value):
    """Return whether *value* is an Apple Podcasts page, not Apple Music."""
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").casefold()
    apple_host = (
        host == "podcasts.apple.com" or host.endswith(".podcasts.apple.com")
        or host == "itunes.apple.com" or host.endswith(".itunes.apple.com")
    )
    return bool(apple_host and _APPLE_PODCAST_ID_RE.search(parsed.path))


def looks_like_podcast_url(value):
    """Recognize podcast pages and common RSS URL shapes without fetching."""
    value = str(value or "").strip()
    if is_apple_podcast_url(value):
        return True
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    path = parsed.path.casefold()
    if host.endswith("youtube.com") and path == "/feeds/videos.xml":
        return False
    host_labels = set(host.split("."))
    path_parts = {part for part in path.split("/") if part}
    if host_labels & _PODCAST_FEED_HOST_LABELS:
        return True
    if path_parts & _PODCAST_FEED_PATH_PARTS:
        return True
    if path.endswith((".rss", "/podcast.xml", "/podcast-feed.xml")):
        return True
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return any(
        name.casefold() in {"feed", "podcast", "rss"}
        or str(item).casefold() in {"podcast", "rss"}
        for name, item in query.items()
    )


def _parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _duration_seconds(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        if ":" not in value:
            return max(0, int(float(value)))
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            return max(0, parts[0] * 60 + parts[1])
        if len(parts) == 3:
            return max(0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    except (TypeError, ValueError):
        pass
    return None


def _episode_media(item, base_url):
    candidates = []
    for child in list(item):
        name = _local_name(child.tag)
        if name == "enclosure":
            candidates.append(child.attrib.get("url"))
        elif name == "link" and child.attrib.get("rel", "").casefold() == "enclosure":
            candidates.append(child.attrib.get("href") or _node_text(child))
        elif name == "content" and child.attrib.get("url"):
            media_type = child.attrib.get("type", "").casefold()
            if not media_type or media_type.startswith(("audio/", "video/")):
                candidates.append(child.attrib.get("url"))
    for candidate in candidates:
        resolved = _http_url(candidate, base_url)
        if resolved:
            return resolved
    link = _child_text(item, "link")
    if urlparse(link).path.casefold().endswith(
            (".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav")):
        return _http_url(link, base_url)
    return ""


def _related_feed_urls(channel, base_url):
    urls = []
    moved = _child_text(channel, "new-feed-url")
    if moved:
        urls.append(_http_url(moved, base_url))
    for link in _children(channel, "link"):
        relation = str(link.attrib.get("rel") or "").casefold()
        if relation in ("self", "next"):
            urls.append(_http_url(link.attrib.get("href"), base_url))
    return list(dict.fromkeys(url for url in urls if url))


def parse_feed(xml, feed_url, archived_at=""):
    """Parse RSS, RDF/RSS or Atom XML into normalized playable episodes."""
    try:
        root = DefusedET.fromstring(xml)
    except (ET.ParseError, ValueError) as exc:
        raise RuntimeError("The response is not a readable RSS or Atom feed.") from exc

    root_name = _local_name(root.tag)
    if root_name == "feed":
        channel = root
        entries = _children(root, "entry")
    else:
        channel = _first_child(root, "channel")
        if channel is None:
            raise RuntimeError("The XML does not contain a podcast channel.")
        entries = _children(channel, "item")
        if not entries and root_name == "rdf":
            entries = _children(root, "item")

    podcast_title = _child_text(channel, "title") or feed_url
    related_urls = _related_feed_urls(channel, feed_url)
    episodes = []
    for item in entries:
        media_url = _episode_media(item, feed_url)
        if not media_url:
            continue
        title = _child_text(item, "title") or urlparse(media_url).path.rsplit("/", 1)[-1]
        published_raw = _child_text(
            item, "pubdate", "published", "updated", "date")
        published = _parse_date(published_raw)
        guid = _child_text(item, "guid", "id")
        page_url = ""
        for link in _children(item, "link"):
            if str(link.attrib.get("rel") or "alternate").casefold() != "enclosure":
                page_url = _http_url(link.attrib.get("href") or _node_text(link), feed_url)
                if page_url:
                    break
        duration = _duration_seconds(_child_text(item, "duration"))
        description = _child_text(item, "description", "summary", "encoded")
        episode = {
            "id": guid or media_url,
            "kind": "podcast",
            "title": title or "Untitled episode",
            "artist": podcast_title,
            "uploader": podcast_title,
            "url": media_url,
            "direct_url": media_url,
            "webpage_url": page_url,
            "guid": guid,
            "published": published.isoformat() if published else published_raw,
            "published_date": published.date().isoformat() if published else "",
            "duration_s": duration,
            "description": description,
            "source_feed": feed_url,
            "archived_at": archived_at,
            "_published_ts": published.timestamp() if published else 0,
        }
        episodes.append(episode)
    return FeedDocument(podcast_title, feed_url, episodes, related_urls)


def _episode_aliases(episode):
    aliases = []
    guid = _SPACE_RE.sub(" ", str(episode.get("guid") or "")).strip().casefold()
    if guid:
        aliases.append("guid:" + guid)
    media_url = _normalized_media_url(episode.get("url"))
    if media_url:
        aliases.append("media:" + media_url)
    title = _SPACE_RE.sub(" ", str(episode.get("title") or "")).strip().casefold()
    published = str(episode.get("published_date") or "")
    if title and published:
        aliases.append(f"date-title:{published}:{title}")
    return aliases


def deduplicate_episodes(episodes):
    """Merge copies while retaining the live feed's playable URL."""
    unique = []
    alias_rows = {}
    # A migrated feed can only be discovered inside an old snapshot.  In
    # that case its live version is read later, but its working enclosure is
    # still preferable to the archived row's old host URL.
    ordered = sorted(
        enumerate(episodes),
        key=lambda pair: (bool(pair[1].get("archived_at")), pair[0]))
    for _position, episode in ordered:
        aliases = _episode_aliases(episode)
        row = next((alias_rows[alias] for alias in aliases
                    if alias in alias_rows), None)
        if row is None:
            row = len(unique)
            unique.append(dict(episode))
        else:
            existing = unique[row]
            for key, value in episode.items():
                if not existing.get(key) and value:
                    existing[key] = value
            existing["copies_found"] = int(existing.get("copies_found") or 1) + 1
        for alias in aliases:
            alias_rows.setdefault(alias, row)
    unique.sort(key=lambda item: (
        float(item.get("_published_ts") or 0),
        str(item.get("title") or "").casefold()), reverse=True)
    for episode in unique:
        episode.pop("_published_ts", None)
        stable = "\n".join(_episode_aliases(episode)) or str(episode.get("url") or "")
        episode["id"] = "podcast:" + hashlib.sha256(
            stable.encode("utf-8", "replace")).hexdigest()[:24]
    return unique


def _request(get, url, *, params=None, timeout=DEFAULT_TIMEOUT_S):
    response = None
    for attempt in range(3):
        try:
            response = get(
                url, params=params, timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": (
                        "application/rss+xml, application/atom+xml, "
                        "application/xml, text/xml, application/json"),
                },
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            retryable = (
                isinstance(exc, (requests.ConnectionError, requests.Timeout))
                or getattr(response, "status_code", None) in {
                    429, 500, 502, 503, 504,
                }
            )
            if not retryable or attempt == 2:
                raise
            response_headers = getattr(response, "headers", {}) or {}
            retry_after = str(response_headers.get("Retry-After") or "")
            delay = float(retry_after) if retry_after.isdigit() else 0.5 * 2 ** attempt
            time.sleep(min(delay, 10))
    raise RuntimeError(f"Could not read {url}")


def search_apple_podcasts(query, country="US", limit=50, *, get=requests.get,
                          timeout=DEFAULT_TIMEOUT_S):
    """Search Apple's public, credential-free podcast directory."""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("Enter a podcast name to search for.")
    response = _request(get, APPLE_SEARCH_URL, params={
        "term": query, "media": "podcast", "entity": "podcast",
        "country": country, "limit": max(1, min(int(limit), 200)),
    }, timeout=timeout)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Apple returned an unreadable podcast search.") from exc
    results = []
    seen = set()
    for item in payload.get("results") or []:
        feed_url = _http_url(item.get("feedUrl"))
        identity = str(item.get("collectionId") or feed_url)
        if not feed_url or identity in seen:
            continue
        seen.add(identity)
        results.append({
            "id": "apple-podcast:" + identity,
            "title": str(item.get("collectionName") or "Untitled podcast"),
            "artist": str(item.get("artistName") or ""),
            "feed_url": feed_url,
            "url": feed_url,
            "webpage_url": str(item.get("collectionViewUrl") or ""),
            "track_count": int(item.get("trackCount") or 0),
            "source": "Apple Podcasts",
        })
    return results


def search_gpodder(query, limit=50, *, get=requests.get,
                    timeout=DEFAULT_TIMEOUT_S):
    """Search gPodder's public podcast directory."""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("Enter a podcast name to search for.")
    response = _request(
        get, GPODDER_SEARCH_URL, params={"q": query}, timeout=timeout)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("gPodder returned an unreadable podcast search.") from exc
    results = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        feed_url = _http_url(item.get("url"))
        if not feed_url:
            continue
        results.append({
            "id": "gpodder:" + feed_url,
            "title": str(item.get("title") or feed_url),
            "artist": str(item.get("author") or ""),
            "feed_url": feed_url,
            "url": feed_url,
            "webpage_url": "",
            "track_count": 0,
            "source": "gPodder",
        })
        if len(results) >= max(1, min(int(limit), 200)):
            break
    return results


def search_fyyd(query, limit=50, *, get=requests.get,
                timeout=DEFAULT_TIMEOUT_S):
    """Search fyyd's public podcast directory."""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("Enter a podcast name to search for.")
    result_limit = max(1, min(int(limit), 100))
    response = _request(get, FYYD_SEARCH_URL, params={
        "term": query, "count": result_limit,
    }, timeout=timeout)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("fyyd returned an unreadable podcast search.") from exc
    results = []
    for item in payload.get("data") or [] if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        feed_url = _http_url(item.get("xmlURL"))
        if not feed_url:
            continue
        results.append({
            "id": "fyyd:" + str(item.get("id") or feed_url),
            "title": str(item.get("title") or feed_url),
            "artist": str(item.get("author") or item.get("subtitle") or ""),
            "feed_url": feed_url,
            "url": feed_url,
            "webpage_url": str(item.get("htmlURL") or ""),
            "track_count": 0,
            "source": "fyyd",
        })
    return results


def search_podverse(query, limit=50, *, get=requests.get,
                    timeout=DEFAULT_TIMEOUT_S):
    """Search Podverse's public podcast directory endpoint."""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("Enter a podcast name to search for.")
    response = _request(get, PODVERSE_SEARCH_URL, params={
        "searchTitle": query,
    }, timeout=timeout)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Podverse returned an unreadable podcast search.") from exc
    items = payload[0] if isinstance(payload, list) and payload else []
    results = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        feed_url = next((
            _http_url(feed.get("url"))
            for feed in item.get("feedUrls") or []
            if isinstance(feed, dict) and _http_url(feed.get("url"))
        ), "")
        if not feed_url:
            continue
        results.append({
            "id": "podverse:" + str(item.get("id") or feed_url),
            "title": str(item.get("title") or feed_url),
            "artist": str(item.get("author") or item.get("subtitle") or ""),
            "feed_url": feed_url,
            "url": feed_url,
            "webpage_url": str(item.get("website") or ""),
            "track_count": 0,
            "source": "Podverse",
        })
        if len(results) >= max(1, min(int(limit), 200)):
            break
    return results


def _directory_identity(feed_url):
    parsed = urlparse(_normalized_feed_url(feed_url))
    return (parsed.netloc, parsed.path.rstrip("/") or "/", parsed.query)


def search_podcasts(query, country="US", limit=50, *, get=requests.get,
                    timeout=DEFAULT_TIMEOUT_S):
    """Search public podcast directories in parallel and merge duplicates."""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("Enter a podcast name to search for.")
    searches = (
        ("Apple Podcasts", lambda: search_apple_podcasts(
            query, country, limit, get=get, timeout=timeout)),
        ("gPodder", lambda: search_gpodder(
            query, limit, get=get, timeout=timeout)),
        ("fyyd", lambda: search_fyyd(
            query, limit, get=get, timeout=timeout)),
        ("Podverse", lambda: search_podverse(
            query, limit, get=get, timeout=timeout)),
    )
    completed = [None] * len(searches)
    errors = []
    with ThreadPoolExecutor(max_workers=len(searches)) as executor:
        futures = {
            executor.submit(search): (index, name)
            for index, (name, search) in enumerate(searches)
        }
        for future in as_completed(futures):
            index, name = futures[future]
            try:
                completed[index] = future.result()
            except Exception as exc:  # one unavailable directory is harmless
                errors.append(f"{name}: {exc}")
    if len(errors) == len(searches):
        raise RuntimeError(
            "No podcast directory could be reached. " + "; ".join(errors))

    merged = []
    by_feed = {}
    for results in completed:
        for item in results or []:
            identity = _directory_identity(item.get("feed_url"))
            if not identity[0]:
                continue
            existing = by_feed.get(identity)
            if existing is None:
                copy = dict(item)
                copy["sources"] = [copy.get("source") or "Podcast directory"]
                merged.append(copy)
                by_feed[identity] = copy
                continue
            source = item.get("source")
            if source and source not in existing["sources"]:
                existing["sources"].append(source)
                existing["source"] = ", ".join(existing["sources"])
            if not existing.get("artist") and item.get("artist"):
                existing["artist"] = item["artist"]
            if not existing.get("track_count") and item.get("track_count"):
                existing["track_count"] = item["track_count"]
    return merged


def resolve_podcast_url(value, *, get=requests.get,
                        timeout=DEFAULT_TIMEOUT_S):
    """Resolve a feed URL or Apple Podcasts page to its current RSS URL."""
    value = _http_url(value)
    if not value:
        raise RuntimeError("Enter a podcast RSS URL or search by name.")
    match = _APPLE_PODCAST_ID_RE.search(urlparse(value).path)
    if match and is_apple_podcast_url(value):
        response = _request(get, APPLE_LOOKUP_URL, params={
            "id": match.group(1), "entity": "podcast",
        }, timeout=timeout)
        for item in response.json().get("results") or []:
            feed_url = _http_url(item.get("feedUrl"))
            if feed_url:
                return feed_url
        raise RuntimeError("Apple did not provide an RSS feed for that podcast.")
    return value


def wayback_captures(feed_url, max_snapshots=DEFAULT_MAX_SNAPSHOTS, *,
                     get=requests.get, timeout=DEFAULT_TIMEOUT_S):
    """List unique successful Wayback captures of one exact feed URL."""
    response = _request(get, WAYBACK_CDX_URL, params={
        "url": feed_url,
        "output": "json",
        "fl": "timestamp,original,digest,statuscode,mimetype",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": max(1, min(int(max_snapshots), DEFAULT_MAX_SNAPSHOTS)),
    }, timeout=timeout)
    try:
        rows = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("The Wayback Machine returned an unreadable index.") from exc
    if not isinstance(rows, list) or not rows:
        return []
    headers = [str(name) for name in rows[0]]
    captures = []
    for values in rows[1:]:
        row = dict(zip(headers, values, strict=False))
        timestamp = str(row.get("timestamp") or "")
        original = _http_url(row.get("original"))
        if re.fullmatch(r"\d{14}", timestamp) and original:
            captures.append(Capture(
                timestamp, original, str(row.get("digest") or "")))
    return captures


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _capture_cache_file(cache_dir, capture):
    if cache_dir is False:
        return None
    root = (Path(app_data_dir()) / "podcast-archive-cache"
            if cache_dir is None else Path(cache_dir))
    identity = hashlib.sha256(
        capture.replay_url.encode("utf-8", "replace")).hexdigest()
    return root / f"{identity}.xml"


def archive_podcast(feed_url, include_wayback=True,
                    max_snapshots=DEFAULT_MAX_SNAPSHOTS,
                    max_feed_urls=DEFAULT_MAX_FEED_URLS,
                    workers=DEFAULT_WORKERS, *, get=requests.get,
                    timeout=DEFAULT_TIMEOUT_S, progress=None,
                    cancel_event: threading.Event | None = None,
                    cache_dir=None):
    """Return one deduplicated history from live and archived feed versions."""
    feed_url = resolve_podcast_url(feed_url, get=get, timeout=timeout)
    pending = [feed_url]
    visited = set()
    documents = []
    warnings = []
    snapshots_found = 0
    snapshots_loaded = 0
    seen_digests = set()

    def report(message, current=0, total=0):
        if progress is not None:
            progress(message, current, total)

    while pending and len(visited) < max_feed_urls:
        if _cancelled(cancel_event):
            raise PodcastArchiveCancelled("Podcast archive scan stopped.")
        requested_url = pending.pop(0)
        normalized = _normalized_feed_url(requested_url)
        if not normalized or normalized in visited:
            continue
        visited.add(normalized)
        report(f"Reading current feed {len(visited)}: {requested_url}")
        try:
            response = _request(get, requested_url, timeout=timeout)
            final_url = _http_url(getattr(response, "url", "")) or requested_url
            document = parse_feed(response.content, final_url)
            documents.append(document)
            for related in (final_url, *document.related_urls):
                if _normalized_feed_url(related) not in visited:
                    pending.append(related)
        except Exception as exc:  # noqa: BLE001 - archives can still work
            warnings.append(f"Current feed {requested_url}: {exc}")

        if not include_wayback:
            continue
        report(f"Finding archived feed versions for {requested_url}")
        try:
            captures = wayback_captures(
                requested_url, max_snapshots, get=get, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - current feed is still useful
            warnings.append(f"Wayback index for {requested_url}: {exc}")
            continue
        unique_captures = []
        for capture in captures:
            digest_key = capture.digest or capture.timestamp + capture.original
            if digest_key not in seen_digests:
                seen_digests.add(digest_key)
                unique_captures.append(capture)
        snapshots_found += len(unique_captures)

        def read_capture(capture):
            cache_file = _capture_cache_file(cache_dir, capture)
            if cache_file is not None:
                try:
                    cached = cache_file.read_bytes()
                    return capture, parse_feed(
                        cached, capture.original, capture.timestamp)
                except (OSError, RuntimeError):
                    pass
            for attempt in range(3):
                response = _request(get, capture.replay_url, timeout=timeout)
                try:
                    document = parse_feed(
                        response.content, capture.original, capture.timestamp)
                except RuntimeError:
                    # Wayback sometimes answers a replay with a temporary
                    # HTML error carrying HTTP 200. A real feed that happens
                    # to be malformed will still contain a feed root and is
                    # not pointlessly fetched three times.
                    looks_like_feed = re.search(
                        br"<(?:[A-Za-z0-9_-]+:)?(?:rss|feed|rdf)\b",
                        response.content[:8192], re.IGNORECASE)
                    if looks_like_feed or attempt == 2:
                        raise
                    time.sleep(1 * 2 ** attempt)
                    continue
                if cache_file is not None:
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        temporary = cache_file.with_suffix(
                            f".tmp-{threading.get_ident()}")
                        temporary.write_bytes(response.content)
                        temporary.replace(cache_file)
                    except OSError:
                        pass
                return capture, document
            raise RuntimeError("The archived feed could not be read.")

        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
            future_captures = {
                pool.submit(read_capture, capture): capture
                for capture in unique_captures
            }
            done = 0
            for future in as_completed(future_captures):
                if _cancelled(cancel_event):
                    for unfinished in future_captures:
                        unfinished.cancel()
                    raise PodcastArchiveCancelled("Podcast archive scan stopped.")
                capture = future_captures[future]
                done += 1
                try:
                    _capture, document = future.result()
                    documents.append(document)
                    snapshots_loaded += 1
                    for related in document.related_urls:
                        if _normalized_feed_url(related) not in visited:
                            pending.append(related)
                except Exception as exc:  # noqa: BLE001 - retain other captures
                    warnings.append(
                        f"Snapshot {capture.timestamp} of {capture.original}: {exc}")
                report(
                    f"Read {done} of {len(unique_captures)} archived versions",
                    done, len(unique_captures))

    if not documents:
        detail = warnings[0] if warnings else "No readable feed was found."
        raise RuntimeError(detail)
    all_episodes = [episode for document in documents
                    for episode in document.episodes]
    episodes = deduplicate_episodes(all_episodes)
    title = next((document.title for document in documents
                  if document.title and document.episodes), documents[0].title)
    if not episodes:
        raise RuntimeError("No playable podcast episodes were found in the feed history.")
    report(f"Found {len(episodes)} unique episodes.")
    return PodcastArchive(
        title=title,
        feed_url=feed_url,
        episodes=episodes,
        feed_urls=[document.feed_url for document in documents
                   if document.feed_url],
        snapshots_found=snapshots_found,
        snapshots_loaded=snapshots_loaded,
        warnings=warnings,
    )


def combined_rss(archive):
    """Serialize a reconstructed podcast as a portable RSS 2.0 document."""
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = archive.title
    ET.SubElement(channel, "link").text = archive.feed_url
    ET.SubElement(channel, "description").text = (
        f"Reconstructed by blindDL from {archive.snapshots_loaded} archived "
        "feed versions and the current feed."
    )
    ET.SubElement(channel, "generator").text = "blindDL podcast archiver"
    for episode in archive.episodes:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = str(episode.get("title") or "Untitled episode")
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = str(episode.get("guid") or episode.get("id") or episode["url"])
        published = _parse_date(episode.get("published"))
        if published:
            ET.SubElement(item, "pubDate").text = format_datetime(published)
        if episode.get("webpage_url"):
            ET.SubElement(item, "link").text = str(episode["webpage_url"])
        if episode.get("description"):
            ET.SubElement(item, "description").text = str(episode["description"])
        ET.SubElement(item, "enclosure", {
            "url": str(episode["url"]),
            "type": "audio/mpeg",
            "length": "0",
        })
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_combined_rss(path, archive):
    Path(path).write_bytes(combined_rss(archive))


def main(argv=None):
    """Source checkout command for creating a combined archive feed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed_url", help="Podcast RSS or Apple Podcasts URL")
    parser.add_argument("-o", "--output", default="podcast-archive.xml")
    parser.add_argument("--current-only", action="store_true",
                        help="Do not query the Wayback Machine")
    parser.add_argument("--max-snapshots", type=int,
                        default=DEFAULT_MAX_SNAPSHOTS)
    args = parser.parse_args(argv)
    archive = archive_podcast(
        args.feed_url, include_wayback=not args.current_only,
        max_snapshots=args.max_snapshots,
        progress=lambda message, _current, _total: print(message),
    )
    write_combined_rss(args.output, archive)
    print(f"Wrote {len(archive.episodes)} episodes to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
