# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The download queue: what it keeps, what it refuses, what survives a restart."""

import os
import tempfile
import unittest
from unittest import mock

from blinddl.saved_queue import SavedQueue


class _SongInfo:
    """Stands in for musicdl's SongInfo, which is a row's whole payload."""

    def __init__(self, data):
        self.data = dict(data)

    def todict(self):
        return dict(self.data)

    @classmethod
    def fromdict(cls, data):
        return cls(data)


class SavedQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "download-queue.json")

    def tearDown(self):
        self.directory.cleanup()

    def _queue(self):
        return SavedQueue(state_path=self.path)

    def test_a_result_is_kept_and_read_back_after_a_restart(self):
        store = self._queue()
        result = {
            "id": "deezer:1",
            "kind": "deezer",
            "title": "One More Time",
            "artist": "Daft Punk",
            "url": "https://www.deezer.com/track/1",
        }
        self.assertTrue(store.add(result, 19, folder="Daft Punk"))

        reopened = self._queue()
        entries = reopened.all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["engine"], 19)
        self.assertEqual(entries[0]["folder"], "Daft Punk")
        self.assertEqual(reopened.result_of(entries[0])["title"],
                         "One More Time")

    def test_the_same_result_is_only_kept_once(self):
        store = self._queue()
        result = {"id": "deezer:1", "title": "One", "url": "u"}
        self.assertTrue(store.add(result, 19))
        # Found again in the next search, with a different row order and a
        # duration the first one did not carry: still the same track.
        self.assertFalse(store.add({**result, "duration_s": 320}, 19))
        self.assertEqual(len(store.all()), 1)

    def test_rows_without_an_id_are_told_apart_by_what_they_say(self):
        store = self._queue()
        self.assertTrue(store.add(
            {"title": "Track", "artist": "A", "source": "Site"}, 0))
        self.assertFalse(store.add(
            {"title": "track", "artist": "a", "source": "site"}, 0))
        self.assertTrue(store.add(
            {"title": "Track", "artist": "B", "source": "Site"}, 0))
        self.assertEqual(len(store.all()), 2)

    def test_a_musicdl_row_keeps_the_payload_it_downloads_from(self):
        # Dropping song_info would leave a row that plays and cannot be
        # fetched, which is the one shape this list must never take.
        store = self._queue()
        store.add({"title": "Song", "source": "Site",
                   "song_info": _SongInfo({"songid": "42"})}, 0)

        entry = self._queue().all()[0]
        with mock.patch.dict(
            "sys.modules",
            {"musicdl": mock.Mock(),
             "musicdl.modules": mock.Mock(),
             "musicdl.modules.utils": mock.Mock(),
             "musicdl.modules.utils.data": mock.Mock(
                 SongInfo=_SongInfo)},
        ):
            restored = SavedQueue(state_path=self.path).result_of(entry)
        self.assertEqual(restored["song_info"].data, {"songid": "42"})

    def test_a_field_that_cannot_be_written_loses_the_field_not_the_row(self):
        store = self._queue()
        store.add({"id": "x", "title": "Keep me", "handle": object()}, 0)
        entry = self._queue().all()[0]
        self.assertEqual(entry["result"]["title"], "Keep me")
        self.assertNotIn("handle", entry["result"])

    def test_bytes_survive_the_round_trip(self):
        store = self._queue()
        store.add({"id": "x", "title": "T", "cover": b"\x00\x01\x02"}, 0)
        entry = self._queue().all()[0]
        self.assertEqual(
            self._queue().result_of(entry)["cover"], b"\x00\x01\x02")

    def test_removing_and_emptying(self):
        store = self._queue()
        for number in range(3):
            store.add({"id": str(number), "title": f"T{number}"}, 0)
        keys = [entry["key"] for entry in store.all()]

        self.assertEqual(store.remove(keys[:2]), 2)
        self.assertEqual([e["key"] for e in self._queue().all()], keys[2:])
        self.assertEqual(store.clear(), 1)
        self.assertEqual(self._queue().all(), [])

    def test_a_corrupt_file_reads_as_an_empty_queue(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(self._queue().all(), [])

    def test_a_queue_with_no_file_never_writes_one(self):
        store = SavedQueue(state_path="")
        self.assertTrue(store.add({"id": "x", "title": "T"}, 0))
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(os.listdir(self.directory.name), [])


if __name__ == "__main__":
    unittest.main()
