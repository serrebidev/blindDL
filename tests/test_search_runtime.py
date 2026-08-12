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
    def test_search_starts_every_provider_before_the_deadline(self):
        total = 24
        state = {"active": 0, "maximum": 0, "lock": threading.Lock()}
        release = threading.Event()
        stop = threading.Event()
        clients = {}
        for index in range(total):
            source = f"Test{index:02d}MusicClient"
            clients[source] = _BlockingClient(source, state, release)

        with mock.patch.object(musicdl_backend, "_clients", clients):
            _items, _answered, asked = musicdl_backend.search(
                "query", timeout_s=0.2, stop=stop)
            stop.set()
            release.set()

        deadline = time.monotonic() + 2
        while (any(t.name.startswith("search-Test")
                   for t in threading.enumerate()) and
               time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertEqual(len(asked), total)
        self.assertEqual(state["maximum"], total)
        self.assertFalse(any(t.name.startswith("search-Test")
                             for t in threading.enumerate()))

    def test_first_use_client_setup_counts_toward_the_deadline(self):
        state = {"active": 0, "maximum": 0, "lock": threading.Lock()}
        release = threading.Event()
        client = _BlockingClient("TestMusicClient", state, release)

        def build_clients():
            time.sleep(0.15)
            return {"TestMusicClient": client}

        started = time.monotonic()
        try:
            with mock.patch.object(
                musicdl_backend, "_get_clients", side_effect=build_clients
            ):
                musicdl_backend.search("query", timeout_s=0.2)
            elapsed = time.monotonic() - started
        finally:
            release.set()

        deadline = time.monotonic() + 2
        while (any(t.name == "search-TestMusicClient"
                   for t in threading.enumerate()) and
               time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertLess(elapsed, 0.3)
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
