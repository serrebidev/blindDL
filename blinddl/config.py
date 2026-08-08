# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Persistent configuration stored in the platform's application-data path."""

import copy
import json
import os

from platformdirs import user_config_dir

from . import APP_NAME


def app_data_dir():
    path = user_config_dir(APP_NAME, appauthor=False, roaming=True)
    os.makedirs(path, exist_ok=True)
    return path


# Saved configs carry every key, so a changed default would never reach a
# user who has run blindDL before. This is how one is handed on anyway.
CONFIG_VERSION = 1

DEFAULTS = {
    # Where finished downloads go.
    "download_dir": os.path.join(os.path.expanduser("~"), "Music", APP_NAME),
    # Prefer audio-only extraction (via ffmpeg) over full video.
    "audio_only": True,
    # Audio container/codec used when audio_only is on: mp3, m4a, flac, wav,
    # opus... "original" keeps whatever the site already serves, so nothing is
    # re-encoded and no quality is lost.
    "audio_format": "mp3",
    # Container used when a full video is downloaded: mp4, mkv, avi, x265
    # (re-encoded small for long-term storage), or "original" to keep
    # whatever container the streams come in.
    "video_format": "mp4",
    # Maximum simultaneous downloads. No hard cap beyond this user setting.
    "max_concurrent": 4,
    # Seconds a music search waits for each site before giving up on it.
    # Sites are searched in parallel, so this is roughly how long a music
    # search takes. Raise it to reach slower sites.
    "search_timeout_s": 5,
    # Music sites the user switched off, by musicdl source name. Empty means
    # search everything -- including sites added by future musicdl updates,
    # which is why the off list is stored rather than the on list.
    "disabled_music_sources": [],
    # Adult sites the user switched off. These are separate from music
    # sources so the two provider lists can be configured independently.
    "disabled_adult_sources": [],
    # Book libraries the user switched off, by book_backend source name.
    "disabled_book_sources": [],
    # Audiobook sites the user switched off, by audiobook_backend source name.
    "disabled_audiobook_sources": [],
    # Internet Archive media collections the user switched off, by
    # archive_backend category name.
    "disabled_archive_sources": [],
    # Torrent indexers the user switched off, by torrent_backend source name.
    "disabled_torrent_sources": [],
    # Move torrent bytes inside blindDL (libtorrent) instead of handing the
    # magnet to the BitTorrent client the user already has. Off by default:
    # a user with qBittorrent or Deluge set up already has somewhere for
    # torrents to go, and blindDL should not quietly take that over.
    "torrent_engine": False,
    # Where the built-in engine saves torrents. Empty means the ordinary
    # download folder; torrents earn their own setting because they keep
    # seeding after they finish, and that is often a different disk.
    "torrent_dir": "",
    # The qBittorrent version blindDL reports to trackers and peers, as
    # "5.2.3". Empty means the newest release, looked up once a day -- which
    # is what keeps blindDL off the "client too old" list some trackers keep.
    "torrent_client_version": "",
    # Last looked-up qBittorrent release, and when it was looked up. These
    # are a cache, not preferences.
    "torrent_client_version_cache": "",
    "torrent_client_version_checked": 0,
    # Swarm speed limits in KiB per second. 0 means unlimited.
    "torrent_max_down_kib": 0,
    "torrent_max_up_kib": 0,
    # How many torrents transfer at once. The rest queue inside the engine.
    "torrent_max_active": 3,
    # Peer connections across all torrents.
    "torrent_max_connections": 500,
    # Incoming port. 0 picks a random one, which is what a client does when
    # it has no reason to prefer a fixed port.
    "torrent_port": 0,
    # Ask the router to forward that port (UPnP and NAT-PMP). Without it,
    # only peers blindDL connects to first can be reached.
    "torrent_port_forward": True,
    # DHT, local peer discovery and peer exchange: the ways a swarm is found
    # without a tracker. Private trackers switch these off per torrent on
    # their own, whatever this says.
    "torrent_dht": True,
    # "prefer", "require" or "off". Encryption hides the protocol from an
    # ISP shaping BitTorrent; requiring it drops peers that cannot do it.
    "torrent_encryption": "prefer",
    # Take pieces in order, so a file can be played before it finishes.
    # Slower overall, which is why it is off unless asked for.
    "torrent_sequential": False,
    # Stop seeding at this share ratio. 0 seeds until blindDL exits.
    "torrent_seed_ratio": 2.0,
    # Stop seeding this many minutes after the download finishes. 0 = no
    # time limit.
    "torrent_seed_minutes": 0,
    # Proxy for swarm traffic: "socks5://host:port", optionally with
    # user:password. Empty means a direct connection.
    "torrent_proxy": "",
    # Delete the partial files when a torrent is cancelled. Off keeps them,
    # so re-queueing the same torrent picks up where it stopped.
    "torrent_delete_partial": True,
    # The user's own indexer feeds, each {"name", "url", "api_key"}. One
    # entry can be a whole Prowlarr or Jackett instance, which is how private
    # trackers are reached: that tool already holds the login and the passkey,
    # so blindDL never stores a tracker password of its own.
    "torznab_feeds": [],
    # Anna's Archive membership key. Empty means downloads are resolved
    # through the public LibGen mirrors instead of the fast partner servers.
    "annas_archive_key": "",
    # Master privacy/content switch. Adult integrations are hidden until the
    # user explicitly enables them in Settings.
    "adult_sites_enabled": False,
    # Optional browser profile whose cookies yt-dlp may read for sites the
    # user is already signed into. Empty means no browser-cookie access.
    "cookies_from_browser": "",
    # Paths only: session secrets remain in user-controlled JSON files rather
    # than being copied into blindDL's config.
    "onlyfans_auth_file": "",
    "justforfans_auth_file": "",
    # Deezer ARL cookie: unlocks native FLAC/MP3 320 downloads and Deezer's
    # word-level (karaoke) lyrics. Empty = Side B audio and LRCLIB lyrics.
    "deezer_arl": "",
    # Embed synced lyrics into Side B (Deezer) downloads.
    "sideb_lyrics": True,
    # Closing the window hides blindDL in the system tray instead of exiting,
    # so queued downloads, seeding torrents and subscription checks keep
    # running. File > Exit, and the tray's own Exit, always exit for real.
    "minimize_to_tray": True,
    # Minimizing the window puts it in the tray as well, rather than on the
    # taskbar. Both are on by default and either can be switched off.
    "tray_on_minimize": True,
    # How often subscriptions are checked for new items, in hours.
    "sub_check_hours": 6,
    # Automatically update yt-dlp, musicdl, wxPython, Deno and ffmpeg.
    "auto_update": True,
    # Minimum hours between automatic update checks.
    "update_check_hours": 24,
    # Timestamp (unix) of the last automatic update check; 0 = never.
    "last_update_check": 0,
    # Bumped when a default changes in a way an existing config should follow.
    # See _migrate below.
    "config_version": CONFIG_VERSION,
}


class Config:
    def __init__(self):
        self.path = os.path.join(app_data_dir(), "config.json")
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for key, value in saved.items():
            if key in self.data:
                self.data[key] = value
        self._migrate(int(saved.get("config_version", 0) or 0))

    def _migrate(self, from_version):
        """Carry changed defaults into a config written by an older blindDL.

        Only for defaults that flipped, and only once: a user who turns the
        setting back off keeps it off, because the version has moved on by
        then and the migration never runs again.
        """
        if from_version >= CONFIG_VERSION:
            return
        if from_version < 1:
            # Closing to the tray became the default once torrents could keep
            # seeding after the window went away.
            self.data["minimize_to_tray"] = True
            self.data["tray_on_minimize"] = True
        self.data["config_version"] = CONFIG_VERSION
        self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __contains__(self, key):
        return key in self.data

    def get(self, key, default=None):
        return self.data.get(key, default)
