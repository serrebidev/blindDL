# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Adult-media providers and native BoyfriendTV URL extraction.

The EchterAlsFake libraries are deliberately imported only when a provider is
used. They require Python 3.12+ and retain their upstream licenses; blindDL
does not copy their source into this module.

Every public function is synchronous because blindDL calls backends from worker
threads.  A fresh API client and event loop is used for every operation; video
objects from a search are reduced to their public URL and fetched again by the
download worker, avoiding reuse of HTTP sessions bound to a closed loop.
"""

from __future__ import annotations

import asyncio
import html
import importlib
import importlib.util
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

from . import ytdlp_backend


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    module: str
    domains: tuple[str, ...]
    search_method: str | None = None
    search_kwargs: dict = field(default_factory=dict)
    config_class: str = "DownloadConfigHLS"
    download_style: str = "standard"
    audience: str = "straight"


AUDIENCE_STRAIGHT = "straight"
AUDIENCE_LGBTQ = "lgbtq"


# All repositories explicitly named unofficial-api-for-* on the upstream
# account as of 2026-08-02.  Beeg and archived Porngo are URL-only because the
# their clients expose no search method; Sex.com is an image/pin provider.
PROVIDERS = {
    provider.key: provider
    for provider in (
        Provider("beeg", "Beeg", "beeg_api", ("beeg.com",)),
        Provider(
            "eporner", "EPorner", "eporner_api", ("eporner.com",),
            "search_videos",
            {
                "sorting_gay": "1",
                "sorting_order": "most-popular",
                "sorting_low_quality": "1",
                "per_page": 20,
                "pages": 1,
                "load_html": False,
                "load_api": False,
            },
            "DownloadConfigRAW", "eporner", AUDIENCE_LGBTQ,
        ),
        Provider(
            "hqporner", "HQPorner", "hqporner_api", ("hqporner.com",),
            "search_videos", {"pages": 1, "load_html": False},
            "DownloadConfigRAW",
        ),
        Provider(
            "missav", "MissAV", "missav_api", ("missav.ws",),
            "search", {"video_count": 20, "load_html": True},
        ),
        Provider(
            "porngo", "Porngo (archived)", "porngo_api", ("porngo.com",),
            config_class="", download_style="porngo",
        ),
        Provider(
            "pornhub", "Pornhub", "pornhub_api", ("pornhub.com",),
            "search_videos", {"pages": 1, "load_html": False,
                              "load_api": True},
        ),
        Provider(
            "porntrex", "Porntrex", "porntrex_api", ("porntrex.com",),
            "search", {"pages": 1, "load_html": False},
            "DownloadConfigRAW",
        ),
        Provider(
            "redtube", "RedTube", "redtube_api", ("redtube.com",),
            "search", {"pages": 1, "load_html": False},
        ),
        Provider(
            "sex", "Sex.com (archived)", "sex_api", ("sex.com",),
            "search", {"pages": 2}, config_class="", download_style="pin",
        ),
        Provider(
            "spankbang", "SpankBang", "spankbang_api", ("spankbang.com",),
            "search", {"pages": 1, "load_html": False},
            download_style="spankbang",
        ),
        Provider(
            "thumbzilla", "Thumbzilla", "thumbzilla_api",
            ("thumbzilla.com",), "search", {"pages": 1},
        ),
        Provider(
            "tube8", "Tube8", "tube8_api", ("tube8.com",),
            "search", {"pages": 1, "load_html": False},
        ),
        Provider(
            "xfreehd", "XFreeHD", "xfreehd_api", ("xfreehd.com",),
            "search", {"pages": 1, "load_html": False},
            "DownloadConfigRAW",
        ),
        Provider(
            "xhamster", "xHamster", "xhamster_api", ("xhamster.com",),
            "search_videos", {"pages": 1, "load_html": False,
                              "minimum_quality": "720p"},
        ),
        Provider(
            "xnxx", "XNXX", "xnxx_api", ("xnxx.com",),
            "search_videos", {"pages": 1, "load_html": False},
        ),
        Provider(
            "xvideos", "XVideos", "xvideos_api", ("xvideos.com",),
            "search", {"pages": 1, "load_html": False},
        ),
        Provider(
            "youporn", "YouPorn", "youporn_api", ("youporn.com",),
            "search_videos", {"pages": 1, "fetch_html": True},
        ),
    )
}

BOYFRIEND_KEY = "boyfriendtv"
BOYFRIEND_LABEL = "BoyfriendTV"
BOYFRIEND_DOMAINS = ("boyfriendtv.com",)
MAX_CONCURRENT_SEARCHES = 4
MAX_RESULTS_PER_SITE = 30
_search_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SEARCHES)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


def sources_by_label(audience=None):
    """Return searchable provider keys in accessible display order."""
    return sorted(
        (
            key for key, provider in PROVIDERS.items()
            if provider.search_method
            and (audience is None or provider.audience == audience)
        ),
        key=lambda key: PROVIDERS[key].label.lower(),
    )


def source_label(key):
    return PROVIDERS[key].label


def unavailable_sources():
    """Search providers whose bundled Python package is not installed."""
    return {
        key for key in sources_by_label()
        if importlib.util.find_spec(PROVIDERS[key].module) is None
    }


def enabled_sources(disabled):
    disabled = set(disabled or ())
    return [key for key in sources_by_label() if key not in disabled]


def _host_matches(host, domains):
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in domains)


def provider_for_url(url):
    host = urlparse(url).hostname
    for provider in PROVIDERS.values():
        if _host_matches(host, provider.domains):
            return provider
    return None


def is_boyfriendtv_url(url):
    return _host_matches(urlparse(url).hostname, BOYFRIEND_DOMAINS)


def is_supported_url(url):
    return is_boyfriendtv_url(url) or provider_for_url(url) is not None


def _import_provider(provider):
    if importlib.util.find_spec(provider.module) is None:
        raise RuntimeError(
            f"{provider.label} support is not installed. "
            "Reinstall blindDL to restore its bundled adult providers."
        )
    try:
        base_api = importlib.import_module("base_api")
        if provider.key == "porngo" and not hasattr(base_api, "setup_logger"):
            # The archived Porngo 1.4.2 package imports the old public name.
            # Active APIs require eaf_base_api 3.x, where it was renamed.
            from base_api.modules.logger import configure_app_logging

            def setup_logger(name=None, log_file=None, level=None,
                             http_ip=None, http_port=None):
                logger = configure_app_logging(
                    logger_name=name, log_file=log_file,
                    level=level if level is not None else 20,
                    http_ip=http_ip, http_port=http_port,
                )
                logger.disabled = True
                return logger

            base_api.setup_logger = setup_logger
            errors = importlib.import_module("base_api.modules.errors")
            if not hasattr(errors, "NetworkingError"):
                errors.NetworkingError = errors.NetworkRequestError
        previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            module = importlib.import_module(provider.module)
            client = module.Client(core=base_api.BaseCore())
        finally:
            logging.disable(previous_disable)
    except ImportError as exc:
        raise RuntimeError(
            f"{provider.label} provider could not load: {exc}"
        ) from exc
    for name in list(logging.Logger.manager.loggerDict):
        if name == provider.module or name.startswith(provider.module + "."):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    # eaf_base_api uses these non-package logger names for iterator failures
    # and networking. Errors are returned through blindDL instead of being
    # printed to a screen-reader user's console.
    for name in ("helper.iterator", "BASE API - [BaseCore]"):
        logging.getLogger(name).disabled = True
    for obj in (client, getattr(client, "core", None)):
        logger = getattr(obj, "logger", None)
        if isinstance(logger, logging.Logger):
            logger.setLevel(logging.CRITICAL)
    return module, client


def _unwrap(result):
    if hasattr(result, "is_success"):
        return result.video if result.is_success else None
    return result


def _first_attr(obj, *names):
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001 - third-party lazy properties can fail
            continue
        if inspect.isawaitable(value):
            # Some upstream models expose async metadata properties. Search
            # normalization is intentionally side-effect free, so skip them
            # and close bare coroutine objects to avoid RuntimeWarning noise.
            if inspect.iscoroutine(value):
                value.close()
            continue
        if value not in (None, "", [], ()):  # keep zero out of metadata text
            return value
    return None


def _duration_seconds(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if len(parts) in (2, 3) and all(part.isdigit() for part in parts):
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    match = re.search(r"([\d.]+)\s*(?:min|minute)", text)
    if match:
        return int(float(match.group(1)) * 60)
    match = re.search(r"([\d.]+)\s*(?:sec|second)", text)
    if match:
        return int(float(match.group(1)))
    return None


def _normalize(provider, media):
    if media is None:
        return None
    url = _first_attr(media, "url", "webpage_url", "embed_url") or ""
    title = _first_attr(media, "title", "name") or "Unknown title"
    artist = _first_attr(
        media, "author_name", "uploader", "author", "pornstar", "pornstars",
        "actors", "models",
    ) or ""
    if isinstance(artist, (list, tuple, set)):
        artist = ", ".join(str(value) for value in artist if value)
    duration = _first_attr(
        media, "duration", "length_seconds", "video_duration", "length",
    )
    media_id = _first_attr(media, "video_id", "id", "key") or url
    if not url:
        return None
    return {
        "id": f"adult:{provider.key}:{media_id}",
        "kind": "adult",
        "provider": provider.key,
        "title": str(title),
        "artist": str(artist),
        "source": provider.label,
        "duration_s": _duration_seconds(duration),
        "file_size": "",
        "url": str(url),
    }


async def _collect_search(provider, query, stop):
    _module, client = _import_provider(provider)
    method = getattr(client, provider.search_method)
    results = method(query, **dict(provider.search_kwargs))
    if inspect.isawaitable(results):
        results = await results
    items = []
    if hasattr(results, "__aiter__"):
        async for result in results:
            if stop is not None and stop.is_set():
                break
            item = _normalize(provider, _unwrap(result))
            if item:
                items.append(item)
            if len(items) >= MAX_RESULTS_PER_SITE:
                break
    else:
        for result in results or ():
            item = _normalize(provider, _unwrap(result))
            if item:
                items.append(item)
            if len(items) >= MAX_RESULTS_PER_SITE:
                break
    return items


def search(query, timeout_s=10.0, on_site=None, stop=None, sources=None):
    """Search selected API providers concurrently.

    Returns ``(items, answered, asked)`` with the same contract as
    :func:`musicdl_backend.search`.  Slow sites may report later through
    ``on_site``; a shared gate prevents repeated searches from multiplying
    active network work.
    """
    keys = enabled_sources(()) if sources is None else list(sources)
    found = {}
    found_lock = threading.Lock()

    def search_one(key):
        provider = PROVIDERS[key]
        with _search_slots:
            if stop is not None and stop.is_set():
                return
            try:
                items = asyncio.run(_collect_search(provider, query, stop))
            except Exception:  # noqa: BLE001 - one provider cannot kill the rest
                items = []
        with found_lock:
            found[key] = items
        if on_site is not None and (stop is None or not stop.is_set()):
            try:
                on_site(provider.label, items)
            except Exception:  # noqa: BLE001 - callback failure is not provider failure
                pass

    threads = []
    for key in keys:
        thread = threading.Thread(
            target=search_one, args=(key,), daemon=True,
            name=f"adult-search-{key}",
        )
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + float(timeout_s)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    with found_lock:
        answered = dict(found)
    items = []
    for key in keys:
        items.extend(answered.get(key, ()))
    return (
        items,
        [PROVIDERS[key].label for key in keys if key in answered],
        [PROVIDERS[key].label for key in keys],
    )


def inspect_url(url):
    """Resolve a supported adult URL into normalized queue items and title."""
    if is_boyfriendtv_url(url):
        item = _inspect_boyfriendtv(url)
        return [item], item["title"]
    provider = provider_for_url(url)
    if provider is None:
        raise ValueError(f"No adult API is registered for: {url}")

    async def resolve():
        _module, client = _import_provider(provider)
        if provider.download_style == "pin":
            return await client.get_pin(url)
        return await client.get_video(url)

    item = _normalize(provider, asyncio.run(resolve()))
    if item is None:
        raise RuntimeError(f"{provider.label} returned no downloadable media.")
    return [item], item["title"]


def _json_objects_after(pattern, text):
    decoder = json.JSONDecoder()
    for match in re.finditer(pattern, text, re.IGNORECASE):
        start = text.find("{", match.end())
        if start < 0:
            continue
        try:
            value, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _media_definitions(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("mediaDefinitions", "hlsData"):
                found.extend(_media_definitions(child))
            elif isinstance(child, (dict, list)):
                found.extend(_media_definitions(child))
        if any(key in value for key in ("videoUrl", "video_url", "url")):
            found.append(value)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, str):
                found.append({"videoUrl": child})
            else:
                found.extend(_media_definitions(child))
    return found


def _quality(definition):
    value = definition.get("quality") or definition.get("height") or 0
    match = re.search(r"\d{3,4}", str(value))
    return int(match.group()) if match else 0


def _inspect_boyfriendtv(url):
    response = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    response.raise_for_status()
    page = response.text
    definitions = []
    patterns = (
        r"(?:var\s+)?flashvars_\d+\s*=",
        r"(?:window\.)?__boyfriendtvPageData\s*=",
        r"(?:window\.)?__pornhubPageData\s*=",
    )
    for pattern in patterns:
        for value in _json_objects_after(pattern, page):
            definitions.extend(_media_definitions(value))

    # Page scripts occasionally expose only the literal media URL.
    unescaped = page.replace(r"\/", "/")
    for media_url in re.findall(
        r'https?://[^\s"\'<>]+?(?:\.m3u8|\.mp4)(?:\?[^\s"\'<>]*)?',
        unescaped,
        re.IGNORECASE,
    ):
        definitions.append({
            "videoUrl": html.unescape(media_url),
            "pageLiteral": True,
        })

    candidates = []
    for definition in definitions:
        media_url = (definition.get("videoUrl") or definition.get("video_url")
                     or definition.get("url"))
        if isinstance(media_url, list):
            media_url = media_url[0] if media_url else None
        if not media_url:
            continue
        media_url = html.unescape(str(media_url)).replace(r"\/", "/")
        lowered_url = media_url.lower()
        # The page contains MP4-looking thumbnails and recommendation
        # previews alongside the player source.  Neither is downloadable as
        # the current video; in particular, thumbnail URLs return HTTP 404.
        if "/thumbs/" in lowered_url or "/pv_" in lowered_url:
            continue
        is_hls = (
            str(definition.get("format") or "").lower() == "hls"
            or ".m3u8" in lowered_url
            or "media=hls4a" in lowered_url
        )
        priority = _quality(definition)
        if "media=hls4a" in lowered_url:
            priority += 100_000
        elif is_hls:
            priority += 80_000
        elif not definition.get("pageLiteral"):
            priority += 40_000
        candidates.append((
            priority,
            urljoin(url, media_url),
        ))
    if not candidates:
        raise RuntimeError("BoyfriendTV exposed no MP4 or HLS stream on this page.")
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    title_match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']'
        r'[^>]+content=["\']([^"\']+)', page, re.IGNORECASE,
    )
    if not title_match:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", page,
                                re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1)).strip() if title_match else url
    return {
        "id": f"adult:{BOYFRIEND_KEY}:{url}",
        "kind": "adult",
        "provider": BOYFRIEND_KEY,
        "title": title,
        "artist": "",
        "source": BOYFRIEND_LABEL,
        "duration_s": None,
        "file_size": "",
        "url": url,
        "direct_url": candidates[0][1],
        "referer": url,
    }


def download(payload, out_dir, progress_cb=None, cancel_event=None):
    """Download a normalized adult item with progress and cancellation."""
    provider_key = payload["provider"]
    if provider_key == BOYFRIEND_KEY:
        ytdlp_backend.download(
            payload["direct_url"], out_dir, audio_only=False,
            progress_cb=progress_cb, cancel_event=cancel_event,
            http_headers={"Referer": payload["referer"], "User-Agent": _UA},
        )
        return
    provider = PROVIDERS[provider_key]

    async def run():
        module, client = _import_provider(provider)
        if provider.download_style == "pin":
            media = await client.get_pin(payload["url"])
            result = await media.download(out_dir)
        else:
            media = await client.get_video(payload["url"])

            def callback(current, total):
                if progress_cb is not None:
                    progress_cb(current, total)

            if provider.download_style == "porngo":
                result = await media.download(
                    quality="720p", path=out_dir, callback=callback,
                    stop_event=cancel_event,
                )
            else:
                config_type = getattr(module, provider.config_class)
                config = config_type(
                    quality="best", path=out_dir, callback=callback,
                    stop_event=cancel_event,
                )
                if provider.download_style == "eporner":
                    result = await media.download(config, mode="best")
                elif provider.download_style == "spankbang":
                    result = await media.download(
                        configuration_hls=config, use_hls=True)
                else:
                    result = await media.download(config)
        if result is False:
            raise RuntimeError(f"{provider.label} could not download this item.")

    os.makedirs(out_dir, exist_ok=True)
    asyncio.run(run())
