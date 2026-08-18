# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""URL-resolution tests for preview.resolve_search_result and friends."""

import unittest
from unittest import mock

from blinddl import preview


class AppleMusicPreviewTests(unittest.TestCase):
    def test_applemusic_row_uses_its_own_preview_url(self):
        item = {
            "kind": "applemusic",
            "title": "Creep (Acoustic)",
            "url": "https://music.apple.com/us/album/123?i=456",
            "preview_url": "https://audio-ssl.itunes.apple.com/itunes-assets/"
                           "AudioPreview126/v4/abc/def.m4a",
        }
        stream, title = preview.resolve_search_result(
            item, audio_only=True, config={})
        self.assertEqual(
            stream, "https://audio-ssl.itunes.apple.com/itunes-assets/"
            "AudioPreview126/v4/abc/def.m4a")
        self.assertEqual(title, "Creep (Acoustic)")

    def test_applemusic_row_without_preview_falls_back_to_ytdlp(self):
        item = {
            "kind": "applemusic",
            "title": "Creep",
            "url": "https://music.apple.com/us/album/123?i=456",
            "preview_url": "",
        }
        with mock.patch.object(
                preview.ytdlp_backend, "resolve_stream",
                return_value="https://youtube.example.com/stream") as resolve:
            stream, _ = preview.resolve_search_result(
                item, audio_only=True, config={})
        resolve.assert_called_once_with(
            "https://music.apple.com/us/album/123?i=456",
            audio_only=True, cookies_from_browser=None, cookies_file=None,
            fix_stream=None)
        self.assertEqual(stream, "https://youtube.example.com/stream")

    def test_musicdl_row_uses_song_info_download_url(self):
        song_info = mock.Mock(download_url="https://cdn.example.com/track.mp3")
        item = {"kind": "music", "title": "Creep", "song_info": song_info}
        stream, _ = preview.resolve_search_result(
            item, audio_only=True, config={})
        self.assertEqual(stream, "https://cdn.example.com/track.mp3")


if __name__ == "__main__":
    unittest.main()

def test_bandcamp_doubled_url_normalized(monkeypatch):
    """Bandcamp's fuzzysearch API doubled-URL regression (2026-08): a result
    URL like ``https://host.bandcamp.comhttps://host.bandcamp.com/…`` must be
    repaired so preview/download see one well-formed URL."""
    from blinddl import bandcamp_backend

    doubled = ("https://djwarrentrack.bandcamp.comhttps://djwarrentrack.bandcamp.com"
               "/album/radiohead-creep-3-versions")
    data = {"results": [{"type": "t", "id": 1, "name": "Creep",
                         "url": doubled, "band_name": "Radiohead"}]}

    def fake_api_get(*args, **kwargs):
        return data

    monkeypatch.setattr(bandcamp_backend, "_api_get", fake_api_get)
    items = bandcamp_backend.search("radiohead creep")
    assert items and items[0]["url"] == (
        "https://djwarrentrack.bandcamp.com/album/radiohead-creep-3-versions")
