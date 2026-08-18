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

import importlib
import logging
import os
import shutil
import sys
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

from . import music_tags  # noqa: E402
from .config import app_data_dir  # noqa: E402

# Sources blindDL searches better itself, so musicdl is not asked for them.
# Its Deezer client hands back Deezer's own stream URL, which is Blowfish
# encrypted: nothing can play it, and its rows collide with the native
# Deezer backend's under the same "Deezer" name, so whichever answered
# first silently hid the other. blindDL searches Deezer through its own
# backend and through Side B in the same music search, both of which
# return something that plays and downloads.
SUPERSEDED_SOURCES = ("DeezerMusicClient",)
ALL_SOURCES = sorted(
    source for source in MusicClientBuilder.REGISTERED_MODULES
    if source not in SUPERSEDED_SOURCES
)

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
# Upper bound on the number of search pages one site is asked for. A source
# that clamps its own page size below SEARCH_SIZE_PER_SOURCE does not answer
# with fewer songs -- it answers with more requests, one page at a time:
# XiaoBai and JBSou each built eighty, and every page then costs a couple of
# further round trips per song it returns. Four pages is far more than a
# result list fifty sites wide has room for.
MAX_SEARCH_PAGES_PER_SOURCE = 4
_lock = threading.Lock()
_clients = None  # dict: source -> single-source MusicClient
_http_timeout_installed = False
_silenced = False
# The stop event of the search running on this thread, so a page can be the
# last one when the user has moved on. musicdl has no cancel token of its own.
_cancel = threading.local()
def cache_dir():
    """Scratch space for musicdl's per-search bookkeeping."""
    path = os.path.join(app_data_dir(), "musicdl-cache")
    os.makedirs(path, exist_ok=True)
    return path


def clear_cache(older_than_s=None):
    """Drop the scratch folders from previous sessions.

    musicdl writes a `<source>/<timestamp> <query>/search_results.pkl` tree
    for every site of every search, and leaves half-finished downloads
    there too, so it grows without bound -- hundreds of megabytes over a
    few weeks of use.

    With *older_than_s* only folders untouched for that long are removed,
    which is what makes this safe to run while blindDL is up: a download
    working in its own scratch folder is not old.
    """
    root = cache_dir()
    if older_than_s is None:
        try:
            shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
        return
    # The tree is <cache>/<source>/<timestamp and query>/, and it is the
    # inner folder that belongs to one search: a source's own folder looks
    # fresh as long as anything under it is being written.
    cutoff = time.time() - older_than_s
    for source_dir in _scandir(root):
        if not source_dir.is_dir():
            continue
        for search_dir in _scandir(source_dir.path):
            try:
                if search_dir.stat().st_mtime >= cutoff:
                    continue
                if search_dir.is_dir():
                    shutil.rmtree(search_dir.path, ignore_errors=True)
                else:
                    os.remove(search_dir.path)
            except OSError:
                continue


def _scandir(path):
    try:
        return list(os.scandir(path))
    except OSError:
        return []


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


class InteractiveVerificationBlocked(RuntimeError):
    """A music source wanted the screen, and blindDL would not give it up."""


def _blocked_verification(*_args, **_kwargs):
    raise InteractiveVerificationBlocked(
        "This source needs browser verification, which blindDL does not run."
    )


def _block_interactive_verification():
    """Stop music sources taking over the screen in the middle of a search.

    Several of musicdl's sources try to verify themselves interactively
    while a search is running: the Deezer and Qobuz "VIP" parsers register a
    spotiflac:// URL handler in the Windows registry, open a browser at an
    approval page, and then wait five minutes for a callback -- once per
    result. Others open a browser for TIDAL and put a message box on top of
    everything, or read an access token from a console blindDL does not
    have.

    None of that can be answered here. A search is something the user
    started with a keystroke, not an invitation to hand the screen to a
    site, and a screen reader user gets a dialog they did not ask for over
    the results they did. Every one of these entry points is made to fail at
    once instead: musicdl tries each parser in turn and suppresses whatever
    they raise, so the site simply moves on to the next way in.
    """
    blocked = (
        ("musicdl.modules.utils.zarz", "capturegrant", None),
        ("musicdl.modules.utils.afkarxyz", "CommunityClientBase",
         "runbrowserverification"),
        ("musicdl.modules.utils.tidalutils", "TidalTvSession", "auth"),
        ("musicdl.modules.utils.youtubeutils", "defaultoauthverifier", None),
        ("musicdl.modules.utils.youtubeutils", "defaultpotokenverifier", None),
    )
    for module_name, name, method in blocked:
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 - an absent source blocks itself
                continue
        target = getattr(module, name, None)
        if target is None:
            continue
        if method is None:
            setattr(module, name, _blocked_verification)
        elif getattr(target, method, None) is not None:
            setattr(target, method, _blocked_verification)


_install_http_timeout()
_silence_musicdl()
_silence_progress_bars()
_block_interactive_verification()


def _reuse_link_test_connections():
    """Stop the per-song link check throwing its connection away each time.

    Every result of every site is checked with a HEAD, and sometimes a GET,
    to find out what the file actually is. musicdl builds a brand new HTTP
    session for each of those checks, so each song pays a fresh TLS
    handshake -- certificate verification and all -- to a host the previous
    song had just finished talking to. A search checks hundreds of songs.

    The session is kept and its cookies dropped instead, which leaves each
    check as stateless as a new session would have been while the
    connection underneath it is reused.
    """
    from musicdl.modules.utils.misc import AudioLinkTester

    original = AudioLinkTester.test
    if getattr(original, "_blinddl_reuses_connections", False):
        return

    def test(self, url, request_overrides=None, renew_session=True):
        if renew_session:
            try:
                self.session.cookies.clear()
            except Exception:  # noqa: BLE001 - a session without cookies
                pass
        return original(self, url, request_overrides=request_overrides,
                        renew_session=False)

    test._blinddl_reuses_connections = True
    AudioLinkTester.test = test


_reuse_link_test_connections()


def remove_stale_url_handler():
    """Delete the spotiflac:// handler a search may have left in Windows.

    The Deezer and Qobuz parsers registered that handler under HKCU before
    opening their approval page, and put it back the way they found it
    afterwards -- unless blindDL closed, or was closed, while a search still
    had one open. What is left points at a blindDL that may no longer be
    there and a temporary file that certainly is not, and Windows keeps
    offering it. Nothing registers it any more, so the leftovers go.

    Only a key whose command names musicdl's own helper is touched.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg  # noqa: PLC0415 - Windows-only standard library
    except ImportError:
        return False
    root = r"Software\Classes\spotiflac"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            root + r"\shell\open\command") as key:
            command, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return False
    if "zarz.py" not in str(command).lower():
        return False
    for subkey in (root + r"\shell\open\command", root + r"\shell\open",
                   root + r"\shell", root):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        except OSError:
            return False
    return True


def _cap_search_pages(provider):
    """Bound how many search pages one musicdl source requests."""
    original = provider._constructsearchurls
    if getattr(original, "_blinddl_capped", False):
        return

    def capped(*args, **kwargs):
        return list(original(*args, **kwargs))[:MAX_SEARCH_PAGES_PER_SOURCE]

    capped._blinddl_capped = True
    provider._constructsearchurls = capped


def _make_cancellable(provider):
    """Let a superseded search stop between pages instead of finishing it.

    musicdl walks a source's pages with no way to interrupt them, so a
    search the user had already replaced kept fetching and parsing every
    page that was left -- fifty sites at a time, while the search they were
    waiting for competed with it for the processor.
    """
    original = provider._search
    if getattr(original, "_blinddl_cancellable", False):
        return

    def cancellable(*args, **kwargs):
        stop = getattr(_cancel, "stop", None)
        if stop is not None and stop.is_set():
            return None
        return original(*args, **kwargs)

    cancellable._blinddl_cancellable = True
    provider._search = cancellable


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
                            # Without this, a source throws its HTTP session
                            # away and builds three new ones -- its own and
                            # one for each of its two link testers -- before
                            # every single request it makes. Each of those
                            # reloads the whole certificate bundle and opens
                            # a new TLS connection to a host it was already
                            # talking to: nearly two thousand certificate
                            # loads in one search, seconds of processor time
                            # spent on nothing. Four sources already turn
                            # this on for themselves.
                            "maintain_session": True,
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
                provider = getattr(
                    clients[source], "music_clients", {}).get(source)
                if provider is not None:
                    _cap_search_pages(provider)
                    _make_cancellable(provider)
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
        # Published for the page loop inside musicdl, which otherwise runs
        # every page it lined up whether or not anyone still wants them.
        _cancel.stop = stop
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

    # Daemon threads, deliberately: a ThreadPoolExecutor keeps the
    # interpreter alive until everything it was handed has finished, so
    # closing blindDL while a slow site was still answering would hang the
    # exit for as long as that site took. What keeps a replaced search from
    # piling up is the stop event its pages now check, not the pool shape.
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


def download(song_info, out_dir, online_lookup=True):
    """Download one SongInfo through musicdl (handles headers/HLS/etc).

    The song still points at the scratch folder its search ran in, so the
    save path is repointed at the user's download folder first -- musicdl
    derives it from work_dir whenever _save_path is unset.

    No granular progress is exposed by musicdl, so the caller should treat
    this as an indeterminate operation that either returns or raises.

    musicdl writes three tags of its own -- title, album, artist -- and
    stops there, which leaves a file no library can file: no album artist to
    group it under, no track number to order it by, no year, no artwork.
    Every finished file is therefore tagged from the search result it came
    from, and, unless *online_lookup* is off, from what MusicBrainz and
    TheAudioDB can add on top. That step is deliberately last and cannot
    fail the download.
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
    for done in downloaded:
        music_tags.tag_download(
            str(getattr(done, "save_path", "") or ""), done,
            online=online_lookup)
    return downloaded
