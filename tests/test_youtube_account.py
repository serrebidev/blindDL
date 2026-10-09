# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from blinddl import youtube_account, ytdlp_backend


def _tile(video_id, title, channel, clock):
    return {"tileRenderer": {
        "contentType": "TILE_CONTENT_TYPE_VIDEO",
        "contentId": video_id,
        "header": {"tileHeaderRenderer": {"thumbnailOverlays": [
            {"thumbnailOverlayTimeStatusRenderer": {
                "text": {"simpleText": clock}}}]}},
        "metadata": {"tileMetadataRenderer": {
            "title": {"simpleText": title},
            "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {
                "text": {"runs": [{"text": channel}]}}}]}}]}},
    }}


class YouTubeAccountTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"BLINDDL_APP_DATA_DIR": folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_feed_links_are_recognised(self):
        feed = youtube_account.feed_for
        self.assertEqual(
            feed("https://www.youtube.com/feed/subscriptions"), "subscriptions")
        self.assertEqual(feed("https://youtube.com/playlist?list=WL"), "WL")
        self.assertEqual(feed("https://www.youtube.com/playlist?list=LL"), "LL")
        self.assertIsNone(feed("https://www.youtube.com/playlist?list=PLabc"))
        self.assertIsNone(feed("https://example.com/feed/subscriptions"))

    def test_tv_tiles_become_items_and_pages_continue(self):
        page = {"contents": [
            _tile("abcdefghijk", "A video", "A channel", "1:02:03"),
            {"tileRenderer": {"contentType": "TILE_CONTENT_TYPE_CHANNEL"}},
            {"nextContinuationData": {"continuation": "more"}},
        ]}
        items, continuation = youtube_account.parse_page(page)
        self.assertEqual(continuation, "more")
        self.assertEqual(items, [{
            "id": "abcdefghijk", "title": "A video",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "duration": 3723, "uploader": "A channel",
        }])

    def test_signed_in_feed_skips_yt_dlp(self):
        youtube_account._save({"refresh_token": "r", "access_token": "a",
                               "expires_at": time.time() + 3600})
        pages = [{"x": [_tile("abcdefghijk", "One", "C", "1:00"),
                        {"nextContinuationData": {"continuation": "c"}}]},
                 {"x": [_tile("bcdefghijkl", "Two", "C", "2:00")]}]
        with mock.patch.object(youtube_account, "_post",
                               side_effect=pages) as post, \
                mock.patch.object(ytdlp_backend.yt_dlp, "YoutubeDL") as ydl:
            items, title = ytdlp_backend.extract_flat(
                "https://www.youtube.com/feed/subscriptions")
        ydl.assert_not_called()
        self.assertEqual(title, "YouTube subscriptions")
        self.assertEqual([i["title"] for i in items], ["One", "Two"])
        self.assertEqual(post.call_args_list[0].args[2],
                         {"Authorization": "Bearer a"})

    def test_expired_access_token_is_refreshed_and_kept(self):
        youtube_account._save({"refresh_token": "r", "access_token": "old",
                               "expires_at": 0})
        with mock.patch.object(youtube_account, "_post_token", return_value={
                "access_token": "new", "expires_in": 3600}):
            self.assertEqual(youtube_account.access_token(), "new")
        with open(youtube_account._token_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["access_token"], "new")

    def test_withdrawn_sign_in_says_so(self):
        youtube_account._save({"refresh_token": "r", "access_token": "old",
                               "expires_at": 0})
        with mock.patch.object(youtube_account, "_post_token",
                               return_value={"error": "invalid_grant"}), \
                self.assertRaisesRegex(youtube_account.SignInError,
                                       "Sign in\\s+again"):
            youtube_account.access_token()

    def test_device_code_waits_then_signs_in(self):
        with mock.patch.object(youtube_account, "_post_token", return_value={
                "error": "authorization_pending"}):
            self.assertFalse(youtube_account.finish_sign_in("d"))
        self.assertFalse(youtube_account.signed_in())
        with mock.patch.object(youtube_account, "_post_token", return_value={
                "access_token": "a", "refresh_token": "r",
                "expires_in": 3600}):
            self.assertTrue(youtube_account.finish_sign_in("d"))
        self.assertTrue(youtube_account.signed_in())
        with mock.patch.object(youtube_account.urllib.request, "urlopen"):
            youtube_account.sign_out()
        self.assertFalse(youtube_account.signed_in())


if __name__ == "__main__":
    unittest.main()
