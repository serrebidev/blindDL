# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Torrent indexers, and the Internet Archive's torrents in particular."""

import unittest
from unittest import mock

from blinddl import search_order, torrent_backend

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

    def test_recent_order_is_sent_to_the_archive(self):
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.DOCS)
            torrent_backend.search_archive(
                "dragnet", order=search_order.ORDER_RECENT)

        self.assertEqual(
            http.return_value.get.call_args.kwargs["params"]["sort[]"],
            "publicdate desc")

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

    def test_recent_ranking_uses_posted_time_and_leaves_unknown_last(self):
        rows = [
            torrent_backend._item(
                "Test", "a", "Dragnet older", seeders=100, posted=10),
            torrent_backend._item(
                "Test", "b", "Dragnet newest", seeders=1, posted=20),
            torrent_backend._item(
                "Test", "c", "Dragnet unknown", seeders=1000, posted=0),
        ]

        ranked = torrent_backend._rank(
            rows, "dragnet", order=search_order.ORDER_RECENT)

        self.assertEqual(
            [row["title"] for row in ranked],
            ["Dragnet newest", "Dragnet older", "Dragnet unknown"])

    def test_native_popularity_order_is_not_replaced_by_swarm_size(self):
        rows = [
            torrent_backend._item(
                torrent_backend.SOURCE_ARCHIVE, "a", "Most downloaded",
                seeders=1),
            torrent_backend._item(
                torrent_backend.SOURCE_ARCHIVE, "b", "Second downloaded",
                seeders=1),
        ]

        ranked = torrent_backend._rank(
            rows, "downloaded", order=search_order.ORDER_POPULAR)

        self.assertEqual(
            [row["title"] for row in ranked],
            ["Most downloaded", "Second downloaded"])

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


class eBookeloTests(unittest.TestCase):
    CARD = (
        '<div class="bookCard">'
        '<div>'
        '<script src="x"></script>'
        '<img src="/images/cover/t/3669.jpg" />'
        '<a href="/ebook/3669/the-hobbit" v-on:click="routeGo">'
        '<h3 class="title">The Hobbit</h3>'
        '<span class="autor">J. R. R. Tolkien</span>'
        '<div class="idioma">'
        '<span class="flag ingl"></span>'
        '<span>Inglés</span>'
        '</div>'
        '</a>'
        '</div>'
        '</div>'
    )
    PAGE = (
        '<h1 class="listHeader">Resultados para: hobbit</h1>'
        + CARD
        + '<div class="bookCard"><div>'
        '<a href="/ebook/1370/el-hobbit"><h3 class="title">El Hobbit</h3>'
        '<span class="autor">J. R. R. Tolkien</span>'
        '<div class="idioma"><span class="flag espa"></span>'
        '<span>Español</span></div></a></div></div>'
    )
    MAGNET_PAGE = (
        '<form id="demagnetize">'
        '<input type="hidden" name="magnet" value="magnet:?xt=urn:btih:abc&amp;dn=The+Hobbit" />'
        '</form>'
    )

    def _search(self):
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(text=self.PAGE)
            rows = torrent_backend.search_ebookelo("hobbit")
            url = http.return_value.get.call_args.args[0]
        return rows, url

    def test_search_asks_for_the_query_on_the_first_page(self):
        _rows, url = self._search()
        self.assertIn("/search/hobbit/page/1", url)

    def test_each_card_becomes_one_torrent_row(self):
        rows, _url = self._search()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "The Hobbit")
        self.assertEqual(rows[0]["source"], torrent_backend.SOURCE_EBOOKELO)
        self.assertEqual(rows[0]["identifier"], "3669")

    def test_the_language_is_read_from_the_flag(self):
        rows, _url = self._search()

        # The site writes "Inglés"; blindDL says it the reader's way.
        self.assertEqual(rows[0]["format"], "English")
        self.assertEqual(rows[1]["format"], "Spanish")

    def test_rows_carry_the_book_and_magnet_pages(self):
        rows, _url = self._search()

        self.assertEqual(rows[0]["url"],
                         "https://ww2.ebookelo.com/ebook/3669/the-hobbit")
        self.assertEqual(
            rows[0]["download_url"],
            "https://ww2.ebookelo.com/download/3669/magnet")

    def test_no_magnet_until_the_download_page_is_asked(self):
        # The hash is not in the search listing, so the row must not pretend
        # to carry one; resolve_magnet fetches it on first download.
        rows, _url = self._search()

        self.assertEqual(rows[0]["magnet"], "")
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(text=self.MAGNET_PAGE)
            magnet = torrent_backend.resolve_magnet(rows[0])
        self.assertTrue(magnet.startswith("magnet:?xt=urn:btih:abc"))

    def test_ebookelo_is_an_ordinary_searched_indexer(self):
        self.assertIn(torrent_backend.SOURCE_EBOOKELO,
                      torrent_backend.ALL_SOURCES)
        self.assertIn(torrent_backend.SOURCE_EBOOKELO,
                      torrent_backend._SEARCHERS)
        self.assertIn(torrent_backend.SOURCE_EBOOKELO,
                      torrent_backend.all_sources())
        self.assertNotIn(
            torrent_backend.SOURCE_EBOOKELO,
            torrent_backend.enabled_sources([torrent_backend.SOURCE_EBOOKELO]))


class AudiobookBayTests(unittest.TestCase):
    POST = (
        '<div class="post"><div class="postTitle">'
        '<h2><a href="/abss/the-hobbit-j-r-r-tolkien/" rel="bookmark">'
        'The Hobbit - J. R. R. Tolkien</a></h2></div>'
        '<div class="postInfo">Category: Children&nbsp; Fantasy&nbsp; <br />'
        'Language: English<span style="margin-left:100px;">Keywords: Retail'
        '</span><br /></div>'
        '<div class="postContent">'
        "<p style='text-align:center;'>Posted: 21 Feb 2026<br />"
        "Format: <span style='color:#a00;'>M4B</span> / Bitrate: "
        "<span style='color:#a00;'>64 Kbps</span><br />"
        "File Size: <span style='color:#00f;'>292.27</span> MBs</p>"
        '</div><div class="postMeta">'
        '<span class="postLink"><a href="/abss/the-hobbit-j-r-r-tolkien/">'
        'Audiobook Details</a></span>'
        '</div></div>'
    )
    DETAIL = (
        '<table><tr><td>Info Hash:</td>'
        '<td>7bb911895886da54b4db91b710bae80b7c943f42</td></tr>'
        '<tr><td>Tracker:</td><td>udp://tracker.opentrackr.org:1337/announce</td>'
        '</tr><tr><td>Tracker:</td><td>udp://open.demonii.com:1337/announce</td>'
        '</tr></table>'
    )

    def _search(self):
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(text=self.POST)
            rows = torrent_backend.search_audiobookbay("hobbit")
            params = http.return_value.get.call_args.kwargs["params"]
        return rows, params

    def test_search_passes_the_query_as_the_site_wants_it(self):
        _rows, params = self._search()
        self.assertEqual(params, {"s": "hobbit"})

    def test_each_post_becomes_one_torrent_row(self):
        rows, _params = self._search()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "The Hobbit - J. R. R. Tolkien")
        self.assertEqual(rows[0]["source"],
                         torrent_backend.SOURCE_AUDIOBOOKBAY)

    def test_language_and_format_lead_the_format_column(self):
        rows, _params = self._search()
        self.assertEqual(rows[0]["format"], "English · M4B")

    def test_file_size_is_read_from_the_post(self):
        rows, _params = self._search()
        self.assertEqual(rows[0]["file_size"], "292.3 MB")

    def test_rows_point_at_the_audiobook_page(self):
        rows, _params = self._search()
        self.assertEqual(
            rows[0]["url"],
            "https://audiobookbay.lu/abss/the-hobbit-j-r-r-tolkien/")

    def test_magnet_is_built_from_the_detail_pages_hash_and_trackers(self):
        rows, _params = self._search()
        with mock.patch.object(torrent_backend, "_http") as http:
            http.return_value.get.return_value = _Response(text=self.DETAIL)
            magnet = torrent_backend.resolve_magnet(rows[0])
        self.assertTrue(magnet.startswith(
            "magnet:?xt=urn:btih:7bb911895886da54b4db91b710bae80b7c943f42"))
        self.assertIn("tracker.opentrackr.org", magnet)

    def test_audiobook_bay_is_an_ordinary_searched_indexer(self):
        self.assertIn(torrent_backend.SOURCE_AUDIOBOOKBAY,
                      torrent_backend.ALL_SOURCES)
        self.assertIn(torrent_backend.SOURCE_AUDIOBOOKBAY,
                      torrent_backend._SEARCHERS)
        self.assertIn(
            torrent_backend.SOURCE_AUDIOBOOKBAY,
            torrent_backend.all_sources())
        self.assertNotIn(
            torrent_backend.SOURCE_AUDIOBOOKBAY,
            torrent_backend.enabled_sources(
                [torrent_backend.SOURCE_AUDIOBOOKBAY]))


if __name__ == "__main__":
    unittest.main()
