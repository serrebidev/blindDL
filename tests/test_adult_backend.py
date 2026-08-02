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
                "aebn", "beeg", "eporner", "hqporner", "missav",
                "justforfans", "mymusclevideo", "onlyfans", "porngo",
                "pornhub", "porntrex", "redtube", "sex", "spankbang",
                "thumbzilla", "thisvid", "tube8", "xfreehd", "xhamster",
                "xnxx", "xvideos", "youporn",
            },
        )
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.boyfriendtv.com/videos/123/example"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://thisvid.com/videos/example/"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://gay.aebn.com/gay/movies/123/example"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://mymusclevideo.com/123/example/"))
        self.assertEqual(
            adult_backend.provider_for_url(
                "https://subdomain.xvideos.com/video.test").key,
            "xvideos",
        )

    def test_eporner_uses_native_filters_where_query_compatible(self):
        provider = adult_backend.PROVIDERS["eporner"]
        expected_gay_filters = {
            adult_backend.CONTENT_STRAIGHT: "0",
            adult_backend.CONTENT_GAY: "2",
            adult_backend.CONTENT_LESBIAN: "0",
            adult_backend.CONTENT_BISEXUAL: "1",
            adult_backend.CONTENT_TRANS: "1",
        }

        self.assertEqual(
            set(expected_gay_filters), set(adult_backend.CONTENT_CATEGORIES))
        for category, expected_filter in expected_gay_filters.items():
            query, kwargs = adult_backend._search_parameters(
                provider, "massage", category)
            if category in (adult_backend.CONTENT_STRAIGHT,
                            adult_backend.CONTENT_GAY):
                self.assertEqual(query, "massage")
            else:
                self.assertEqual(query, f"massage {category}")
            self.assertEqual(kwargs["sorting_gay"], expected_filter)

    def test_xnxx_uses_native_mode_for_every_content_category(self):
        provider = adult_backend.PROVIDERS["xnxx"]

        for category, mode in adult_backend.XNXX_CONTENT_MODES.items():
            query, kwargs = adult_backend._search_parameters(
                provider, "massage", category)
            self.assertEqual(query, "massage")
            self.assertEqual(kwargs["mode"], mode)

    def test_xhamster_uses_native_lesbian_category(self):
        query, kwargs = adult_backend._search_parameters(
            adult_backend.PROVIDERS["xhamster"],
            "massage",
            adult_backend.CONTENT_LESBIAN,
        )

        self.assertEqual(query, "massage")
        self.assertEqual(kwargs["category"], "lesbian")

    def test_xhamster_uses_native_gay_and_trans_catalog_paths(self):
        provider = adult_backend.PROVIDERS["xhamster"]

        for category in (
                adult_backend.CONTENT_GAY, adult_backend.CONTENT_TRANS):
            query, kwargs = adult_backend._search_parameters(
                provider, "massage", category)
            self.assertEqual(query, "massage")
            self.assertNotIn("category", kwargs)

        query, _kwargs = adult_backend._search_parameters(
            provider, "massage", adult_backend.CONTENT_BISEXUAL)
        self.assertEqual(query, "massage bisexual")

    def test_xhamster_fallback_parser_reads_current_video_cards(self):
        page = """
          <a data-role="thumb-link"
             href="https://xhamster.com/videos/example-xh123"
             aria-label="Example &amp; title"></a>
          <a data-role="thumb-link"
             href="https://xhamster.com/videos/example-xh123"
             aria-label="Duplicate"></a>
          <a href="https://xhamster.com/creators/videos/not-a-card"
             aria-label="Ignored"></a>
        """
        parser = adult_backend._XHamsterSearchParser()

        parser.feed(page)

        self.assertEqual(parser.items, [(
            "https://xhamster.com/videos/example-xh123",
            "Example & title",
        )])

    def test_provider_without_native_filter_uses_category_term(self):
        query, kwargs = adult_backend._search_parameters(
            adult_backend.PROVIDERS["pornhub"],
            "massage",
            adult_backend.CONTENT_TRANS,
        )

        self.assertEqual(query, "massage trans")
        self.assertNotIn("category", kwargs)

    def test_category_term_is_not_duplicated(self):
        query, _kwargs = adult_backend._search_parameters(
            adult_backend.PROVIDERS["pornhub"],
            "gay massage",
            adult_backend.CONTENT_GAY,
        )

        self.assertEqual(query, "gay massage")

    def test_gay_results_exclude_explicit_trans_metadata(self):
        trans_results = (
            {"title": "Asian shemale massage", "artist": "", "url": ""},
            {"title": "Massage", "artist": "Trans Woman", "url": ""},
            {"title": "Massage", "artist": "", "url": "/ladyboy/video/"},
            {"title": "FTM men together", "artist": "", "url": ""},
            {"title": "T-girl massage", "artist": "", "url": ""},
        )

        for item in trans_results:
            with self.subTest(item=item):
                self.assertFalse(adult_backend._matches_content_category(
                    item, adult_backend.CONTENT_GAY))
                self.assertTrue(adult_backend._matches_content_category(
                    item, adult_backend.CONTENT_TRANS))

    def test_gay_filter_keeps_gender_expression_terms(self):
        for title in ("Gay femboy massage", "Sissy men together",
                      "Crossdresser boyfriend"):
            with self.subTest(title=title):
                self.assertTrue(adult_backend._matches_content_category(
                    {"title": title}, adult_backend.CONTENT_GAY))

        self.assertTrue(adult_backend._matches_content_category({
            "title": "Gay massage",
            "url": "https://example.invalid/video?ts=123456",
        }, adult_backend.CONTENT_GAY))

    def test_pornhub_search_loads_api_metadata(self):
        kwargs = adult_backend.PROVIDERS["pornhub"].search_kwargs

        self.assertTrue(kwargs["load_api"])
        self.assertFalse(kwargs["load_html"])

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

    def test_thisvid_search_parser_normalizes_unique_video_links(self):
        page = """
            <a class="tumbpu" href="/videos/one/" title="One &amp; Two"></a>
            <a href="https://thisvid.com/videos/two/" class="tumbpu"></a>
            <a class="tumbpu" href="/videos/one/" title="Duplicate"></a>
            <a class="other" href="/videos/ignored/"></a>
        """
        parser = adult_backend._ThisVidSearchParser()

        parser.feed(page)

        self.assertEqual(
            parser.items,
            [
                ("https://thisvid.com/videos/one/", "One & Two"),
                ("https://thisvid.com/videos/two/", "Two"),
            ],
        )

    def test_thisvid_search_parser_excludes_private_video_cards(self):
        page = """
            <a class="tumbpu" href="/videos/private/" title="Private">
              <span class="thumb private"><img alt="Private"></span>
            </a>
            <a class="tumbpu" href="/videos/public/" title="Public">
              <span class="thumb"><img alt="Public"></span>
            </a>
        """
        parser = adult_backend._ThisVidSearchParser()

        parser.feed(page)

        self.assertEqual(
            parser.items,
            [("https://thisvid.com/videos/public/", "Public")],
        )

    def test_thisvid_uses_native_catalog_paths_where_logical(self):
        expected = {
            adult_backend.CONTENT_STRAIGHT: ("female/", "massage"),
            adult_backend.CONTENT_GAY: ("male/", "massage"),
            adult_backend.CONTENT_LESBIAN: ("female/", "massage lesbian"),
            adult_backend.CONTENT_BISEXUAL: ("search/", "massage bisexual"),
            adult_backend.CONTENT_TRANS: ("female/", "massage trans"),
        }

        for category, (path, query) in expected.items():
            with self.subTest(category=category), mock.patch.object(
                    adult_backend.requests, "get",
                    return_value=_Response("")) as get:
                adult_backend._search_thisvid("massage", category)

            get.assert_called_once_with(
                f"https://thisvid.com/{path}",
                params={"q": query},
                headers={"User-Agent": adult_backend._UA},
                timeout=30,
            )

    def test_mymusclevideo_parser_normalizes_unique_titled_links(self):
        page = """
            <a href="/123/first-video/" title="First video"></a>
            <a href="https://mymusclevideo.com/456/second/"
               title="Second &amp; video"></a>
            <a href="/123/first-video/" title="Duplicate"></a>
            <a href="/videos/recent/" title="Not a video"></a>
        """
        parser = adult_backend._MyMuscleVideoSearchParser()

        parser.feed(page)

        self.assertEqual(parser.items, [
            ("https://mymusclevideo.com/123/first-video/", "First video"),
            ("https://mymusclevideo.com/456/second/", "Second & video"),
        ])

    def test_mymusclevideo_search_is_gay_only_and_does_not_add_term(self):
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response("")) as get:
            adult_backend._search_mymusclevideo(
                "massage", adult_backend.CONTENT_GAY)

        get.assert_called_once_with(
            "https://mymusclevideo.com/search/video/",
            params={"s": "massage"},
            headers={"User-Agent": adult_backend._UA},
            timeout=30,
        )

    def test_search_skips_gay_only_provider_for_other_categories(self):
        with mock.patch.object(
                adult_backend, "_collect_search") as collect:
            _items, _answered, asked = adult_backend.search(
                "massage", timeout_s=0,
                sources=["mymusclevideo"],
                category=adult_backend.CONTENT_STRAIGHT,
            )

        collect.assert_not_called()
        self.assertEqual(asked, [])

    def test_thisvid_url_inspection_uses_bundled_ytdlp(self):
        extracted = [{
            "id": "42",
            "title": "Example",
            "url": "https://thisvid.com/videos/example/",
            "duration": 90,
            "uploader": "Creator",
        }]
        with mock.patch.object(
                adult_backend.ytdlp_backend, "extract_flat",
                return_value=(extracted, "Example")) as extract:
            items, title = adult_backend.inspect_url(extracted[0]["url"])

        extract.assert_called_once_with(
            extracted[0]["url"], cookies_from_browser="")
        self.assertEqual(title, "Example")
        self.assertEqual(items[0]["provider"], "thisvid")
        self.assertEqual(items[0]["duration_s"], 90)

    def test_thisvid_download_uses_bundled_ytdlp_video_mode(self):
        payload = {
            "provider": "thisvid",
            "url": "https://thisvid.com/videos/example/",
        }
        with mock.patch.object(
                adult_backend.ytdlp_backend, "download") as download:
            adult_backend.download(payload, "output")

        download.assert_called_once_with(
            payload["url"], "output", audio_only=False,
            progress_cb=None, cancel_event=None,
            cookies_from_browser=None,
        )

    def test_aebn_url_inspection_returns_movie_metadata(self):
        class Session:
            def __init__(self, **_kwargs):
                self.timeout = None
                self.headers = {}
                self.closed = False

            def close(self):
                self.closed = True

        session = Session()

        class SessionFactory:
            def __new__(cls, **_kwargs):
                return session

        class Movie:
            def __init__(self, url, received_session):
                self.url = url
                self.session = received_session
                self.movie_id = "123"
                self.title = "Example Movie"
                self.performers = ["One", "Two"]
                self.total_duration_seconds = 3600

        components = (mock.Mock(), Movie, SessionFactory, mock.Mock())
        url = "https://gay.aebn.com/gay/movies/123/example"
        with mock.patch.object(
                adult_backend, "_import_aebn", return_value=components):
            items, title = adult_backend.inspect_url(url)

        self.assertEqual(title, "Example Movie")
        self.assertEqual(items[0]["provider"], "aebn")
        self.assertEqual(items[0]["artist"], "One, Two")
        self.assertEqual(items[0]["duration_s"], 3600)
        self.assertEqual(items[0]["adult_category"], "gay")
        self.assertTrue(session.closed)

    def test_aebn_download_uses_accessible_adapter(self):
        payload = {
            "provider": "aebn",
            "url": "https://straight.aebn.com/straight/movies/123/example",
        }
        progress = mock.Mock()
        cancel = mock.Mock()
        with mock.patch.object(adult_backend, "_download_aebn") as download:
            adult_backend.download(
                payload, "output", progress_cb=progress,
                cancel_event=cancel)

        download.assert_called_once_with(
            payload, "output", progress_cb=progress,
            cancel_event=cancel,
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
