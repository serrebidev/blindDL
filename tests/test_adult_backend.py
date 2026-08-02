# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import types
import unittest
from unittest import mock

from blinddl import adult_backend


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class AdultProviderTests(unittest.TestCase):
    def test_inventory_contains_every_unofficial_api_repository(self):
        self.assertEqual(
            set(adult_backend.PROVIDERS),
            {
                "beeg", "eporner", "hqporner", "missav", "porngo",
                "pornhub", "porntrex", "redtube", "sex", "spankbang",
                "thumbzilla", "tube8", "xfreehd", "xhamster", "xnxx",
                "xvideos", "youporn",
            },
        )
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.boyfriendtv.com/videos/123/example"))
        self.assertEqual(
            adult_backend.provider_for_url(
                "https://subdomain.xvideos.com/video.test").key,
            "xvideos",
        )

    def test_normalize_unwraps_common_video_metadata(self):
        video = types.SimpleNamespace(
            url="https://example.invalid/video",
            video_id="42",
            title="Example",
            pornstars=["One", "Two"],
            length_seconds="125",
        )
        item = adult_backend._normalize(
            adult_backend.PROVIDERS["pornhub"], video)
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["artist"], "One, Two")
        self.assertEqual(item["duration_s"], 125)
        self.assertEqual(item["provider"], "pornhub")

    def test_boyfriendtv_extracts_highest_public_media_definition(self):
        page = """
            <meta property="og:title" content="A &amp; B">
            <script>
            var flashvars_123 = {"mediaDefinitions": [
                {"quality": "480", "videoUrl": "https://cdn.invalid/480.mp4"},
                {"quality": "1080", "videoUrl": "https://cdn.invalid/master.m3u8"}
            ]};
            </script>
        """
        with mock.patch.object(
                adult_backend.requests, "get", return_value=_Response(page)):
            item = adult_backend._inspect_boyfriendtv(
                "https://boyfriendtv.com/videos/123/example")
        self.assertEqual(item["title"], "A & B")
        self.assertEqual(item["direct_url"],
                         "https://cdn.invalid/master.m3u8")
        self.assertEqual(item["provider"], adult_backend.BOYFRIEND_KEY)

    def test_boyfriendtv_prefers_player_hls_over_page_thumbnails(self):
        page = r'''
            <meta property="og:title" content="Example">
            <img src="https://cdn.invalid/thumbs/poster.MP4">
            <script>
            var playerConfig = {sources: {hlsAuto:
              "https:\/\/cdn.invalid\/key=abc\/media=hls4A\/_TPL_.mp4"}};
            </script>
        '''
        with mock.patch.object(
                adult_backend.requests, "get", return_value=_Response(page)):
            item = adult_backend._inspect_boyfriendtv(
                "https://boyfriendtv.com/videos/123/example")
        self.assertEqual(
            item["direct_url"],
            "https://cdn.invalid/key=abc/media=hls4A/_TPL_.mp4",
        )

    def test_standard_api_download_builds_config_and_reports_progress(self):
        calls = []

        class Config:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class Video:
            async def download(self, config):
                calls.append(config)
                config.callback(50, 100)
                return True

        class Client:
            async def get_video(self, url):
                calls.append(url)
                return Video()

        module = types.SimpleNamespace(DownloadConfigHLS=Config)
        progress = mock.Mock()
        provider = adult_backend.PROVIDERS["pornhub"]
        payload = {"provider": "pornhub", "url": "https://pornhub.com/v/1"}
        with mock.patch.object(adult_backend, "_import_provider",
                               return_value=(module, Client())):
            adult_backend.download(payload, "output", progress_cb=progress)
        self.assertEqual(calls[0], payload["url"])
        self.assertEqual(calls[1].quality, "best")
        self.assertEqual(calls[1].path, "output")
        progress.assert_called_once_with(50, 100)
        self.assertEqual(provider.download_style, "standard")


if __name__ == "__main__":
    unittest.main()
