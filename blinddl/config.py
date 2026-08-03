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


DEFAULTS = {
    # Where finished downloads go.
    "download_dir": os.path.join(os.path.expanduser("~"), "Music", APP_NAME),
    # Prefer audio-only extraction (via ffmpeg) over full video.
    "audio_only": True,
    # Audio container/codec used when audio_only is on: mp3, m4a, flac, wav, opus...
    "audio_format": "mp3",
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
    # How often subscriptions are checked for new items, in hours.
    "sub_check_hours": 6,
    # Automatically update yt-dlp, musicdl, wxPython, Deno and ffmpeg.
    "auto_update": True,
    # Minimum hours between automatic update checks.
    "update_check_hours": 24,
    # Timestamp (unix) of the last automatic update check; 0 = never.
    "last_update_check": 0,
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
