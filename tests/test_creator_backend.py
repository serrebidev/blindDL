# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blinddl import creator_backend


class _Session:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class CreatorBackendTests(unittest.TestCase):
    def test_provider_for_url_matches_only_exact_platform_domains(self):
        self.assertEqual(
            creator_backend.provider_for_url("https://onlyfans.com/example"),
            "onlyfans",
        )
        self.assertEqual(
            creator_backend.provider_for_url("https://justfor.fans/example"),
            "justforfans",
        )
        self.assertIsNone(creator_backend.provider_for_url(
            "https://notonlyfans.com/example"))

    def test_onlyfans_auth_file_validation_does_not_echo_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "auth.json")
            path.write_text('{"cookie":"auth_id=secret"}', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "sess") as raised:
                creator_backend._onlyfans_auth(str(path))

        self.assertNotIn("secret", str(raised.exception))

    def test_onlyfans_signing_header_matches_dynamic_rules(self):
        rules = {
            "static_param": "static",
            "checksum_indexes": [0, 2, 4],
            "checksum_constant": 7,
            "format": "{}:{}",
            "app_token": "token",
        }
        auth = {"user_agent": "Agent", "x_bc": "bc"}

        with mock.patch.object(creator_backend.time, "time", return_value=100):
            headers = creator_backend._onlyfans_headers(
                "https://onlyfans.com/api2/v2/users/me?x=1", auth, rules)

        self.assertEqual(headers["time"], "100")
        self.assertEqual(headers["app-token"], "token")
        self.assertEqual(headers["user-agent"], "Agent")
        self.assertRegex(headers["sign"], r"^[0-9a-f]{40}:\d+$")

    def test_onlyfans_inspection_skips_drm_and_returns_normal_media(self):
        session = _Session()
        creator = {
            "id": 5,
            "name": "Creator",
            "username": "creator",
            "postsCount": 1,
            "archivedPostsCount": 0,
        }
        posts = [{
            "id": 10,
            "media": [
                {"id": 20, "type": "video", "files": {
                    "full": {"url": "https://cdn.invalid/normal.mp4"}}},
                {"id": 21, "type": "video", "files": {
                    "full": {"url": None}, "drm": {"manifest": {}}}},
            ],
        }]
        auth = {"cookies": {}, "x_bc": "bc", "user_agent": "Agent"}
        with (mock.patch.object(
                creator_backend, "_onlyfans_auth", return_value=auth),
              mock.patch.object(
                  creator_backend, "_onlyfans_rules", return_value={}),
              mock.patch.object(
                  creator_backend, "_onlyfans_session", return_value=session),
              mock.patch.object(
                  creator_backend, "_onlyfans_json", return_value=creator),
              mock.patch.object(
                  creator_backend, "_onlyfans_posts", side_effect=[posts, []])):
            items, title = creator_backend.inspect_onlyfans(
                "https://onlyfans.com/creator", "auth.json")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["media_id"], "20")
        self.assertEqual(items[0]["provider"], "onlyfans")
        self.assertIn("1 DRM-protected", title)
        self.assertTrue(session.closed)

    def test_justforfans_parser_selects_best_hls_and_large_images(self):
        page = """
        <div mbsc-card class="mbsc-card" id="card-1">
          <div onclick="location.href='creator?post=42'"></div>
          <script>MakeMovieVideoJS('x', {
            "720": "https://cdn.invalid/720.m3u8",
            "1080": "https://cdn.invalid/1080.m3u8"
          })</script>
          <img class="galThumb" src="https://cdn.invalid/small.jpg">
          <img class="expandable" data-lazy="https://cdn.invalid/large.jpg">
        </div>
        """

        items, protected = creator_backend._jff_cards(
            page, "creator", "auth.json", "https://justfor.fans/creator")

        self.assertEqual(protected, 0)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["direct_url"],
                         "https://cdn.invalid/1080.m3u8")
        self.assertEqual(items[1]["direct_url"],
                         "https://cdn.invalid/large.jpg")
        self.assertEqual(items[0]["provider"], "justforfans")

    def test_justforfans_parser_skips_dash_protected_video(self):
        page = """
        <div mbsc-card class="mbsc-card" id="card-1">
          <div onclick="location.href='creator?post=42'"></div>
          <script>MakeMovieVideoJS('x', {
            "1080": "https://cdn.invalid/protected.mpd"
          })</script>
        </div>
        """

        items, protected = creator_backend._jff_cards(
            page, "creator", "auth.json", "https://justfor.fans/creator")

        self.assertEqual(items, [])
        self.assertEqual(protected, 1)


if __name__ == "__main__":
    unittest.main()
