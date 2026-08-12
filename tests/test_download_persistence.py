# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blinddl import torrent_engine
from blinddl.config import DEFAULTS
from blinddl.downloader import (
    ADD_ALREADY_ACTIVE,
    ADD_RESUMED,
    ADD_SKIPPED,
    DownloadItem,
    DownloadQueue,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_DOWNLOADING,
    STATUS_ERROR,
    STATUS_QUEUED,
)
from musicdl.modules.utils.data import SongInfo


class DownloadPersistenceTests(unittest.TestCase):
    def config(self):
        config = copy.deepcopy(DEFAULTS)
        config["torrent_engine"] = False
        return config

    def test_active_and_finished_rows_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            active = DownloadItem("Active", "soulseek", {
                "username": "friend",
                "remote_path": "Music\\Track.flac",
                "picture": b"small",
            })
            active.status = STATUS_DOWNLOADING
            done = DownloadItem("Done", "ytdlp", "https://example.invalid/media")
            done.status = STATUS_DONE
            cancelled = DownloadItem("Cancelled", "book", {"url": "example"})
            cancelled.status = STATUS_CANCELLED
            queue.items = [active, done, cancelled]
            queue._save_state()

            restored = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )

        self.assertEqual(
            [item.status for item in restored.items],
            [STATUS_QUEUED, STATUS_DONE, STATUS_CANCELLED],
        )
        self.assertEqual(restored.items[0].payload["picture"], b"small")
        self.assertEqual(restored.items[0].id, active.id)

    def test_saved_soulseek_settings_error_is_automatically_requeued(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            item = DownloadItem(
                "1010 The Difference.m4a",
                "soulseek",
                {"username": "friend", "remote_path": "1010 The Difference.m4a"},
            )
            item.status = STATUS_ERROR
            item.percent = 71
            item.error = (
                "Soulseek settings changed during this transfer. Queue it again."
            )
            queue.items = [item]
            queue._save_state()

            restored = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )

        self.assertEqual(restored.items[0].status, STATUS_QUEUED)
        self.assertEqual(restored.items[0].percent, 71)
        self.assertEqual(restored.items[0].error, "")

    def test_batch_additions_persists_once_and_notifies_every_item(self):
        with tempfile.TemporaryDirectory() as folder:
            notified = []
            queue = DownloadQueue(
                self.config(),
                notified.append,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            with mock.patch.object(queue, "_save_state") as save:
                with queue.batch_additions():
                    for number in range(100):
                        queue.add_ytdlp(f"https://example/{number}", str(number))

        save.assert_called_once_with()
        self.assertEqual(len(notified), 100)
        self.assertEqual(queue.counts(), (0, 100, 0, 0))

    def test_requeueing_completed_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            completed = queue.add_ytdlp(
                "https://example.invalid/watch?v=same", "Finished"
            )
            completed.status = STATUS_DONE
            queue._notify(completed)

            result = queue.add_ytdlp(
                "https://example.invalid/watch?v=same", "Fresh search title"
            )

        self.assertIs(result, completed)
        self.assertEqual(result.add_action, ADD_SKIPPED)
        self.assertEqual(result.status, STATUS_DONE)
        self.assertEqual(len(queue.items), 1)

    def test_requeueing_known_partial_resumes_existing_row(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            partial = queue.add_soulseek({
                "username": "Friend",
                "remote_path": "Music\\Album\\Track.flac",
                "average_speed": 10,
            }, "Old title")
            partial.status = STATUS_CANCELLED
            partial.percent = 63
            partial.error = "cancelled"
            partial.cancel_event.set()
            queue._notify(partial)

            result = queue.add_soulseek({
                "username": "friend",
                "remote_path": "Music/Album/Track.flac",
                "average_speed": 9000,
            }, "Current title")

        self.assertIs(result, partial)
        self.assertEqual(result.add_action, ADD_RESUMED)
        self.assertEqual(result.status, STATUS_QUEUED)
        self.assertEqual(result.percent, 63)
        self.assertEqual(result.error, "")
        self.assertFalse(result.cancel_event.is_set())
        self.assertEqual(result.title, "Current title")
        self.assertEqual(result.payload["average_speed"], 9000)
        self.assertEqual(len(queue.items), 1)
        self.assertEqual(queue.counts(), (0, 1, 0, 0))

    def test_requeueing_active_file_does_not_duplicate_it(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            active = queue.add_archive({
                "identifier": "radio-show",
                "file_name": "episode.mp3",
                "direct_url": "https://archive.invalid/old-token",
            }, "Episode")

            result = queue.add_archive({
                "identifier": "radio-show",
                "file_name": "episode.mp3",
                "direct_url": "https://archive.invalid/new-token",
                "size_bytes": 1234,
            }, "Episode")

        self.assertIs(result, active)
        self.assertEqual(result.add_action, ADD_ALREADY_ACTIVE)
        self.assertEqual(len(queue.items), 1)

    def test_same_video_url_with_different_output_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            queue.add_ytdlp("https://example.invalid/media", "Audio", True)
            queue.add_ytdlp("https://example.invalid/media", "Video", False)

        self.assertEqual(len(queue.items), 2)

    def test_musicdl_song_info_round_trips_without_pickle(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            item = DownloadItem(
                "Song", "musicdl", SongInfo(song_name="Song", source="test")
            )
            queue.items = [item]
            queue._save_state()
            restored = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            saved_text = state.read_text(encoding="utf-8")

        self.assertIsInstance(restored.items[0].payload, SongInfo)
        self.assertEqual(restored.items[0].payload.song_name, "Song")
        self.assertNotIn("pickle", saved_text.casefold())

    def test_unsupported_payload_becomes_visible_error_instead_of_disappearing(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            queue.items = [DownloadItem("Opaque", "unknown", object())]
            queue._save_state()
            restored = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )

        self.assertEqual(restored.items[0].status, STATUS_ERROR)
        self.assertIn("Could not restore", restored.items[0].error)

    def test_completed_seed_is_reattached_only_when_engine_is_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            config = self.config()
            config["torrent_engine"] = True
            queue = DownloadQueue(config, None, state_path=state, start_workers=False)
            item = DownloadItem("Seed", "torrent", {"infohash": "abc"})
            item.status = STATUS_DONE
            item.seeding = True
            queue.items = [item]
            queue._save_state()
            with mock.patch.object(torrent_engine, "available", return_value=True):
                restored = DownloadQueue(
                    config, None, state_path=state, start_workers=False
                )

        self.assertEqual(restored.items[0].status, STATUS_QUEUED)
        self.assertTrue(restored.items[0].seeding)

    def test_shutdown_records_only_torrents_still_seeding(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            active = DownloadItem("Active seed", "torrent", {"infohash": "ABC"})
            stopped = DownloadItem("Stopped seed", "torrent", {"infohash": "DEF"})
            for item in (active, stopped):
                item.status = STATUS_DONE
                item.seeding = True
            queue.items = [active, stopped]
            with mock.patch.object(
                torrent_engine, "seeding", return_value=[("abc", "Active seed", 1, 0)]
            ):
                queue.shutdown()
            document = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(document["items"][0]["seeding"])
        self.assertFalse(document["items"][1]["seeding"])

    def test_clear_finished_keeps_active_seed_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            seed = DownloadItem("Seed", "torrent", {"infohash": "abc"})
            seed.status = STATUS_DONE
            seed.seeding = True
            ordinary = DownloadItem("Ordinary", "ytdlp", "url")
            ordinary.status = STATUS_DONE
            queue.items = [seed, ordinary]

            queue.remove_finished()

        self.assertEqual(queue.items, [seed])

    def test_stopping_torrent_file_seed_without_infohash_updates_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DownloadQueue(
                self.config(),
                None,
                state_path=Path(folder) / "downloads.json",
                start_workers=False,
            )
            seed = DownloadItem(
                "Private tracker seed",
                "torrent",
                {"download_url": "https://example.invalid/private.torrent"},
            )
            seed.status = STATUS_DONE
            seed.seeding = True
            queue.items = [seed]

            changed = queue.mark_torrent_stopped(
                "computed-hash", "Private tracker seed"
            )

        self.assertTrue(changed)
        self.assertFalse(seed.seeding)

    def test_corrupt_state_is_ignored_and_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "downloads.json"
            state.write_text("not json", encoding="utf-8")
            queue = DownloadQueue(
                self.config(), None, state_path=state, start_workers=False
            )
            self.assertEqual(queue.items, [])
            queue.items = [DownloadItem("New", "ytdlp", "url")]
            queue._save_state()
            document = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(document["version"], 1)


if __name__ == "__main__":
    unittest.main()
