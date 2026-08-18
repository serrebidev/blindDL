# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Optional in-app BitTorrent engine, built on libtorrent.

blindDL hands a chosen torrent to the user's own client by default, which is
what torrent_backend documents. Turning "Download torrents in blindDL" on in
Settings, Torrents swaps that hand-off for this module: the bytes then move
inside blindDL, the Downloads tab shows real progress, and finished files
land in the Library tab with everything else.

libtorrent is the same engine qBittorrent, Deluge and Transmission are built
on, so nothing here is a re-implementation of BitTorrent -- it is a session
configured to behave the way a well-mannered client does.

Client identity
---------------
A BitTorrent client tells the swarm who it is twice: a peer ID prefix in the
peer handshake, and a User-Agent on tracker announces. Left alone, libtorrent
announces itself as libtorrent, and a fair number of trackers only admit
clients from a list they maintain -- a raw libtorrent build is often not on
it, and a stale version of a listed client is often refused as well.

So the session identifies as the current qBittorrent release, which is on
every such list: peer ID `-qBXYZ0-` and User-Agent `qBittorrent/X.Y.Z`. That
is not a disguise for something else -- qBittorrent *is* libtorrent, this
session speaks the same protocol with the same library, and the version is
looked up from qBittorrent's own releases so it stays current instead of
drifting into the "too old" bucket. The user can pin a version by hand in
Settings if a tracker wants a particular one.

Nothing in here talks to an indexer; searching stays in torrent_backend.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

from . import torrent_backend
from .config import app_data_dir

# The package that provides the `libtorrent` module. It ships binary wheels,
# so there is no compiler involved -- but only for the Python versions it has
# built wheels for, which is what makes the install fail on a brand-new one.
LIBTORRENT_PACKAGE = "libtorrent"

# Used when qBittorrent's release feed cannot be reached (offline, or GitHub
# rate-limiting an unauthenticated request). Kept current with releases.
QBITTORRENT_FALLBACK_VERSION = "5.2.3"
QBITTORRENT_RELEASES_URL = (
    "https://api.github.com/repos/qbittorrent/qBittorrent/releases/latest")
# How long a looked-up qBittorrent version is trusted before asking again.
VERSION_CHECK_SECONDS = 24 * 3600
VERSION_FETCH_TIMEOUT_S = 10

# Enum values from libtorrent's settings_pack. Hard-coded rather than read off
# the module because the Python bindings do not expose settings_pack itself.
_ENC_FORCED = 0
_ENC_ENABLED = 1
_ENC_DISABLED = 2
_ENC_LEVEL_BOTH = 3
_PROXY_NONE = 0
_PROXY_SOCKS5 = 2
_PROXY_SOCKS5_PW = 3
_PROXY_HTTP = 4
_PROXY_HTTP_PW = 5

# error | storage | tracker | status. Enough to save resume data and to see a
# torrent fail; the per-piece and per-peer categories would flood the queue.
_ALERT_MASK = 1 | 8 | 16 | 64

# A ratio or time limit of 0 means "seed until blindDL exits". libtorrent has
# no such value, so its own limits are pushed out of reach and the maintenance
# thread applies the user's limits instead.
_NO_LIMIT = 24 * 365 * 3600

POLL_SECONDS = 0.5
MAINTENANCE_SECONDS = 1.0
RESUME_SAVE_SECONDS = 30.0

ENCRYPTION_CHOICES = [
    ("Prefer encrypted connections", "prefer"),
    ("Require encrypted connections", "require"),
    ("Do not encrypt", "off"),
]


class TorrentEngineError(RuntimeError):
    """The engine could not start, or a torrent failed outright."""


class TorrentDownloadCancelled(Exception):
    """Raised when the user cancels a torrent that is still transferring."""


# -- libtorrent availability -------------------------------------------------


_lt = None
_lt_error = ""


def libtorrent_module():
    """Import libtorrent on first use, or explain why it is not there.

    The import is deferred so a blindDL without libtorrent installed starts
    and runs normally; only the torrent engine is unavailable, and only when
    the setting asks for it.
    """
    global _lt, _lt_error
    if _lt is not None:
        return _lt
    try:
        import libtorrent  # noqa: PLC0415 - deferred on purpose
    except ImportError as exc:
        _lt_error = str(exc)
        raise TorrentEngineError(install_hint()) from exc
    _lt = libtorrent
    return _lt


def available():
    """Whether the engine can run right now."""
    try:
        libtorrent_module()
    except TorrentEngineError:
        return False
    return True


def version():
    """libtorrent's own version string, or "" when it is not installed."""
    try:
        return str(libtorrent_module().__version__)
    except (TorrentEngineError, AttributeError):
        return ""


def install_hint():
    """What to tell the user when libtorrent will not import."""
    if getattr(sys, "frozen", False):
        return (
            "This blindDL installation is missing its built-in libtorrent "
            "engine. Reinstall or update blindDL; Python and pip are not "
            "required. Until then, turn this setting off to open torrents "
            "in your usual BitTorrent client."
        )
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        "The libtorrent package is not installed, so blindDL cannot download "
        "torrents itself. Help, Check for updates installs it. If the "
        f"install fails, libtorrent publishes no build for Python {python} "
        "yet; run blindDL on an earlier Python, or leave the setting off and "
        "torrents will open in your own BitTorrent client as before.")


def install(log=lambda _line: None):
    """pip install libtorrent. Returns True when the module imports after."""
    global _lt, _lt_error
    if available():
        return True
    if getattr(sys, "frozen", False):
        log("libtorrent ships with blindDL releases; update blindDL instead.")
        return False
    command = [sys.executable, "-m", "pip", "install", "--upgrade",
               LIBTORRENT_PACKAGE]
    log("$ " + " ".join(command))
    options = {}
    if os.name == "nt":
        options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=900, encoding="utf-8", errors="replace",
                              **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  failed to run pip: {exc}")
        return False
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines()[-10:]:
        log("  " + line)
    _lt = None
    _lt_error = ""
    if proc.returncode != 0:
        log(install_hint())
        return False
    return available()


# -- the version blindDL reports to trackers ---------------------------------


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(text):
    """(major, minor, patch) out of "release-5.2.3", or None."""
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _fetch_latest_qbittorrent():
    """Ask GitHub for qBittorrent's newest release tag. None on any failure."""
    request = Request(QBITTORRENT_RELEASES_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "blindDL",
    })
    try:
        # The URL is a module constant using HTTPS, never user-controlled.
        with urlopen(  # nosec B310
            request, timeout=VERSION_FETCH_TIMEOUT_S
        ) as response:
            payload = json.load(response)
    except Exception:  # noqa: BLE001 - any network/parse failure is the same
        return None
    return _parse_version(payload.get("tag_name"))


def client_version(config, allow_network=True):
    """The qBittorrent version this session claims to be, as (x, y, z).

    A version typed into Settings wins. Otherwise the newest release is looked
    up once a day and remembered in the config, so an offline start still gets
    a recent version rather than a stale built-in one.
    """
    pinned = _parse_version(config.get("torrent_client_version", ""))
    if pinned:
        return pinned

    fallback = _parse_version(QBITTORRENT_FALLBACK_VERSION) or (5, 2, 3)
    cached = _parse_version(config.get("torrent_client_version_cache", ""))
    checked = float(config.get("torrent_client_version_checked", 0) or 0)
    fresh = cached and (time.time() - checked) < VERSION_CHECK_SECONDS
    if fresh or not allow_network:
        return cached or fallback

    latest = _fetch_latest_qbittorrent()
    if latest:
        config["torrent_client_version_cache"] = "%d.%d.%d" % latest
        config["torrent_client_version_checked"] = time.time()
        config.save()
        return latest
    return cached or fallback


def client_identity(config, allow_network=True):
    """(peer_fingerprint, user_agent) for the configured client version."""
    major, minor, patch = client_version(config, allow_network=allow_network)
    lt = libtorrent_module()
    # qBittorrent's own peer ID: "qB" plus its four version digits, e.g.
    # -qB5230- for 5.2.3. The trailing 0 is the build slot qBittorrent
    # leaves empty.
    fingerprint = lt.generate_fingerprint("qB", major, minor, patch, 0)
    return fingerprint, f"qBittorrent/{major}.{minor}.{patch}"


# -- settings ----------------------------------------------------------------


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_PROXY_RE = re.compile(
    r"^(?:(?P<scheme>socks5|socks5h|http|https)://)?"
    r"(?:(?P<user>[^:@/]+)(?::(?P<password>[^@/]*))?@)?"
    r"(?P<host>[^:@/]+)(?::(?P<port>\d+))?/?$", re.IGNORECASE)


def parse_proxy(text):
    """Split a proxy string into libtorrent settings, or {} when unset.

    Accepts "host:port", "socks5://host:port" and
    "socks5://user:password@host:port". A proxy typed here carries peer
    traffic as well as tracker traffic, which is the only arrangement worth
    having: a proxy that covered announces alone would still expose the
    machine to every peer in the swarm.
    """
    text = str(text or "").strip()
    if not text:
        return {}
    match = _PROXY_RE.match(text)
    if not match:
        raise TorrentEngineError(
            "The torrent proxy must look like socks5://host:port, or "
            "socks5://user:password@host:port.")
    scheme = (match.group("scheme") or "socks5").lower()
    user = match.group("user") or ""
    password = match.group("password") or ""
    http = scheme in ("http", "https")
    if http:
        proxy_type = _PROXY_HTTP_PW if user else _PROXY_HTTP
    else:
        proxy_type = _PROXY_SOCKS5_PW if user else _PROXY_SOCKS5
    return {
        "proxy_type": proxy_type,
        "proxy_hostname": match.group("host"),
        "proxy_port": _int(match.group("port"), 8080 if http else 1080),
        "proxy_username": user,
        "proxy_password": password,
        # Route peer connections and DNS through the proxy too, so the real
        # address does not leak around it.
        "proxy_peer_connections": True,
        "proxy_hostnames": True,
    }


def session_settings(config, allow_network=True):
    """The libtorrent settings dict for the user's current preferences."""
    fingerprint, user_agent = client_identity(
        config, allow_network=allow_network)
    port = _int(config.get("torrent_port"), 0)
    swarm = bool(config.get("torrent_dht", True))
    encryption = str(config.get("torrent_encryption", "prefer")).lower()
    if encryption == "require":
        policy = _ENC_FORCED
    elif encryption == "off":
        policy = _ENC_DISABLED
    else:
        policy = _ENC_ENABLED

    settings = {
        "user_agent": user_agent,
        "peer_fingerprint": fingerprint,
        "alert_mask": _ALERT_MASK,
        "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
        "enable_dht": swarm,
        "enable_lsd": swarm,
        "enable_upnp": bool(config.get("torrent_port_forward", True)),
        "enable_natpmp": bool(config.get("torrent_port_forward", True)),
        "out_enc_policy": policy,
        "in_enc_policy": policy,
        "allowed_enc_level": _ENC_LEVEL_BOTH,
        # Stored in KiB/s because that is the unit every torrent client's
        # speed limit uses; libtorrent wants bytes. 0 stays 0 (unlimited).
        "download_rate_limit": max(0, _int(
            config.get("torrent_max_down_kib"), 0)) * 1024,
        "upload_rate_limit": max(0, _int(
            config.get("torrent_max_up_kib"), 0)) * 1024,
        "active_downloads": max(1, _int(config.get("torrent_max_active"), 3)),
        "active_seeds": max(1, _int(config.get("torrent_max_active"), 3)) * 2,
        "active_limit": max(2, _int(config.get("torrent_max_active"), 3) * 4),
        "connections_limit": max(10, _int(
            config.get("torrent_max_connections"), 500)),
        # Seeding is stopped by the maintenance thread, which knows what 0
        # means. libtorrent's own limits are pushed out of the way.
        "share_ratio_limit": 1000000,
        "seed_time_limit": _NO_LIMIT,
        "proxy_type": _PROXY_NONE,
        "proxy_hostname": "",
        "proxy_port": 0,
        "proxy_username": "",
        "proxy_password": "",
    }
    settings.update(parse_proxy(config.get("torrent_proxy", "")))
    return settings


# -- the session -------------------------------------------------------------


def _resume_dir():
    path = os.path.join(app_data_dir(), "torrents")
    os.makedirs(path, exist_ok=True)
    return path


def _key_for(atp):
    """The info hash a torrent is filed under, v1 preferred, v2 otherwise."""
    hashes = getattr(atp, "info_hashes", None)
    if hashes is not None:
        for attribute in ("v1", "v2"):
            value = str(getattr(hashes, attribute, "") or "")
            if value and set(value) != {"0"}:
                return value.lower()
    return str(getattr(atp, "info_hash", "") or "").lower()


class _Torrent:
    """One torrent the engine is looking after."""

    def __init__(self, key, handle, title, save_path=""):
        self.key = key
        self.handle = handle
        self.title = title
        self.save_path = os.fspath(save_path)
        # Set once the payload is complete; seeding limits run from it.
        self.finished_at = 0.0
        self.stopping = False


class TorrentEngine:
    """One libtorrent session shared by every torrent blindDL downloads.

    A single session is what libtorrent is built for: one listen port, one
    DHT node, one set of rate limits across every torrent, which is also the
    only way a global speed limit can mean anything.
    """

    def __init__(self, config):
        lt = libtorrent_module()
        self._lt = lt
        self._lock = threading.RLock()
        self._torrents = {}
        self._uploads_cache = []
        self._settings = session_settings(config)
        self.session = lt.session(dict(self._settings))
        self._stop = threading.Event()
        self._last_resume_save = 0.0
        self._seed_ratio = 0.0
        self._seed_minutes = 0
        self.set_seed_limits(config)
        self._thread = threading.Thread(
            target=self._maintain, daemon=True, name="blinddl-torrents")
        self._thread.start()

    # -- settings ----------------------------------------------------------

    def apply_config(self, config):
        """Push changed settings into the running session.

        The listen port is the one thing that cannot change under a live
        session without dropping every connection, so it is only applied when
        the user actually changed it.
        """
        wanted = session_settings(config, allow_network=False)
        with self._lock:
            changed = {key: value for key, value in wanted.items()
                       if self._settings.get(key) != value}
            if not changed:
                return
            self._settings.update(changed)
            self.session.apply_settings(changed)

    # -- adding and removing -----------------------------------------------

    def add(self, item, save_path, config):
        """Add one search result to the session and return its _Torrent."""
        os.makedirs(save_path, exist_ok=True)
        atp = self._params_for(item, save_path, config)
        key = _key_for(atp)
        with self._lock:
            existing = self._torrents.get(key)
            if existing is not None:
                return existing
            resume = self._read_resume(key)
            if resume is not None:
                # Resume data carries the pieces already on disk, which is
                # what saves a full re-check of a part-finished download.
                resume.save_path = save_path
                if getattr(atp, "ti", None) is not None:
                    resume.ti = atp.ti
                resume.flags = atp.flags
                atp = resume
            try:
                handle = self.session.add_torrent(atp)
            except RuntimeError as exc:
                raise TorrentEngineError(
                    f"libtorrent refused that torrent: {exc}") from exc
            torrent = _Torrent(
                key, handle, str(item.get("title") or "Torrent"), save_path
            )
            self._torrents[key] = torrent
        return torrent

    def _params_for(self, item, save_path, config):
        lt = self._lt
        # Sources that publish the hash only on the book's own page (eBookelo,
        # Audiobook Bay) resolve it here, at download time.
        magnet = torrent_backend.resolve_magnet(item)
        if magnet:
            atp = lt.parse_magnet_uri(magnet)
        elif item.get("download_url") or item.get("torrent_path"):
            # Some rows carry a .torrent rather than a magnet: a private
            # tracker's is authenticated, its announce URL carrying the
            # passkey, and an Archive item's carries the webseed that makes
            # it fetchable with no peers. Either way the file itself is the
            # thing that works, so it is fetched and handed over whole.
            path = torrent_backend.fetch_torrent_file(item, _resume_dir())
            atp = lt.add_torrent_params()
            try:
                ti = lt.torrent_info(path)
                atp.ti = ti
                # add_torrent_params keeps its info hashes separate from the
                # torrent_info; the session fills them in only after
                # add_torrent, but _key_for needs them before that to file
                # the torrent (and its resume data) under its real hash.
                atp.info_hashes = ti.info_hashes()
            except RuntimeError as exc:
                raise TorrentEngineError(
                    f"That tracker's torrent file could not be read: {exc}"
                ) from exc
        else:
            raise TorrentEngineError(
                "That result carries no magnet link or info hash.")

        atp.save_path = save_path
        flags = atp.flags
        if config.get("torrent_sequential"):
            # Pieces in order: the file is playable while it downloads, which
            # is the difference between waiting and listening.
            flags |= lt.torrent_flags.sequential_download
        if not config.get("torrent_dht", True):
            # A user who turned the public swarm off means it for every
            # torrent, not just the ones a tracker marked private.
            flags |= (lt.torrent_flags.disable_dht |
                      lt.torrent_flags.disable_lsd |
                      lt.torrent_flags.disable_pex)
        atp.flags = flags
        return atp

    def remove(self, torrent, delete_files=False):
        """Take a torrent out of the session, optionally deleting its data."""
        lt = self._lt
        with self._lock:
            self._torrents.pop(torrent.key, None)
            self._uploads_cache = [
                row for row in self._uploads_cache if row["key"] != torrent.key
            ]
            torrent.stopping = True
        try:
            if delete_files:
                self.session.remove_torrent(
                    torrent.handle, lt.session.delete_files)
            else:
                self.session.remove_torrent(torrent.handle)
        except (RuntimeError, ValueError):
            pass
        if delete_files:
            self._forget_resume(torrent.key)

    def stop_seeding(self, key):
        """Stop seeding one finished torrent. True when one was seeding."""
        with self._lock:
            torrent = self._torrents.get(str(key or "").lower())
        if torrent is None:
            return False
        self.remove(torrent)
        return True

    def pause_seeding(self, key):
        with self._lock:
            torrent = self._torrents.get(str(key or "").lower())
        if torrent is None:
            return False
        lt = self._lt
        try:
            # libtorrent 2.1's pause() no longer clears auto_managed, so the
            # queue manager starts the seed again a moment later. Take it out
            # of auto-management and pause it in one step.
            torrent.handle.set_flags(
                lt.torrent_flags.paused,
                lt.torrent_flags.paused | lt.torrent_flags.auto_managed,
            )
        except RuntimeError:
            return False
        return True

    def resume_seeding(self, key):
        with self._lock:
            torrent = self._torrents.get(str(key or "").lower())
        if torrent is None:
            return False
        lt = self._lt
        try:
            torrent.handle.set_flags(
                lt.torrent_flags.auto_managed,
                lt.torrent_flags.paused | lt.torrent_flags.auto_managed,
            )
        except RuntimeError:
            return False
        return True

    def delete_seed(self, key):
        """Stop one seed and ask libtorrent to delete its payload files."""
        with self._lock:
            torrent = self._torrents.get(str(key or "").lower())
        if torrent is None:
            return False
        self.remove(torrent, delete_files=True)
        return True

    def seeding(self):
        """[(key, title, ratio, upload_rate)] for everything still seeding."""
        with self._lock:
            torrents = list(self._torrents.values())
        rows = []
        for torrent in torrents:
            if not torrent.finished_at:
                continue
            try:
                status = torrent.handle.status()
            except RuntimeError:
                continue
            rows.append((torrent.key, torrent.title, _ratio(status),
                         status.upload_rate))
        return rows

    def uploads(self):
        """Cached snapshots for the Uploads tab, with no libtorrent call."""
        with self._lock:
            return [dict(row) for row in self._uploads_cache]

    def _seed_statuses(self):
        """Read native statuses on the engine's maintenance thread."""
        with self._lock:
            torrents = list(self._torrents.values())
        statuses = []
        for torrent in torrents:
            if torrent.stopping or not torrent.finished_at:
                continue
            try:
                status = torrent.handle.status()
            except RuntimeError:
                continue
            statuses.append((torrent, status))
        return statuses

    def _cache_uploads(self, statuses):
        rows = []
        for torrent, status in statuses:
            ratio = _ratio(status)
            peers = max(0, int(getattr(status, "num_peers", 0) or 0))
            paused = _status_paused(status, self._lt)
            rows.append(
                {
                    "key": torrent.key,
                    "title": torrent.title,
                    "service": "BitTorrent",
                    "peer": f"{peers} peer" if peers == 1 else f"{peers} peers",
                    "status": "Paused" if paused else "Seeding",
                    "uploaded": int(getattr(status, "all_time_upload", 0) or 0),
                    "total": int(getattr(status, "total_wanted", 0) or 0),
                    "ratio": ratio,
                    "speed": float(getattr(status, "upload_rate", 0) or 0),
                    "active": True,
                    "paused": paused,
                    "path": torrent.save_path,
                    "error": "",
                    "started_at": torrent.finished_at,
                    "completed_at": None,
                }
            )
        with self._lock:
            self._uploads_cache = rows

    # -- background upkeep -------------------------------------------------

    def _maintain(self):
        while not self._stop.is_set():
            try:
                self._drain_alerts()
                statuses = self._seed_statuses()
                self._cache_uploads(statuses)
                self._enforce_seed_limits(statuses)
                self._maybe_save_resume()
            except Exception:  # noqa: BLE001 - a background thread must live
                pass
            self._stop.wait(MAINTENANCE_SECONDS)

    def _drain_alerts(self):
        lt = self._lt
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                self._write_resume(alert)

    def _enforce_seed_limits(self, statuses=None):
        """Drop torrents that have met the user's ratio or time limit."""
        with self._lock:
            ratio_limit = self._seed_ratio
            minute_limit = self._seed_minutes
        if statuses is None:
            statuses = self._seed_statuses()
        now = time.time()
        for torrent, status in statuses:
            if ratio_limit > 0 and _ratio(status) >= ratio_limit:
                self.remove(torrent)
            elif (minute_limit > 0 and
                    now - torrent.finished_at >= minute_limit * 60):
                self.remove(torrent)

    def set_seed_limits(self, config):
        """Copy the seeding limits in, so the upkeep thread holds no config."""
        with self._lock:
            self._seed_ratio = max(0.0, _float(
                config.get("torrent_seed_ratio"), 0.0))
            self._seed_minutes = max(0, _int(
                config.get("torrent_seed_minutes"), 0))

    def _maybe_save_resume(self):
        now = time.monotonic()
        if now - self._last_resume_save < RESUME_SAVE_SECONDS:
            return
        self._last_resume_save = now
        self.request_resume_save()

    def request_resume_save(self):
        """Ask libtorrent for resume data for everything worth saving."""
        with self._lock:
            torrents = list(self._torrents.values())
        for torrent in torrents:
            try:
                if torrent.handle.status().need_save_resume:
                    torrent.handle.save_resume_data()
            except RuntimeError:
                continue

    def _resume_path(self, key):
        return os.path.join(_resume_dir(), f"{key}.fastresume")

    def _write_resume(self, alert):
        lt = self._lt
        try:
            key = _key_for(alert.params)
            data = lt.write_resume_data_buf(alert.params)
        except Exception:  # noqa: BLE001 - never lose the session over this
            return
        if not key:
            return
        try:
            with open(self._resume_path(key), "wb") as handle:
                handle.write(data)
        except OSError:
            pass

    def _read_resume(self, key):
        lt = self._lt
        try:
            with open(self._resume_path(key), "rb") as handle:
                return lt.read_resume_data(handle.read())
        except (OSError, RuntimeError):
            return None

    def _forget_resume(self, key):
        try:
            os.remove(self._resume_path(key))
        except OSError:
            pass

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, timeout=5.0):
        """Stop cleanly: save resume data, then let the session go.

        Seeding stops when blindDL exits, so this is also where a torrent's
        progress is written down -- without it, a part-finished download would
        be re-checked piece by piece on the next start.
        """
        self._stop.set()
        try:
            self.request_resume_save()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                alerts = self.session.pop_alerts()
            except Exception:  # noqa: BLE001
                break
            for alert in alerts:
                try:
                    if isinstance(alert, self._lt.save_resume_data_alert):
                        self._write_resume(alert)
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                pending = any(
                    _needs_resume(torrent) for torrent in self._torrents.values())
            if not pending:
                break
            time.sleep(0.1)
        with self._lock:
            self._torrents.clear()
            self._uploads_cache = []


def _needs_resume(torrent):
    try:
        return torrent.handle.status().need_save_resume
    except RuntimeError:
        return False


def _ratio(status):
    """Share ratio, measured against what the torrent actually is."""
    downloaded = max(status.all_time_download, status.total_wanted, 1)
    return status.all_time_upload / float(downloaded)


def _status_paused(status, lt):
    """Whether a torrent_status is paused, across libtorrent 2.0 and 2.1.

    libtorrent 2.1 dropped the dedicated ``paused`` field from
    torrent_status; the pause state now lives in the torrent's flags.
    """
    flags = getattr(status, "flags", None)
    if flags is not None:
        return bool(int(flags) & int(lt.torrent_flags.paused))
    return bool(getattr(status, "paused", False))


# -- module-level engine -----------------------------------------------------


_engine_lock = threading.Lock()
_engine = None


def engine(config):
    """The shared session, started on first use and kept in step with config."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = TorrentEngine(config)
        else:
            _engine.apply_config(config)
        _engine.set_seed_limits(config)
        return _engine


def running():
    """Whether a session exists, without starting one."""
    return _engine is not None


def shutdown():
    """Close the session, if one was ever started."""
    global _engine
    with _engine_lock:
        if _engine is None:
            return
        try:
            _engine.shutdown()
        finally:
            _engine = None


def stop_seeding(key):
    """Stop seeding one torrent by info hash. False when it was not seeding."""
    with _engine_lock:
        current = _engine
    if current is None:
        return False
    return current.stop_seeding(key)


def pause_seeding(key):
    with _engine_lock:
        current = _engine
    return current.pause_seeding(key) if current is not None else False


def resume_seeding(key):
    with _engine_lock:
        current = _engine
    return current.resume_seeding(key) if current is not None else False


def delete_seed(key):
    with _engine_lock:
        current = _engine
    return current.delete_seed(key) if current is not None else False


def seeding():
    with _engine_lock:
        current = _engine
    return current.seeding() if current is not None else []


def uploads():
    """Current torrent seeds without starting the optional engine."""
    with _engine_lock:
        current = _engine
    return current.uploads() if current is not None else []


# -- downloading one torrent -------------------------------------------------


def save_path_for(config, fallback_dir):
    """Where torrents land: their own folder when set, downloads otherwise."""
    path = str(config.get("torrent_dir") or "").strip()
    return path or fallback_dir


def download(item, out_dir, config, progress_cb=None, cancel_event=None,
             keep_partial_event=None):
    """Download one torrent, blocking until its files are complete.

    Returns the folder the data was saved in. Seeding carries on afterwards
    inside the session, under the ratio and time limits from Settings, so the
    swarm is not abandoned the moment the last piece arrives.
    """
    current = engine(config)
    save_path = save_path_for(config, out_dir)
    torrent = current.add(item, save_path, config)
    delete_partial = bool(config.get("torrent_delete_partial", True))

    def report(**fields):
        if progress_cb is not None:
            progress_cb(fields)

    while True:
        if cancel_event is not None and cancel_event.is_set():
            keep_partial = (
                keep_partial_event is not None and keep_partial_event.is_set()
            )
            current.remove(
                torrent, delete_files=delete_partial and not keep_partial
            )
            raise TorrentDownloadCancelled()
        try:
            status = torrent.handle.status()
        except RuntimeError as exc:
            raise TorrentEngineError(f"That torrent was dropped: {exc}") from exc

        message = _status_error(status)
        if message:
            current.remove(torrent, delete_files=delete_partial)
            raise TorrentEngineError(message)

        report(**_progress(status))
        if status.is_finished or status.is_seeding:
            break
        time.sleep(POLL_SECONDS)

    torrent.finished_at = time.time()
    current.request_resume_save()
    return save_path


def _status_error(status):
    """A human-readable failure for a torrent, or "" while it is healthy."""
    errc = getattr(status, "errc", None)
    if errc is not None and errc.value():
        return errc.message()
    error = str(getattr(status, "error", "") or "")
    return error


_STATE_LABELS = {
    "checking_files": "Checking existing files",
    "checking_resume_data": "Checking existing files",
    "downloading_metadata": "Fetching torrent details",
    "allocating": "Allocating",
}


def _progress(status):
    """The fields the Downloads tab shows for one torrent."""
    percent = min(100.0, max(0.0, status.progress * 100.0))
    rate = status.download_rate
    remaining = max(status.total_wanted - status.total_wanted_done, 0)
    eta = remaining / rate if rate > 0 and remaining else 0
    state = _STATE_LABELS.get(str(status.state.name), "")
    return {
        "percent": percent,
        "rate": rate,
        "eta": eta,
        "state": state,
        "seeds": status.num_seeds,
        "peers": max(status.num_peers - status.num_seeds, 0),
    }


__all__ = [
    "ENCRYPTION_CHOICES",
    "TorrentDownloadCancelled",
    "TorrentEngineError",
    "available",
    "client_identity",
    "client_version",
    "download",
    "engine",
    "install",
    "install_hint",
    "libtorrent_module",
    "parse_proxy",
    "running",
    "pause_seeding",
    "resume_seeding",
    "delete_seed",
    "save_path_for",
    "seeding",
    "session_settings",
    "shutdown",
    "stop_seeding",
    "version",
]
