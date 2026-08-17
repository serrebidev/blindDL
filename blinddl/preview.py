# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Resolve search results and pasted URLs to playable media streams."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from . import (
    adult_backend, archive_backend, audiobook_backend, deezer_backend,
    sideb_backend, torrent_backend, ytdlp_backend,
)

# A Deezer track id, wherever it is carried: a deezer.com link, or the
# "deezer:<id>" / "sideb:<id>" ids that search results are keyed by.
_DEEZER_TRACK_RE = re.compile(
    r"(?:deezer\.com/(?:[a-z]{2}/)?track/|(?:deezer|sideb):)(\d+)",
    re.IGNORECASE,
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


def _youtube_stream_for(item, title, config):
    """Resolve a Deezer/Side B track to a full YouTube stream.

    The last resort for a Deezer track: used when no ARL cookie is
    configured, or when Deezer itself will not serve the recording. Search
    YouTube for the artist and title and play the best match.
    """
    artist = item.get("artist") or item.get("singers") or ""
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(str(name) for name in artist if name)
    query = " ".join(filter(None, (str(artist), title)))
    return ytdlp_backend.resolve_stream(
        f"ytsearch1:{query}",
        audio_only=True,
        cookies_from_browser=config.get("cookies_from_browser"),
        cookies_file=config.get("cookies_file"),
    )


def deezer_track_url(item):
    """The deezer.com track URL of one result, or ``None``.

    Deezer tracks reach the results list by three different routes -- the
    native Deezer backend, Side B, and musicdl's own Deezer client -- and
    only the first two carry a ``kind`` that says so. All three do carry
    the track id somewhere, and that id is what both playback paths need,
    so it is read off whichever field has it rather than trusted to the
    kind alone.
    """
    if str(item.get("source") or "").startswith("Deezer") or item.get(
            "kind") in ("sideb", "deezer"):
        for value in (item.get("url"), item.get("id")):
            match = _DEEZER_TRACK_RE.search(str(value or ""))
            if match:
                return f"https://www.deezer.com/track/{match.group(1)}"
    return None


def _deezer_stream(item, title, config, full):
    """Resolve one Deezer track to something a player can actually open.

    A preview is the 30-second clip Deezer publishes for everyone: no
    sign-in, no transcode, and it starts at once. Full playback wants the
    whole recording, which means decrypting Deezer's own stream with the
    configured ARL cookie. Either way YouTube is the fallback, not the
    first answer -- a YouTube match can be the wrong recording, and
    YouTube can refuse the request outright.
    """
    track_url = deezer_track_url(item)
    if not full:
        preview_url = sideb_backend.get_deezer_preview_url(
            track_url or item.get("id", ""))
        if preview_url:
            return preview_url
    if track_url and (config.get("deezer_arl") or "").strip():
        try:
            return deezer_backend.playback_file(track_url, config)
        except Exception:  # noqa: BLE001 - YouTube is the fallback
            pass
    return _youtube_stream_for(item, title, config)


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
    # A Deezer row from musicdl has no page URL of its own, and its stream
    # URL is an encrypted CDN link that opens as nothing. The track page is
    # what a copied Deezer link is meant to be.
    deezer_url = deezer_track_url(item)
    if deezer_url:
        return deezer_url
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

    # Deezer is checked before the generic music-site branch below. A row
    # that came from musicdl's own Deezer client carries a song_info whose
    # download URL is Deezer's encrypted stream: handing that to a player
    # produces silence, not music. Every Deezer row therefore plays the
    # same way, whichever backend found it.
    if deezer_track_url(item) is not None:
        return _deezer_stream(item, title, config, full=False), title

    if item.get("kind") == "applemusic":
        # blindDL previews Apple Music rows without Apple credentials: the
        # applemusic search results carry Apple's own short preview m4a, and
        # the track's page URL is something yt-dlp cannot open at all.
        preview_url = item.get("preview_url") or ""
        if str(preview_url).startswith("http"):
            return str(preview_url), title

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


def resolve_full_playback(item, audio_only, config):
    """Return ``(stream URI, title)`` for full playback of one result.

    Preview deliberately gives Deezer results their 30-second clip. Full
    playback skips that and plays the whole recording: Deezer's own stream
    decrypted with the configured ARL cookie, or a YouTube match when
    there is no ARL. Every other kind already previews from a full stream,
    so it resolves the same way a preview would.
    """
    title = str(item.get("title") or "Playback")
    if deezer_track_url(item) is not None:
        return _deezer_stream(item, title, config, full=True), title
    return resolve_search_result(item, audio_only, config)


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
        # "Play URL" means the whole thing, not Deezer's 30-second clip.
        stream, item_title = resolve_full_playback(
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
