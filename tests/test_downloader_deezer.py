# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import logging
import tempfile
import unittest
from unittest import mock

from blinddl import deezer_backend, ytdlp_backend

# musicdl creates its global file logger while blinddl.downloader imports it.
# Tests run in a restricted workspace, so replace only that import-time handler
# with an in-memory one instead of touching the user's %LOCALAPPDATA% log.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl.downloader import DownloadItem, DownloadQueue


class DownloadQueueDeezerTests(unittest.TestCase):
    def make_queue(self, out_dir):
        queue = object.__new__(DownloadQueue)
        queue.config = {
            "deezer_arl": "test-arl",
            "download_dir": out_dir,
            "audio_format": "mp3",
            "sideb_lyrics": True,
        }
        queue.notify = None
        return queue

    def make_item(self):
        return DownloadItem(
            "Test track", "sideb", "https://www.deezer.com/track/3135556")

    def test_arl_routes_sideb_item_to_native_deezer(self):
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            with mock.patch(
                    "blinddl.downloader.deezer_backend.download") as native, \
                    mock.patch(
                        "blinddl.downloader.sideb_backend.download") as sideb:
                queue._run_sideb(self.make_item())

        native.assert_called_once()
        sideb.assert_not_called()

    def test_quality_failure_falls_back_to_sideb(self):
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            with mock.patch(
                    "blinddl.downloader.deezer_backend.download",
                    side_effect=deezer_backend.DeezerQualityError("quality")), \
                    mock.patch(
                        "blinddl.downloader.sideb_backend.download") as sideb:
                queue._run_sideb(self.make_item())

        sideb.assert_called_once()

    def test_a_track_youtube_will_not_serve_falls_back_to_deezer_at_128(self):
        # Deezer publishes whole soundtrack albums at 128 and nothing above
        # it. Those tracks used to fail outright: too low for the configured
        # quality, so the download went to YouTube, where the connection was
        # refused -- while the same track played perfectly off Deezer.
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            attempts = []

            def native(*args, **kwargs):
                attempts.append(kwargs.get("low_quality", False))
                if not kwargs.get("low_quality"):
                    raise deezer_backend.DeezerQualityError("no FLAC")
                return "C:/downloads/track.mp3"

            with mock.patch(
                    "blinddl.downloader.deezer_backend.download",
                    side_effect=native),                     mock.patch(
                        "blinddl.downloader.sideb_backend.download",
                        side_effect=OSError("connection reset")):
                path = queue._run_sideb(self.make_item())

        self.assertEqual(path, "C:/downloads/track.mp3")
        # The configured quality is still asked for first, and 128 is only
        # ever reached after YouTube has also been tried and failed.
        self.assertEqual(attempts, [False, True])

    def test_a_track_nothing_can_serve_reports_what_side_b_said(self):
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            with mock.patch(
                    "blinddl.downloader.deezer_backend.download",
                    side_effect=deezer_backend.DeezerQualityError("no FLAC")),                     mock.patch(
                        "blinddl.downloader.sideb_backend.download",
                        side_effect=OSError("connection reset")):
                with self.assertRaisesRegex(OSError, "connection reset"):
                    queue._run_sideb(self.make_item())

    def test_a_cancelled_side_b_download_is_not_retried_at_128(self):
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            with mock.patch(
                    "blinddl.downloader.deezer_backend.download",
                    side_effect=deezer_backend.DeezerQualityError("no FLAC")
            ) as native,                     mock.patch(
                        "blinddl.downloader.sideb_backend.download",
                        side_effect=ytdlp_backend.DownloadCancelled()):
                with self.assertRaises(ytdlp_backend.DownloadCancelled):
                    queue._run_sideb(self.make_item())

        self.assertEqual(native.call_count, 1)

    def test_invalid_arl_error_does_not_fail_silently(self):
        with tempfile.TemporaryDirectory() as out_dir:
            queue = self.make_queue(out_dir)
            with mock.patch(
                    "blinddl.downloader.deezer_backend.download",
                    side_effect=RuntimeError("invalid ARL")), \
                    mock.patch(
                        "blinddl.downloader.sideb_backend.download") as sideb:
                with self.assertRaisesRegex(RuntimeError, "invalid ARL"):
                    queue._run_sideb(self.make_item())

        sideb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
