# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import tempfile
import threading
import unittest
from unittest import mock

from blinddl import deezer_backend


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
            "audio_format": "flac",
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


if __name__ == "__main__":
    unittest.main()
