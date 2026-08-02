# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from blinddl import ytdlp_backend


class _YoutubeDL:
    instances = []

    def __init__(self, options):
        self.options = options
        self.downloaded = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        return {"id": "1", "title": "Example", "webpage_url": url}

    def download(self, urls):
        self.downloaded.extend(urls)


class YtDlpBackendTests(unittest.TestCase):
    def setUp(self):
        _YoutubeDL.instances.clear()

    def test_extract_passes_selected_browser_cookies(self):
        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _YoutubeDL):
            items, title = ytdlp_backend.extract_flat(
                "https://example.invalid/video", cookies_from_browser="edge")

        self.assertEqual(title, "Example")
        self.assertEqual(items[0]["title"], "Example")
        self.assertEqual(
            _YoutubeDL.instances[0].options["cookiesfrombrowser"], ("edge",))

    def test_download_suppresses_console_progress_but_keeps_hook(self):
        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _YoutubeDL):
            ytdlp_backend.download(
                "https://example.invalid/video", "output",
                audio_only=False, cookies_from_browser="firefox")

        options = _YoutubeDL.instances[0].options
        self.assertTrue(options["noprogress"])
        self.assertEqual(options["cookiesfrombrowser"], ("firefox",))
        self.assertEqual(
            _YoutubeDL.instances[0].downloaded,
            ["https://example.invalid/video"],
        )


if __name__ == "__main__":
    unittest.main()
