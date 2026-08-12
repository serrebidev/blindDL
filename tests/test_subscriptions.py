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

    def add_ytdlp(self, url, title, audio_only=None):
        self.ytdlp.append((url, title, audio_only))

    def add_sideb(self, url, title):
        self.sideb.append((url, title))

    def batch_additions(self):
        return nullcontext()


def _item(item_id):
    return {"id": item_id, "title": item_id.upper(),
            "url": f"https://www.youtube.com/watch?v={item_id}"}


class SubscriptionStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
