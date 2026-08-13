# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import tempfile
import threading
import unittest
from unittest import mock

from blinddl import deezer_backend, search_order, sideb_backend


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
                mock.patch.object(deezer_backend, "_decrypt_stream"), \
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
                mock.patch.object(deezer_backend, "_decrypt_stream"), \
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
                mock.patch.object(deezer_backend, "_decrypt_stream"), \
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
                mock.patch.object(deezer_backend, "_decrypt_stream"), \
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

    def test_preview_url_returns_none_on_error(self):
        with mock.patch.object(sideb_backend.requests, "get") as get:
            get.side_effect = OSError("network down")
            url = sideb_backend.get_deezer_preview_url("3135556")
        self.assertIsNone(url)


if __name__ == "__main__":
    unittest.main()
