# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Apple Music search through iTunes' public, credential-free Search API."""

import unittest
from unittest import mock

from blinddl import applemusic_backend


def _track(track_id, name, url=None, artist="Artist", album="Album",
           millis=180000):
    if url is None:
        url = f"https://music.apple.com/us/song/{track_id}"
    return {
        "trackId": track_id,
        "trackName": name,
        "artistName": artist,
        "collectionName": album,
        "trackTimeMillis": millis,
        "trackViewUrl": url,
    }


class AppleMusicSearchTests(unittest.TestCase):
    def test_search_returns_normalized_tracks(self):
        payload = {"results": [_track(1, "One"), _track(2, "Two")]}
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response) as get:
            items = applemusic_backend.search("query")

        self.assertEqual(get.call_args.kwargs["params"]["limit"], 200)
        self.assertEqual(get.call_args.kwargs["params"]["entity"], "song")
        self.assertEqual([item["title"] for item in items], ["One", "Two"])
        self.assertEqual(items[0]["kind"], "applemusic")
        self.assertEqual(items[0]["artist"], "Artist")
        self.assertEqual(items[0]["duration_s"], 180)

    def test_search_skips_tracks_without_a_link(self):
        payload = {"results": [_track(1, "One", url=""),
                               _track(2, "Two")]}
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response):
            items = applemusic_backend.search("query")

        self.assertEqual([item["title"] for item in items], ["Two"])

    def test_search_returns_empty_on_error(self):
        error = applemusic_backend.requests.exceptions.ConnectionError(
            "network down"
        )
        with mock.patch.object(applemusic_backend.requests, "get",
                               side_effect=error):
            self.assertEqual(applemusic_backend.search("query"), [])


if __name__ == "__main__":
    unittest.main()
