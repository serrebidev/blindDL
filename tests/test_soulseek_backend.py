# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
import dbm
import json
import shelve
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aioslsk.protocol.primitives import (
    Attribute,
    AttributeKey,
    DirectoryData,
    FileData,
)
from aioslsk.search.model import SearchResult
from aioslsk.shares.manager import SharesManager
from aioslsk.transfer.model import FailReason, TransferDirection

from blinddl.config import DEFAULTS
from blinddl.downloader import DownloadItem, DownloadQueue
from blinddl import soulseek_backend


def _guarded_handler(name, original):
    """Wrap a stand-in for the aioslsk handler blindDL guards."""
    factories = {
        "_on_peer_transfer_queue": soulseek_backend._guarded_queue_handler,
        "_on_peer_transfer_request": soulseek_backend._guarded_request_handler,
    }
    return factories[name](original)


def _file(path, extension, size=1024, duration=0):
    attributes = []
    if duration:
        attributes.append(Attribute(AttributeKey.DURATION.value, duration))
    return FileData(0, path, size, extension, attributes)


class SoulseekBackendTests(unittest.TestCase):
    def config(self, folder):
        config = copy.deepcopy(DEFAULTS)
        config.update(
            {
                "download_dir": folder,
                "soulseek_enabled": True,
                "soulseek_username": "listener",
                "soulseek_password": "secret",
            }
        )
        return config

    def test_settings_use_default_download_dir_and_public_extra_shares(self):
        with tempfile.TemporaryDirectory() as folder:
            extra = f"{folder} extra"
            config = self.config(folder)
            config["soulseek_shared_folders"] = [extra, extra]
            config["soulseek_max_upload_kib"] = 64
            config["soulseek_max_download_kib"] = 128
            config["soulseek_rooms"] = ["Ambient", "ambient", "Jazz"]
            config["soulseek_private_rooms"] = ["Secret", "secret"]
            config["soulseek_friends"] = ["alice", "ALICE", "bob"]

            settings = soulseek_backend._build_settings(
                soulseek_backend._config_snapshot(config)
            )

        self.assertEqual(settings.shares.download, folder)
        self.assertEqual(
            [entry.path for entry in settings.shares.directories],
            [folder, extra],
        )
        self.assertTrue(
            all(
                entry.share_mode.value == "everyone"
                for entry in settings.shares.directories
            )
        )
        self.assertEqual(settings.network.limits.upload_speed_kbps, 64)
        self.assertEqual(settings.network.limits.download_speed_kbps, 128)
        self.assertTrue(settings.network.server.reconnect.auto)
        self.assertEqual(settings.rooms.favorites, {"Ambient", "Jazz"})
        self.assertEqual(
            soulseek_backend._config_snapshot(config)["private_rooms"], ["Secret"]
        )
        self.assertEqual(settings.users.friends, {"alice", "bob"})

    @unittest.skipUnless(sys.platform == "win32", "Windows drive semantics")
    def test_aioslsk_shared_directories_on_different_drives_are_unrelated(self):
        music = soulseek_backend.SharedDirectory(
            r"C:\Music", r"C:\Music", "music"
        )
        downloads = soulseek_backend.SharedDirectory(
            r"D:\Downloads", r"D:\Downloads", "downloads"
        )

        self.assertFalse(music.is_parent_of(downloads))
        self.assertFalse(music.is_child_of(downloads))
        self.assertFalse(downloads.is_parent_of(music))
        self.assertFalse(downloads.is_child_of(music))
        self.assertTrue(music.is_parent_of(r"C:\Music\Album"))

        settings = soulseek_backend.Settings(
            credentials=soulseek_backend.CredentialsSettings(
                username="listener", password="secret"
            )
        )
        settings.shares.directories = [
            soulseek_backend.SharedDirectorySettingEntry(path=r"C:\Music"),
            soulseek_backend.SharedDirectorySettingEntry(path=r"D:\Downloads"),
        ]
        manager = SharesManager(settings, mock.Mock(), mock.Mock())
        manager.load_from_settings()

        self.assertEqual(
            [entry.absolute_path for entry in manager.shared_directories],
            [r"C:\Music", r"D:\Downloads"],
        )

    def test_free_slot_priority_is_separate_but_reaches_aioslsk_uploader(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.config(folder)
            config["soulseek_friends"] = ["friend"]
            config["soulseek_priority_users"] = ["priority"]
            snapshot = soulseek_backend._config_snapshot(config)
            settings = soulseek_backend._build_settings(snapshot)

        self.assertEqual(snapshot["friends"], ["friend"])
        self.assertEqual(snapshot["priority_users"], ["priority"])
        self.assertEqual(settings.users.friends, {"friend", "priority"})

    def test_library_share_can_be_disabled_without_losing_extra_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            extra = f"{folder} extra"
            config = self.config(folder)
            config["soulseek_share_library"] = False
            config["soulseek_shared_folders"] = [extra]

            settings = soulseek_backend._build_settings(
                soulseek_backend._config_snapshot(config)
            )

        self.assertEqual([entry.path for entry in settings.shares.directories], [extra])

    def test_search_result_preserves_peer_availability_and_audio_metadata(self):
        result = SearchResult(
            ticket=1,
            username="fast-peer",
            has_free_slots=True,
            avg_speed=2 * 1024 * 1024,
            queue_size=3,
            shared_items=[
                _file("Music\\Artist\\Track.flac", "flac", 8 * 1024 * 1024, 245)
            ],
        )

        item = soulseek_backend._result_item(result, result.shared_items[0])

        self.assertEqual(item["title"], "Track.flac")
        self.assertEqual(item["folder"], "Music\\Artist")
        self.assertEqual(item["username"], "fast-peer")
        self.assertEqual(item["duration_s"], 245)
        self.assertEqual(item["format"], "FLAC")
        self.assertIn("free slot", item["source"])
        self.assertIn("3 waiting", item["source"])
        self.assertIn("2.0 MB/s", item["source"])
        self.assertEqual(item["availability"], "free slot, 3 waiting, 2.0 MB/s average")

    def test_downloader_reports_soulseek_progress(self):
        queue = DownloadQueue(
            self.config("downloads"),
            mock.Mock(),
            state_path="",
            start_workers=False,
        )
        item = DownloadItem(
            "Track.flac",
            "soulseek",
            {"username": "peer", "remote_path": "Track.flac"},
        )

        def fake_download(payload, config, progress_cb, cancel_event):
            progress_cb(
                {
                    "downloaded": 512,
                    "total": 1024,
                    "speed": 256,
                    "eta": 2,
                    "state": "Downloading",
                    "queue_position": None,
                }
            )

        with mock.patch.object(soulseek_backend, "download", side_effect=fake_download):
            queue._run_soulseek(item)

        self.assertEqual(item.percent, 50)
        self.assertEqual(item.speed, "256.0 B/s")
        self.assertEqual(item.eta, "0:02")
        queue.notify.assert_called()

    def test_downloader_requeues_after_soulseek_settings_change(self):
        speeds = []
        queue = DownloadQueue(
            self.config("downloads"),
            lambda changed: speeds.append(changed.speed),
            state_path="",
            start_workers=False,
        )
        item = DownloadItem(
            "Track.flac",
            "soulseek",
            {"username": "peer", "remote_path": "Track.flac"},
        )
        attempts = 0

        def fake_download(payload, config, progress_cb, cancel_event):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise soulseek_backend.SoulseekSettingsChanged()
            progress_cb(
                {
                    "downloaded": 768,
                    "total": 1024,
                    "speed": 256,
                    "eta": 1,
                    "state": "Downloading",
                    "queue_position": None,
                }
            )

        with mock.patch.object(soulseek_backend, "download", side_effect=fake_download):
            queue._run_soulseek(item)

        self.assertEqual(attempts, 2)
        self.assertEqual(item.percent, 75)
        self.assertIn(
            "Soulseek settings changed; reconnecting automatically",
            speeds,
        )

    def test_cancel_wins_while_soulseek_transfer_is_requeued(self):
        queue = DownloadQueue(
            self.config("downloads"),
            mock.Mock(),
            state_path="",
            start_workers=False,
        )
        item = DownloadItem(
            "Track.flac",
            "soulseek",
            {"username": "peer", "remote_path": "Track.flac"},
        )

        def interrupted(payload, config, progress_cb, cancel_event):
            cancel_event.set()
            raise soulseek_backend.SoulseekSettingsChanged()

        with (
            mock.patch.object(soulseek_backend, "download", side_effect=interrupted),
            self.assertRaises(soulseek_backend.SoulseekDownloadCancelled),
        ):
            queue._run_soulseek(item)


class SoulseekLeecherGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._guard = dict(soulseek_backend._leecher_guard)
        soulseek_backend._leecher_counts.clear()

    def tearDown(self):
        soulseek_backend._leecher_guard.update(self._guard)
        soulseek_backend._leecher_counts.clear()

    def guard(self, enabled=True, allowed=()):
        soulseek_backend._leecher_guard["enabled"] = enabled
        soulseek_backend._leecher_guard["allowed"] = frozenset(allowed)

    def counted(self, count):
        return mock.patch.object(
            soulseek_backend,
            "_shared_file_count",
            mock.AsyncMock(return_value=count),
        )

    def test_refusing_leechers_is_on_by_default(self):
        self.assertTrue(DEFAULTS["soulseek_block_leechers"])

    def test_toggling_the_guard_does_not_force_a_reconnect(self):
        config = copy.deepcopy(DEFAULTS)
        config.update({"soulseek_enabled": True, "download_dir": "."})
        allowing = soulseek_backend._config_snapshot(config)
        config["soulseek_block_leechers"] = False
        refusing = soulseek_backend._config_snapshot(config)

        self.assertNotEqual(allowing["block_leechers"], refusing["block_leechers"])
        self.assertEqual(
            soulseek_backend._signature(allowing),
            soulseek_backend._signature(refusing),
        )

    def test_friends_and_priority_users_are_always_allowed(self):
        soulseek_backend._set_leecher_guard(
            {
                "block_leechers": True,
                "friends": ["Alice"],
                "priority_users": ["Bob"],
            }
        )

        self.assertTrue(soulseek_backend._leecher_guard["enabled"])
        self.assertEqual(
            soulseek_backend._leecher_guard["allowed"], frozenset({"alice", "bob"})
        )

    async def test_a_peer_sharing_nothing_is_refused(self):
        self.guard()
        with self.counted(0):
            refused = await soulseek_backend._refuses_upload(
                SimpleNamespace(username="freeloader")
            )
        self.assertTrue(refused)

    async def test_a_peer_who_shares_is_allowed(self):
        self.guard()
        with self.counted(12):
            refused = await soulseek_backend._refuses_upload(
                SimpleNamespace(username="sharer")
            )
        self.assertFalse(refused)

    async def test_a_friend_who_shares_nothing_is_still_allowed(self):
        self.guard(allowed={"alice"})
        with self.counted(0) as lookup:
            refused = await soulseek_backend._refuses_upload(
                SimpleNamespace(username="Alice")
            )
        self.assertFalse(refused)
        lookup.assert_not_awaited()

    async def test_the_guard_is_skipped_when_switched_off(self):
        self.guard(enabled=False)
        with self.counted(0) as lookup:
            refused = await soulseek_backend._refuses_upload(
                SimpleNamespace(username="freeloader")
            )
        self.assertFalse(refused)
        lookup.assert_not_awaited()

    async def test_an_unanswered_lookup_leaves_the_upload_alone(self):
        # A server hiccup must not start refusing peers who do share.
        self.guard()
        with self.counted(None):
            refused = await soulseek_backend._refuses_upload(
                SimpleNamespace(username="unknown")
            )
        self.assertFalse(refused)

    async def test_a_counted_peer_is_not_looked_up_again(self):
        self.guard()
        service = SimpleNamespace(
            _client=mock.AsyncMock(return_value=SimpleNamespace(shared_file_count=0))
        )

        with mock.patch.object(soulseek_backend, "_SERVICE", service):
            first = await soulseek_backend._shared_file_count("freeloader")
            second = await soulseek_backend._shared_file_count("FreeLoader")

        # One request covers a peer asking for a whole folder, and the name is
        # matched however the peer capitalises it.
        self.assertEqual((first, second), (0, 0))
        service._client.assert_awaited_once()

    async def test_a_refused_queue_request_never_reaches_aioslsk(self):
        original = mock.AsyncMock()
        manager = SimpleNamespace()
        connection = SimpleNamespace(username="freeloader", queue_message=mock.Mock())
        message = SimpleNamespace(filename="Album/Track.flac")
        guarded = _guarded_handler("_on_peer_transfer_queue", original)

        self.guard()
        with self.counted(0):
            await guarded(manager, message, connection)

        original.assert_not_awaited()
        refusal = connection.queue_message.call_args[0][0]
        self.assertEqual(refusal.filename, "Album/Track.flac")
        self.assertEqual(refusal.reason, FailReason.FILE_NOT_SHARED)

    async def test_a_sharing_peer_reaches_aioslsk_untouched(self):
        original = mock.AsyncMock()
        manager = SimpleNamespace()
        connection = SimpleNamespace(username="sharer", queue_message=mock.Mock())
        message = SimpleNamespace(filename="Album/Track.flac")
        guarded = _guarded_handler("_on_peer_transfer_queue", original)

        self.guard()
        with self.counted(40):
            await guarded(manager, message, connection)

        original.assert_awaited_once_with(manager, message, connection)
        connection.queue_message.assert_not_called()

    async def test_a_download_request_is_never_refused_as_an_upload(self):
        original = mock.AsyncMock()
        manager = SimpleNamespace()
        connection = SimpleNamespace(username="freeloader", queue_message=mock.Mock())
        message = SimpleNamespace(
            ticket=7,
            filename="Album/Track.flac",
            direction=TransferDirection.DOWNLOAD.value,
        )
        guarded = _guarded_handler("_on_peer_transfer_request", original)

        self.guard()
        with self.counted(0):
            await guarded(manager, message, connection)

        original.assert_awaited_once_with(manager, message, connection)
        connection.queue_message.assert_not_called()

    async def test_a_direct_upload_request_from_a_leecher_is_refused(self):
        original = mock.AsyncMock()
        manager = SimpleNamespace()
        connection = SimpleNamespace(username="freeloader", queue_message=mock.Mock())
        message = SimpleNamespace(
            ticket=7,
            filename="Album/Track.flac",
            direction=TransferDirection.UPLOAD.value,
        )
        guarded = _guarded_handler("_on_peer_transfer_request", original)

        self.guard()
        with self.counted(0):
            await guarded(manager, message, connection)

        original.assert_not_awaited()
        refusal = connection.queue_message.call_args[0][0]
        self.assertEqual(refusal.ticket, 7)
        self.assertFalse(refusal.allowed)
        self.assertEqual(refusal.reason, FailReason.FILE_NOT_SHARED)


class SoulseekAsyncSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_restart_raises_requeueable_transfer_error(self):
        transfer = SimpleNamespace(local_path=None)
        client = SimpleNamespace(
            transfers=SimpleNamespace(
                download=mock.AsyncMock(return_value=transfer)
            )
        )
        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=client)
        service._client = object()

        with self.assertRaises(soulseek_backend.SoulseekSettingsChanged):
            await service._download(
                {},
                {"username": "peer", "remote_path": "Track.flac"},
                None,
                threading.Event(),
            )

    async def test_browse_user_normalizes_public_and_locked_folder_files(self):
        public = [
            DirectoryData("Music\\Album", [_file("Track.flac", "flac", 50)])
        ]
        locked = [DirectoryData("Private", [_file("Secret.mp3", "mp3", 25)])]

        async def execute(command, response=False):
            self.assertTrue(response)
            return public, locked

        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=execute)
        rows = await service._browse_user({}, "peer")

        self.assertEqual(
            rows[0]["files"][0]["remote_path"],
            "Music\\Album\\Track.flac",
        )
        self.assertFalse(rows[0]["files"][0]["locked"])
        self.assertTrue(rows[1]["files"][0]["locked"])

    async def test_profile_combines_peer_information_and_server_statistics(self):
        class Client:
            users = SimpleNamespace(
                get_user_object=lambda username: SimpleNamespace(
                    status=SimpleNamespace(name="ONLINE")
                )
            )

            async def __call__(self, command, response=False):
                if command.__class__.__name__ == "PeerGetUserInfoCommand":
                    return SimpleNamespace(
                        description="hello",
                        picture=b"image",
                        has_slots_free=True,
                        upload_slots=3,
                        queue_length=2,
                        upload_permissions=SimpleNamespace(value="everyone"),
                    )
                return SimpleNamespace(
                    avg_speed=1024,
                    uploads=9,
                    shared_file_count=12,
                    shared_folder_count=4,
                )

        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=Client())
        profile = await service._user_profile({}, "peer")

        self.assertEqual(profile["description"], "hello")
        self.assertEqual(profile["status"], "Online")
        self.assertTrue(profile["has_slots_free"])
        self.assertEqual(profile["shared_files"], 12)

    async def test_account_check_logs_in_without_scanning_or_upnp(self):
        client = SimpleNamespace(
            start=mock.AsyncMock(),
            login=mock.AsyncMock(),
            stop=mock.AsyncMock(),
        )
        with mock.patch.object(
            soulseek_backend, "SoulSeekClient", return_value=client
        ) as client_class:
            await soulseek_backend._verify_account_async("new-user", "secret", 5)

        settings = client_class.call_args.args[0]
        self.assertEqual(settings.credentials.username, "new-user")
        self.assertFalse(settings.shares.scan_on_start)
        self.assertFalse(settings.network.upnp.enabled)
        self.assertFalse(settings.network.server.reconnect.auto)
        self.assertGreater(settings.network.listening.port, 0)
        self.assertGreater(settings.network.listening.obfuscated_port, 0)
        self.assertNotEqual(
            settings.network.listening.port,
            settings.network.listening.obfuscated_port,
        )
        client.start.assert_awaited_once()
        client.login.assert_awaited_once()
        client.stop.assert_awaited_once()

    async def test_room_commands_use_aioslsk_processed_response_values(self):
        ambient = SimpleNamespace(
            name="Ambient", private=False, joined=False, user_count=10, users=[]
        )

        async def execute(command, response=False):
            self.assertTrue(response)
            if command.__class__.__name__ == "GetRoomListCommand":
                return [ambient]
            ambient.joined = command.__class__.__name__ == "JoinRoomCommand"
            return ambient

        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=execute)

        rooms = await service._refresh_rooms({})
        self.assertEqual(rooms[0]["name"], "Ambient")
        await service._join_room({}, "Ambient", False)
        self.assertTrue(service.rooms_snapshot()[0]["joined"])
        await service._leave_room({}, "Ambient")
        self.assertFalse(service.rooms_snapshot()[0]["joined"])

    async def test_search_filters_media_type_sorts_peers_and_caps_results(self):
        request = SimpleNamespace(
            results=[
                SearchResult(
                    ticket=1,
                    username="queued",
                    has_free_slots=False,
                    avg_speed=100,
                    queue_size=8,
                    shared_items=[
                        _file("Books\\Novel.epub", "epub"),
                        _file("Music\\Track.mp3", "mp3"),
                    ],
                ),
                SearchResult(
                    ticket=1,
                    username="free",
                    has_free_slots=True,
                    avg_speed=1000,
                    queue_size=0,
                    shared_items=[
                        _file("Books\\Second.pdf", "pdf"),
                        _file("Video\\Movie.mkv", "mkv"),
                    ],
                ),
            ]
        )

        class Searches:
            async def search(self, query):
                return request

            def remove_request(self, removed):
                self.removed = removed

        client = SimpleNamespace(searches=Searches())
        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=client)
        snapshot = {"max_results": 1}

        items = await service._search(snapshot, "book", "book", 0.01, threading.Event())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Second.pdf")
        self.assertIs(client.searches.removed, request)

    async def test_streaming_search_delivers_batches_until_stopped(self):
        request = SimpleNamespace(
            results=[
                SearchResult(
                    ticket=1,
                    username="peer",
                    has_free_slots=True,
                    avg_speed=100,
                    queue_size=0,
                    shared_items=[_file("Music\\Track.mp3", "mp3")],
                ),
            ]
        )

        class Searches:
            async def search(self, query):
                return request

            def remove_request(self, removed):
                self.removed = removed

        client = SimpleNamespace(searches=Searches())
        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=client)
        stop_event = threading.Event()
        batches = []

        def on_batch(batch):
            batches.append(batch)
            # The search would run forever; stop it after the first delivery.
            stop_event.set()

        items = await service._search(
            {}, "query", "audio", 5.0, stop_event, on_batch=on_batch
        )

        self.assertEqual(items, [])
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0]["title"], "Track.mp3")
        self.assertEqual(batches[0][0]["username"], "peer")
        self.assertIs(client.searches.removed, request)

    async def test_private_and_room_messages_are_sent_and_exposed(self):
        calls = []

        async def execute(command, response=False):
            calls.append(command)

        service = soulseek_backend._Service()
        service._username = "listener"
        service._configure = mock.AsyncMock(return_value=execute)
        emitted = []
        service.add_listener(emitted.append)

        await service._send_private_message(
            {},
            "friend",
            "hello",  # snapshot is consumed by the mock
        )
        await service._send_room_message({}, "Ambient", "good evening")
        service._on_room_message(
            SimpleNamespace(
                message=SimpleNamespace(
                    timestamp=123,
                    room=SimpleNamespace(name="Ambient"),
                    user=SimpleNamespace(name="listener"),
                    message="good evening",
                )
            )
        )
        service._on_private_message(
            SimpleNamespace(
                message=SimpleNamespace(
                    timestamp=124,
                    user=SimpleNamespace(name="friend"),
                    message="hi back",
                )
            )
        )

        self.assertEqual(calls[0].username, "friend")
        self.assertEqual(calls[1].room, "Ambient")
        self.assertTrue(service.private_messages_snapshot()[0]["outgoing"])
        self.assertFalse(service.private_messages_snapshot()[1]["outgoing"])
        self.assertTrue(service.room_messages_snapshot()[0]["outgoing"])
        self.assertEqual(
            [event["type"] for event in emitted],
            ["private_message", "room_message", "private_message"],
        )

    async def test_chat_history_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            service = soulseek_backend._Service(history)
            service._username = "listener"
            service._on_room_message(
                SimpleNamespace(
                    message=SimpleNamespace(
                        timestamp=123,
                        room=SimpleNamespace(name="Ambient"),
                        user=SimpleNamespace(name="friend"),
                        message="hello room",
                    )
                )
            )
            service._append_private_message({
                "timestamp": 124,
                "user": "friend",
                "message": "hello privately",
                "outgoing": False,
            })
            restored = soulseek_backend._Service(history)

            self.assertEqual(
                restored.room_messages_snapshot()[0]["message"], "hello room"
            )
            self.assertEqual(
                restored.private_messages_snapshot()[0]["message"],
                "hello privately",
            )
            document = json.loads(history.read_text(encoding="utf-8"))
            self.assertEqual(document["version"], 1)

    async def test_friend_changes_update_aioslsk_settings_and_snapshot(self):
        class Users:
            @staticmethod
            def get_user_object(username):
                return SimpleNamespace(
                    name=username, status=SimpleNamespace(name="ONLINE")
                )

        client = SimpleNamespace(
            settings=SimpleNamespace(users=SimpleNamespace(friends={"alice"})),
            users=Users(),
        )
        service = soulseek_backend._Service()
        service._configure = mock.AsyncMock(return_value=client)
        snapshot = {"friends": ["alice"]}

        friends = await service._change_friend(snapshot, "bob", True)
        self.assertEqual(client.settings.users.friends, {"alice", "bob"})
        self.assertEqual([friend["username"] for friend in friends], ["alice", "bob"])

        friends = await service._change_friend(snapshot, "ALICE", False)
        self.assertEqual(client.settings.users.friends, {"bob"})
        self.assertEqual([friend["username"] for friend in friends], ["bob"])


class SoulseekCacheDirectoryTests(unittest.TestCase):
    """The shelve caches must never be shared between Python versions.

    aioslsk stores its share index with ``shelve``, whose ``dbm`` backend is
    whatever the running interpreter happens to offer. A frozen build carries
    its own Python, so it is regularly a different version from a source
    checkout, and 3.13 changed the default backend to one older interpreters
    cannot read. Sharing a directory made released builds fail to connect on
    any machine that had run blindDL from source.
    """

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        patcher = mock.patch.object(
            soulseek_backend, "app_data_dir", return_value=self.root.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cache_directory_is_scoped_to_the_running_python(self):
        path = Path(soulseek_backend._cache_dir())
        expected = f"soulseek-py{sys.version_info.major}{sys.version_info.minor}"

        self.assertEqual(path.name, expected)
        self.assertTrue(path.is_dir())

    def test_a_readable_cache_is_carried_forward(self):
        legacy = Path(self.root.name) / "soulseek"
        legacy.mkdir()
        with shelve.open(str(legacy / "shares_index"), "c") as database:
            database["index"] = []
        (legacy / "transfers").write_bytes(b"")

        migrated = Path(soulseek_backend._cache_dir())

        self.assertTrue(any(migrated.iterdir()))
        self.assertTrue((migrated / "transfers").is_file())
        # The originals stay put; an older build may still be relying on them.
        self.assertTrue((legacy / "transfers").is_file())

    def test_a_cache_this_python_cannot_read_is_left_behind(self):
        legacy = Path(self.root.name) / "soulseek"
        legacy.mkdir()
        (legacy / "shares_index").write_bytes(b"SQLite format 3\x00")

        with mock.patch.object(
            soulseek_backend.dbm, "whichdb", return_value="dbm.nonexistent"
        ):
            migrated = Path(soulseek_backend._cache_dir())

        self.assertEqual(list(migrated.iterdir()), [])

    def test_an_unrecognized_cache_is_left_behind(self):
        legacy = Path(self.root.name) / "soulseek"
        legacy.mkdir()
        (legacy / "shares_index").write_bytes(b"not a database")

        migrated = Path(soulseek_backend._cache_dir())

        self.assertEqual(list(migrated.iterdir()), [])

    def test_probe_opens_the_caches_and_names_the_backend(self):
        result = soulseek_backend.runtime_probe()

        self.assertIn("aioslsk backend and persistent caches", result)
        # A build whose dbm backends are incomplete cannot reach this point.
        self.assertRegex(result, r"\(dbm\.\w+\)$")

    def test_probe_rejects_a_build_with_no_usable_dbm_backend(self):
        with mock.patch.object(soulseek_backend.dbm, "whichdb", return_value=""):
            with self.assertRaises(soulseek_backend.SoulseekError) as caught:
                soulseek_backend.runtime_probe()

        self.assertIn("dbm backends are incomplete", str(caught.exception))

    def test_probe_reports_a_missing_backend_instead_of_a_raw_dbm_error(self):
        missing = dbm.error[0]("db type is dbm.sqlite3, but the module is not available")
        with mock.patch.object(
            soulseek_backend.SharesShelveCache, "read", side_effect=missing
        ):
            with self.assertRaises(soulseek_backend.SoulseekError) as caught:
                soulseek_backend.runtime_probe()

        self.assertIn("dbm.sqlite3", str(caught.exception))
        self.assertIn("dbm backends are incomplete", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
