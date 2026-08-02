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
