# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Every search provider must answer with at least 200 results.

A shallow search reads as a broken app, so each backend's request size and
the cap on ranked results are pinned to 200. These tests fail loudly if any
cap is ever turned back down.
"""

import logging
import unittest
from unittest import mock

with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl import (
        adult_backend,
        annas_backend,
        archive_backend,
        audiobook_backend,
        book_backend,
        deezer_backend,
        musicdl_backend,
        torrent_backend,
        ytdlp_backend,
    )


class ResultLimitFloorTests(unittest.TestCase):
    def test_ranked_result_caps_are_at_least_200(self):
        floors = {
            "books": book_backend.MAX_RESULTS_PER_SOURCE,
            "audiobooks": audiobook_backend.MAX_RESULTS_PER_SOURCE,
            "archive": archive_backend.MAX_RESULTS_PER_SOURCE,
            "torrents": torrent_backend.MAX_RESULTS_PER_SOURCE,
            "adult": adult_backend.MAX_RESULTS_PER_SITE,
            "annas": annas_backend.SEARCH_ROWS,
        }
        for name, cap in floors.items():
            self.assertGreaterEqual(cap, 200, name)

    def test_request_sizes_are_at_least_200(self):
        sizes = {
            "books": book_backend.SEARCH_ROWS,
            "audiobooks": audiobook_backend.SEARCH_ROWS,
            "archive": archive_backend.SEARCH_ROWS,
            "torrents": torrent_backend.SEARCH_ROWS,
            "deezer": deezer_backend._SEARCH_TARGET,
            "ytdlp": ytdlp_backend.SEARCH_COUNT,
            "musicdl": musicdl_backend.SEARCH_SIZE_PER_SOURCE,
        }
        for name, size in sizes.items():
            self.assertGreaterEqual(size, 200, name)


if __name__ == "__main__":
    unittest.main()
