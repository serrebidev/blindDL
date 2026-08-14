# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import logging
import sys
import threading
import time
import unittest
from unittest import mock

# musicdl creates a file logger at import time. Keep tests self-contained.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl import musicdl_backend
from blinddl.__main__ import _flush_standard_streams


class _BlockingClient:
    def __init__(self, source, state, release):
        self.source = source
        self.state = state
        self.release = release

    def search(self, keyword):
        with self.state["lock"]:
            self.state["started"] = self.state.get("started", 0) + 1
            self.state["active"] += 1
            self.state["maximum"] = max(
                self.state["maximum"], self.state["active"])
        try:
            self.release.wait(2)
            return {self.source: []}
        finally:
            with self.state["lock"]:
                self.state["active"] -= 1


class SearchConcurrencyTests(unittest.TestCase):
    def test_search_starts_every_provider_concurrently(self):
        total = 24
        state = {"active": 0, "maximum": 0, "lock": threading.Lock()}
        release = threading.Event()
        clients = {}
        for index in range(total):
            source = f"Test{index:02d}MusicClient"
            clients[source] = _BlockingClient(source, state, release)

        finished = threading.Event()

        def run_search():
            try:
                musicdl_backend.search("query", timeout_s=1)
            finally:
                finished.set()

        with mock.patch.object(musicdl_backend, "_clients", clients):
            worker = threading.Thread(target=run_search, daemon=True)
            worker.start()
            deadline = time.monotonic() + 1
            while state["active"] < total and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(state["active"], total)
            self.assertEqual(state["maximum"], total)
            release.set()
            worker.join(2)

        self.assertTrue(finished.is_set())

    def test_search_starts_every_provider_before_the_deadline(self):
        total = 24
        state = {
            "active": 0,
            "maximum": 0,
            "started": 0,
            "lock": threading.Lock(),
        }
        release = threading.Event()
        stop = threading.Event()
        clients = {}
        for index in range(total):
            source = f"Test{index:02d}MusicClient"
            clients[source] = _BlockingClient(source, state, release)

        with mock.patch.object(musicdl_backend, "_clients", clients):
            _items, _answered, asked = musicdl_backend.search(
                "query", timeout_s=2, stop=stop)
            stop.set()
            release.set()

        deadline = time.monotonic() + 2
        while (any(t.name.startswith("search-Test")
                   for t in threading.enumerate()) and
               time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertEqual(len(asked), total)
        self.assertEqual(state["started"], total)
        self.assertFalse(any(t.name.startswith("search-Test")
                             for t in threading.enumerate()))

    def test_search_calls_the_provider_without_the_wrapper_executor(self):
        source = "TestMusicClient"
        provider = mock.Mock()
        provider.search.return_value = []
        client = mock.Mock()
        client.music_clients = {source: provider}
        client.requests_overrides = {source: {"headers": {"X-Test": "1"}}}
        client.search_rules = {source: {"kind": "song"}}

        with mock.patch.object(musicdl_backend, "_clients", {source: client}):
            musicdl_backend.search("query", timeout_s=1)

        provider.search.assert_called_once_with(
            keyword="query",
            num_threadings=musicdl_backend.SOURCE_SEARCH_THREADS,
            request_overrides={"headers": {"X-Test": "1"}},
            rule={"kind": "song"},
        )
        client.search.assert_not_called()

    def test_first_use_client_setup_counts_toward_the_deadline(self):
        state = {"active": 0, "maximum": 0, "lock": threading.Lock()}
        release = threading.Event()
        client = _BlockingClient("TestMusicClient", state, release)

        # Building the clients takes most of the deadline, so a search that
        # counts it finishes at about the 0.8s deadline and one that starts
        # the clock afterwards runs to about 1.4s. The two are three tenths
        # of a second either side of the bound below rather than the fiftieth
        # they were at a tenth of these timings, which is what left a busy CI
        # runner able to fail a correct search on scheduling jitter alone.
        def build_clients():
            time.sleep(0.6)
            return {"TestMusicClient": client}

        started = time.monotonic()
        try:
            with mock.patch.object(
                musicdl_backend, "_get_clients", side_effect=build_clients
            ):
                musicdl_backend.search("query", timeout_s=0.8)
            elapsed = time.monotonic() - started
        finally:
            release.set()

        deadline = time.monotonic() + 2
        while (any(t.name == "search-TestMusicClient"
                   for t in threading.enumerate()) and
               time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertLess(elapsed, 1.1)
        self.assertFalse(any(t.name == "search-TestMusicClient"
                             for t in threading.enumerate()))

    def test_musicdl_clients_limit_nested_search_workers(self):
        client = object()
        with (mock.patch.object(musicdl_backend, "_clients", None),
              mock.patch.object(musicdl_backend, "ALL_SOURCES",
                                ["TestMusicClient"]),
              mock.patch.object(musicdl_backend, "cache_dir",
                                return_value="cache"),
              mock.patch.object(musicdl_backend, "_silence_progress_bars"),
              mock.patch.object(musicdl_backend, "MusicClient",
                                return_value=client) as constructor):
            self.assertEqual(
                musicdl_backend._get_clients(), {"TestMusicClient": client})

        self.assertEqual(
            constructor.call_args.kwargs["clients_threadings"],
            {"TestMusicClient": musicdl_backend.SOURCE_SEARCH_THREADS},
        )


class _StubProvider:
    """A musicdl source, down to the two methods blindDL wraps."""

    def __init__(self, pages=80):
        self.pages = pages
        self.searched = []

    def _constructsearchurls(self, keyword="", rule=None, request_overrides=None):
        return [f"https://example.invalid/search?page={index}"
                for index in range(self.pages)]

    def _search(self, keyword="", search_url="", request_overrides=None,
                song_infos=None, progress=None):
        self.searched.append(search_url)
        return "searched"


class SearchAmplificationTests(unittest.TestCase):
    def test_a_source_is_asked_for_a_bounded_number_of_pages(self):
        # A source that clamps its own page size answers a request for 200
        # songs with twenty pages, not with fewer songs, and every page
        # costs further round trips per song. Two sources built eighty each.
        provider = _StubProvider()

        musicdl_backend._cap_search_pages(provider)
        urls = provider._constructsearchurls(keyword="query")

        self.assertEqual(len(urls),
                         musicdl_backend.MAX_SEARCH_PAGES_PER_SOURCE)

    def test_a_source_that_answers_in_one_page_is_left_alone(self):
        provider = _StubProvider(pages=1)

        musicdl_backend._cap_search_pages(provider)

        self.assertEqual(len(provider._constructsearchurls(keyword="q")), 1)

    def test_capping_a_source_twice_does_not_stack_wrappers(self):
        provider = _StubProvider()

        musicdl_backend._cap_search_pages(provider)
        wrapped = provider._constructsearchurls
        musicdl_backend._cap_search_pages(provider)

        self.assertIs(provider._constructsearchurls, wrapped)

    def test_a_superseded_search_stops_between_pages(self):
        # musicdl has no cancel token, so a search the user had replaced ran
        # every page it had lined up before its results were thrown away.
        provider = _StubProvider()
        musicdl_backend._make_cancellable(provider)
        stop = threading.Event()

        try:
            musicdl_backend._cancel.stop = stop
            provider._search(search_url="first")
            stop.set()
            provider._search(search_url="second")
        finally:
            musicdl_backend._cancel.stop = None

        self.assertEqual(provider.searched, ["first"])

    def test_a_page_still_runs_when_nothing_asked_for_a_stop(self):
        provider = _StubProvider()
        musicdl_backend._make_cancellable(provider)

        self.assertEqual(provider._search(search_url="only"), "searched")
        self.assertEqual(provider.searched, ["only"])

    def test_searches_share_one_pool_instead_of_stacking_threads(self):
        pool = musicdl_backend._search_pool()

        self.assertIs(musicdl_backend._search_pool(), pool)
        self.assertGreaterEqual(pool._max_workers,
                                len(musicdl_backend.ALL_SOURCES))


class RuntimeShutdownTests(unittest.TestCase):
    def test_flush_tolerates_windowed_and_closed_streams(self):
        broken = mock.Mock()
        broken.flush.side_effect = OSError("stream is closed")
        with (mock.patch.object(sys, "stdout", None),
              mock.patch.object(sys, "stderr", broken)):
            _flush_standard_streams()
        broken.flush.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
