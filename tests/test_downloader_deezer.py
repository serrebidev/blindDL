# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import logging
import tempfile
import unittest
from unittest import mock

from blinddl import deezer_backend

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
