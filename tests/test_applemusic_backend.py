# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Apple Music backend: search, URL resolution, and in-app downloads."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from blinddl import applemusic_backend, search_kind


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


def _response(payload):
    """One requests.get answer: raises nothing, returns *payload*."""
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = payload
    return response


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

    def test_search_types_match_one_itunes_field(self):
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"results": []}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response) as get:
            applemusic_backend.search("query", kind=search_kind.KIND_BEST)
            best = get.call_args.kwargs["params"]
            applemusic_backend.search("query", kind=search_kind.KIND_TRACK)
            track = get.call_args.kwargs["params"]
            applemusic_backend.search(
                "query", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_SONGS)
            artist = get.call_args.kwargs["params"]

        # Best match sends no attribute at all, which is what makes iTunes
        # search every field rather than one of them.
        self.assertNotIn("attribute", best)
        self.assertEqual(track["attribute"], "songTerm")
        self.assertEqual(artist["attribute"], "artistTerm")
        self.assertEqual(artist["entity"], "song")

    def test_artist_scope_albums_matches_the_artist_field(self):
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"results": []}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response) as get:
            applemusic_backend.search(
                "query", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_ALBUMS)

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["entity"], "album")
        self.assertEqual(params["attribute"], "artistTerm")

    def test_artist_scope_playlists_comes_from_the_catalog_api(self):
        api = mock.Mock()
        api.getsearchresults.return_value = {
            "results": {"playlists": {"data": [{
                "id": "pl.1",
                "attributes": {
                    "name": "Daft Punk Essentials",
                    "curatorName": "Apple Music Electronic",
                    "trackCount": 42,
                },
            }]}},
        }
        with mock.patch.object(
            applemusic_backend, "_anonymous_api", return_value=api
        ):
            items = applemusic_backend.search(
                "query", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_PLAYLISTS)

        self.assertEqual(items[0]["kind"], "applemusic_playlist")
        self.assertEqual(items[0]["title"], "Daft Punk Essentials")
        self.assertEqual(items[0]["artist"], "Apple Music Electronic")
        self.assertEqual(items[0]["format"], "Playlist, 42 tracks")
        self.assertEqual(
            items[0]["url"], "https://music.apple.com/us/playlist/pl.1"
        )

    def test_album_search_returns_album_rows(self):
        payload = {"results": [{
            "collectionId": 55,
            "collectionName": "Discovery",
            "artistName": "Daft Punk",
            "trackCount": 14,
            "collectionViewUrl": "https://music.apple.com/us/album/55",
            "artworkUrl100": "https://example.test/100x100bb.jpg",
        }, {
            "collectionId": 56,
            "collectionName": "No link",
        }]}
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response) as get:
            items = applemusic_backend.search(
                "discovery", kind=search_kind.KIND_ALBUM)

        self.assertEqual(get.call_args.kwargs["params"]["entity"], "album")
        self.assertEqual(get.call_args.kwargs["params"]["attribute"],
                         "albumTerm")
        # A release with no page to open cannot be downloaded, so it is left
        # out rather than listed as a row that does nothing.
        self.assertEqual([item["title"] for item in items], ["Discovery"])
        self.assertEqual(items[0]["kind"], "applemusic_album")
        self.assertEqual(items[0]["format"], "Album, 14 tracks")
        self.assertEqual(items[0]["url"],
                         "https://music.apple.com/us/album/55")
        self.assertTrue(applemusic_backend.supports_kind(
            search_kind.KIND_ALBUM))

    def test_search_returns_empty_on_error(self):
        error = applemusic_backend.requests.exceptions.ConnectionError(
            "network down"
        )
        with mock.patch.object(applemusic_backend.requests, "get",
                               side_effect=error):
            self.assertEqual(applemusic_backend.search("query"), [])


class AppleMusicUrlTests(unittest.TestCase):
    def test_parse_song_url(self):
        info = applemusic_backend.parse_apple_url(
            "https://music.apple.com/us/song/123456")
        self.assertEqual(info["media_type"], "song")
        self.assertEqual(info["media_id"], "123456")
        self.assertEqual(info["storefront"], "us")

    def test_parse_album_url_with_slug(self):
        info = applemusic_backend.parse_apple_url(
            "https://music.apple.com/ca/album/the-album/1440915329")
        self.assertEqual(info["media_type"], "album")
        self.assertEqual(info["media_id"], "1440915329")

    def test_parse_playlist_url_with_pl_id(self):
        info = applemusic_backend.parse_apple_url(
            "https://music.apple.com/us/playlist/mix/pl.f4d106fed2bd")
        self.assertEqual(info["media_type"], "playlist")
        self.assertEqual(info["media_id"], "pl.f4d106fed2bd")

    def test_parse_album_song_subid(self):
        info = applemusic_backend.parse_apple_url(
            "https://music.apple.com/us/album/a/1234?i=5678")
        self.assertEqual(info["media_type"], "album")
        self.assertEqual(info["sub_id"], "5678")

    def test_parse_itunes_store_url(self):
        info = applemusic_backend.parse_apple_url(
            "https://itunes.apple.com/us/album/the-album/id1234")
        self.assertEqual(info["media_type"], "album")
        self.assertEqual(info["media_id"], "1234")

    def test_parse_rejects_foreign_urls(self):
        self.assertIsNone(applemusic_backend.parse_apple_url(
            "https://example.com/album/1"))
        self.assertIsNone(applemusic_backend.parse_apple_url(""))

    def test_is_apple_music_url(self):
        self.assertTrue(applemusic_backend.is_apple_music_url(
            "https://geo.music.apple.com/us/album/x/1"))
        self.assertTrue(applemusic_backend.is_apple_music_url(
            "https://itunes.apple.com/us/album/x/id1"))
        self.assertFalse(applemusic_backend.is_apple_music_url(
            "https://example.com/x"))


class AppleMusicExtractTests(unittest.TestCase):
    def test_extract_song_looks_up_metadata(self):
        lookup = {"results": [_track(1, "One")]}
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = lookup
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response) as get:
            items, title = applemusic_backend.extract_flat(
                "https://music.apple.com/us/song/1")

        self.assertEqual(title, "One")
        self.assertEqual(items[0]["artist"], "Artist")
        self.assertEqual(items[0]["album"], "Album")
        self.assertEqual(items[0]["duration_s"], 180)
        self.assertEqual(get.call_args.args[0],
                         applemusic_backend._ITUNES_LOOKUP_URL)

    def test_extract_song_falls_back_to_placeholder(self):
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"results": []}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response):
            items, _title = applemusic_backend.extract_flat(
                "https://music.apple.com/us/song/9")

        self.assertEqual(items[0]["url"], "https://music.apple.com/us/song/9")

    def test_extract_album_returns_track_list(self):
        results = [_track(1, "Album")] + [
            _track(10, "Ten", url="https://music.apple.com/us/album/a/1?i=10"),
            _track(11, "Eleven", url="https://music.apple.com/us/album/a/1?i=11"),
        ]
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"results": results}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response):
            items, title = applemusic_backend.extract_flat(
                "https://music.apple.com/us/album/slug/99")

        self.assertEqual([item["title"] for item in items],
                         ["Ten", "Eleven"])
        self.assertEqual(title, "Album")

    def test_extract_playlist_uses_catalog_fallback(self):
        # iTunes lookup does not know playlist (pl.*) ids, so resolution
        # falls back to the catalog API with an anonymous token.
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"results": []}
        fake_api = mock.Mock()
        fake_api.getplaylist.return_value = {
            "data": [{
                "attributes": {"name": "Mix"},
                "relationships": {"tracks": {"data": [
                    {"id": "100", "attributes": {
                        "name": "A", "artistName": "X",
                        "durationInMillis": 200000}},
                    {"id": "101", "attributes": {"name": "B"}},
                ]}},
            }],
        }
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=response), \
             mock.patch.object(applemusic_backend, "_anonymous_api",
                               return_value=fake_api):
            items, title = applemusic_backend.extract_flat(
                "https://music.apple.com/us/playlist/pl.abc")

        self.assertEqual(title, "Mix")
        self.assertEqual([item["title"] for item in items], ["A", "B"])
        self.assertTrue(items[0]["url"].endswith("/song/100"))

    def test_extract_rejects_unsupported(self):
        with self.assertRaises(RuntimeError):
            applemusic_backend.extract_flat(
                "https://music.apple.com/us/artist/123")
        with self.assertRaises(RuntimeError):
            applemusic_backend.extract_flat("https://example.com/x")


class AppleMusicCookieTests(unittest.TestCase):
    def test_parse_netscape_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cookies.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# Netscape HTTP Cookie File\n"
                    ".music.apple.com\tTRUE\t/\tTRUE\t0\tfoo\tbar\n"
                    "#HttpOnly_.music.apple.com\tTRUE\t/\tTRUE\t0\t"
                    "media-user-token\ttoken123\n"
                    "broken-line\n"
                )
            cookies = applemusic_backend._cookies_from_file(path)
        self.assertEqual(cookies,
                         {"foo": "bar", "media-user-token": "token123"})

    def test_download_requires_cookies(self):
        with self.assertRaises(RuntimeError) as ctx:
            applemusic_backend.download(
                "https://music.apple.com/us/song/1", "out", {})
        self.assertIn("cookies", str(ctx.exception).lower())

    def test_download_requires_media_user_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cookies.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("a\tTRUE\t/\tTRUE\t0\tfoo\tbar\n")
            with self.assertRaises(RuntimeError) as ctx:
                applemusic_backend.download(
                    "https://music.apple.com/us/song/1", "out",
                    {"apple_music_cookies": path})
            self.assertIn("media-user-token", str(ctx.exception))


class AppleMusicDownloadTests(unittest.TestCase):
    def _fake_item(self):
        return SimpleNamespace(
            media_tags=SimpleNamespace(
                track=3, title="Three",
                asmp4tags=lambda: {"\xa9nam": ["Three"]}),
            cover_url="",
            lyrics=None,
            stream_info=SimpleNamespace(
                audio_track=SimpleNamespace(
                    stream_url="https://example.com/stream.m3u8")),
            decryption_key=SimpleNamespace(
                audio_track=SimpleNamespace(key="00" * 16)),
        )

    def _fake_ffmpeg(self, run):
        def _run(command, **kwargs):
            with open(command[-1], "wb") as handle:
                handle.write(b"fake-m4a")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        run.side_effect = _run

    @staticmethod
    def _fake_hls_download(self, filename, info_dict):
        with open(filename, "wb") as handle:
            handle.write(b"encrypted")
        return True, ""

    def _patch_download_tools(self, run):
        """Context stack for the two real-world calls a download makes."""
        self._fake_ffmpeg(run)
        return mock.patch(
            "yt_dlp.downloader.hls.HlsFD.download",
            autospec=True, side_effect=self._fake_hls_download)

    def test_download_song_uses_in_process_pipeline(self):
        api = mock.Mock()
        api.getsong.return_value = {
            "data": [{"id": "1", "attributes": {"name": "One"}}]
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(applemusic_backend, "_authenticated_api",
                                   return_value=(api, None)), \
                 mock.patch("musicdl.modules.utils.appleutils."
                            "AppleMusicClientDownloadSongUtils.getdownloaditem",
                            return_value=self._fake_item()), \
                 mock.patch.object(applemusic_backend.subprocess, "run") as run, \
                 self._patch_download_tools(run):
                applemusic_backend.download(
                    "https://music.apple.com/us/song/1", tmp,
                    {"apple_music_cookies": "/x"})
            self.assertTrue(os.path.isfile(os.path.join(tmp, "03 One.m4a")))
            self.assertIn("-decryption_key", run.call_args.args[0])
            self.assertIn("ffmpeg", run.call_args.args[0])

    def test_download_song_mp3_v0_converts(self):
        api = mock.Mock()
        api.getsong.return_value = {
            "data": [{"id": "1", "attributes": {"name": "One"}}]
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(applemusic_backend, "_authenticated_api",
                                   return_value=(api, None)), \
                 mock.patch("musicdl.modules.utils.appleutils."
                            "AppleMusicClientDownloadSongUtils.getdownloaditem",
                            return_value=self._fake_item()), \
                 mock.patch.object(applemusic_backend.subprocess, "run") as run, \
                 self._patch_download_tools(run):
                applemusic_backend.download(
                    "https://music.apple.com/us/song/1", tmp,
                    {"apple_music_cookies": "/x",
                     "apple_music_format": "mp3_v0"})
            self.assertTrue(os.path.isfile(os.path.join(tmp, "03 One.mp3")))
            self.assertFalse(os.path.isfile(os.path.join(tmp, "03 One.m4a")))
            mp3_command = run.call_args_list[-1].args[0]
            self.assertTrue(mp3_command[-1].endswith(".mp3"))
            self.assertIn("libmp3lame", mp3_command)
            self.assertIn("-q:a", mp3_command)
            self.assertEqual(len(run.call_args_list), 2)

    def test_download_song_keeps_m4a_by_default(self):
        api = mock.Mock()
        api.getsong.return_value = {
            "data": [{"id": "1", "attributes": {"name": "One"}}]
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(applemusic_backend, "_authenticated_api",
                                   return_value=(api, None)), \
                 mock.patch("musicdl.modules.utils.appleutils."
                            "AppleMusicClientDownloadSongUtils.getdownloaditem",
                            return_value=self._fake_item()), \
                 mock.patch.object(applemusic_backend.subprocess, "run") as run, \
                 self._patch_download_tools(run):
                applemusic_backend.download(
                    "https://music.apple.com/us/song/1", tmp,
                    {"apple_music_cookies": "/x"})
            self.assertTrue(os.path.isfile(os.path.join(tmp, "03 One.m4a")))
            self.assertEqual(len(run.call_args_list), 1)

    def test_download_album_writes_subfolder(self):
        api = mock.Mock()
        api.client.get.return_value = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{
                "attributes": {"name": "Album"},
                "relationships": {"tracks": {"data": [
                    {"id": "1", "attributes": {"playParams": {"catalogId": "1"}}},
                    {"id": "2", "attributes": {"playParams": {"catalogId": "2"}}},
                ]}},
            }]},
        )
        api.getsong.side_effect = [
            {"data": [{"id": "1", "attributes": {"name": "One"}}]},
            {"data": [{"id": "2", "attributes": {"name": "Two"}}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(applemusic_backend, "_authenticated_api",
                                   return_value=(api, None)), \
                 mock.patch("musicdl.modules.utils.appleutils."
                            "AppleMusicClientDownloadSongUtils.getdownloaditem",
                            side_effect=[self._fake_item(),
                                         self._fake_item()]), \
                 mock.patch.object(applemusic_backend.subprocess, "run") as run, \
                 self._patch_download_tools(run):
                applemusic_backend.download(
                    "https://music.apple.com/us/album/x/9", tmp,
                    {"apple_music_cookies": "/x"})
            subfolder = os.path.join(tmp, "Album")
            self.assertTrue(
                os.path.isfile(os.path.join(subfolder, "03 One.m4a")))
            self.assertTrue(
                os.path.isfile(os.path.join(subfolder, "03 Two.m4a")))
            self.assertEqual(len(run.call_args_list), 2)

    def test_download_album_without_tracks_errors(self):
        api = mock.Mock()
        api.client.get.return_value = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"attributes": {"name": "Empty"}}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(applemusic_backend, "_authenticated_api",
                                   return_value=(api, None)):
                with self.assertRaises(RuntimeError):
                    applemusic_backend.download(
                        "https://music.apple.com/us/album/x/9", tmp,
                        {"apple_music_cookies": "/x"})


class AppleMusicBrowseTests(unittest.TestCase):
    """Opening the album or the artist a search result names."""

    def test_a_search_result_carries_the_ids_that_open_it(self):
        results = {"results": [{
            "trackName": "One More Time",
            "artistName": "Daft Punk",
            "collectionName": "Discovery",
            "trackViewUrl": "https://music.apple.com/us/album/x/1?i=2",
            "collectionId": 1,
            "artistId": 5,
            "trackTimeMillis": 320000,
        }]}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=_response(results)):
            items = applemusic_backend.search("one more time")

        self.assertEqual(items[0]["album_id"], "1")
        self.assertEqual(items[0]["artist_id"], "5")

    def test_a_track_the_catalog_api_built_cannot_be_browsed_from(self):
        # The catalog API's track records carry neither id, so those rows
        # arrive with "" and are simply not offered the two commands.
        item = applemusic_backend._track_item({"trackName": "Solo"}, "url")
        self.assertEqual(item["album_id"], "")
        self.assertEqual(item["artist_id"], "")

    def test_browsing_an_album_lists_its_tracks_in_running_order(self):
        results = {"results": [
            {"collectionName": "Discovery", "collectionId": 1,
             "artistId": 5, "wrapperType": "collection"},
            {"trackName": "One More Time", "artistName": "Daft Punk",
             "collectionId": 1, "artistId": 5,
             "trackViewUrl": "https://music.apple.com/us/album/x/1?i=2"},
            {"trackName": "Aerodynamic", "artistName": "Daft Punk",
             "collectionId": 1, "artistId": 5,
             "trackViewUrl": "https://music.apple.com/us/album/x/1?i=3"},
        ]}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=_response(results)) as get:
            items, title = applemusic_backend.album_items(1)

        self.assertEqual(get.call_args.kwargs["params"]["id"], "1")
        self.assertEqual(title, "Discovery")
        self.assertEqual([item["title"] for item in items],
                         ["One More Time", "Aerodynamic"])
        self.assertEqual({item["album"] for item in items}, {"Discovery"})

    def test_an_album_id_itunes_does_not_know_lists_nothing(self):
        # A playlist's pl.* id reaches lookup and comes back with the query
        # echoed and no results. That is "nothing to list", not a failure.
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=_response({"results": []})):
            items, title = applemusic_backend.album_items("pl.abc")

        self.assertEqual(items, [])
        self.assertEqual(title, "")

    def test_browsing_an_artist_lists_every_release_they_have_out(self):
        results = {"results": [
            {"wrapperType": "artist", "artistId": 5, "artistName": "Daft Punk"},
            {"collectionId": 1, "collectionName": "Discovery", "trackCount": 14,
             "artistId": 5, "artistName": "Daft Punk",
             "collectionViewUrl": "https://music.apple.com/us/album/1"},
            {"collectionId": 2, "collectionName": "Homework", "trackCount": 16,
             "artistId": 5, "artistName": "Daft Punk",
             "collectionViewUrl": "https://music.apple.com/us/album/2"},
        ]}
        with mock.patch.object(applemusic_backend.requests, "get",
                               return_value=_response(results)) as get:
            items, name = applemusic_backend.artist_albums(5)

        self.assertEqual(get.call_args.kwargs["params"]["entity"], "album")
        self.assertEqual(name, "Daft Punk")
        # The artist record itself is first and has no release to open, so
        # it names the page rather than becoming a row on it.
        self.assertEqual([item["title"] for item in items],
                         ["Discovery", "Homework"])
        self.assertEqual([item["kind"] for item in items],
                         ["applemusic_album", "applemusic_album"])
        self.assertEqual({item["artist_id"] for item in items}, {"5"})
        self.assertEqual(items[0]["format"], "Album, 14 tracks")

    def test_browsing_without_an_id_says_so_rather_than_asking(self):
        with mock.patch.object(applemusic_backend.requests, "get") as get:
            with self.assertRaises(RuntimeError):
                applemusic_backend.artist_albums("")
            with self.assertRaises(RuntimeError):
                applemusic_backend.album_items(None)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
