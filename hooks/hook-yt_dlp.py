# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""PyInstaller collection for yt-dlp without browser-only Emscripten modules."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


def supported_urllib3(module_name: str) -> bool:
    return not module_name.startswith("urllib3.contrib.emscripten")


hiddenimports = [
    "yt_dlp.compat._legacy",
    "yt_dlp.compat._deprecated",
    "yt_dlp.utils._legacy",
    "yt_dlp.utils._deprecated",
    "Cryptodome",
    "mutagen",
    "brotli",
    "certifi",
    "curl_cffi",
]
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("requests")
hiddenimports += collect_submodules("urllib3", filter=supported_urllib3)

excludedimports = [
    "bundle",
    "devscripts",
    "test",
    "youtube_dl",
    "youtube_dlc",
    "ytdlp_plugins",
]

datas = collect_data_files("curl_cffi", includes=["cacert.pem"])
datas += collect_data_files("yt_dlp_ejs", includes=["**/*.js"])
