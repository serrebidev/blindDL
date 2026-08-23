# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import threading
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import requests

from blinddl import podcast_archiver


LIVE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
 xmlns:atom="http://www.w3.org/2005/Atom">
 <channel>
  <title>Long Running Show</title>
  <itunes:new-feed-url>https://new.example/feed.xml</itunes:new-feed-url>
  <atom:link rel="self" href="https://new.example/feed.xml" />
  <item><title>Current episode</title><guid>current</guid>
   <pubDate>Sun, 23 Aug 2026 10:00:00 GMT</pubDate>
   <itunes:duration>1:02:03</itunes:duration>
   <enclosure url="https://new.example/audio/current.mp3" type="audio/mpeg" />
  </item>
  <item><title>Moved episode</title><guid>new-host-guid</guid>
   <pubDate>Sat, 22 Aug 2026 10:00:00 GMT</pubDate>
   <enclosure url="https://new.example/audio/moved.mp3" type="audio/mpeg" />
  </item>
 </channel>
</rss>"""

ARCHIVED_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Old Show Name</title>
 <item><title>Moved episode</title><guid>old-host-guid</guid>
  <pubDate>Sat, 22 Aug 2026 10:00:00 GMT</pubDate>
  <enclosure url="https://old.example/files/moved.mp3" type="audio/mpeg" />
 </item>
 <item><title>Lost episode</title><guid>lost</guid>
  <pubDate>Fri, 21 Aug 2020 09:00:00 GMT</pubDate>
  <enclosure url="https://old.example/files/lost.mp3?utm_source=feed" />
 </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Atom Show</title>
 <link rel="self" href="https://atom.example/feed" />
 <entry><title>Atom episode</title><id>atom-one</id>
  <published>2025-01-02T03:04:05Z</published>
  <link rel="enclosure" href="https://atom.example/one.m4a" />
 </entry>
</feed>"""


class Response:
    def __init__(self, *, content=b"", payload=None, url="", status=200,
                 headers=None):
        self.content = content
        self._payload = payload
        self.url = url
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class PodcastParsingTests(unittest.TestCase):
    def test_rss_extracts_playable_fields_and_moved_feed(self):
        feed = podcast_archiver.parse_feed(
            LIVE_RSS, "https://old.example/feed.xml")

        self.assertEqual(feed.title, "Long Running Show")
        self.assertEqual(len(feed.episodes), 2)
        self.assertEqual(feed.episodes[0]["duration_s"], 3723)
        self.assertEqual(feed.episodes[0]["published_date"], "2026-08-23")
        self.assertEqual(feed.related_urls, ["https://new.example/feed.xml"])

    def test_atom_enclosures_are_playable(self):
        feed = podcast_archiver.parse_feed(ATOM, "https://atom.example/feed")

        self.assertEqual(feed.episodes[0]["title"], "Atom episode")
        self.assertEqual(feed.episodes[0]["direct_url"],
                         "https://atom.example/one.m4a")

    def test_host_migration_deduplicates_by_title_and_date(self):
        live = podcast_archiver.parse_feed(
            LIVE_RSS, "https://new.example/feed.xml").episodes
        archived = podcast_archiver.parse_feed(
            ARCHIVED_RSS, "https://old.example/feed.xml", "20200102030405").episodes

        episodes = podcast_archiver.deduplicate_episodes(live + archived)

        self.assertEqual([item["title"] for item in episodes], [
            "Current episode", "Moved episode", "Lost episode"])
        moved = next(item for item in episodes if item["title"] == "Moved episode")
        self.assertEqual(moved["url"], "https://new.example/audio/moved.mp3")
        self.assertEqual(moved["copies_found"], 2)

    def test_combined_rss_contains_each_unique_enclosure(self):
        episodes = podcast_archiver.deduplicate_episodes(
            podcast_archiver.parse_feed(
                LIVE_RSS, "https://new.example/feed.xml").episodes)
        archive = podcast_archiver.PodcastArchive(
            "Long Running Show", "https://new.example/feed.xml", episodes,
            ["https://new.example/feed.xml"], snapshots_loaded=4)

        root = ET.fromstring(podcast_archiver.combined_rss(archive))

        items = root.findall("./channel/item")
        self.assertEqual(len(items), 2)
        self.assertEqual(
            items[0].find("enclosure").attrib["url"],
            "https://new.example/audio/current.mp3")


class PodcastDirectoryTests(unittest.TestCase):
    def test_recognizes_common_podcast_urls_without_stealing_other_media(self):
        self.assertTrue(podcast_archiver.looks_like_podcast_url(
            "https://feeds.simplecast.com/MhX_XZQZ"))
        self.assertTrue(podcast_archiver.looks_like_podcast_url(
            "https://example.test/shows/name/rss"))
        self.assertTrue(podcast_archiver.looks_like_podcast_url(
            "https://podcasts.apple.com/ca/podcast/show/id12345"))
        self.assertFalse(podcast_archiver.looks_like_podcast_url(
            "https://music.apple.com/ca/album/show/12345"))
        self.assertFalse(podcast_archiver.looks_like_podcast_url(
            "https://www.youtube.com/feeds/videos.xml?channel_id=123"))

    def test_transient_timeout_is_retried(self):
        get = mock.Mock(side_effect=[
            requests.Timeout("slow"),
            Response(payload={"results": []}),
        ])

        with mock.patch.object(podcast_archiver.time, "sleep") as sleep:
            results = podcast_archiver.search_apple_podcasts("Show", get=get)

        self.assertEqual(results, [])
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_apple_search_returns_unique_feed_urls(self):
        payload = {"results": [
            {"collectionId": 7, "collectionName": "The Show",
             "artistName": "Publisher", "feedUrl": "https://show/feed",
             "trackCount": 1300},
            {"collectionId": 7, "collectionName": "The Show",
             "artistName": "Publisher", "feedUrl": "https://show/feed"},
            {"collectionId": 8, "collectionName": "No feed"},
        ]}
        get = mock.Mock(return_value=Response(payload=payload))

        results = podcast_archiver.search_apple_podcasts(
            "The Show", country="CA", get=get)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["feed_url"], "https://show/feed")
        self.assertEqual(results[0]["track_count"], 1300)
        self.assertEqual(get.call_args.kwargs["params"]["entity"], "podcast")

    def test_apple_page_resolves_to_rss(self):
        get = mock.Mock(return_value=Response(payload={"results": [{
            "feedUrl": "https://show.example/rss"}]}))

        url = podcast_archiver.resolve_podcast_url(
            "https://podcasts.apple.com/ca/podcast/show/id12345", get=get)

        self.assertEqual(url, "https://show.example/rss")
        self.assertEqual(get.call_args.kwargs["params"]["id"], "12345")

    def test_gpodder_search_normalizes_public_directory_results(self):
        get = mock.Mock(return_value=Response(payload=[{
            "url": "https://show.example/rss",
            "title": "The Show",
            "author": "A Publisher",
        }]))

        results = podcast_archiver.search_gpodder("Show", get=get)

        self.assertEqual(results[0]["feed_url"], "https://show.example/rss")
        self.assertEqual(results[0]["source"], "gPodder")

    def test_fyyd_and_podverse_results_expose_feed_urls(self):
        fyyd_get = mock.Mock(return_value=Response(payload={"data": [{
            "id": 8, "title": "FY Show", "author": "FY Publisher",
            "xmlURL": "https://fy.example/feed.xml",
        }]}))
        podverse_get = mock.Mock(return_value=Response(payload=[[{
            "id": "pv", "title": "PV Show",
            "feedUrls": [{"url": "https://pv.example/podcast.rss"}],
        }], 1]))

        fyyd = podcast_archiver.search_fyyd("Show", get=fyyd_get)
        podverse = podcast_archiver.search_podverse(
            "Show", get=podverse_get)

        self.assertEqual(fyyd[0]["feed_url"], "https://fy.example/feed.xml")
        self.assertEqual(podverse[0]["feed_url"],
                         "https://pv.example/podcast.rss")

    def test_combined_directory_search_deduplicates_feed_addresses(self):
        apple = [{
            "title": "The Show", "artist": "Publisher",
            "feed_url": "https://show.example/rss",
            "url": "https://show.example/rss", "track_count": 10,
            "source": "Apple Podcasts",
        }]
        gpodder = [{
            "title": "The Show", "artist": "",
            "feed_url": "http://show.example/rss/",
            "url": "http://show.example/rss/", "track_count": 0,
            "source": "gPodder",
        }]
        with (
            mock.patch.object(podcast_archiver, "search_apple_podcasts",
                              return_value=apple),
            mock.patch.object(podcast_archiver, "search_gpodder",
                              return_value=gpodder),
            mock.patch.object(podcast_archiver, "search_fyyd",
                              side_effect=requests.Timeout("offline")),
            mock.patch.object(podcast_archiver, "search_podverse",
                              return_value=[]),
        ):
            results = podcast_archiver.search_podcasts("The Show")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["track_count"], 10)
        self.assertEqual(results[0]["source"], "Apple Podcasts, gPodder")


class PodcastArchiveTests(unittest.TestCase):
    def _get(self, url, params=None, **_kwargs):
        if url == podcast_archiver.WAYBACK_CDX_URL:
            if params["url"] == "https://old.example/feed.xml":
                return Response(payload=[
                    ["timestamp", "original", "digest", "statuscode", "mimetype"],
                    ["20200102030405", "https://old.example/feed.xml",
                     "digest-one", "200", "application/xml"],
                ])
            return Response(payload=[
                ["timestamp", "original", "digest", "statuscode", "mimetype"]])
        if url == (
                "https://web.archive.org/web/20200102030405id_/"
                "https://old.example/feed.xml"):
            return Response(content=ARCHIVED_RSS, url=url)
        if url == "https://old.example/feed.xml":
            return Response(content=LIVE_RSS, url="https://new.example/feed.xml")
        if url == "https://new.example/feed.xml":
            return Response(content=LIVE_RSS, url=url)
        raise AssertionError(f"Unexpected URL: {url}")

    def test_walks_redirect_and_new_feed_then_merges_archived_versions(self):
        progress = mock.Mock()

        archive = podcast_archiver.archive_podcast(
            "https://old.example/feed.xml", get=self._get, progress=progress,
            cache_dir=False)

        self.assertEqual(archive.title, "Long Running Show")
        self.assertEqual(len(archive.episodes), 3)
        self.assertEqual(archive.snapshots_found, 1)
        self.assertEqual(archive.snapshots_loaded, 1)
        self.assertIn("https://new.example/feed.xml", archive.feed_urls)
        self.assertTrue(progress.called)

    def test_current_only_never_calls_wayback(self):
        get = mock.Mock(return_value=Response(
            content=LIVE_RSS, url="https://new.example/feed.xml"))

        archive = podcast_archiver.archive_podcast(
            "https://new.example/feed.xml", include_wayback=False, get=get,
            cache_dir=False)

        self.assertEqual(len(archive.episodes), 2)
        self.assertFalse(any(call.args[0] == podcast_archiver.WAYBACK_CDX_URL
                             for call in get.call_args_list))

    def test_readable_snapshots_are_cached_between_scans(self):
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            return self._get(url, **kwargs)

        with tempfile.TemporaryDirectory() as cache:
            for _run in range(2):
                podcast_archiver.archive_podcast(
                    "https://old.example/feed.xml", get=get, cache_dir=cache)

        replay_url = (
            "https://web.archive.org/web/20200102030405id_/"
            "https://old.example/feed.xml")
        self.assertEqual(calls.count(replay_url), 1)

    def test_temporary_wayback_html_response_is_retried(self):
        replay_url = (
            "https://web.archive.org/web/20200102030405id_/"
            "https://old.example/feed.xml")
        replay_calls = 0

        def get(url, **kwargs):
            nonlocal replay_calls
            if url == replay_url:
                replay_calls += 1
                if replay_calls == 1:
                    return Response(
                        content=b"<html>Temporarily unavailable</html>",
                        url=url)
            return self._get(url, **kwargs)

        with mock.patch.object(podcast_archiver.time, "sleep"):
            archive = podcast_archiver.archive_podcast(
                "https://old.example/feed.xml", get=get, cache_dir=False)

        self.assertEqual(len(archive.episodes), 3)
        self.assertEqual(replay_calls, 2)

    def test_cancelled_scan_stops_before_network_access(self):
        cancel = threading.Event()
        cancel.set()

        with self.assertRaises(podcast_archiver.PodcastArchiveCancelled):
            podcast_archiver.archive_podcast(
                "https://show.example/feed", get=mock.Mock(),
                cancel_event=cancel, cache_dir=False)


if __name__ == "__main__":
    unittest.main()
