# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import asyncio
import threading
import time
import types
import unittest
from unittest import mock

from blinddl import adult_backend, search_order


class _Response:
    def __init__(self, text, payload=None):
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class AdultProviderTests(unittest.TestCase):
    def test_inventory_contains_every_unofficial_api_repository(self):
        self.assertEqual(
            set(adult_backend.PROVIDERS),
            {
                "aebn", "beeg", "eporner", "gay0day", "gayfuckporn",
                "gayporno", "homo", "hqporner", "icegay", "justforfans",
                "machotube", "missav", "mymusclevideo", "onlyfans",
                "porngo", "pornhub", "porntrex", "redtube", "sex",
                "spankbang", "thumbzilla", "thisvid", "tube8", "xfreehd",
                "xhamster", "xnxx", "xvideos", "youporn",
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
        self.assertTrue(adult_backend.is_supported_url(
            "https://gay0day.com/videos/123/example/"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.gayporno.fm/example_123.html"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.gayfuckporn.com/example/123.html"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.icegay.tv/movies/123/example"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://www.machotube.tv/movies/123/example"))
        self.assertTrue(adult_backend.is_supported_url(
            "https://homo.xxx/videos/123/"))
        self.assertEqual(
            adult_backend.provider_for_url(
                "https://subdomain.xvideos.com/video.test").key,
            "xvideos",
        )
        self.assertEqual(
            adult_backend.provider_for_url(
                "https://xvideos2.com/video.test").key,
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

    def test_eporner_native_search_normalizes_public_api_results(self):
        payload = {"videos": [{
            "url": "https://www.eporner.com/video-example/",
            "title": "Example",
            "keywords": "amateur",
            "length_sec": 42,
        }]}
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response("", payload)) as get:
            items = adult_backend._search_eporner(
                "amateur", adult_backend.CONTENT_STRAIGHT,
                search_order.ORDER_RELEVANCE)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Example")
        self.assertEqual(items[0]["duration_s"], 42)
        self.assertEqual(items[0]["adult_category"], "straight")
        self.assertEqual(get.call_args.kwargs["params"]["gay"], "0")

    def test_current_provider_signature_drops_obsolete_options(self):
        calls = []

        class Client:
            async def search_videos(
                    self, query, pages=1, iterator_config=None):
                calls.append((query, pages, iterator_config))
                if False:
                    yield None

        provider = adult_backend.PROVIDERS["pornhub"]
        with mock.patch.object(
                adult_backend, "_import_provider",
                return_value=(types.SimpleNamespace(), Client())):
            items = asyncio.run(adult_backend._collect_search(
                provider, "amateur", None,
                adult_backend.CONTENT_STRAIGHT))

        self.assertEqual(items, [])
        self.assertEqual(calls[0][0], "amateur")
        self.assertEqual(calls[0][1], 1)
        self.assertIsNotNone(calls[0][2])

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

    def test_straight_search_never_appends_straight_term(self):
        provider = adult_backend.PROVIDERS["pornhub"]

        for query in ("cop", "cop straight", "Straight cop"):
            with self.subTest(query=query):
                categorized, _kwargs = adult_backend._search_parameters(
                    provider, query, adult_backend.CONTENT_STRAIGHT)
                self.assertEqual(categorized, query)

    def test_gay_catalog_search_passes_query_through_unchanged(self):
        for key in adult_backend._GAY_CATALOG_SEARCH:
            with self.subTest(provider=key):
                self.assertEqual(
                    adult_backend.PROVIDERS[key].search_categories,
                    (adult_backend.CONTENT_GAY,),
                )
                query, _kwargs = adult_backend._search_parameters(
                    adult_backend.PROVIDERS[key], "straight cop",
                    adult_backend.CONTENT_GAY)
                self.assertEqual(query, "straight cop")

    def test_gay_catalog_titles_keep_straight_cop_kink_label(self):
        for key in adult_backend._GAY_CATALOG_SEARCH:
            with self.subTest(provider=key):
                self.assertTrue(adult_backend._matches_content_category({
                    "provider": key,
                    "title": "Straight Cop Gets Fucked",
                }, adult_backend.CONTENT_GAY))

    def test_provider_order_uses_its_native_sort_parameter(self):
        _query, kwargs = adult_backend._search_parameters(
            adult_backend.PROVIDERS["pornhub"],
            "massage",
            adult_backend.CONTENT_STRAIGHT,
            search_order.ORDER_RECENT,
        )

        self.assertEqual(kwargs["sort_by"], "mr")
        self.assertTrue(kwargs["keep_original_order"])
        self.assertTrue(adult_backend.supports_order(
            "pornhub", search_order.ORDER_POPULAR))

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

    def test_gay_results_exclude_female_and_mixed_metadata(self):
        unwanted = (
            {"provider": "eporner", "title":
             "Hentai femdom cop cosplay - anal fingering & rimjob"},
            {"provider": "eporner", "title": "Natalie Mars - Cop Gets Fucked",
             "content_tags": "uniform, anal, shemale"},
            {"provider": "eporner", "title":
             "Amateur wife is my gay sex slave and teen boy football blowjob"},
        )

        for item in unwanted:
            with self.subTest(item=item):
                self.assertFalse(adult_backend._matches_content_category(
                    item, adult_backend.CONTENT_GAY))

    def test_query_based_gay_results_require_positive_male_evidence(self):
        self.assertFalse(adult_backend._matches_content_category({
            "provider": "eporner",
            "title": "Ambiguous cop cosplay",
        }, adult_backend.CONTENT_GAY))
        self.assertFalse(adult_backend._matches_content_category({
            "provider": "eporner",
            "title": "Men in cop uniforms",
        }, adult_backend.CONTENT_GAY))

    def test_gay_results_exclude_bisexual_and_foreign_female_titles(self):
        for title in (
            "Bisexual sex in the car with daddy",
            "Top 10 Bi Scenes - Biphoria",
            'Thick Latina Fucks "Gay" Best Friend',
            "HAN VAR BØG, MEN HAN VANDT MIG SOM EN RIGTIG FISSE",
        ):
            with self.subTest(title=title):
                self.assertFalse(adult_backend._matches_content_category({
                    "provider": "youporn", "title": title,
                }, adult_backend.CONTENT_GAY))
        self.assertTrue(adult_backend._matches_content_category({
            "provider": "eporner",
            "title": "Bevis and Albert",
            "content_tags": "gay, handjob, anal",
        }, adult_backend.CONTENT_GAY))

    def test_trusted_gay_catalog_keeps_ambiguous_male_only_title(self):
        self.assertTrue(adult_backend._matches_content_category({
            "provider": "mymusclevideo",
            "title": "Must have been cold in the room",
        }, adult_backend.CONTENT_GAY))

    def test_gay_filter_keeps_gender_expression_terms(self):
        for title in ("Gay femboy massage", "Sissy men together",
                      "Crossdresser boyfriend"):
            with self.subTest(title=title):
                self.assertTrue(adult_backend._matches_content_category(
                    {"provider": "mymusclevideo", "title": title},
                    adult_backend.CONTENT_GAY))

        self.assertTrue(adult_backend._matches_content_category({
            "provider": "mymusclevideo",
            "title": "Gay massage",
            "url": "https://example.invalid/video?ts=123456",
        }, adult_backend.CONTENT_GAY))

    def test_missav_is_not_offered_outside_straight_search(self):
        for key in ("missav", "hqporner"):
            with self.subTest(provider=key):
                self.assertEqual(
                    adult_backend.PROVIDERS[key].search_categories,
                    (adult_backend.CONTENT_STRAIGHT,),
                )

    def test_pornhub_search_loads_api_metadata(self):
        kwargs = adult_backend.PROVIDERS["pornhub"].search_kwargs

        self.assertTrue(kwargs["load_api"])
        self.assertFalse(kwargs["load_html"])

    def test_nonresponsive_thumbzilla_search_is_not_advertised(self):
        self.assertIsNone(adult_backend.PROVIDERS["thumbzilla"].search_method)

    def test_normalize_unwraps_common_video_metadata(self):
        video = types.SimpleNamespace(
            url="https://example.invalid/video",
            video_id="42",
            title="Example",
            pornstars=["One Two", "Three Four"],
            length_seconds="125",
        )
        item = adult_backend._normalize(
            adult_backend.PROVIDERS["pornhub"], video)
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["artist"], "One Two, Three Four")
        self.assertEqual(item["duration_s"], 125)
        self.assertEqual(item["provider"], "pornhub")

    def test_performer_names_drops_page_navigation_garbage(self):
        garbage = [
            "HQPORNER", "Categories", "Girls", "Brynn Tyler", "blonde",
            "ass licking", "i want you to put it in my ass, sir",
            "1080p", "Nacho Vidal", "brynn tyler", "See all recent porn",
        ]
        self.assertEqual(
            adult_backend._performer_names(garbage),
            "Brynn Tyler, Nacho Vidal",
        )
        self.assertEqual(adult_backend._performer_names([]), "")
        self.assertEqual(adult_backend._performer_names(["One Two"]),
                         "One Two")

    def test_unwrap_reads_current_scraperesult_shape(self):
        ok = types.SimpleNamespace(succeeded=True, item="video",
                                   is_success=True, video="old")
        failed = types.SimpleNamespace(succeeded=False, item="video",
                                       is_success=False, video="old")

        self.assertEqual(adult_backend._unwrap(ok), "video")
        self.assertIsNone(adult_backend._unwrap(failed))

    def test_unwrap_keeps_legacy_scraperesult_shape(self):
        ok = types.SimpleNamespace(is_success=True, video="old")
        failed = types.SimpleNamespace(is_success=False, video="old")

        self.assertEqual(adult_backend._unwrap(ok), "old")
        self.assertIsNone(adult_backend._unwrap(failed))
        self.assertEqual(adult_backend._unwrap("plain"), "plain")

    def test_load_normalize_fields_skips_unknown_and_missing_fields(self):
        class Media:
            def __init__(self):
                self.loaded = []

            async def get_field(self, name):
                if name not in ("title", "duration"):
                    raise ValueError(f"no field {name}")
                self.loaded.append(name)
                return name

        media = Media()
        asyncio.run(adult_backend._load_normalize_fields(media))
        self.assertEqual(media.loaded, ["title", "duration"])

    def test_load_normalize_fields_skips_plain_objects(self):
        plain = {"url": "https://example.invalid/video", "title": "X"}
        asyncio.run(adult_backend._load_normalize_fields(plain))
        self.assertEqual(plain["title"], "X")

    def test_normalize_skips_callable_author_and_keeps_loaded_context(self):
        video = types.SimpleNamespace(
            url="https://example.invalid/video",
            video_id="42",
            title="Example",
            author=lambda: "not loaded",
            uploader_name="Actual creator",
            categories=["gay"],
            pornstars_urls=["/gay/pornstar/example/"],
        )

        item = adult_backend._normalize(
            adult_backend.PROVIDERS["youporn"], video)

        self.assertEqual(item["artist"], "Actual creator")
        self.assertIn("gay", item["content_tags"])

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

    def test_thisvid_playlist_expands_public_entries_without_cookies(self):
        page = """
            <h1>Leather: Current Video</h1>
            <a class="tumbpu"
               href="https://thisvid.com/playlist/102612/video/public-video/"
               title="Public video"><span class="thumb"></span></a>
            <a class="tumbpu"
               href="https://thisvid.com/playlist/102612/video/private-video/"
               title="Private video"><span class="thumb private">
               <img alt="Private"></span></a>
        """
        url = "https://thisvid.com/playlist/102612/video/current-video/"
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(page)) as get, mock.patch.object(
                    adult_backend.ytdlp_backend, "extract_flat") as extract:
            items, title = adult_backend.inspect_url(url)

        get.assert_called_once_with(
            url, headers={"User-Agent": adult_backend._UA}, timeout=30)
        extract.assert_not_called()
        self.assertEqual(title, "Leather")
        self.assertEqual(
            [item["title"] for item in items],
            ["Current Video", "Public video"],
        )
        self.assertFalse(any(item["requires_login"] for item in items))

    def test_thisvid_playlist_includes_private_entries_with_cookies(self):
        page = """
            <h1>Leather: Current Video</h1>
            <a class="tumbpu"
               href="/playlist/102612/video/private-video/"
               title="Private video"><span class="thumb private"></span></a>
        """
        url = "https://thisvid.com/playlist/102612/video/current-video/"
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(page)):
            items, _title = adult_backend.inspect_url(
                url, config={"cookies_from_browser": "chrome"})

        self.assertEqual(len(items), 2)
        self.assertEqual(
            items[1]["url"], "https://thisvid.com/videos/private-video/")
        self.assertTrue(items[1]["requires_login"])
        self.assertEqual(items[1]["cookies_from_browser"], "chrome")

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

    def test_gay_catalog_search_uses_site_url_and_pattern(self):
        expected = {
            "gay0day": ("https://gay0day.com/search/cop/",
                         "https://gay0day.com/videos/167371/good-cop-bad-cop/"),
            "gayporno": ("https://www.gayporno.fm/search/cop",
                          "https://www.gayporno.fm/damon-phoenix_1880321.html"),
            "gayfuckporn": ("https://www.gayfuckporn.com/?search=cop",
                             "https://www.gayfuckporn.com/aaron-trainer/3050866.html"),
            "icegay": ("https://www.icegay.tv/search/cop",
                        "https://www.icegay.tv/movies/1435531/ballsy-cops"),
            "machotube": ("https://www.machotube.tv/search/cop",
                           "https://www.machotube.tv/movies/1457751/bold-cops"),
            "homo": ("https://homo.xxx/search/cop/",
                      "https://homo.xxx/videos/40170/"),
        }
        for key, (search_url, video_url) in expected.items():
            with self.subTest(provider=key), mock.patch.object(
                    adult_backend.requests, "get",
                    return_value=_Response("")) as get:
                items = adult_backend._search_gay_catalog(
                    key, "cop", adult_backend.CONTENT_GAY)

            self.assertEqual(items, [])
            self.assertEqual(get.call_args.args[0], search_url)
            self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"],
                             adult_backend._UA)
            self.assertEqual(get.call_args.kwargs["timeout"], 30)

    def test_gay_search_parser_reads_anchor_titles_and_dedupes(self):
        page = """
            <a href="https://gay0day.com/videos/1/good-cop/"
               title="Good Cop"></a>
            <a href="/videos/2/bad-cop/" title="Bad &amp; Cop"></a>
            <a href="https://gay0day.com/videos/1/good-cop/"
               title="Duplicate"></a>
            <a href="/categories/" title="Not a video"></a>
        """
        parser = adult_backend._GaySearchParser(
            r"https://gay0day\.com/videos/\d+/[^/]+/?",
            "https://gay0day.com/")

        parser.feed(page)

        self.assertEqual(parser.items, [
            ("https://gay0day.com/videos/1/good-cop/", "Good Cop"),
            ("https://gay0day.com/videos/2/bad-cop/", "Bad & Cop"),
        ])

    def test_gay_search_parser_reads_card_image_alt_title(self):
        page = """
            <a class="js-gallery-link"
               href="/aaron-trainer/3050866.html">
              <img src="thumb.jpg" alt="Aaron Trainer">
            </a>
            <a class="js-gallery-link" href="/other/123.html">
              <img src="thumb.jpg">
            </a>
        """
        parser = adult_backend._GaySearchParser(
            r"https://www\.gayfuckporn\.com/[^/]+/\d+\.html",
            "https://www.gayfuckporn.com/")

        parser.feed(page)

        self.assertEqual(parser.items, [
            ("https://www.gayfuckporn.com/aaron-trainer/3050866.html",
             "Aaron Trainer"),
        ])

    def test_mymusclevideo_playlist_url_expands_to_queue_items(self):
        page = """
            <title>cop Playlist - MyMusclevideo.com</title>
            <a href="/38947/leather-muscle-pig/"
               title="LEATHER MUSCLE PIG !"></a>
            <a href="/40665/sexy-cop-1/" title="Sexy Cop 1"></a>
        """
        url = "https://mymusclevideo.com/playlist/17311/cop/"
        config = {"cookies_from_browser": "firefox"}
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(page)) as get, mock.patch.object(
                    adult_backend.ytdlp_backend, "extract_flat") as extract:
            items, title = adult_backend.inspect_url(url, config=config)

        get.assert_called_once_with(
            url, headers={"User-Agent": adult_backend._UA}, timeout=30)
        extract.assert_not_called()
        self.assertEqual(title, "cop")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "LEATHER MUSCLE PIG !")
        self.assertEqual(items[0]["adult_category"], adult_backend.CONTENT_GAY)
        self.assertEqual(items[0]["cookies_from_browser"], "firefox")

    def test_mymusclevideo_empty_playlist_has_clear_error(self):
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response("<title>Empty</title>")):
            with self.assertRaisesRegex(RuntimeError, "no public videos"):
                adult_backend.inspect_url(
                    "https://mymusclevideo.com/playlist/1/empty/")

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

    def test_search_starts_every_selected_provider_at_once(self):
        sources = [
            "eporner", "hqporner", "missav", "pornhub",
            "porntrex", "redtube", "thisvid", "tube8",
        ]
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()
        release = threading.Event()

        async def collect(*args, **kwargs):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            try:
                await asyncio.to_thread(release.wait, 2)
                return []
            finally:
                with lock:
                    state["active"] -= 1

        try:
            with mock.patch.object(
                    adult_backend, "_collect_search", side_effect=collect):
                adult_backend.search(
                    "amateur", timeout_s=0.2, sources=sources,
                    category=adult_backend.CONTENT_STRAIGHT)
        finally:
            release.set()

        deadline = time.monotonic() + 2
        while (any(thread.name.startswith("adult-search-")
                   for thread in threading.enumerate()) and
               time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertEqual(state["maximum"], len(sources))
        self.assertFalse(any(thread.name.startswith("adult-search-")
                             for thread in threading.enumerate()))

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
            extracted[0]["url"], cookies_from_browser="", cookies_file="",
            fix_stream=None)
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
            adult_backend.download(payload, "output", video_format="mkv")

        download.assert_called_once_with(
            payload["url"], "output", audio_only=False, video_format="mkv",
            progress_cb=None, cancel_event=None,
            cookies_from_browser=None,
            cookies_file=None,
            fix_stream=None,
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

    def test_stream_fix_for_maps_each_provider(self):
        for key in ("gayporno", "icegay", "machotube", "gayfuckporn"):
            self.assertIs(
                adult_backend.stream_fix_for(key),
                adult_backend._tube_sign_info,
            )
        self.assertIs(
            adult_backend.stream_fix_for("homo"),
            adult_backend._hls_wrapper_fix,
        )
        for key in ("thisvid", "mymusclevideo", "pornhub", "gay0day"):
            self.assertIsNone(adult_backend.stream_fix_for(key))

    def test_tube_sign_info_leaves_fresh_keys_alone(self):
        info = {
            "url": "https://vcdn03.gayporno.fm/key=x,end=9999999999/video.mp4",
            "formats": [{
                "url": "https://vcdn03.gayporno.fm/key=x,end=9999999999/v.mp4",
                "height": 720,
                "format_id": "0",
            }],
        }
        with mock.patch.object(adult_backend.requests, "post") as post:
            adult_backend._tube_sign_info(info)
        post.assert_not_called()

    def test_tube_sign_info_resigns_stale_keys(self):
        page = "<div class=\"js-tube-config\" " \
               "data-v-update-url=\"https://u3.gayporno.fm/video\"></div>"
        fresh = "https://vcdn03.gayporno.fm/key=new,end=9999999999/video.mp4"
        info = {
            "url": "https://vcdn03.gayporno.fm/key=old,end=1/video.mp4",
            "webpage_url": "https://www.gayporno.fm/video_1.html",
            "formats": [{
                "url": "https://vcdn03.gayporno.fm/key=old,end=1/video.mp4",
                "height": 720,
                "format_id": "0",
            }],
        }

        def fake_post(url, **kwargs):
            self.assertEqual(
                url, "https://u3.gayporno.fm/ah/sign")
            self.assertEqual(kwargs["json"], {"urls": {"mp4": {"720": info["formats"][0]["url"]}}})
            return _Response("", payload={"urls": {"mp4": fresh}})

        adult_backend._tube_sign_endpoints.pop("www.gayporno.fm", None)
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(page)) as get, mock.patch.object(
                    adult_backend.requests, "post", side_effect=fake_post):
            adult_backend._tube_sign_info(info)

        get.assert_called_once()
        self.assertEqual(info["formats"][0]["url"], fresh)
        self.assertEqual(info["url"], fresh)

    def test_tube_sign_endpoint_is_cached_per_host(self):
        page = "<div class=\"js-tube-config\" " \
               "data-v-update-url=\"https://u3.icegay.tv/video\"></div>"
        adult_backend._tube_sign_endpoints.pop("www.icegay.tv", None)
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(page)) as get:
            self.assertEqual(
                adult_backend._tube_sign_endpoint(
                    "https://www.icegay.tv/movies/1/x"),
                "https://u3.icegay.tv/ah/sign",
            )
        get.assert_called_once()
        # A second call is served from the cache.
        with mock.patch.object(adult_backend.requests, "get") as get:
            self.assertEqual(
                adult_backend._tube_sign_endpoint(
                    "https://www.icegay.tv/movies/2/y"),
                "https://u3.icegay.tv/ah/sign",
            )
        get.assert_not_called()

    def test_best_hls_variant_picks_highest_bandwidth(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=793606,RESOLUTION=854x480\n"
            "https://cdn.example.com/480.m3u8\n"
            "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1624248,RESOLUTION=1280x720\n"
            "https://cdn.example.com/720.m3u8\n"
        )
        self.assertEqual(
            adult_backend._best_hls_variant(master),
            "https://cdn.example.com/720.m3u8",
        )

    def test_hls_wrapper_fix_resolves_master_playlist(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=793606,RESOLUTION=854x480\n"
            "https://cdn.example.com/480.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1624248,RESOLUTION=1280x720\n"
            "https://cdn.example.com/720.m3u8\n"
        )
        info = {
            "url": "https://homo.xxx/get_file/11/token/40000/40170/40170.mp4/",
            "webpage_url": "https://homo.xxx/videos/40170/",
            "formats": [{
                "url": "https://homo.xxx/get_file/11/token/40000/40170/40170.mp4/",
                "format_id": "0",
            }],
        }
        with mock.patch.object(
                adult_backend.requests, "get",
                return_value=_Response(master)) as get:
            replacement = adult_backend._hls_wrapper_fix(info)
        self.assertEqual(replacement, "https://cdn.example.com/720.m3u8")
        self.assertEqual(info["url"], replacement)
        self.assertEqual(info["formats"][0]["url"], replacement)
        get.assert_called_once()

    def test_hls_wrapper_fix_ignores_non_playlist_urls(self):
        info = {"url": "https://homo.xxx/videos/40170/", "formats": []}
        with mock.patch.object(adult_backend.requests, "get") as get:
            self.assertIsNone(adult_backend._hls_wrapper_fix(info))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
