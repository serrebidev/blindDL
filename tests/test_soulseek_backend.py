# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from aioslsk.protocol.primitives import (
    Attribute,
    AttributeKey,
    DirectoryData,
    FileData,
)
from aioslsk.search.model import SearchResult

from blinddl.config import DEFAULTS
from blinddl.downloader import DownloadItem, DownloadQueue
from blinddl import soulseek_backend


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
        self.assertEqual(settings.users.friends, {"alice", "bob"})

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
        queue = object.__new__(DownloadQueue)
        queue.config = self.config("downloads")
        queue.notify = mock.Mock()
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


class SoulseekAsyncSearchTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
