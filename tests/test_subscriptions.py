# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

from contextlib import nullcontext
import json
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest import mock

from blinddl import search_order, subscriptions


class _Queue:
    def __init__(self):
        self.ytdlp = []
        self.sideb = []
        self.applemusic = []
        self.soulseek = []
        self.folders = []

    def add_ytdlp(self, url, title, audio_only=None, folder=""):
        self.ytdlp.append((url, title, audio_only))
        self.folders.append(folder)

    def add_sideb(self, url, title, folder=""):
        self.sideb.append((url, title))
        self.folders.append(folder)

    def add_applemusic(self, url, title, folder=""):
        self.applemusic.append((url, title))
        self.folders.append(folder)

    def add_soulseek(self, payload, title):
        self.soulseek.append((payload, title))

    def batch_additions(self):
        return nullcontext()


def _release(album_id, title, artist="Band"):
    return {
        "id": f"deezer:album:{album_id}",
        "kind": "deezer_album",
        "title": title,
        "artist": artist,
        "album": title,
        "album_id": str(album_id),
        "url": f"https://www.deezer.com/album/{album_id}",
    }


def _track(track_id, title):
    return {
        "id": f"deezer:{track_id}",
        "kind": "deezer",
        "title": title,
        "url": f"https://www.deezer.com/track/{track_id}",
    }


def _shared(username, remote_path):
    return {
        "title": remote_path.rsplit("\\", 1)[-1],
        "kind": "soulseek",
        "username": username,
        "remote_path": remote_path,
        "folder": remote_path.rsplit("\\", 1)[0],
        "locked": False,
    }


def _item(item_id):
    return {"id": item_id, "title": item_id.upper(),
            "url": f"https://www.youtube.com/watch?v={item_id}"}


class _StoreCase:
    """A store on a scratch directory, with a queue that only records."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(
            subscriptions, "app_data_dir", lambda: self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.queue = _Queue()
        self.store = subscriptions.SubscriptionStore(
            {"cookies_from_browser": None, "sub_check_hours": 6}, self.queue)


class SubscriptionStoreTests(_StoreCase, unittest.TestCase):
    def _check(self, sub_id, items, title="Playlist"):
        with mock.patch.object(
                subscriptions.ytdlp_backend, "extract_flat",
                return_value=(items, title)):
            return self.store.check_one(sub_id)

    def test_only_unseen_items_are_queued(self):
        sub = self.store.add("https://www.youtube.com/playlist?list=PL1",
                             "Playlist", ["a"])
        count, error = self._check(sub["id"], [_item("a"), _item("b")])

        self.assertEqual((count, error), (1, ""))
        self.assertEqual([row[1] for row in self.queue.ytdlp], ["B"])
        self.assertEqual(self.store.get(sub["id"])["seen_ids"], ["a", "b"])

    def test_queued_items_land_in_a_folder_named_after_the_feed(self):
        # A subscription is a channel or playlist: what it publishes belongs
        # together, not loose among every other download.
        sub = self.store.add("https://www.youtube.com/@channel", "Channel", [])
        self._check(sub["id"], [_item("a")], title="The Channel")

        # The feed's own name, which the check has just refreshed.
        self.assertEqual(self.queue.folders, ["The Channel"])

    def test_new_subscriptions_default_to_most_recent(self):
        sub = self.store.add("https://www.youtube.com/hashtag/rimworld",
                             "RimWorld", [])

        self.assertEqual(sub["order"], search_order.ORDER_RECENT)
        self.assertGreater(sub["created_at"], 0)

    def test_check_forwards_each_subscriptions_saved_order(self):
        sub = self.store.add(
            "https://www.youtube.com/hashtag/rimworld", "RimWorld", [],
            order=search_order.ORDER_POPULAR)
        with mock.patch.object(
                subscriptions.ytdlp_backend, "extract_flat",
                return_value=([], "RimWorld")) as extract:
            self.store.check_one(sub["id"])

        self.assertEqual(
            extract.call_args.kwargs["order"], search_order.ORDER_POPULAR)
        self.assertEqual(
            extract.call_args.kwargs["limit"],
            subscriptions.ytdlp_backend.SUBSCRIPTION_FEED_LIMIT)

    def test_legacy_subscription_without_order_keeps_best_match(self):
        sub = self.store.add("https://www.youtube.com/results?search_query=x",
                             "X", [])
        del sub["order"]
        with mock.patch.object(
                subscriptions.ytdlp_backend, "extract_flat",
                return_value=([], "X")) as extract:
            self.store.check_one(sub["id"])

        self.assertEqual(
            extract.call_args.kwargs["order"], search_order.ORDER_RELEVANCE)

    def test_feed_order_can_be_changed(self):
        sub = self.store.add("https://www.youtube.com/hashtag/x", "X", [])
        self.store.set_order(sub["id"], search_order.ORDER_POPULAR)

        self.assertEqual(
            self.store.get(sub["id"])["order"],
            search_order.ORDER_POPULAR)

    def test_repeated_ids_in_one_listing_are_queued_once(self):
        sub = self.store.add("https://www.youtube.com/@x", "X", [])
        count, _error = self._check(
            sub["id"], [_item("a"), _item("a"), _item("b")])

        self.assertEqual(count, 2)
        self.assertEqual(len(self.queue.ytdlp), 2)

    def test_seen_ids_trim_drops_the_oldest_first(self):
        sub = self.store.add("https://www.youtube.com/playlist?list=PL1",
                             "Playlist", [],
                             order=search_order.ORDER_RELEVANCE)
        with mock.patch.object(subscriptions, "MAX_SEEN_IDS", 3):
            self._check(sub["id"], [_item(c) for c in "abcde"])

        self.assertEqual(self.store.get(sub["id"])["seen_ids"],
                         ["c", "d", "e"])

    def test_recent_feed_trim_retains_the_newest_ids(self):
        with mock.patch.object(subscriptions, "MAX_SEEN_IDS", 3):
            sub = self.store.add(
                "https://www.youtube.com/hashtag/example", "Example",
                list("abcde"), order=search_order.ORDER_RECENT)

        # The feed supplied a (newest) through e (oldest), while persistence
        # remains oldest-to-newest so the tail always contains recent IDs.
        self.assertEqual(sub["seen_ids"], ["c", "b", "a"])

    def test_recent_check_trim_retains_the_newest_ids(self):
        sub = self.store.add(
            "https://www.youtube.com/hashtag/example", "Example", [],
            order=search_order.ORDER_RECENT)
        with mock.patch.object(subscriptions, "MAX_SEEN_IDS", 3):
            self._check(sub["id"], [_item(c) for c in "abcde"])

        self.assertEqual(
            self.store.get(sub["id"])["seen_ids"], ["c", "b", "a"])

    def test_a_failed_check_reports_the_error_without_losing_state(self):
        sub = self.store.add("https://www.youtube.com/playlist?list=PL1",
                             "Playlist", ["a"])
        with mock.patch.object(
                subscriptions.ytdlp_backend, "extract_flat",
                side_effect=RuntimeError("no such playlist")):
            count, error = self.store.check_one(sub["id"])

        self.assertEqual(count, 0)
        self.assertEqual(error, "no such playlist")
        self.assertEqual(self.store.get(sub["id"])["seen_ids"], ["a"])
        self.assertEqual(self.queue.ytdlp, [])

    def test_wrong_shaped_saved_state_is_ignored(self):
        path = Path(self.dir.name) / "subscriptions.json"
        with path.open("w", encoding="utf-8") as stream:
            json.dump({"not": "a list"}, stream)

        restored = subscriptions.SubscriptionStore(
            {"cookies_from_browser": None, "sub_check_hours": 6}, self.queue
        )

        self.assertEqual(restored.snapshot(), [])

    def test_malformed_saved_rows_are_filtered_and_normalized(self):
        path = Path(self.dir.name) / "subscriptions.json"
        with path.open("w", encoding="utf-8") as stream:
            json.dump(
                [
                    None,
                    {"id": "", "url": "https://invalid"},
                    {"id": "valid", "url": "https://example", "seen_ids": {}},
                ],
                stream,
            )

        restored = subscriptions.SubscriptionStore(
            {"cookies_from_browser": None, "sub_check_hours": 6}, self.queue
        )

        self.assertEqual(len(restored.snapshot()), 1)
        self.assertEqual(restored.get("valid")["seen_ids"], [])
        self.assertEqual(restored.get("valid")["title"], "https://example")

    def test_overlapping_checks_do_not_queue_the_same_item_twice(self):
        sub = self.store.add("https://www.youtube.com/@x", "X", [])
        entered = threading.Event()
        release = threading.Event()

        def extract(*args, **kwargs):
            entered.set()
            release.wait(5)
            return [_item("a")], "X"

        results = []
        with mock.patch.object(
            subscriptions.ytdlp_backend, "extract_flat", side_effect=extract
        ):
            first = threading.Thread(
                target=lambda: results.append(self.store.check_one(sub["id"]))
            )
            second = threading.Thread(
                target=lambda: results.append(self.store.check_one(sub["id"]))
            )
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(self.queue.ytdlp), 1)
        self.assertEqual(sorted(results), [(0, ""), (1, "")])


class FollowedArtistTests(_StoreCase, unittest.TestCase):
    """An artist is followed by their releases, not by their tracks."""

    def _artist_sub(self, seen=()):
        return self.store.add(
            "https://www.deezer.com/artist/9", "Band", list(seen),
            kind=subscriptions.KIND_ARTIST)

    def test_a_new_release_arrives_as_a_record_in_a_folder_of_its_own(self):
        # A discography flattened into loose tracks is neither something to
        # watch for changes nor something to receive: one new album has to
        # count as one new thing and land together.
        sub = self._artist_sub(["deezer:album:1"])
        with mock.patch.object(
                subscriptions.deezer_backend, "artist_albums",
                return_value=([_release(1, "First"), _release(2, "Second")],
                              "Band")), \
                mock.patch.object(
                    subscriptions.deezer_backend, "extract_flat",
                    return_value=([_track("a", "A"), _track("b", "B")],
                                  "Second")):
            count, error = self.store.check_one(sub["id"])

        self.assertEqual((count, error), (1, ""))
        self.assertEqual([row[1] for row in self.queue.sideb], ["A", "B"])
        self.assertEqual(self.queue.folders, ["Band - Second"] * 2)
        self.assertEqual(self.store.get(sub["id"])["seen_ids"],
                         ["deezer:album:1", "deezer:album:2"])

    def test_a_release_that_could_not_be_read_is_tried_again_next_time(self):
        sub = self._artist_sub()
        with mock.patch.object(
                subscriptions.deezer_backend, "artist_albums",
                return_value=([_release(1, "First")], "Band")), \
                mock.patch.object(
                    subscriptions.deezer_backend, "extract_flat",
                    side_effect=RuntimeError("geo-blocked")):
            count, error = self.store.check_one(sub["id"])

        self.assertEqual(count, 0)
        self.assertIn("geo-blocked", error)
        self.assertEqual(self.store.get(sub["id"])["seen_ids"], [])

    def test_a_check_does_not_spend_the_rate_limit_counting_tracks(self):
        # Deezer allows fifty requests in five seconds, and the endpoint
        # that lists a discography leaves the track counts out. Filling
        # forty of them in costs forty requests for a number no
        # subscription ever shows -- and leaves none to read the new
        # release with, which is the only thing the check is there to do.
        with mock.patch.object(
                subscriptions.deezer_backend, "artist_albums",
                return_value=([], "Band")) as albums:
            subscriptions.artist_releases("https://www.deezer.com/artist/9")

        self.assertIs(albums.call_args.kwargs["track_counts"], False)

    def test_an_artist_can_be_followed_by_name(self):
        with mock.patch.object(
                subscriptions.deezer_backend, "search_artists",
                return_value=[{"id": "27", "name": "Daft Punk",
                               "url": "https://www.deezer.com/artist/27"}]):
            url, name = subscriptions.resolve_artist("daft punk")

        self.assertEqual(url, "https://www.deezer.com/artist/27")
        self.assertEqual(name, "Daft Punk")

    def test_a_link_to_something_that_is_not_an_artist_is_refused(self):
        with self.assertRaises(RuntimeError):
            subscriptions.resolve_artist("https://www.deezer.com/album/1")


class FollowedUserTests(_StoreCase, unittest.TestCase):
    """A Soulseek user is followed by what they share."""

    def _user_sub(self, seen=()):
        return self.store.add(
            subscriptions.USER_URL_PREFIX + "dj", "dj", list(seen),
            kind=subscriptions.KIND_USER, username="dj")

    def _browse(self, paths):
        return mock.patch.object(
            subscriptions.soulseek_backend, "browse_user",
            return_value=[{
                "name": "Music",
                "files": [_shared("dj", path) for path in paths],
            }])

    def test_a_newly_shared_file_is_queued_under_the_sharer(self):
        sub = self._user_sub()
        with self._browse(["Music\\Album\\one.flac"]):
            self.store.check_one(sub["id"])
        with self._browse(["Music\\Album\\one.flac",
                           "Music\\Album\\two.flac"]):
            count, error = self.store.check_one(sub["id"])

        self.assertEqual((count, error), (1, ""))
        payload, title = self.queue.soulseek[-1]
        self.assertEqual(title, "two.flac")
        self.assertEqual(payload["target_relative_path"],
                         "dj\\Album\\two.flac")

    def test_a_file_the_user_stopped_sharing_is_forgotten(self):
        # A share is listed whole, so what it no longer holds is not worth
        # remembering -- and remembering it would push a big sharer's oldest
        # files out of the seen list, where they would arrive again as new.
        sub = self._user_sub()
        with self._browse(["Music\\one.flac", "Music\\two.flac"]):
            self.store.check_one(sub["id"])
        with self._browse(["Music\\two.flac"]):
            count, _error = self.store.check_one(sub["id"])

        self.assertEqual(count, 0)
        self.assertEqual(len(self.store.get(sub["id"])["seen_ids"]), 1)

    def test_locked_files_are_not_offered(self):
        locked = _shared("dj", "Music\\private.flac")
        locked["locked"] = True
        with mock.patch.object(
                subscriptions.soulseek_backend, "browse_user",
                return_value=[{"name": "Music", "files": [locked]}]):
            items, title = subscriptions.user_files("dj", {})

        self.assertEqual((items, title), ([], "dj"))


class FollowedLinkTests(_StoreCase, unittest.TestCase):
    """A link is listed by whichever backend can read it."""

    def test_an_apple_music_playlist_goes_to_the_catalogue_that_can_read_it(
            self):
        # yt-dlp cannot read music.apple.com at all, so a followed Apple
        # Music playlist used to fail every check it was given.
        url = "https://music.apple.com/us/playlist/chill/pl.123"
        sub = self.store.add(url, "Chill", [])
        with mock.patch.object(
                subscriptions.applemusic_backend, "extract_flat",
                return_value=([{"id": "applemusic:1", "kind": "applemusic",
                                "title": "One",
                                "url": "https://music.apple.com/us/song/1"}],
                              "Chill")) as apple, \
                mock.patch.object(
                    subscriptions.ytdlp_backend, "extract_flat") as ytdlp:
            count, error = self.store.check_one(sub["id"])

        self.assertEqual((count, error), (1, ""))
        self.assertEqual(apple.call_count, 1)
        self.assertEqual(ytdlp.call_count, 0)
        self.assertEqual(self.queue.applemusic,
                         [("https://music.apple.com/us/song/1", "One")])

    def test_saved_rows_from_before_kinds_existed_are_still_links(self):
        path = Path(self.dir.name) / "subscriptions.json"
        with path.open("w", encoding="utf-8") as stream:
            json.dump([{"id": "old", "url": "https://example", "title": "Old"}],
                      stream)

        restored = subscriptions.SubscriptionStore(
            {"cookies_from_browser": None, "sub_check_hours": 6}, self.queue)

        self.assertEqual(restored.get("old")["kind"], subscriptions.KIND_FEED)


if __name__ == "__main__":
    unittest.main()
