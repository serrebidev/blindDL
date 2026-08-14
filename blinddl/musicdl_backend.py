# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""musicdl backend: search across music platforms via CharlesPikachu/musicdl.

Searches every client registered in musicdl that can be constructed without
extra account config (Deezer, SoundCloud, Netease, QQ, Kugou, Kuwo, Migu,
Jamendo, JioSaavn, YouTube Music, and dozens more). A few sources (e.g.
FLMP3) refuse to initialize without cookies and are skipped automatically.

Each source gets its own single-source MusicClient, because musicdl's
multi-source constructor aborts entirely when one source fails to build.
Searches return normalized dicts that keep the original SongInfo attached
so the download queue can hand it back to musicdl.

Every selected site starts at the same time on its own background thread.
Whatever answered within SEARCH_TIMEOUT_S (default 30s) is returned; late
sites can still report through the per-site callback.

musicdl is a console tool at heart: it logs to stderr and paints rich
progress bars while it works, and it drops a search_results.pkl under a
timestamped folder per site per search. Neither belongs in a GUI app -- the
output floods the terminal blindDL was started from, and the scratch
folders were landing in the user's music library -- so this module silences
both and keeps musicdl's scratch files in a cache directory. Only finished
downloads go to the user's download folder.
"""

import logging
import os
import shutil
import threading
import time

# musicdl configures an exclusive per-user FileHandler at import time. On
# Windows that prevents a second blindDL process (including the frozen release
# self-test) from importing musicdl while another instance still owns the log.
# blindDL does not use that third-party log, so substitute a no-op handler only
# for the import and immediately restore logging's real FileHandler class.
_file_handler = logging.FileHandler
try:
    logging.FileHandler = lambda *args, **kwargs: logging.NullHandler()
    from musicdl.musicdl import MusicClient, MusicClientBuilder
finally:
    logging.FileHandler = _file_handler

from requests.adapters import HTTPAdapter  # noqa: E402
from rich.progress import Progress  # noqa: E402

from .config import app_data_dir  # noqa: E402

ALL_SOURCES = sorted(MusicClientBuilder.REGISTERED_MODULES.keys())

# Per-search wall clock budget. Sites that answer later are dropped.
SEARCH_TIMEOUT_S = 30.0
# How many songs each source is asked for. Upstream adapters often cap their
# own page size lower, but the ones that can answer do.
SEARCH_SIZE_PER_SOURCE = 200
# Hard socket timeout, so an abandoned search thread dies instead of
# hanging on a dead host for the rest of the session.
HTTP_TIMEOUT_S = 30
# musicdl can create another worker pool inside every source. Keep each one at
# a single worker; otherwise the source-level pools can multiply this modest
# fan-out into hundreds of runnable threads.
SOURCE_SEARCH_THREADS = 1
_lock = threading.Lock()
_clients = None  # dict: source -> single-source MusicClient
_http_timeout_installed = False
_silenced = False
def cache_dir():
    """Scratch space for musicdl's per-search bookkeeping."""
    path = os.path.join(app_data_dir(), "musicdl-cache")
    os.makedirs(path, exist_ok=True)
    return path


def clear_cache():
    """Drop the scratch folders from previous sessions.

    musicdl writes a `<source>/<timestamp> <query>/search_results.pkl` tree
    for every site of every search, which would grow without bound.
    """
    try:
        shutil.rmtree(cache_dir(), ignore_errors=True)
    except OSError:
        pass


class _QuietProgress(Progress):
    """musicdl's progress bars, with the painting turned off."""

    def __init__(self, *args, **kwargs):
        kwargs["disable"] = True
        super().__init__(*args, **kwargs)


def _silence_musicdl():
    """Keep musicdl's console output out of blindDL's terminal.

    Importing musicdl runs logging.basicConfig with a StreamHandler, so
    every site's INFO/WARNING chatter goes to stderr; its rich progress
    bars go to stdout on top of that. Drop the stream handler and hand the
    source modules a progress class that never draws.
    """
    global _silenced
    if _silenced:
        return
    root = logging.getLogger()
    for handler in list(root.handlers):
        # FileHandler is a subclass of StreamHandler, hence the exact check.
        if type(handler) is logging.StreamHandler:
            root.removeHandler(handler)
    logging.getLogger("musicdl").setLevel(logging.CRITICAL)
    _silenced = True


def _silence_progress_bars():
    """Swap rich's Progress for the disabled one in every musicdl module.

    The source modules bind Progress by name at import time, so patching
    rich itself would come too late; this walks the modules instead. New
    sources can be imported lazily, so it is cheap to re-run before a
    search.
    """
    import sys

    for name, module in list(sys.modules.items()):
        if not name.startswith("musicdl") or module is None:
            continue
        if getattr(module, "Progress", None) is Progress:
            setattr(module, "Progress", _QuietProgress)


def _install_http_timeout():
    """Give every musicdl request a default timeout.

    musicdl calls requests without one, so a site that accepts the
    connection and then never answers blocks its thread forever. Patching
    the adapter covers requests.get/post and session calls alike, and only
    fills in a timeout where the caller did not specify one.
    """
    global _http_timeout_installed
    if _http_timeout_installed:
        return
    original = HTTPAdapter.send

    def send(self, request, *args, **kwargs):
        if not args and kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT_S
        return original(self, request, *args, **kwargs)

    HTTPAdapter.send = send
    _http_timeout_installed = True


_install_http_timeout()
_silence_musicdl()
_silence_progress_bars()


def _get_clients():
    global _clients
    with _lock:
        if _clients is None:
            work_dir = cache_dir()
            clients = {}
            for source in ALL_SOURCES:
                try:
                    clients[source] = MusicClient(
                        music_sources=[source],
                        clients_threadings={source: SOURCE_SEARCH_THREADS},
                        init_music_clients_cfg={source: {
                            "work_dir": work_dir,
                            "disable_print": True,
                            # Retrying a dead site three times only burns
                            # the search budget.
                            "max_retries": 1,
                            # Ask each source for a full page rather than
                            # musicdl's default handful.
                            "search_size_per_source": SEARCH_SIZE_PER_SOURCE,
                            "search_size_per_page": SEARCH_SIZE_PER_SOURCE,
                        }},
                    )
                except Exception:  # noqa: BLE001 - source needs cookies/config
                    continue
            _silence_progress_bars()  # catches lazily imported sources
            _clients = clients
        return _clients


def warm_up():
    """Build the per-site clients ahead of the first search.

    Constructing all 48 clients takes about six seconds, which would
    otherwise be charged to the user's first search on top of its own
    budget. Safe to call from a background thread at startup.
    """
    try:
        clear_cache()
        _get_clients()
    except Exception:  # noqa: BLE001 - the next search will report properly
        pass


def _short_source(source):
    """'DeezerMusicClient' -> 'Deezer', the name shown in the results list."""
    return source.replace("MusicClient", "")


def source_label(source):
    """Human-facing name for a musicdl source."""
    return _short_source(source)


def sources_by_label():
    """Every site musicdl registers, ordered the way a list should read."""
    return sorted(ALL_SOURCES, key=lambda s: source_label(s).lower())


def unavailable_sources():
    """Sites that cannot be used because they need account details.

    A few sources refuse to initialize without cookies or a quark parser
    config (TIDAL, FLMP3, ...). Returns an empty set while the clients are
    still being built, so a caller never blocks on this.
    """
    if _clients is None:
        return set()
    return set(ALL_SOURCES) - set(_clients)


def enabled_sources(disabled):
    """The sites to search, given the user's switched-off list."""
    disabled = set(disabled or ())
    return [s for s in ALL_SOURCES if s not in disabled]


def _normalize(source, songs):
    items = []
    for index, song in enumerate(songs):
        items.append({
            "id": f"{source}:{song.download_url if isinstance(song.download_url, str) else index}:{song.song_name}",
            "title": song.song_name or "Unknown title",
            "artist": song.singers or "",
            "album": song.album or "",
            "source": _short_source(source),
            "duration_s": song.duration_s,
            "file_size": song.file_size or "",
            # What the site actually serves, so the results list can say so
            # before anything is downloaded.
            "format": str(song.ext or "").lstrip(".").upper(),
            "song_info": song,
        })
    return items


def search(keyword, timeout_s=SEARCH_TIMEOUT_S, on_site=None, stop=None,
           sources=None, order=None):
    """Search the chosen music sites at once and return after timeout_s.

    sources is a list of musicdl source names; None means every site that
    could be built. Every provider starts immediately on its own background
    thread. Sites still working when the budget runs out are not waited for,
    but they are not thrown away either: on_site(source, items) fires for every
    site that answers, late ones included, so a caller can keep filling a
    results list after this function has already returned. Set the `stop`
    event to silence queued work and late callbacks from a superseded search.

    ``order`` is accepted for the shared backend contract. musicdl's site
    adapters do not expose sorting, so they keep their own best-match order.

    Returns (items, answered, asked): items are the normalized result dicts
    available at the deadline, answered is the list of sites that replied by
    then, asked is every site the search went out to. Sites in asked but not
    in answered are still working; they report through on_site later. Some
    are genuinely slow -- Deezer resolves a download mirror per song and can
    take four minutes -- so a caller should tell the user they are pending
    rather than call the search empty.
    """
    # Include first-use client construction in the user-visible budget. The
    # normal startup warm-up makes this nearly free, but an immediate search
    # must not silently run longer than the configured timeout.
    deadline = time.monotonic() + timeout_s
    clients = _get_clients()
    if sources is not None:
        wanted = set(sources)
        clients = {s: c for s, c in clients.items() if s in wanted}
    found = {}  # source -> normalized items, filled in by the worker threads
    found_lock = threading.Lock()
    def search_one(source, client):
        if stop is not None and stop.is_set():
            return
        try:
            # Each MusicClient contains exactly one provider. Calling its
            # provider directly avoids MusicClient.search creating a
            # redundant executor inside this already-background thread.
            provider = getattr(client, "music_clients", {}).get(source)
            if provider is None:
                results = client.search(keyword) or {}
                songs = results.get(source) or []
            else:
                songs = provider.search(
                    keyword=keyword,
                    num_threadings=SOURCE_SEARCH_THREADS,
                    request_overrides=client.requests_overrides[source],
                    rule=client.search_rules[source],
                ) or []
        except Exception:  # noqa: BLE001 - one bad site must not kill the rest
            songs = []
        items = _normalize(source, songs)
        with found_lock:
            found[source] = items
        if on_site is not None and (stop is None or not stop.is_set()):
            try:
                on_site(_short_source(source), items)
            except Exception:  # noqa: BLE001 - a bad callback is not the site's fault
                pass

    threads = []
    for source, client in clients.items():
        thread = threading.Thread(target=search_one, args=(source, client),
                                  name=f"search-{source}", daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    # Snapshot under the lock: a straggler finishing mid-iteration would
    # otherwise change the dict while we walk it.
    with found_lock:
        answered = dict(found)

    items = []
    for source in sorted(answered):
        items.extend(answered[source])
    return (items,
            [_short_source(s) for s in sorted(answered)],
            [_short_source(s) for s in sorted(clients)])


def download(song_info, out_dir):
    """Download one SongInfo through musicdl (handles headers/HLS/etc).

    The song still points at the scratch folder its search ran in, so the
    save path is repointed at the user's download folder first -- musicdl
    derives it from work_dir whenever _save_path is unset.

    No granular progress is exposed by musicdl, so the caller should treat
    this as an indeterminate operation that either returns or raises.
    """
    clients = _get_clients()
    os.makedirs(out_dir, exist_ok=True)
    song_info.work_dir = out_dir
    song_info._save_path = None
    client = clients.get(song_info.source)
    if client is None:
        raise RuntimeError(f"Source not available: {song_info.source}")
    downloaded = client.download([song_info])
    # musicdl drops its bookkeeping next to the audio; the user's music
    # folder should hold music.
    try:
        os.remove(os.path.join(out_dir, "download_results.pkl"))
    except OSError:
        pass
    if not downloaded:
        raise RuntimeError(f"musicdl could not download: {song_info.song_name}")
    return downloaded
