# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import unittest
import tempfile
from pathlib import Path
from unittest import mock

import yt_dlp
from yt_dlp.cookies import CookieLoadError

from blinddl import search_order, ytdlp_backend


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
        return {
            "id": "1", "title": "Example", "webpage_url": url,
            "url": "https://media.example/stream.mp4",
        }

    def download(self, urls):
        self.downloaded.extend(urls)


def _playlist_youtube_dl(info):
    """A YoutubeDL stub whose extract_info returns *info* verbatim."""

    class _Stub(_YoutubeDL):
        def extract_info(self, url, download=False):
            return info

    return _Stub


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

    def test_cookie_attempts_prefer_file_then_browser_then_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
            path = f.name
        try:
            with mock.patch.object(
                    ytdlp_backend, "_automatic_cookie_file",
                    return_value={"auto": True}):
                # A valid cookies file wins over any browser choice.
                self.assertEqual(
                    ytdlp_backend._cookie_attempts("edge", path),
                    [{"cookiefile": path}, {}],
                )
                # A named browser is used as-is, with no silent auto-detection.
                self.assertEqual(
                    ytdlp_backend._cookie_attempts("edge", None),
                    [{"cookiesfrombrowser": ("edge",)}, {}],
                )
                # No choice at all: just no cookies.
                self.assertEqual(
                    ytdlp_backend._cookie_attempts(None, None),
                    [{}],
                )
                # The opt-in auto choice adds the automatic export.
                self.assertEqual(
                    ytdlp_backend._cookie_attempts("auto", None),
                    [{"auto": True}, {}],
                )
                self.assertEqual(
                    ytdlp_backend._cookie_attempts("auto", path),
                    [{"cookiefile": path}, {"auto": True}, {}],
                )
        finally:
            Path(path).unlink()

    def test_auto_browser_cookies_opt_in_uses_the_exported_file(self):
        with (
            mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL", _YoutubeDL),
            mock.patch.object(
                ytdlp_backend, "_automatic_cookie_file",
                return_value={"cookiefile": "exported.txt"}),
        ):
            ytdlp_backend.extract_flat(
                "https://example.invalid/video", cookies_from_browser="auto")

        self.assertEqual(
            _YoutubeDL.instances[0].options["cookiefile"], "exported.txt")
        self.assertNotIn(
            "cookiesfrombrowser", _YoutubeDL.instances[0].options)

    def test_broken_browser_cookies_fall_back_to_no_cookies(self):
        class _FailCookies(_YoutubeDL):
            def extract_info(self, url, download=False):
                if self.options.get("cookiesfrombrowser"):
                    raise CookieLoadError("failed to load cookies")
                return {
                    "id": "1", "title": "Example", "webpage_url": url,
                    "url": "https://media.example/stream.mp4",
                }

        with (
            mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL", _FailCookies),
            mock.patch.object(
                ytdlp_backend, "_automatic_cookie_file", return_value={}),
        ):
            items, title = ytdlp_backend.extract_flat(
                "https://example.invalid/video", cookies_from_browser="firefox")

        self.assertEqual(title, "Example")
        self.assertEqual(len(_YoutubeDL.instances), 2)
        self.assertEqual(
            _YoutubeDL.instances[0].options["cookiesfrombrowser"], ("firefox",))
        self.assertNotIn("cookiesfrombrowser", _YoutubeDL.instances[1].options)
        self.assertNotIn("cookiefile", _YoutubeDL.instances[1].options)

    def test_existing_cookies_file_wins_over_the_browser(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
            path = f.name
        try:
            with mock.patch.object(
                    ytdlp_backend.yt_dlp, "YoutubeDL", _YoutubeDL):
                ytdlp_backend.extract_flat(
                    "https://example.invalid/video",
                    cookies_from_browser="edge", cookies_file=path)
            opts = _YoutubeDL.instances[0].options
            self.assertEqual(opts["cookiefile"], path)
            self.assertNotIn("cookiesfrombrowser", opts)
        finally:
            Path(path).unlink()

    def test_resolve_stream_falls_back_when_browser_cookies_fail(self):
        class _FailCookies(_YoutubeDL):
            def extract_info(self, url, download=False):
                if self.options.get("cookiesfrombrowser"):
                    raise CookieLoadError("failed to load cookies")
                return {
                    "id": "1", "title": "Example", "webpage_url": url,
                    "url": "https://media.example/stream.mp4",
                }

        with (
            mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL", _FailCookies),
            mock.patch.object(
                ytdlp_backend, "_automatic_cookie_file", return_value={}),
        ):
            stream = ytdlp_backend.resolve_stream(
                "https://example.invalid/video", audio_only=True,
                cookies_from_browser="firefox")

        self.assertEqual(stream, "https://media.example/stream.mp4")
        self.assertEqual(len(_YoutubeDL.instances), 2)

    def test_download_falls_back_when_browser_cookies_fail(self):
        class _FailCookies(_YoutubeDL):
            def download(self, urls):
                if self.options.get("cookiesfrombrowser"):
                    raise CookieLoadError("failed to load cookies")
                self.downloaded.extend(urls)

        with (
            mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL", _FailCookies),
            mock.patch.object(
                ytdlp_backend, "_automatic_cookie_file", return_value={}),
        ):
            result = ytdlp_backend.download(
                "https://example.invalid/video", "out", audio_only=True,
                cookies_from_browser="firefox")

        self.assertEqual(result, "")
        self.assertEqual(len(_YoutubeDL.instances), 2)

    def test_download_falls_back_when_cookies_fail_wrapped_in_download_error(self):
        # download() and resolve_stream() do not set ignoreerrors, so yt-dlp's
        # cookiejar property reports the cookie failure with "ERROR:" and
        # raises a DownloadError whose __context__ is the CookieLoadError.
        # The fallback must recognise that wrapped form too.
        class _FailCookiesWrapped(_YoutubeDL):
            def download(self, urls):
                if self.options.get("cookiesfrombrowser"):
                    cookie_error = CookieLoadError("failed to load cookies")
                    wrapped = yt_dlp.utils.DownloadError(
                        "ERROR: could not find firefox cookies database")
                    wrapped.__context__ = cookie_error
                    raise wrapped
                self.downloaded.extend(urls)

        with (
            mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _FailCookiesWrapped),
            mock.patch.object(
                ytdlp_backend, "_automatic_cookie_file", return_value={}),
        ):
            result = ytdlp_backend.download(
                "https://example.invalid/video", "out", audio_only=True,
                cookies_from_browser="firefox")

        self.assertEqual(result, "")
        self.assertEqual(len(_YoutubeDL.instances), 2)

    def test_watch_url_with_list_expands_to_the_whole_playlist(self):
        # yt-dlp only redirects watch?v=...&list=... to its playlist when the
        # top-level URL is resolved, which "in_playlist" does and True does not.
        playlist = {
            "_type": "playlist", "title": "Let's Play",
            "entries": [
                {"id": "a", "title": "Part 1", "ie_key": "Youtube", "url": "a"},
                {"id": "b", "title": "Part 2", "ie_key": "Youtube", "url": "b"},
            ],
        }
        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL",
                _playlist_youtube_dl(playlist)):
            items, title = ytdlp_backend.extract_flat(
                "https://www.youtube.com/watch?v=a&list=PL1")

        self.assertEqual(title, "Let's Play")
        self.assertEqual([i["id"] for i in items], ["a", "b"])
        self.assertEqual(items[0]["url"], "https://www.youtube.com/watch?v=a")
        self.assertEqual(
            _YoutubeDL.instances[0].options["extract_flat"], "in_playlist")

    def test_channel_tabs_are_flattened_and_deduplicated(self):
        # A bare channel URL comes back as a playlist of tab playlists.
        channel = {
            "_type": "playlist", "title": "Veritasium",
            "entries": [
                {"_type": "playlist", "title": "Videos", "entries": [
                    {"id": "a", "title": "One", "ie_key": "Youtube",
                     "url": "a"},
                ]},
                {"_type": "playlist", "title": "Live", "entries": [
                    {"id": "a", "title": "One", "ie_key": "Youtube",
                     "url": "a"},
                    {"id": "b", "title": "Two", "ie_key": "Youtube",
                     "url": "b"},
                ]},
            ],
        }
        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL",
                _playlist_youtube_dl(channel)):
            items, title = ytdlp_backend.extract_flat(
                "https://www.youtube.com/@veritasium")

        self.assertEqual(title, "Veritasium")
        self.assertEqual([i["id"] for i in items], ["a", "b"])

    def test_ranked_feeds_are_capped_but_playlists_are_not(self):
        listing = {"_type": "playlist", "title": "Feed", "entries": []}
        stub = _playlist_youtube_dl(listing)
        with mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL", stub):
            ytdlp_backend.extract_flat(
                "https://www.youtube.com/results?search_query=rimworld")
            ytdlp_backend.extract_flat(
                "https://www.youtube.com/hashtag/rimworld")
            ytdlp_backend.extract_flat(
                "https://www.youtube.com/playlist?list=PL1")
            ytdlp_backend.extract_flat(
                "https://www.youtube.com/hashtag/rimworld", limit=5)

        caps = [i.options.get("playlistend") for i in _YoutubeDL.instances]
        self.assertEqual(
            caps,
            [ytdlp_backend.RANKED_FEED_LIMIT, ytdlp_backend.RANKED_FEED_LIMIT,
             None, 5])

    def test_youtube_search_and_hashtag_feeds_receive_native_order(self):
        recent = ytdlp_backend.search_url(
            "rimworld", search_order.ORDER_RECENT)
        popular = ytdlp_backend.ordered_feed_url(
            "https://www.youtube.com/hashtag/rimworld",
            search_order.ORDER_POPULAR)

        self.assertIn("search_query=rimworld", recent)
        self.assertIn("sp=CAISAhAB8AEB", recent)
        self.assertIn("search_query=%23rimworld", popular)
        self.assertIn("sp=CAMSAhAB8AEB", popular)

    def test_feed_order_does_not_rewrite_channels_or_playlists(self):
        for url in (
                "https://www.youtube.com/@veritasium",
                "https://www.youtube.com/playlist?list=PL1"):
            self.assertEqual(
                ytdlp_backend.ordered_feed_url(
                    url, search_order.ORDER_POPULAR),
                url,
            )

    def test_shorthand_subscription_targets_become_urls(self):
        self.assertEqual(
            ytdlp_backend.normalize_url("  @veritasium "),
            "https://www.youtube.com/@veritasium")
        self.assertEqual(
            ytdlp_backend.normalize_url("#rimworld"),
            "https://www.youtube.com/hashtag/rimworld")
        self.assertEqual(
            ytdlp_backend.normalize_url("PLdvFbaCu1RVgZtWw0_2Pkd"),
            "https://www.youtube.com/playlist?list=PLdvFbaCu1RVgZtWw0_2Pkd")
        self.assertEqual(
            ytdlp_backend.normalize_url("UCHnyfMqiRRG1u-2MsSQLbXA"),
            "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA")
        self.assertEqual(
            ytdlp_backend.normalize_url("youtube.com/@x/videos"),
            "https://youtube.com/@x/videos")
        self.assertEqual(
            ytdlp_backend.normalize_url("https://example.invalid/v"),
            "https://example.invalid/v")

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

    def test_download_returns_the_finished_path_for_queue_actions(self):
        class _FinishedDL(_YoutubeDL):
            def download(self, urls):
                super().download(urls)
                path = str(Path(self.options["test_output"]) / "video.mp4")
                Path(path).write_bytes(b"video")
                self.options["postprocessor_hooks"][0]({
                    "status": "finished", "filepath": path,
                })

        with tempfile.TemporaryDirectory() as folder:
            class _ConfiguredFinishedDL(_FinishedDL):
                def __init__(self, options):
                    options["test_output"] = folder
                    super().__init__(options)

            with mock.patch.object(
                    ytdlp_backend.yt_dlp, "YoutubeDL", _ConfiguredFinishedDL):
                result = ytdlp_backend.download(
                    "https://example.invalid/video", folder,
                    audio_only=False,
                )

        self.assertEqual(result, str(Path(folder) / "video.mp4"))

    def test_video_preview_resolves_one_progressive_stream(self):
        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _YoutubeDL):
            stream = ytdlp_backend.resolve_stream(
                "https://example.invalid/video", audio_only=False,
                cookies_from_browser="edge")

        options = _YoutubeDL.instances[0].options
        self.assertEqual(stream, "https://media.example/stream.mp4")
        self.assertIn("acodec!=none", options["format"])
        self.assertIn("vcodec!=none", options["format"])
        self.assertEqual(options["cookiesfrombrowser"], ("edge",))

    def test_format_fallback_when_requested_format_unavailable(self):
        call_count = [0]

        class _FallbackDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise yt_dlp.utils.ExtractorError(
                        "Requested format is not available")
                return {
                    "id": "1", "title": "Fallback", "webpage_url": url,
                    "url": "https://media.example/fallback.mp4",
                }

        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _FallbackDL):
            stream = ytdlp_backend.resolve_stream(
                "https://example.invalid/video", audio_only=True)

        self.assertEqual(stream, "https://media.example/fallback.mp4")
        self.assertEqual(call_count[0], 2)

    def test_format_fallback_passes_through_other_errors(self):
        class _ErrorDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                raise yt_dlp.utils.ExtractorError("Video unavailable")

        with mock.patch.object(
                ytdlp_backend.yt_dlp, "YoutubeDL", _ErrorDL):
            with self.assertRaises(yt_dlp.utils.ExtractorError):
                ytdlp_backend.resolve_stream(
                    "https://example.invalid/video", audio_only=True)


if __name__ == "__main__":
    unittest.main()
