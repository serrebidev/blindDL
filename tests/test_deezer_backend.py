# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import threading
import unittest
from unittest import mock

from blinddl import deezer_backend, search_kind, search_order, sideb_backend


def _track_payload(track_id, title, artist="Artist"):
    """A Deezer /playlist/{id}/tracks entry: a bare track object."""
    return {
        "id": track_id,
        "title": title,
        "link": f"https://www.deezer.com/track/{track_id}",
        "duration": 200,
        "rank": 10,
        "artist": {"name": artist},
        "album": {"title": "Album"},
    }


def _write_decrypted(_stream, _track_id, dest_path, _progress_cb=None,
                     _cancel_event=None):
    """Stand in for _decrypt_stream: the file has to appear for staging."""
    with open(dest_path, "wb") as handle:
        handle.write(b"audio")


class DeezerBackendTests(unittest.TestCase):
    def setUp(self):
        deezer_backend._sessions.clear()

    def tearDown(self):
        deezer_backend._sessions.clear()

    def test_login_retains_http_session_for_csrf_cookie(self):
        http = mock.Mock()
        user_data = {
            "checkForm": "csrf",
            "USER": {
                "USER_ID": "42",
                "OPTIONS": {"license_token": "license"},
            },
        }
        with mock.patch.object(
                deezer_backend, "_new_http_session", return_value=http), \
                mock.patch.object(
                    deezer_backend, "_gw_call", return_value=user_data) as gw:
            first = deezer_backend._login("test-arl")
            second = deezer_backend._login("test-arl")

        self.assertIs(first, second)
        self.assertIs(first["http"], http)
        gw.assert_called_once_with(http, "deezer.getUserData", None)

    def test_popular_search_uses_deezers_track_rank(self):
        payload = {"data": [
            {"id": 1, "title": "Less popular", "rank": 10},
            {"id": 2, "title": "More popular", "rank": 100},
        ]}
        with mock.patch.object(
                deezer_backend, "_api_get", return_value=payload):
            items = deezer_backend.search(
                "example", order=search_order.ORDER_POPULAR)

        self.assertEqual(
            [item["title"] for item in items],
            ["More popular", "Less popular"])
        self.assertFalse(deezer_backend.supports_order(
            search_order.ORDER_RECENT))

    def test_search_paginates_to_200(self):
        def page(_path, params):
            index = params["index"]
            return {"data": [
                {"id": track_id, "title": f"T{track_id}", "rank": track_id}
                for track_id in range(index + 1, index + 101)
            ]}

        with mock.patch.object(deezer_backend, "_api_get",
                               side_effect=page) as api:
            items = deezer_backend.search("example")

        self.assertEqual(len(items), 200)
        self.assertEqual(
            [call.args for call in api.call_args_list],
            [("/search/track", {"q": "example", "limit": 100, "index": 0}),
             ("/search/track", {"q": "example", "limit": 100, "index": 100})],
        )

    def test_track_search_keeps_only_titles_that_really_match(self):
        # Deezer's own track:"..." term matches the query's words anywhere,
        # including inside the word "track", so the narrowing is done here.
        payload = {"data": [
            {"id": 1, "title": "One More Time", "rank": 5},
            {"id": 2, "title": "One More Time (Radio Edit)", "rank": 4},
            {"id": 3, "title": "Baby One More", "rank": 9},
            {"id": 4, "title": "Bonus Track", "rank": 8},
        ]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload) as api:
            items = deezer_backend.search(
                "one more time", kind=search_kind.KIND_TRACK)

        self.assertEqual(api.call_args.args[0], "/search/track")
        self.assertEqual(api.call_args.args[1]["q"], "one more time")
        self.assertEqual(
            [item["title"] for item in items],
            ["One More Time", "One More Time (Radio Edit)"],
        )

    def test_best_match_search_keeps_every_result(self):
        payload = {"data": [
            {"id": 1, "title": "One More Time", "rank": 5},
            {"id": 2, "title": "Something else", "rank": 4},
        ]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload) as api:
            items = deezer_backend.search(
                "one more time", kind=search_kind.KIND_BEST)

        self.assertEqual(api.call_args.args[1]["q"], "one more time")
        self.assertEqual(len(items), 2)

    def test_artist_search_looks_the_artist_up_before_taking_their_tracks(self):
        def api(path, params=None):
            if path == "/search/artist":
                return {"data": [{"id": 27, "name": "Daft Punk"},
                                 {"id": 99, "name": "Daft Punk Experience"}]}
            if path == "/artist/27/top":
                return {"data": [
                    {"id": 1, "title": "One More Time",
                     "album": {"title": "Discovery"}, "rank": 9},
                ]}
            return {"data": [
                {"id": 2, "title": "Tribute", "album": {}, "rank": 1},
            ]}

        with mock.patch.object(deezer_backend, "_api_get",
                               side_effect=api) as calls:
            items = deezer_backend.search(
                "daft punk", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_SONGS)

        self.assertEqual(calls.call_args_list[0].args[0], "/search/artist")
        self.assertEqual([item["title"] for item in items],
                         ["One More Time", "Tribute"])
        # /artist/{id}/top names the artist on the request rather than on
        # each track, so the rows would otherwise arrive without one.
        self.assertEqual([item["artist"] for item in items],
                         ["Daft Punk", "Daft Punk Experience"])

    def test_artist_search_survives_one_artist_that_cannot_be_read(self):
        def api(path, params=None):
            if path == "/search/artist":
                return {"data": [{"id": 1, "name": "Gone"},
                                 {"id": 2, "name": "Here"}]}
            if path == "/artist/1/top":
                raise RuntimeError("410 Gone")
            return {"data": [{"id": 9, "title": "Song", "rank": 1}]}

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api):
            items = deezer_backend.search(
                "example", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_SONGS)

        self.assertEqual([item["artist"] for item in items], ["Here"])

    def test_artist_search_albums_scope_returns_album_rows(self):
        def api(path, params=None):
            if path == "/search/artist":
                return {"data": [{"id": 27, "name": "Daft Punk"}]}
            if path.startswith("/artist/27/albums"):
                return {"data": [
                    {"id": 7, "title": "Discovery", "nb_tracks": 14},
                ]}
            return {"data": []}

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api):
            items = deezer_backend.search(
                "daft punk", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_ALBUMS)

        self.assertEqual([item["kind"] for item in items], ["deezer_album"])
        self.assertEqual(items[0]["title"], "Discovery")
        self.assertEqual(items[0]["artist"], "Daft Punk")
        self.assertEqual(items[0]["format"], "Album, 14 tracks")

    def test_artist_search_playlists_scope_returns_playlist_rows(self):
        payload = {"data": [
            {"id": 5, "title": "French Touch", "nb_tracks": 12,
             "user": {"name": "Editor"},
             "link": "https://www.deezer.com/playlist/5"},
        ]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload) as calls:
            items = deezer_backend.search(
                "daft punk", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_PLAYLISTS)

        self.assertEqual(calls.call_args.args[0], "/search/playlist")
        self.assertEqual(
            [item["kind"] for item in items], ["deezer_playlist"])
        self.assertEqual(items[0]["title"], "French Touch")
        self.assertEqual(items[0]["artist"], "Editor")
        self.assertEqual(items[0]["format"], "Playlist, 12 tracks")

    def test_artist_search_all_scope_combines_all_three_kinds(self):
        def api(path, params=None):
            if path == "/search/artist":
                return {"data": [{"id": 27, "name": "Daft Punk"}]}
            if path.startswith("/artist/27/top"):
                return {"data": [
                    {"id": 1, "title": "One More Time",
                     "album": {"title": "Discovery"}, "rank": 9},
                ]}
            if path.startswith("/artist/27/albums"):
                return {"data": [
                    {"id": 7, "title": "Discovery", "nb_tracks": 14},
                ]}
            if path == "/search/playlist":
                return {"data": [
                    {"id": 5, "title": "French Touch", "nb_tracks": 12,
                     "user": {"name": "Editor"}},
                ]}
            return {"data": []}

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api):
            items = deezer_backend.search(
                "daft punk", kind=search_kind.KIND_ARTIST,
                artist_scope=search_kind.ARTIST_SCOPE_ALL)

        self.assertEqual(
            [item["kind"] for item in items],
            ["deezer", "deezer_album", "deezer_playlist"],
        )

    def test_album_search_returns_album_rows_with_their_track_counts(self):
        payload = {"data": [
            {"id": 7, "title": "Discovery", "nb_tracks": 14,
             "artist": {"name": "Daft Punk"},
             "link": "https://www.deezer.com/album/7"},
            {"id": 8, "title": "Single", "nb_tracks": 1,
             "artist": {"name": "Daft Punk"}},
        ]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload) as api:
            items = deezer_backend.search(
                "discovery", kind=search_kind.KIND_ALBUM)

        self.assertEqual(api.call_args.args[0], "/search/album")
        # The album endpoint already matches album titles, so no field term.
        self.assertEqual(api.call_args.args[1]["q"], "discovery")
        self.assertEqual([item["kind"] for item in items],
                         ["deezer_album", "deezer_album"])
        self.assertEqual(items[0]["title"], "Discovery")
        self.assertEqual(items[0]["artist"], "Daft Punk")
        self.assertEqual(items[0]["format"], "Album, 14 tracks")
        self.assertEqual(items[1]["format"], "Album, 1 track")
        self.assertEqual(items[1]["url"], "https://www.deezer.com/album/8")

    def test_a_search_result_carries_the_ids_that_open_it(self):
        # The album and the artist of a row were two strings in two columns
        # with nowhere to go from them. These ids are what turn them back
        # into places the Search tab can open.
        payload = {"data": [{
            "id": 3135556,
            "title": "Harder, Better, Faster, Stronger",
            "link": "https://www.deezer.com/track/3135556",
            "duration": 224,
            "rank": 900000,
            "artist": {"id": 27, "name": "Daft Punk"},
            "album": {"id": 302127, "title": "Discovery"},
        }]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload):
            items = deezer_backend.search("harder better")

        self.assertEqual(items[0]["artist_id"], "27")
        self.assertEqual(items[0]["album_id"], "302127")

    def test_a_track_from_an_endpoint_that_names_neither_carries_no_ids(self):
        # /artist/{id}/top hands back tracks with no album object at all, and
        # a missing id has to read as "cannot be browsed" rather than crash
        # the row that is being built.
        item = deezer_backend._track_to_item(
            {"id": 1, "title": "Solo", "duration": 100})
        self.assertEqual(item["artist_id"], "")
        self.assertEqual(item["album_id"], "")

    def test_album_rows_say_whether_they_are_a_single_or_an_ep(self):
        payload = {"data": [
            {"id": 9, "title": "One More Time", "nb_tracks": 1,
             "record_type": "single", "artist": {"id": 27, "name": "Daft Punk"}},
        ]}
        with mock.patch.object(deezer_backend, "_api_get",
                               return_value=payload):
            items = deezer_backend.search(
                "one more time", kind=search_kind.KIND_ALBUM)

        self.assertEqual(items[0]["format"], "Single, 1 track")
        self.assertEqual(items[0]["record_type"], "single")
        self.assertEqual(items[0]["album_id"], "9")
        self.assertEqual(items[0]["artist_id"], "27")

    def test_browsing_an_album_lists_its_tracks_in_running_order(self):
        album = {
            "id": 302127,
            "title": "Discovery",
            "artist": {"id": 27, "name": "Daft Punk"},
        }
        tracks = {"data": [
            {"id": 1, "title": "One More Time", "duration": 320},
            {"id": 2, "title": "Aerodynamic", "duration": 212},
        ]}

        def api_get(path, params=None):
            return tracks if path.endswith("/tracks") else album

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api_get):
            items, title = deezer_backend.album_items(302127)

        self.assertEqual(title, "Discovery")
        self.assertEqual([item["title"] for item in items],
                         ["One More Time", "Aerodynamic"])
        # Every track knows the album it came off and who made it, so the
        # browse can be stepped through in either direction.
        self.assertEqual({item["album_id"] for item in items}, {"302127"})
        self.assertEqual({item["artist_id"] for item in items}, {"27"})

    def test_browsing_an_album_without_an_id_says_so_rather_than_asking(self):
        with mock.patch.object(deezer_backend, "_api_get") as api:
            with self.assertRaises(RuntimeError):
                deezer_backend.album_items("")
        api.assert_not_called()

    def test_browsing_an_artist_follows_their_whole_discography(self):
        artist = {"id": 27, "name": "Daft Punk"}
        pages = {
            "/artist/27/albums?limit=100": {
                "data": [{"id": 1, "title": "Homework", "nb_tracks": 16,
                          "record_type": "album"}],
                "next": "https://api.deezer.com/artist/27/albums?index=100",
            },
            "https://api.deezer.com/artist/27/albums?index=100": {
                "data": [{"id": 2, "title": "Da Funk", "nb_tracks": 1,
                          "record_type": "single"}],
            },
        }

        def api_get(path, params=None):
            if path == "/artist/27":
                return artist
            return pages[path]

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api_get):
            items, name = deezer_backend.artist_albums(27)

        self.assertEqual(name, "Daft Punk")
        self.assertEqual([item["title"] for item in items],
                         ["Homework", "Da Funk"])
        # /artist/{id}/albums names the artist on the request rather than on
        # each release, so the rows would otherwise arrive without one -- and
        # without the id that opens this same page again.
        self.assertEqual([item["artist"] for item in items],
                         ["Daft Punk", "Daft Punk"])
        self.assertEqual({item["artist_id"] for item in items}, {"27"})
        self.assertEqual([item["format"] for item in items],
                         ["Album, 16 tracks", "Single, 1 track"])

    def test_browsing_an_artist_stops_at_the_row_budget(self):
        artist = {"id": 27, "name": "Daft Punk"}
        page = {"data": [
            {"id": index, "title": f"Release {index}", "nb_tracks": 1}
            for index in range(1, 6)
        ]}

        def api_get(path, params=None):
            return artist if path == "/artist/27" else page

        with mock.patch.object(deezer_backend, "_api_get", side_effect=api_get):
            items, _name = deezer_backend.artist_albums(27, limit=3)

        self.assertEqual(len(items), 3)

    def test_album_search_cannot_answer_most_popular(self):
        # /search/album publishes neither a rank nor a date, so claiming the
        # order was honoured would be a lie the status line then repeats.
        self.assertTrue(deezer_backend.supports_order(
            search_order.ORDER_POPULAR))
        self.assertFalse(deezer_backend.supports_order(
            search_order.ORDER_POPULAR, search_kind.KIND_ALBUM))
        self.assertTrue(deezer_backend.supports_order(
            search_order.ORDER_RELEVANCE, search_kind.KIND_ALBUM))
        self.assertTrue(deezer_backend.supports_kind(search_kind.KIND_ARTIST))

    def test_download_uses_plural_tokens_and_actual_media_format(self):
        session = {
            "api_token": "csrf",
            "license_token": "license",
            "http": mock.Mock(),
            "http_lock": threading.Lock(),
        }
        metadata = {
            "DATA": {
                "SNG_ID": "3135556",
                "SNG_TITLE": "Test track",
                "ART_NAME": "Test artist",
                "TRACK_TOKEN": "track-token",
            }
        }
        media_response = mock.Mock()
        media_response.json.return_value = {
            "data": [{
                "media": [{
                    # Deezer may fall back from requested FLAC to MP3 320.
                    "format": "MP3_320",
                    "sources": [{"url": "https://media.invalid/track"}],
                }]
            }]
        }
        stream_response = mock.MagicMock()

        config = {
            "deezer_arl": "test-arl",
            "deezer_format": "flac",
            "sideb_lyrics": False,
        }
        with tempfile.TemporaryDirectory() as out_dir, \
                mock.patch.object(
                    deezer_backend, "_login", return_value=session), \
                mock.patch.object(
                    deezer_backend, "_gw_call", return_value=metadata), \
                mock.patch.object(
                    deezer_backend.requests, "post",
                    return_value=media_response) as post, \
                mock.patch.object(
                    deezer_backend.requests, "get",
                    return_value=stream_response), \
                mock.patch.object(
                deezer_backend, "_decrypt_stream", side_effect=_write_decrypted
            ), \
                mock.patch.object(deezer_backend, "_cover_bytes",
                                  return_value=None), \
                mock.patch.object(deezer_backend, "_tag_mp3") as tag_mp3:
            path = deezer_backend.download(
                "https://www.deezer.com/track/3135556", out_dir, config)

        request_json = post.call_args.kwargs["json"]
        self.assertNotIn("track_token", request_json)
        self.assertEqual(request_json["track_tokens"], ["track-token"])
        self.assertTrue(path.endswith(".mp3"))
        tag_mp3.assert_called_once()

    def test_download_prefers_flac_when_gateway_lists_mp3_first(self):
        session = {
            "api_token": "csrf",
            "license_token": "license",
            "http": mock.Mock(),
            "http_lock": threading.Lock(),
        }
        metadata = {
            "DATA": {
                "SNG_ID": "3135556",
                "SNG_TITLE": "Test track",
                "ART_NAME": "Test artist",
                "TRACK_TOKEN": "track-token",
            }
        }
        media_response = mock.Mock()
        media_response.json.return_value = {
            "data": [{
                "media": [
                    {
                        "format": "MP3_320",
                        "sources": [{"url": "https://media.invalid/mp3"}],
                    },
                    {
                        "format": "FLAC",
                        "sources": [{"url": "https://media.invalid/flac"}],
                    },
                ]
            }]
        }
        stream_response = mock.MagicMock()
        config = {
            "deezer_arl": "test-arl",
            "deezer_format": "flac",
            "sideb_lyrics": False,
        }
        with tempfile.TemporaryDirectory() as out_dir, \
                mock.patch.object(
                    deezer_backend, "_login", return_value=session), \
                mock.patch.object(
                    deezer_backend, "_gw_call", return_value=metadata), \
                mock.patch.object(
                    deezer_backend.requests, "post",
                    return_value=media_response) as post, \
                mock.patch.object(
                    deezer_backend.requests, "get",
                    return_value=stream_response), \
                mock.patch.object(
                deezer_backend, "_decrypt_stream", side_effect=_write_decrypted
            ), \
                mock.patch.object(deezer_backend, "_cover_bytes",
                                  return_value=None), \
                mock.patch.object(deezer_backend, "_tag_flac") as tag_flac:
            path = deezer_backend.download(
                "https://www.deezer.com/track/3135556", out_dir, config)

        request_json = post.call_args.kwargs["json"]
        self.assertEqual(
            request_json["media"][0]["formats"],
            [{"cipher": "BF_CBC_STRIPE", "format": "FLAC"}],
        )
        self.assertEqual(post.call_count, 1)
        self.assertTrue(path.endswith(".flac"))
        tag_flac.assert_called_once()

    def test_flac_falls_back_to_mp3_320_in_a_second_request(self):
        session = {
            "api_token": "csrf",
            "license_token": "license",
            "http": mock.Mock(),
            "http_lock": threading.Lock(),
        }
        metadata = {
            "DATA": {
                "SNG_ID": "3135556",
                "SNG_TITLE": "Test track",
                "ART_NAME": "Test artist",
                "TRACK_TOKEN": "track-token",
            }
        }
        no_flac = mock.Mock()
        no_flac.json.return_value = {"data": []}
        mp3 = mock.Mock()
        mp3.json.return_value = {
            "data": [{
                "media": [{
                    "format": "MP3_320",
                    "sources": [{"url": "https://media.invalid/mp3"}],
                }]
            }]
        }
        stream_response = mock.MagicMock()
        config = {
            "deezer_arl": "test-arl",
            "deezer_format": "flac",
            "sideb_lyrics": False,
        }
        with tempfile.TemporaryDirectory() as out_dir, \
                mock.patch.object(
                    deezer_backend, "_login", return_value=session), \
                mock.patch.object(
                    deezer_backend, "_gw_call", return_value=metadata), \
                mock.patch.object(
                    deezer_backend.requests, "post",
                    side_effect=[no_flac, mp3]) as post, \
                mock.patch.object(
                    deezer_backend.requests, "get",
                    return_value=stream_response), \
                mock.patch.object(
                deezer_backend, "_decrypt_stream", side_effect=_write_decrypted
            ), \
                mock.patch.object(deezer_backend, "_cover_bytes",
                                  return_value=None), \
                mock.patch.object(deezer_backend, "_tag_mp3") as tag_mp3:
            path = deezer_backend.download(
                "https://www.deezer.com/track/3135556", out_dir, config)

        formats = [
            call.kwargs["json"]["media"][0]["formats"][0]["format"]
            for call in post.call_args_list
        ]
        self.assertEqual(formats, ["FLAC", "MP3_320"])
        self.assertTrue(path.endswith(".mp3"))
        tag_mp3.assert_called_once()

    def test_flac_is_the_default_deezer_format(self):
        self.assertEqual(self._requested_formats("flac"), ["FLAC"])

    def test_mp3_320_setting_requests_only_320(self):
        self.assertEqual(self._requested_formats("mp3_320"), ["MP3_320"])

    def test_missing_setting_defaults_to_flac(self):
        self.assertEqual(self._requested_formats(None), ["FLAC"])

    def _requested_formats(self, deezer_format):
        session = {
            "api_token": "csrf",
            "license_token": "license",
            "http": mock.Mock(),
            "http_lock": threading.Lock(),
        }
        metadata = {
            "DATA": {
                "SNG_ID": "3135556",
                "SNG_TITLE": "Test track",
                "ART_NAME": "Test artist",
                "TRACK_TOKEN": "track-token",
            }
        }
        media_response = mock.Mock()
        media_response.json.return_value = {
            "data": [{
                "media": [{
                    "format": "FLAC",
                    "sources": [{"url": "https://media.invalid/track"}],
                }]
            }]
        }
        stream_response = mock.MagicMock()
        config = {
            "deezer_arl": "test-arl",
            "sideb_lyrics": False,
        }
        if deezer_format is not None:
            config["deezer_format"] = deezer_format
        with tempfile.TemporaryDirectory() as out_dir, \
                mock.patch.object(
                    deezer_backend, "_login", return_value=session), \
                mock.patch.object(
                    deezer_backend, "_gw_call", return_value=metadata), \
                mock.patch.object(
                    deezer_backend.requests, "post",
                    return_value=media_response) as post, \
                mock.patch.object(
                    deezer_backend.requests, "get",
                    return_value=stream_response), \
                mock.patch.object(
                deezer_backend, "_decrypt_stream", side_effect=_write_decrypted
            ), \
                mock.patch.object(deezer_backend, "_cover_bytes",
                                  return_value=None), \
                mock.patch.object(deezer_backend, "_tag_flac"):
            deezer_backend.download(
                "https://www.deezer.com/track/3135556", out_dir, config)
        return [f["format"]
                for f in post.call_args.kwargs["json"]["media"][0]["formats"]]

    def test_lyrics_prefer_deezer_and_fall_back_to_lrclib(self):
        with mock.patch.object(
                deezer_backend, "_fetch_deezer_lyrics",
                return_value="word synced") as deezer, \
                mock.patch.object(
                    deezer_backend, "_fetch_lrclib_lyrics") as lrclib:
            result = deezer_backend._fetch_lyrics({"SNG_ID": "1"}, "arl")
        self.assertEqual(result, "word synced")
        deezer.assert_called_once()
        lrclib.assert_not_called()

        with mock.patch.object(
                deezer_backend, "_fetch_deezer_lyrics", return_value=None), \
                mock.patch.object(
                    deezer_backend, "_fetch_lrclib_lyrics",
                    return_value="line synced"):
            result = deezer_backend._fetch_lyrics({"SNG_ID": "1"}, "arl")
        self.assertEqual(result, "line synced")

    def test_extract_flat_playlist_ignores_country_prefix(self):
        playlist = {"title": "Road trip"}
        tracks = {"data": [_track_payload(1, "One"), _track_payload(2, "Two")]}
        with mock.patch.object(
                deezer_backend, "_api_get",
                side_effect=[playlist, tracks]) as api:
            items, title = deezer_backend.extract_flat(
                "https://www.deezer.com/us/playlist/14810204783")

        self.assertEqual(title, "Road trip")
        self.assertEqual([item["title"] for item in items], ["One", "Two"])
        self.assertEqual(
            [call.args[0] for call in api.call_args_list],
            ["/playlist/14810204783", "/playlist/14810204783/tracks"])

    def test_extract_flat_playlist_follows_pagination(self):
        playlist = {"title": "Big playlist"}
        page1 = {"data": [_track_payload(1, "One")],
                 "next": "https://api.deezer.com/playlist/9/tracks?index=25"}
        page2 = {"data": [_track_payload(2, "Two")]}
        with mock.patch.object(
                deezer_backend, "_api_get",
                side_effect=[playlist, page1, page2]) as api:
            items, title = deezer_backend.extract_flat(
                "https://www.deezer.com/playlist/9")

        self.assertEqual(title, "Big playlist")
        self.assertEqual([item["title"] for item in items], ["One", "Two"])
        # The second page came from the API's absolute `next` URL, which
        # _api_get must pass through untouched rather than double-prefixing.
        self.assertEqual(
            api.call_args_list[2].args[0],
            "https://api.deezer.com/playlist/9/tracks?index=25")

    def test_extract_flat_playlist_tolerates_track_wrapper(self):
        playlist = {"title": "Wrapped"}
        tracks = {"data": [{"track": _track_payload(1, "One")},
                           _track_payload(2, "Two")]}
        with mock.patch.object(
                deezer_backend, "_api_get",
                side_effect=[playlist, tracks]):
            items, title = deezer_backend.extract_flat(
                "https://www.deezer.com/playlist/9")

        self.assertEqual(title, "Wrapped")
        self.assertEqual([item["title"] for item in items], ["One", "Two"])

    def test_api_get_accepts_full_next_url(self):
        resp = mock.Mock()
        resp.json.return_value = {"data": []}
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(deezer_backend.requests, "get",
                               return_value=resp) as get:
            deezer_backend._api_get(
                "https://api.deezer.com/playlist/9/tracks?index=25")

        self.assertEqual(
            get.call_args.args[0],
            "https://api.deezer.com/playlist/9/tracks?index=25")

    def test_api_get_prefixes_relative_paths(self):
        resp = mock.Mock()
        resp.json.return_value = {"data": []}
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(deezer_backend.requests, "get",
                               return_value=resp) as get:
            deezer_backend._api_get("/track/3135556")

        self.assertEqual(
            get.call_args.args[0], "https://api.deezer.com/track/3135556")


class DeezerPlaybackFileTests(unittest.TestCase):
    """Full playback comes from Deezer itself, decrypted, not from YouTube."""

    def setUp(self):
        deezer_backend._sessions.clear()
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        patcher = mock.patch.object(
            deezer_backend, "playback_cache_dir", return_value=self.cache.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self):
        return {
            "api_token": "csrf",
            "license_token": "license",
            "http": mock.Mock(),
            "http_lock": threading.Lock(),
        }

    def _metadata(self):
        return {"DATA": {"SNG_ID": "3135556", "SNG_TITLE": "Test track",
                         "ART_NAME": "Test artist",
                         "TRACK_TOKEN": "track-token"}}

    def test_playback_asks_for_the_cheapest_quality_first(self):
        media_response = mock.Mock()
        media_response.json.return_value = {"data": [{"media": [{
            "format": "MP3_128",
            "sources": [{"url": "https://media.invalid/track"}],
        }]}]}
        with mock.patch.object(deezer_backend, "_login",
                               return_value=self._session()), \
                mock.patch.object(deezer_backend, "_gw_call",
                                  return_value=self._metadata()), \
                mock.patch.object(deezer_backend.requests, "post",
                                  return_value=media_response) as post, \
                mock.patch.object(deezer_backend.requests, "get",
                                  return_value=mock.MagicMock()), \
                mock.patch.object(deezer_backend, "_decrypt_stream") as write:
            write.side_effect = lambda *args, **kwargs: open(
                args[2], "wb").write(b"audio")
            path = deezer_backend.playback_file(
                "https://www.deezer.com/track/3135556",
                {"deezer_arl": "test-arl", "deezer_format": "flac"})

        # Playing keeps nothing, so the download setting's FLAC is not what
        # playback waits on: the smallest stream that starts soonest is.
        self.assertEqual(
            post.call_args_list[0].kwargs["json"]["media"][0]["formats"],
            [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}])
        self.assertTrue(path.endswith("3135556.mp3"))
        self.assertEqual(os.path.getsize(path), len(b"audio"))

    def test_the_same_track_twice_is_not_fetched_twice(self):
        ready = os.path.join(self.cache.name, "3135556.mp3")
        with open(ready, "wb") as handle:
            handle.write(b"already decrypted")
        media_response = mock.Mock()
        media_response.json.return_value = {"data": [{"media": [{
            "format": "MP3_128",
            "sources": [{"url": "https://media.invalid/track"}],
        }]}]}
        with mock.patch.object(deezer_backend, "_login",
                               return_value=self._session()), \
                mock.patch.object(deezer_backend, "_gw_call",
                                  return_value=self._metadata()), \
                mock.patch.object(deezer_backend.requests, "post",
                                  return_value=media_response), \
                mock.patch.object(deezer_backend.requests, "get") as get:
            path = deezer_backend.playback_file(
                "https://www.deezer.com/track/3135556",
                {"deezer_arl": "test-arl", "deezer_format": "flac"})

        self.assertEqual(path, ready)
        get.assert_not_called()

    def test_playback_without_an_arl_says_so(self):
        with self.assertRaises(RuntimeError):
            deezer_backend.playback_file(
                "https://www.deezer.com/track/3135556",
                {"deezer_arl": "", "deezer_format": "flac"})

    def test_the_cache_keeps_only_the_last_few_tracks(self):
        for index in range(deezer_backend.PLAYBACK_CACHE_FILES + 4):
            path = os.path.join(self.cache.name, f"{index}.mp3")
            with open(path, "wb") as handle:
                handle.write(b"x")
            os.utime(path, (index, index))

        deezer_backend._prune_playback_cache()

        self.assertEqual(
            len(os.listdir(self.cache.name)),
            deezer_backend.PLAYBACK_CACHE_FILES)


class SidebDeezerPreviewTests(unittest.TestCase):
    def test_preview_url_from_track_id(self):
        with mock.patch.object(sideb_backend.requests, "get") as get:
            get.return_value.json.return_value = {
                "id": 3135556,
                "preview": "https://cdns-preview.dzcdn.net/stream/abc123",
            }
            get.return_value.raise_for_status = mock.Mock()
            url = sideb_backend.get_deezer_preview_url("3135556")
        self.assertEqual(url, "https://cdns-preview.dzcdn.net/stream/abc123")

    def test_preview_url_from_track_url(self):
        with mock.patch.object(sideb_backend.requests, "get") as get:
            get.return_value.json.return_value = {
                "id": 3135556,
                "preview": "https://cdns-preview.dzcdn.net/stream/abc123",
            }
            get.return_value.raise_for_status = mock.Mock()
            url = sideb_backend.get_deezer_preview_url(
                "https://www.deezer.com/track/3135556")
        self.assertEqual(url, "https://cdns-preview.dzcdn.net/stream/abc123")

    def test_preview_url_from_search_result_id(self):
        # Search results carry their track id as "deezer:<id>" or
        # "sideb:<id>"; preview.py hands that straight in and the id must be
        # pulled out of the prefix, not sent to the API verbatim.
        for item_id in ("deezer:3135556", "sideb:3135556"):
            with self.subTest(item_id=item_id):
                with mock.patch.object(sideb_backend.requests, "get") as get:
                    get.return_value.json.return_value = {
                        "id": 3135556,
                        "preview": "https://cdns-preview.dzcdn.net/stream/abc123",
                    }
                    get.return_value.raise_for_status = mock.Mock()
                    url = sideb_backend.get_deezer_preview_url(item_id)
                self.assertEqual(
                    get.call_args.args[0],
                    "https://api.deezer.com/track/3135556",
                )
                self.assertEqual(
                    url, "https://cdns-preview.dzcdn.net/stream/abc123"
                )

    def test_preview_url_ignores_non_track_ids(self):
        # An album or playlist id must not be mistaken for a track; the API
        # is never asked about it and the caller falls back to a search.
        with mock.patch.object(sideb_backend.requests, "get") as get:
            url = sideb_backend.get_deezer_preview_url("deezer:album:3135556")
        self.assertIsNone(url)
        get.assert_not_called()

    def test_preview_url_returns_none_on_error(self):
        with mock.patch.object(sideb_backend.requests, "get") as get:
            get.side_effect = OSError("network down")
            url = sideb_backend.get_deezer_preview_url("3135556")
        self.assertIsNone(url)


class SidebSearchCostTests(unittest.TestCase):
    def test_a_metadata_search_does_not_drag_in_the_downloader(self):
        # Importing Side B's audio provider pulls in yt-dlp and ytmusicapi,
        # about half a second of work that a search asking Deezer for
        # metadata has no use for. It used to run on the first search.
        import subprocess
        import sys

        probe = (
            "import sys;"
            "import sideb.providers.metadata.deezer;"
            "print('yt_dlp' in sys.modules, 'ytmusicapi' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["False", "False"])

    def test_the_audio_provider_is_still_reachable_by_name(self):
        from sideb.providers import AudioProvider, YouTubeAudio, is_instrumental

        self.assertEqual(YouTubeAudio.__name__, "YouTubeAudio")
        self.assertEqual(AudioProvider.__name__, "AudioProvider")
        self.assertTrue(is_instrumental("Song (Instrumental)"))

    def test_one_tls_context_serves_every_side_b_client(self):
        from sideb.utils.http import default_ssl_context

        # Building one parses the whole certifi bundle: about 25 ms, and
        # Side B builds its clients per search and per queued track.
        self.assertIs(default_ssl_context(), default_ssl_context())


if __name__ == "__main__":
    unittest.main()
