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
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests

from . import creator_backend, ytdlp_backend


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
    search_categories: tuple[str, ...] = ()


CONTENT_STRAIGHT = "straight"
CONTENT_GAY = "gay"
CONTENT_LESBIAN = "lesbian"
CONTENT_BISEXUAL = "bisexual"
CONTENT_TRANS = "trans"
CONTENT_CATEGORIES = (
    CONTENT_STRAIGHT,
    CONTENT_GAY,
    CONTENT_LESBIAN,
    CONTENT_BISEXUAL,
    CONTENT_TRANS,
)
CONTENT_QUERY_TERMS = {
    CONTENT_STRAIGHT: "straight",
    CONTENT_GAY: "gay",
    CONTENT_LESBIAN: "lesbian",
    CONTENT_BISEXUAL: "bisexual",
    CONTENT_TRANS: "trans",
}
XNXX_CONTENT_MODES = {
    CONTENT_STRAIGHT: "",
    CONTENT_GAY: "/gay",
    CONTENT_LESBIAN: "/lesbian",
    CONTENT_BISEXUAL: "/bisexual",
    CONTENT_TRANS: "/trans",
}
THISVID_CONTENT_PATHS = {
    CONTENT_STRAIGHT: "female/",
    CONTENT_GAY: "male/",
    CONTENT_LESBIAN: "female/",
    CONTENT_BISEXUAL: "search/",
    CONTENT_TRANS: "female/",
}
XHAMSTER_CONTENT_PATHS = {
    CONTENT_STRAIGHT: "search/",
    CONTENT_GAY: "gay/search/",
    CONTENT_LESBIAN: "search/",
    CONTENT_BISEXUAL: "search/",
    CONTENT_TRANS: "shemale/search/",
}

# Some sites mix explicitly trans-tagged videos into their nominal gay feeds.
# Keep this deliberately limited to unambiguous metadata terms: gender
# expression such as "femboy" or "crossdresser" is not itself trans content.
_TRANS_RESULT_PATTERN = re.compile(
    r"(?ix)\b(?:"
    r"shemales?|tranny|trannies|trans|transgender(?:ed)?|transsexuals?|"
    r"trans(?:woman|women|man|men|girl|girls|boy|boys)|"
    r"ladyboys?|t[-\s]?girls?|futanari|ftm|mtf|ts"
    r")\b"
)


# All repositories explicitly named unofficial-api-for-* on the upstream
# account as of 2026-08-02, plus ThisVid through yt-dlp and native public-page
# search. Beeg and archived Porngo are URL-only because their clients expose
# no search method; Sex.com is an image/pin provider.
PROVIDERS = {
    provider.key: provider
    for provider in (
        Provider(
            "aebn", "AEBN", "aebn_dl", ("aebn.com",),
            config_class="", download_style="aebn",
        ),
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
            "DownloadConfigRAW", "eporner",
        ),
        Provider(
            "hqporner", "HQPorner", "hqporner_api", ("hqporner.com",),
            "search_videos", {"pages": 1, "load_html": False},
            "DownloadConfigRAW",
        ),
        Provider(
            "justforfans", "JustForFans", "requests", ("justfor.fans",),
            config_class="", download_style="creator",
        ),
        Provider(
            "missav", "MissAV", "missav_api", ("missav.ws",),
            "search", {"video_count": 20, "load_html": True},
        ),
        Provider(
            "mymusclevideo", "MyMuscleVideo", "yt_dlp",
            ("mymusclevideo.com",), "search", download_style="ytdlp",
            search_categories=("gay",),
        ),
        Provider(
            "onlyfans", "OnlyFans", "requests", ("onlyfans.com",),
            config_class="", download_style="creator",
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
            config_class="", download_style="pin",
        ),
        Provider(
            "spankbang", "SpankBang", "spankbang_api", ("spankbang.com",),
            config_class="", download_style="spankbang",
        ),
        Provider(
            "thumbzilla", "Thumbzilla", "thumbzilla_api",
            ("thumbzilla.com",), "search", {"pages": 1},
        ),
        Provider(
            "thisvid", "ThisVid", "yt_dlp", ("thisvid.com",),
            "search", download_style="ytdlp",
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
_aebn_init_lock = threading.Lock()
_provider_logging_lock = threading.Lock()
_provider_logging_depth = 0
_provider_previous_logging_level = logging.NOTSET

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


@contextmanager
def _silence_provider_logging():
    """Keep third-party provider diagnostics out of the accessible UI console."""
    global _provider_logging_depth, _provider_previous_logging_level
    with _provider_logging_lock:
        if _provider_logging_depth == 0:
            _provider_previous_logging_level = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
        _provider_logging_depth += 1
    try:
        yield
    finally:
        with _provider_logging_lock:
            _provider_logging_depth -= 1
            if _provider_logging_depth == 0:
                logging.disable(_provider_previous_logging_level)


class _ThisVidSearchParser(HTMLParser):
    """Extract public ThisVid result links without external parser packages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.seen = set()
        self._pending = None
        self._pending_private = False

    @staticmethod
    def _marks_private(values):
        classes = set((values.get("class") or "").casefold().split())
        return (
            "private" in classes
            or (values.get("alt") or "").strip().casefold() == "private"
        )

    def _finish_pending(self):
        if self._pending is None:
            return
        url, title = self._pending
        if not self._pending_private and url not in self.seen:
            self.seen.add(url)
            self.items.append((url, title))
        self._pending = None
        self._pending_private = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self._pending is not None:
            self._pending_private = (
                self._pending_private or self._marks_private(values))
        if tag.casefold() != "a":
            return
        classes = set((values.get("class") or "").split())
        if "tumbpu" not in classes:
            return
        # A malformed result without a closing anchor must not absorb the
        # privacy state of the next card.
        self._finish_pending()
        url = urljoin("https://thisvid.com/", values.get("href") or "")
        parsed = urlparse(url)
        if (not _host_matches(parsed.hostname, ("thisvid.com",))
                or "/videos/" not in parsed.path):
            return
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        title = html.unescape(values.get("title") or "").strip()
        if not title:
            title = slug.replace("-", " ").strip().title()
        self._pending = (url, title)
        self._pending_private = self._marks_private(values)

    def handle_endtag(self, tag):
        if tag.casefold() == "a":
            self._finish_pending()

    def close(self):
        super().close()
        self._finish_pending()


class _MyMuscleVideoSearchParser(HTMLParser):
    """Extract titled public video cards from MyMuscleVideo search pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        url = urljoin("https://mymusclevideo.com/", values.get("href") or "")
        parsed = urlparse(url)
        if (not _host_matches(parsed.hostname, ("mymusclevideo.com",))
                or not re.fullmatch(r"/\d+/[^/]+/", parsed.path)
                or url in self.seen):
            return
        title = html.unescape(values.get("title") or "").strip()
        if not title:
            return
        self.seen.add(url)
        self.items.append((url, title))


class _XHamsterSearchParser(HTMLParser):
    """Extract current xHamster video cards when its API parser is stale."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        if values.get("data-role") != "thumb-link":
            return
        url = values.get("href") or ""
        parsed = urlparse(url)
        if (not _host_matches(parsed.hostname, ("xhamster.com",))
                or not parsed.path.startswith("/videos/") or url in self.seen):
            return
        title = html.unescape(
            values.get("aria-label") or values.get("title") or "").strip()
        if not title:
            return
        self.seen.add(url)
        self.items.append((url, title))


def _search_xhamster_fallback(query, category):
    from curl_cffi.requests import Session

    categorized_query, kwargs = _search_parameters(
        PROVIDERS["xhamster"], query, category)
    params = {"quality": kwargs.get("minimum_quality", "720p")}
    if kwargs.get("category"):
        params["cats"] = kwargs["category"]
    url = (
        "https://xhamster.com/" + XHAMSTER_CONTENT_PATHS[category]
        + quote(categorized_query, safe="")
        + "?" + urlencode(params, doseq=True)
    )
    session = Session(impersonate="chrome")
    try:
        response = session.get(url, headers={"User-Agent": _UA}, timeout=30)
        response.raise_for_status()
        parser = _XHamsterSearchParser()
        parser.feed(response.text)
        parser.close()
    finally:
        session.close()
    return [
        {
            "id": f"adult:xhamster:{video_url}",
            "kind": "adult",
            "provider": "xhamster",
            "title": title,
            "artist": "",
            "source": "xHamster",
            "duration_s": None,
            "file_size": "",
            "url": video_url,
            "adult_category": category,
        }
        for video_url, title in parser.items[:MAX_RESULTS_PER_SITE]
    ]


def _search_mymusclevideo(query, category):
    categorized_query, _kwargs = _search_parameters(
        PROVIDERS["mymusclevideo"], query, category)
    response = requests.get(
        "https://mymusclevideo.com/search/video/",
        params={"s": categorized_query},
        headers={"User-Agent": _UA},
        timeout=30,
    )
    response.raise_for_status()
    parser = _MyMuscleVideoSearchParser()
    parser.feed(response.text)
    parser.close()
    return [
        {
            "id": f"adult:mymusclevideo:{url}",
            "kind": "adult",
            "provider": "mymusclevideo",
            "title": title,
            "artist": "",
            "source": "MyMuscleVideo",
            "duration_s": None,
            "file_size": "",
            "url": url,
            "adult_category": category,
        }
        for url, title in parser.items[:MAX_RESULTS_PER_SITE]
    ]


def _search_thisvid(query, category):
    categorized_query, _kwargs = _search_parameters(
        PROVIDERS["thisvid"], query, category)
    response = requests.get(
        urljoin("https://thisvid.com/", THISVID_CONTENT_PATHS[category]),
        params={"q": categorized_query},
        headers={"User-Agent": _UA},
        timeout=30,
    )
    response.raise_for_status()
    parser = _ThisVidSearchParser()
    parser.feed(response.text)
    parser.close()
    return [
        {
            "id": f"adult:thisvid:{url}",
            "kind": "adult",
            "provider": "thisvid",
            "title": title,
            "artist": "",
            "source": "ThisVid",
            "duration_s": None,
            "file_size": "",
            "url": url,
            "adult_category": category,
        }
        for url, title in parser.items[:MAX_RESULTS_PER_SITE]
    ]


def sources_by_label():
    """Return searchable provider keys in accessible display order."""
    return sorted(
        (key for key, provider in PROVIDERS.items() if provider.search_method),
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


def _import_aebn():
    if importlib.util.find_spec("aebn_dl") is None:
        raise RuntimeError(
            "AEBN support is not installed. Reinstall blindDL to restore "
            "its bundled AEBN downloader."
        )
    try:
        from aebn_dl import Downloader
        from aebn_dl import utils as aebn_utils
        from aebn_dl.custom_session import CustomSession
        from aebn_dl.movie_scraper import Movie
    except ImportError as exc:
        raise RuntimeError(f"AEBN provider could not load: {exc}") from exc
    return Downloader, Movie, CustomSession, aebn_utils


def _aebn_content_category(url):
    parts = [part for part in urlparse(url).path.split("/") if part]
    if (len(parts) < 3 or parts[0] not in (CONTENT_STRAIGHT, CONTENT_GAY)
            or parts[1] != "movies" or not parts[2].isdigit()):
        raise ValueError(
            "AEBN support requires a straight.aebn.com or gay.aebn.com "
            "movie URL."
        )
    return parts[0]


def _inspect_aebn(url):
    category = _aebn_content_category(url)
    _downloader, Movie, CustomSession, _utils = _import_aebn()
    session = CustomSession(impersonate="chrome")
    session.timeout = 30
    session.headers.update({
        "User-Agent": _UA,
        "Connection": "keep-alive",
    })
    try:
        movie = Movie(url, session)
    finally:
        session.close()
    performers = ", ".join(str(name) for name in movie.performers or () if name)
    item = {
        "id": f"adult:aebn:{movie.movie_id}",
        "kind": "adult",
        "provider": "aebn",
        "title": movie.title,
        "artist": performers,
        "source": "AEBN",
        "duration_s": movie.total_duration_seconds,
        "file_size": "",
        "url": url,
        "adult_category": category,
    }
    return [item], item["title"]


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


def _matches_content_category(item, category):
    """Enforce category boundaries when a site's own feed is imprecise."""
    if category != CONTENT_GAY:
        return True
    url_path = urlparse(str(item.get("url", ""))).path
    searchable = " ".join((
        str(item.get("title", "")), str(item.get("artist", "")), url_path,
    ))
    return _TRANS_RESULT_PATTERN.search(searchable) is None


def _search_parameters(provider, query, category):
    """Return a category-aware query and provider keyword arguments."""
    if category not in CONTENT_CATEGORIES:
        raise ValueError(f"Unknown adult content category: {category}")
    kwargs = dict(provider.search_kwargs)
    native_filter = False

    # XNXX supports category paths for every content choice. Its wrapper's
    # Mode enum omits these paths, but the public method accepts strings and
    # forwards them to /search/<mode>/<query>.
    if provider.key == "xnxx":
        kwargs["mode"] = XNXX_CONTENT_MODES[category]
        native_filter = True

    # EPorner can distinguish its default catalog from gay-only content while
    # retaining the user's text query. Its category-browse method also lists
    # lesbian, bisexual, and trans categories, but cannot combine them with a
    # text query, so those categories continue through the query fallback.
    if provider.key == "eporner":
        kwargs["sorting_gay"] = {
            CONTENT_STRAIGHT: "0",
            CONTENT_GAY: "2",
            CONTENT_LESBIAN: "0",
            CONTENT_BISEXUAL: "1",
            CONTENT_TRANS: "1",
        }[category]

        native_filter = category in (CONTENT_STRAIGHT, CONTENT_GAY)

    # xHamster exposes lesbian as a query-compatible native category. Its
    # other orientation categories are not accepted by the installed API.
    if provider.key == "xhamster":
        if category == CONTENT_LESBIAN:
            kwargs["category"] = "lesbian"
            native_filter = True
        elif category in (CONTENT_GAY, CONTENT_TRANS):
            native_filter = True

    # ThisVid calls its two query-compatible top-level catalogs female and
    # male, while labeling them Straight and Gay in the page UI. Lesbian and
    # trans queries remain on the female catalog with an explicit term;
    # bisexual can span both catalogs and therefore uses the general search.
    if provider.key == "thisvid" and category in (
            CONTENT_STRAIGHT, CONTENT_GAY):
        native_filter = True

    # MyMuscleVideo is a gay-only catalog, so its own video search is already
    # the category filter and should receive the user's terms unchanged.
    if provider.key == "mymusclevideo":
        native_filter = True

    term = CONTENT_QUERY_TERMS[category]
    words = set(re.findall(r"[a-z]+", query.casefold()))
    categorized_query = (
        query if native_filter or term in words else f"{query} {term}")
    return categorized_query, kwargs


async def _collect_search(provider, query, stop, category=CONTENT_STRAIGHT):
    if provider.key == "thisvid":
        return await asyncio.to_thread(_search_thisvid, query, category)
    if provider.key == "mymusclevideo":
        return await asyncio.to_thread(
            _search_mymusclevideo, query, category)
    if provider.key == "xhamster":
        return await asyncio.to_thread(
            _search_xhamster_fallback, query, category)
    _module, client = _import_provider(provider)
    method = getattr(client, provider.search_method)
    categorized_query, kwargs = _search_parameters(
        provider, query, category)
    results = method(categorized_query, **kwargs)
    if inspect.isawaitable(results):
        results = await results
    items = []
    if hasattr(results, "__aiter__"):
        async for result in results:
            if stop is not None and stop.is_set():
                break
            item = _normalize(provider, _unwrap(result))
            if item:
                item["adult_category"] = category
                items.append(item)
            if len(items) >= MAX_RESULTS_PER_SITE:
                break
    else:
        for result in results or ():
            item = _normalize(provider, _unwrap(result))
            if item:
                item["adult_category"] = category
                items.append(item)
            if len(items) >= MAX_RESULTS_PER_SITE:
                break
    return items


def search(query, timeout_s=10.0, on_site=None, stop=None, sources=None,
           category=CONTENT_STRAIGHT):
    """Search selected API providers concurrently.

    Returns ``(items, answered, asked)`` with the same contract as
    :func:`musicdl_backend.search`.  Slow sites may report later through
    ``on_site``; a shared gate prevents repeated searches from multiplying
    active network work.
    """
    keys = enabled_sources(()) if sources is None else list(sources)
    keys = [
        key for key in keys
        if (not PROVIDERS[key].search_categories
            or category in PROVIDERS[key].search_categories)
    ]
    found = {}
    found_lock = threading.Lock()

    def search_one(key):
        provider = PROVIDERS[key]
        with _search_slots:
            if stop is not None and stop.is_set():
                return
            try:
                with _silence_provider_logging():
                    items = asyncio.run(
                        _collect_search(provider, query, stop, category))
                items = [
                    item for item in items
                    if _matches_content_category(item, category)
                ]
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


def inspect_url(url, config=None):
    """Resolve a supported adult URL into normalized queue items and title."""
    if is_boyfriendtv_url(url):
        item = _inspect_boyfriendtv(url)
        return [item], item["title"]
    provider = provider_for_url(url)
    if provider is None:
        raise ValueError(f"No adult API is registered for: {url}")
    if provider.download_style == "creator":
        return creator_backend.inspect_url(url, config=config)
    if provider.download_style == "aebn":
        return _inspect_aebn(url)
    if provider.download_style == "ytdlp":
        browser = config["cookies_from_browser"] if config is not None else ""
        extracted, title = ytdlp_backend.extract_flat(
            url, cookies_from_browser=browser)
        items = []
        for entry in extracted:
            items.append({
                "id": f"adult:{provider.key}:{entry['id']}",
                "kind": "adult",
                "provider": provider.key,
                "title": entry["title"],
                "artist": entry.get("uploader", ""),
                "source": provider.label,
                "duration_s": entry.get("duration"),
                "file_size": "",
                "url": entry["url"],
                "cookies_from_browser": browser,
            })
        if not items:
            raise RuntimeError(f"{provider.label} returned no downloadable media.")
        return items, title

    async def resolve():
        _module, client = _import_provider(provider)
        if provider.download_style == "pin":
            return await client.get_pin(url)
        return await client.get_video(url)

    with _silence_provider_logging():
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


def _download_aebn(payload, out_dir, progress_cb=None, cancel_event=None):
    """Run the MIT-licensed AEBN downloader without console/file logging."""
    if cancel_event is not None and cancel_event.is_set():
        raise ytdlp_backend.DownloadCancelled()
    _aebn_content_category(payload["url"])
    Downloader, _movie, _session, aebn_utils = _import_aebn()
    callback_lock = threading.Lock()

    class AccessibleDownloader(Downloader):
        def _process_manifest(self, scraped_movie, requires_scene_boundaries):
            super()._process_manifest(scraped_movie, requires_scene_boundaries)
            start = self.start_segment or 0
            end = self.end_segment or self.manifest.total_number_of_data_segments
            stream_count = 1 if self.target_stream else 2
            self._blinddl_total = max(1, end - start) * stream_count
            self._blinddl_completed = 0

        def _download_segment(self, stream, segment_number=None):
            if cancel_event is not None and cancel_event.is_set():
                return None
            result = super()._download_segment(stream, segment_number)
            if segment_number is not None and progress_cb is not None:
                with callback_lock:
                    self._blinddl_completed += 1
                    current = self._blinddl_completed
                progress_cb({
                    "status": "downloading",
                    "downloaded_bytes": current,
                    "total_bytes": self._blinddl_total,
                })
            return result

    # Upstream creates a working-directory log file and replaces
    # sys.excepthook in Downloader.__init__. A quiet constructor keeps the GUI
    # accessible and avoids global process changes. The lock makes that brief
    # module-level substitution safe with concurrent downloads.
    def quiet_logger(name, log_level):
        logger = logging.Logger(f"blinddl.aebn.{name}", logging.CRITICAL)
        logger.addHandler(logging.NullHandler())
        return logger

    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".blinddl-aebn-", dir=out_dir) as work_dir:
        with _aebn_init_lock:
            original_new_logger = aebn_utils.new_logger
            aebn_utils.new_logger = quiet_logger
            try:
                downloader = AccessibleDownloader(
                    url=payload["url"],
                    output_dir=out_dir,
                    work_dir=work_dir,
                    target_height=payload.get("target_height"),
                    start_segment=payload.get("start_segment", 0),
                    end_segment=payload.get("end_segment"),
                    no_metadata=True,
                    keep_logs=True,
                    show_progress=False,
                    log_level="CRITICAL",
                )
            finally:
                aebn_utils.new_logger = original_new_logger
        try:
            downloader.run()
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ytdlp_backend.DownloadCancelled() from exc
            raise
        finally:
            session = getattr(downloader, "session", None)
            if session is not None:
                session.close()
    if cancel_event is not None and cancel_event.is_set():
        raise ytdlp_backend.DownloadCancelled()


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
    if provider.download_style == "creator":
        creator_backend.download(
            payload, out_dir, progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        return
    if provider.download_style == "aebn":
        _download_aebn(
            payload, out_dir, progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        return
    if provider.download_style == "ytdlp":
        ytdlp_backend.download(
            payload["url"], out_dir, audio_only=False,
            progress_cb=progress_cb, cancel_event=cancel_event,
            cookies_from_browser=payload.get("cookies_from_browser"),
        )
        return

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
    with _silence_provider_logging():
        asyncio.run(run())
