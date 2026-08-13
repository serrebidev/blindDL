# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Resolve search results and pasted URLs to playable media streams."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from . import (
    adult_backend, archive_backend, audiobook_backend, deezer_backend,
    sideb_backend, torrent_backend, ytdlp_backend,
)

DIRECT_MEDIA_EXTENSIONS = {
    ".3gp", ".aac", ".aiff", ".avi", ".flac", ".flv", ".m3u8", ".m4a",
    ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".oga",
    ".ogg", ".ogv", ".opus", ".ts", ".wav", ".webm", ".wma", ".wmv",
}


def _first_http_url(value):
    """Return the first HTTP URL from musicdl's varied URL structures."""
    if isinstance(value, str):
        return value if value.lower().startswith(("http://", "https://")) else None
    if isinstance(value, Mapping):
        for key in ("url", "download_url", "play_url", "src"):
            found = _first_http_url(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _first_http_url(child)
            if found:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found = _first_http_url(child)
            if found:
                return found
    return None


def _is_direct_media_url(url):
    path = urlparse(str(url)).path.lower()
    return any(path.endswith(extension) for extension in DIRECT_MEDIA_EXTENSIONS)


def result_url(item):
    """Return the best shareable URL for one search result, or ``None``.

    Prefers the human-facing page URL so a copied link opens the site rather
    than a short-lived CDN stream. Music results carry no page URL, so their
    download URL is the only thing worth copying.
    """
    if item.get("kind") == "torrent":
        # The magnet is the useful thing to paste into a torrent client; the
        # indexer's own page is not what anyone copies a torrent for.
        magnet = torrent_backend.magnet_for(item)
        if magnet:
            return magnet
    for key in ("url", "direct_url"):
        found = _first_http_url(item.get(key))
        if found:
            return found
    song_info = item.get("song_info")
    if song_info is not None:
        return _first_http_url(getattr(song_info, "download_url", None))
    return None


def resolve_search_result(item, audio_only, config):
    """Return ``(stream URI, title)`` for one normalized search result."""
    title = str(item.get("title") or "Preview")
    kind = item.get("kind")

    if item.get("song_info") is not None:
        stream = _first_http_url(getattr(item["song_info"], "download_url", None))
        if stream:
            return stream, title
        raise RuntimeError("This music source did not provide a preview stream.")

    if kind == "archive":
        # A whole item previews from its first file; a chosen episode has
        # its own URL already.
        stream = archive_backend.first_stream(item)
        if not stream:
            raise RuntimeError("This item has no playable file.")
        return stream, title

    if kind == "audiobook":
        # Preview plays the opening chapter; the rest arrive with the
        # download. Its URL points straight at an audio file.
        stream = audiobook_backend.first_stream(item)
        if not stream:
            raise RuntimeError("This audiobook has no playable chapter.")
        return stream, title

    if kind in ("sideb", "deezer"):
        # Use Deezer's own 30-second preview clip when available — it is
        # faster and more reliable than searching YouTube.
        preview_url = sideb_backend.get_deezer_preview_url(
            item.get("id", ""))
        if preview_url:
            return preview_url, title
        query = " ".join(
            filter(
                None,
                (
                    str(item.get("artist") or ""),
                    title,
                ),
            )
        )
        target = f"ytsearch1:{query}"
        return (
            ytdlp_backend.resolve_stream(
                target,
                audio_only=True,
                cookies_from_browser=config.get("cookies_from_browser"),
                cookies_file=config.get("cookies_file"),
            ),
            title,
        )

    direct = _first_http_url(item.get("direct_url"))
    if direct:
        return direct, title
    url = _first_http_url(item.get("url"))
    if not url:
        raise RuntimeError("This result has no playable URL.")
    return (
        ytdlp_backend.resolve_stream(
            url,
            audio_only=audio_only,
            cookies_from_browser=config.get("cookies_from_browser"),
            cookies_file=config.get("cookies_file"),
        ),
        title,
    )


def resolve_url(url, audio_only, config):
    """Return ``(stream URI, title)`` for a pasted media URL."""
    if _is_direct_media_url(url):
        return url, url
    if adult_backend.is_supported_url(url):
        if not config.get("adult_sites_enabled"):
            raise RuntimeError("Adult sites are disabled. Enable them in Settings.")
        items, title = adult_backend.inspect_url(url, config=config)
        if not items:
            raise RuntimeError("No playable items were found at that URL.")
        stream, item_title = resolve_search_result(
            items[0], audio_only=False, config=config
        )
        return stream, item_title or title

    if sideb_backend.is_deezer_url(url):
        # Public API first (fast, no sideb dependency), then fall back.
        try:
            items, title = deezer_backend.extract_flat(url, config)
        except Exception:  # noqa: BLE001 - sideb is the fallback
            items, title = sideb_backend.extract_flat(url, config)
        if not items:
            raise RuntimeError("No playable tracks were found at that URL.")
        stream, item_title = resolve_search_result(
            items[0], audio_only=True, config=config
        )
        return stream, item_title or title

    stream = ytdlp_backend.resolve_stream(
        url,
        audio_only=audio_only,
        cookies_from_browser=config.get("cookies_from_browser"),
        cookies_file=config.get("cookies_file"),
    )
    return stream, url
