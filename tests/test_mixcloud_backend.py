# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Mixcloud search: what a cloudcast becomes, and how deep a search goes."""

import unittest
from unittest import mock

import requests

from blinddl import mixcloud_backend, search_order


def _cloudcast(number, name=None, user="A Host", length=3600):
    return {
        "key": f"/host/show-{number}/",
        "url": f"https://www.mixcloud.com/host/show-{number}/",
        "name": name or f"Show {number}",
        "user": {"name": user, "username": "host"},
        "audio_length": length,
    }


def _page(cloudcasts):
    return {"data": list(cloudcasts)}


class _Session:
    """Stands in for requests.Session, answering with canned pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.headers = {}
        self.requests = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = (
            self.pages.pop(0) if self.pages else _page([]))
        return response

    def close(self):
        self.closed = True


class MixcloudItemTests(unittest.TestCase):
    def test_a_cloudcast_becomes_a_row_the_results_list_can_read(self):
        item = mixcloud_backend._item(_cloudcast(1, "Late Night Set"))
        self.assertEqual(item["kind"], "mixcloud")
        self.assertEqual(item["title"], "Late Night Set")
        # A mix is credited to whoever put it together, not to an artist.
        self.assertEqual(item["artist"], "A Host")
        self.assertEqual(item["source"], "Mixcloud")
        self.assertEqual(item["duration_s"], 3600)
        self.assertEqual(item["url"], "https://www.mixcloud.com/host/show-1/")

    def test_a_host_with_no_display_name_falls_back_to_the_username(self):
        raw = _cloudcast(1)
        raw["user"] = {"username": "host"}
        self.assertEqual(mixcloud_backend._item(raw)["artist"], "host")

    def test_a_length_the_api_left_out_reads_as_unknown_not_as_a_crash(self):
        raw = _cloudcast(1)
        raw["audio_length"] = None
        self.assertEqual(mixcloud_backend._item(raw)["duration_s"], 0)


class MixcloudSearchTests(unittest.TestCase):
    def _search(self, pages, **kwargs):
        session = _Session(pages)
        with mock.patch.object(
            mixcloud_backend.requests, "Session", return_value=session
        ):
            items = mixcloud_backend.search("house", **kwargs)
        return items, session

    def test_a_search_pages_until_it_has_a_full_answer(self):
        # One page is 100 rows and every other blindDL provider answers with
        # 200, so a shallow search here would read as a broken source.
        first = _page([_cloudcast(n) for n in range(100)])
        second = _page([_cloudcast(n) for n in range(100, 200)])
        items, session = self._search([first, second])

        self.assertEqual(len(items), 200)
        self.assertEqual(len(session.requests), 2)
        # Paged by an offset blindDL works out itself. The API omits its own
        # "next" link from the first page of plenty of queries even though
        # the offset behind it answers perfectly, and following that link
        # alone capped those searches at one page.
        self.assertEqual(
            [request[1]["offset"] for request in session.requests], [0, 100])
        self.assertTrue(session.closed)

    def test_a_page_of_shows_already_listed_ends_the_search(self):
        # Consecutive Mixcloud pages overlap. A page that is entirely
        # repeats means the catalogue has stopped moving, and asking for
        # another four would just be four more round trips of the same.
        repeat = [_cloudcast(n) for n in range(100)]
        items, session = self._search([_page(repeat), _page(repeat)])
        self.assertEqual(len(items), 100)
        self.assertEqual(len(session.requests), 2)

    def test_a_search_stops_when_the_catalogue_runs_out(self):
        items, session = self._search([_page([_cloudcast(n) for n in range(7)])])
        self.assertEqual(len(items), 7)
        self.assertEqual(len(session.requests), 1)

    def test_the_same_show_twice_is_listed_once(self):
        page = _page([_cloudcast(1), _cloudcast(1), _cloudcast(2)])
        items, _session = self._search([page])
        self.assertEqual([item["title"] for item in items], ["Show 1", "Show 2"])

    def test_a_show_with_no_url_is_dropped(self):
        broken = _cloudcast(1)
        broken["url"] = ""
        items, _session = self._search([_page([broken, _cloudcast(2)])])
        self.assertEqual([item["title"] for item in items], ["Show 2"])

    def test_a_page_that_fails_keeps_the_pages_that_worked(self):
        # Half an answer is worth more than an error nobody can act on.
        session = _Session([_page([_cloudcast(n) for n in range(100)])])
        calls = {"n": 0}
        real_get = session.get

        def flaky(url, params=None, timeout=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise requests.RequestException("mixcloud is down")
            return real_get(url, params=params, timeout=timeout)

        session.get = flaky
        with mock.patch.object(
            mixcloud_backend.requests, "Session", return_value=session
        ):
            items = mixcloud_backend.search("house")
        self.assertEqual(len(items), 100)
        self.assertTrue(session.closed)

    def test_a_search_never_returns_more_than_it_was_asked_for(self):
        page = _page([_cloudcast(n) for n in range(100)])
        items, _session = self._search([page], count=25)
        self.assertEqual(len(items), 25)


class MixcloudOrderTests(unittest.TestCase):
    def test_mixcloud_answers_best_match_and_says_so_for_the_rest(self):
        # The search endpoint takes a query and nothing else. Saying it can
        # sort would mean reordering a page of best matches and presenting
        # the result as the newest of something.
        self.assertTrue(
            mixcloud_backend.supports_order(search_order.ORDER_RELEVANCE))
        self.assertFalse(
            mixcloud_backend.supports_order(search_order.ORDER_RECENT))
        self.assertFalse(
            mixcloud_backend.supports_order(search_order.ORDER_POPULAR))


if __name__ == "__main__":
    unittest.main()
