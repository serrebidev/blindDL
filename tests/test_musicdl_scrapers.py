# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Search/parse tests for the blindDL-added musicdl sources.

These three clients were ported from MusicGrabber (The Unlicense) into the
vendored musicdl tree; see musicdl/VENDORED.md. The tests exercise parsing and
the token-pool dance with mocked HTTP so they never touch the network.
"""

import tempfile
import unittest
from unittest import mock

from musicdl.modules.sources import MusicClientBuilder
from musicdl.modules.thirdpartysites import freeqobuz
from musicdl.modules.thirdpartysites.freeqobuz import (
    FreeQobuzMusicClient,
    resolve_qobuz_stream_url,
    search_qobuz_catalog,
)
from musicdl.modules.thirdpartysites.freemp3cloud import FreeMp3CloudMusicClient
from musicdl.modules.thirdpartysites.zvu4it import Zvu4ITMusicClient


class _FakeResponse:
    def __init__(self, text="", url="http://example.invalid/", status_code=200,
                 json_data=None):
        self.text, self.url, self.status_code = text, url, status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json or {}


class _FakeProgress:
    def __init__(self):
        self.descriptions = []

    def add_task(self, *args, **kwargs):
        return 1

    def update(self, task_id, *args, **kwargs):
        if "description" in kwargs:
            self.descriptions.append(kwargs["description"])


class _TinyTester:
    """Replacement for client.audio_link_tester.test: everything checks out."""

    def __init__(self, ext="mp3"):
        self.ext = ext
        self.urls = []

    def test(self, url, request_overrides=None, renew_session=True):
        self.urls.append(url)
        return {
            "ok": True,
            "ext": self.ext,
            "download_url": url,
            "file_size_bytes": 1234567,
            "file_size": "1.18 MB",
            "status_code": 200,
            "ctype": "audio/mpeg",
        }


def _patch_tester(**overrides):
    """Class-level stand-in for AudioLinkTester.test.

    musicdl's @usesearchheaderscookies decorator rebuilds each client's
    audio_link_tester on every search, so a per-instance fake would be
    discarded; patching the class method covers searches and direct parses
    alike and keeps the tests off the network.
    """
    status = {
        "ok": True,
        "ext": "mp3",
        "download_url": "",
        "file_size_bytes": 1234567,
        "file_size": "1.18 MB",
        "status_code": 200,
        "ctype": "audio/mpeg",
    }
    status.update(overrides)

    def _test(self, url, request_overrides=None, renew_session=True):
        status["download_url"] = url
        return dict(status)

    return mock.patch("musicdl.modules.utils.misc.AudioLinkTester.test",
                      new=_test)


def _client(cls):
    return cls(search_size_per_source=10, disable_print=True,
               work_dir=tempfile.mkdtemp(prefix="blinddl-musicdl-test-"))


ZVU4IT_BLOCK = """
<div class="f-table">
  <div class="artist-name"><a href="/artist/114">Radiohead</a></div>
  <div class="track-name">Creep</div>
  <div class="time-text">3:59</div>
  <a class="mp3" href="//data.zvu4it.org/download-track/abc123.mp3"></a>
  <img src="//img.zvu4it.org/img/123">
</div>
"""

ZVU4IT_PAGE = ZVU4IT_BLOCK + ZVU4IT_BLOCK + """
<div class="f-table">
  <div class="artist-name"><a href="/artist/114">Radiohead</a></div>
  <div class="track-name">Let Down</div>
  <div class="time-text">5:00</div>
  <a class="mp3" href="//data.zvu4it.org/download-track/def456.mp3"></a>
</div>
</div>
<div id="amp-player"></div>
"""

FMC_LANDING = """
<html><body>
<input name="__RequestVerificationToken" type="hidden" value="tok123">
</body></html>
"""

FMC_SEARCH = """
<div class="play-item">
  <div class="s-artist">Radiohead</div>
  <div class="s-title">Creep</div>
  <div class="s-time">3:59</div>
  <div class="s-hq"></div>
  <div class="downl"><a href="https://cdnm.meln.top/mr/Radiohead%20-%20Creep.mp3"></a></div>
</div>
<div class="play-item">
  <div class="s-artist">Radiohead</div>
  <div class="s-title">Creep (live)</div>
  <div class="s-time">4:02</div>
  <div class="downl"><a href="https://cdnm.meln.top/mr/Radiohead%20-%20Creep%20(live).mp3"></a></div>
</div>
"""

QOBUZ_ITEM = {
    "id": 33933680,
    "title": "Creep",
    "isrc": "GBAYE9200070",
    "duration": 239,
    "performer": {"name": "Radiohead"},
    "album": {
        "title": "Pablo Honey",
        "image": {"large": "https://img.qobuz.com/cover.jpg", "small": ""},
    },
}

TOKENS = [
    {"token": "tok-bad", "app_id": "111", "app_secret": "secret1", "country": "FR"},
    {"token": "tok-good", "app_id": "222", "app_secret": "secret2", "country": "US"},
]


class Zvu4ITTests(unittest.TestCase):
    def test_parses_result_block(self):
        client = _client(Zvu4ITMusicClient)
        # The parser checks the MP3 link before it builds anything, so
        # without the stand-in tester this one test reached out to
        # zvu4it.org for a URL out of a fixture -- and a build host whose
        # network said otherwise failed on a parser that was working.
        with _patch_tester():
            song = client._parsesearchresultfromblock(ZVU4IT_BLOCK)
        self.assertEqual(song.cover_url, "https://img.zvu4it.org/img/123")

        self.assertTrue(song.with_valid_download_url)
        self.assertEqual(song.song_name, "Creep")
        self.assertEqual(song.singers, "Radiohead")
        self.assertEqual(song.duration_s, 239)
        self.assertEqual(song.duration, "00:03:59")
        self.assertEqual(song.ext, "mp3")
        self.assertEqual(song.download_url,
                         "https://data.zvu4it.org/download-track/abc123.mp3")

    def test_search_dedupes_same_download_url_and_honours_limit(self):
        client = _client(Zvu4ITMusicClient)
        client.search_size_per_page = 2
        songs = []
        with _patch_tester(), mock.patch.object(client, "get", return_value=_FakeResponse(
                text=ZVU4IT_PAGE)):
            client._search("radiohead", client._constructsearchurls("radiohead")[0],
                           {}, songs, _FakeProgress())
        # Creep appears twice in the page but shares one download URL, so it is
        # deduped; the second distinct track fills the size limit.
        self.assertEqual({s.song_name for s in songs}, {"Creep", "Let Down"})

    def test_block_without_link_is_skipped(self):
        client = _client(Zvu4ITMusicClient)
        song = client._parsesearchresultfromblock("<div class='f-table'></div>")
        self.assertFalse(song.with_valid_download_url)


class FreeMp3CloudTests(unittest.TestCase):
    def setUp(self):
        self.client = _client(FreeMp3CloudMusicClient)

    def test_search_harvests_token_and_posts_hq_marker(self):
        calls = {}

        def fake_post(url, **kwargs):
            calls["url"], calls["data"] = url, kwargs.get("data", {})
            return _FakeResponse(text=FMC_SEARCH)

        with _patch_tester(), \
             mock.patch.object(self.client, "get", return_value=_FakeResponse(
                 text=FMC_LANDING, url="https://a2.freemp3cloud.com/")), \
             mock.patch.object(self.client, "post", side_effect=fake_post):
            songs = []
            self.client._search("radiohead creep",
                                self.client._constructsearchurls("radiohead creep")[0],
                                {}, songs, _FakeProgress())

        self.assertEqual(len(songs), 2)
        # the search POST goes to wherever the landing page actually answered
        self.assertEqual(calls["url"], "https://a2.freemp3cloud.com/")
        self.assertEqual(calls["data"]["searchSong"], "radiohead creep")
        self.assertEqual(calls["data"]["__RequestVerificationToken"], "tok123")
        self.assertEqual(songs[0].raw_data["search"]["quality"], "320kbps")
        self.assertEqual(songs[1].raw_data["search"]["quality"], "128kbps")
        self.assertEqual(songs[0].bitrate, 320)
        self.assertEqual(songs[1].bitrate, 128)
        self.assertEqual(songs[0].duration, "00:03:59")
        self.assertEqual(songs[1].duration, "00:04:02")

    def test_missing_antiforgery_token_degrades_gracefully(self):
        songs = []
        with mock.patch.object(self.client, "get", return_value=_FakeResponse(
                text="<html></html>")), \
             mock.patch.object(self.client, "post") as post:
            self.client._search("x", "https://g2.freemp3cloud.com/", {}, songs,
                                _FakeProgress())
        self.assertEqual(songs, [])
        post.assert_not_called()


class FreeQobuzTests(unittest.TestCase):
    def setUp(self):
        freeqobuz._known_bad.clear()
        freeqobuz._good_token = None
        freeqobuz._pool_cache = []
        freeqobuz._pool_cache_at = 0.0

    def test_resolve_walks_pool_and_skips_samples(self):
        def fake_signed(token, path, params, signed_concat=None):
            if token["token"] == "tok-bad":
                return {"sample": True}
            return {"url": "https://streaming-qobuz-std.akamaized.net/file?eid=1",
                    "format_id": 6}

        with mock.patch("musicdl.modules.thirdpartysites.freeqobuz._fetch_shared_tokens",
                        return_value=TOKENS), \
             mock.patch("musicdl.modules.thirdpartysites.freeqobuz._signed_call",
                        side_effect=fake_signed):
            url = resolve_qobuz_stream_url(33933680)

        self.assertEqual(url, "https://streaming-qobuz-std.akamaized.net/file?eid=1")
        # the sample-only token is written off for this cycle, the winner remembered
        self.assertEqual(freeqobuz._known_bad, {"tok-bad"})
        self.assertEqual(freeqobuz._good_token, "tok-good")

    def test_catalog_search_returns_items(self):
        with mock.patch("musicdl.modules.thirdpartysites.freeqobuz._fetch_shared_tokens",
                        return_value=TOKENS), \
             mock.patch("musicdl.modules.thirdpartysites.freeqobuz._signed_call",
                        return_value={"tracks": {"items": [QOBUZ_ITEM]}}):
            items = search_qobuz_catalog("radiohead creep", 5)
        self.assertEqual(items, [QOBUZ_ITEM])

    def test_client_search_builds_flac_songinfo(self):
        client = _client(FreeQobuzMusicClient)
        with _patch_tester(ext="m4a"), \
             mock.patch("musicdl.modules.thirdpartysites.freeqobuz.search_qobuz_catalog",
                        return_value=[QOBUZ_ITEM]), \
             mock.patch("musicdl.modules.thirdpartysites.freeqobuz.resolve_qobuz_stream_url",
                        return_value="https://streaming-qobuz-std.akamaized.net/file?eid=1"):
            songs = []
            client._search("radiohead creep", "qobuz://catalog-search", {}, songs,
                           _FakeProgress())

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].song_name, "Creep")
        self.assertEqual(songs[0].singers, "Radiohead")
        self.assertEqual(songs[0].album, "Pablo Honey")
        self.assertEqual(songs[0].identifier, "33933680")
        self.assertEqual(songs[0].ext, "flac")  # not m4a despite the tester
        self.assertEqual(songs[0].download_url,
                         "https://streaming-qobuz-std.akamaized.net/file?eid=1")
        self.assertTrue(songs[0].with_valid_download_url)

    def test_client_skips_items_without_stream(self):
        client = _client(FreeQobuzMusicClient)
        with mock.patch("musicdl.modules.thirdpartysites.freeqobuz.search_qobuz_catalog",
                        return_value=[QOBUZ_ITEM]), \
             mock.patch("musicdl.modules.thirdpartysites.freeqobuz.resolve_qobuz_stream_url",
                        return_value=None):
            songs = []
            client._search("radiohead creep", "qobuz://catalog-search", {}, songs,
                           _FakeProgress())
        self.assertEqual(songs, [])


class RegistrationTests(unittest.TestCase):
    def test_new_sources_registered(self):
        for name in ("Zvu4ITMusicClient", "FreeMp3CloudMusicClient",
                     "FreeQobuzMusicClient"):
            self.assertIn(name, MusicClientBuilder.REGISTERED_MODULES)


if __name__ == "__main__":
    unittest.main()