# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Torrent indexers, and the Internet Archive's torrents in particular."""

import unittest
from unittest import mock

from blinddl import torrent_backend

from tests.test_book_backend import _Response


class ArchiveTorrentTests(unittest.TestCase):
    DOCS = {"response": {"docs": [
        {"identifier": "Dragnet_OTR", "title": "Dragnet the 50's radio show",
         "creator": ["NBC"], "item_size": "1048576"},
        {"identifier": "OTRR_Dragnet_Singles", "title": "Dragnet - Singles",
         "creator": "OTRR", "item_size": "2097152"},
        {"identifier": "", "title": "No identifier at all"},
    ]}}

    def _search(self):
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.DOCS)
            rows = torrent_backend.search_archive("dragnet")
            params = http.return_value.get.call_args.kwargs["params"]
        return rows, params

    def test_only_items_published_as_torrents_are_asked_for(self):
        # Most of the Archive is not a torrent; without this the results are
        # full of items BitTorrent cannot fetch.
        _rows, params = self._search()

        self.assertIn('format:("Archive BitTorrent")', params["q"])
        self.assertIn("dragnet", params["q"])

    def test_punctuation_cannot_break_the_query(self):
        # The Archive answers a malformed query with an empty result set
        # rather than an error, so an unescaped bracket or quote reads as
        # "nothing found" instead of "the search was broken".
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.DOCS)
            torrent_backend.search_archive('Dragnet "Big (Convertible)"')
            query = http.return_value.get.call_args.kwargs["params"]["q"]

        # What is left is only the query's own structure: the wrapper
        # brackets and the quoted format phrase, each balanced.
        self.assertEqual(query, '(Dragnet  Big  Convertible) AND '
                                'format:("Archive BitTorrent")')
        self.assertEqual(query.count("("), query.count(")"))

    def test_a_row_points_at_the_items_own_torrent(self):
        rows, _params = self._search()

        self.assertEqual(rows[0]["source"], torrent_backend.SOURCE_ARCHIVE)
        self.assertEqual(
            rows[0]["download_url"],
            "https://archive.org/download/Dragnet_OTR/"
            "Dragnet_OTR_archive.torrent")
        self.assertEqual(rows[0]["url"],
                         "https://archive.org/details/Dragnet_OTR")
        self.assertEqual(rows[0]["file_size"], "1.0 MB")

    def test_an_archive_row_offers_no_magnet(self):
        # There is no info hash in the search reply, so the .torrent is the
        # only thing that can start the download; a half-built magnet would
        # connect to nothing.
        rows, _params = self._search()

        self.assertEqual(rows[0]["magnet"], "")
        self.assertEqual(torrent_backend.magnet_for(rows[0]), "")

    def test_the_permanent_webseed_counts_as_a_seed(self):
        # Ranking sorts on seeders first. The Archive reports no swarm, but
        # it webseeds every torrent itself, so a zero here would bury rows
        # that always download beneath public torrents that never do.
        rows, _params = self._search()

        self.assertGreaterEqual(rows[0]["seeders"], 1)
        ranked = torrent_backend._rank(list(rows), "dragnet")
        self.assertTrue(ranked)

    def test_a_creator_list_reads_as_one_name(self):
        rows, _params = self._search()

        self.assertEqual(rows[0]["uploader"], "NBC")
        self.assertEqual(rows[1]["uploader"], "OTRR")

    def test_an_item_without_an_identifier_is_skipped(self):
        rows, _params = self._search()

        self.assertEqual(len(rows), 2)
        self.assertNotIn("No identifier at all",
                         [row["title"] for row in rows])

    def test_the_archive_is_searched_like_any_other_indexer(self):
        self.assertIn(torrent_backend.SOURCE_ARCHIVE,
                      torrent_backend.ALL_SOURCES)
        self.assertIn(torrent_backend.SOURCE_ARCHIVE,
                      torrent_backend._SEARCHERS)
        self.assertIn(torrent_backend.SOURCE_ARCHIVE,
                      torrent_backend.all_sources())
        # A user who switches it off must not be searched anyway.
        self.assertNotIn(
            torrent_backend.SOURCE_ARCHIVE,
            torrent_backend.enabled_sources([torrent_backend.SOURCE_ARCHIVE]))


if __name__ == "__main__":
    unittest.main()
