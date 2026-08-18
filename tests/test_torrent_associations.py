# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Tests for opening torrents and magnet links handed to blindDL.

Nothing here touches the real registry or the real desktop database: the
one thing a test must never do is change what the machine it runs on opens
its files with.
"""

import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from blinddl import associations, torrent_backend
from blinddl.single_instance import (
    RESTORE_MESSAGE,
    RestoreServer,
    open_message,
)

MAGNET = (
    "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
    "&dn=Some+Release+Name&tr=udp%3A%2F%2Ftracker.invalid%3A1337"
)


class RecognisingLinksTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blinddl-torrents-")

    def _torrent_file(self, name="A Release.torrent"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"d4:infod4:name5:helloee")
        return path

    def test_a_magnet_is_recognised(self):
        self.assertTrue(torrent_backend.is_torrent_link(MAGNET))

    def test_a_torrent_file_on_disk_is_recognised(self):
        self.assertTrue(torrent_backend.is_torrent_link(self._torrent_file()))

    def test_a_torrent_path_that_is_not_there_is_not_a_link(self):
        """Windows hands over a path; a stale one must not become a row."""
        self.assertFalse(torrent_backend.is_torrent_link(
            os.path.join(self.dir, "gone.torrent")))

    def test_ordinary_arguments_are_not_torrents(self):
        for argument in ("--self-test", "https://example.invalid/", "", None):
            with self.subTest(argument=argument):
                self.assertFalse(torrent_backend.is_torrent_link(argument))

    def test_a_magnet_becomes_a_queue_row_named_after_itself(self):
        item = torrent_backend.item_from_link(MAGNET)
        self.assertEqual(item["kind"], "torrent")
        self.assertEqual(item["title"], "Some Release Name")
        self.assertEqual(item["magnet"], MAGNET)
        self.assertEqual(item["infohash"],
                         "0123456789abcdef0123456789abcdef01234567")

    def test_a_magnet_with_no_name_falls_back_to_its_hash(self):
        bare = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        item = torrent_backend.item_from_link(bare)
        self.assertEqual(item["title"],
                         "0123456789abcdef0123456789abcdef01234567")

    def test_a_torrent_file_becomes_a_row_that_keeps_its_path(self):
        path = self._torrent_file()
        item = torrent_backend.item_from_link(path)
        self.assertEqual(item["title"], "A Release")
        self.assertEqual(item["torrent_path"], path)
        self.assertEqual(item["magnet"], "")

    def test_a_quoted_path_from_a_file_manager_is_unwrapped(self):
        path = self._torrent_file()
        item = torrent_backend.item_from_link(f'"{path}"')
        self.assertEqual(item["torrent_path"], path)

    def test_a_missing_file_says_so(self):
        with self.assertRaises(RuntimeError):
            torrent_backend.item_from_link(
                os.path.join(self.dir, "gone.torrent"))

    def test_a_local_torrent_is_used_where_it_lies(self):
        """Fetching it would be wrong, not merely wasteful: the file the
        user picked is the copy carrying a private tracker's passkey."""
        path = self._torrent_file()
        item = torrent_backend.item_from_link(path)
        with mock.patch.object(torrent_backend, "_http") as http:
            result = torrent_backend.fetch_torrent_file(item, self.dir)
        self.assertEqual(result, path)
        http.assert_not_called()


class HandingOverToARunningInstanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blinddl-instance-")
        self.path = os.path.join(self.dir, "running-instance.json")

    def _server(self, on_restore, on_open=None):
        server = RestoreServer(on_restore, self.path, on_open=on_open)
        server.start()
        self.addCleanup(server.stop)
        return server

    def _send(self, port, message):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
            peer.sendall(message)

    def test_a_magnet_reaches_the_instance_that_owns_the_queue(self):
        opened, restored = [], threading.Event()
        server = self._server(restored.set, opened.append)

        self._send(server.port, open_message(MAGNET))

        for _ in range(100):
            if opened:
                break
            threading.Event().wait(0.02)
        self.assertEqual(opened, [MAGNET])
        # The window comes back too: whoever clicked wants to see it happen.
        self.assertTrue(restored.wait(2))

    def test_a_long_magnet_survives_the_wire(self):
        """The old fixed 64-byte read would have truncated this one."""
        long_magnet = MAGNET + "".join(
            f"&tr=udp%3A%2F%2Ftracker{index}.invalid%3A1337"
            for index in range(60))
        self.assertGreater(len(long_magnet), 1024)
        opened = []
        server = self._server(lambda: None, opened.append)

        self._send(server.port, open_message(long_magnet))

        for _ in range(100):
            if opened:
                break
            threading.Event().wait(0.02)
        self.assertEqual(opened, [long_magnet])

    def test_a_plain_restore_still_only_restores(self):
        opened, restored = [], threading.Event()
        server = self._server(restored.set, opened.append)

        self._send(server.port, RESTORE_MESSAGE)

        self.assertTrue(restored.wait(2))
        self.assertEqual(opened, [])

    def test_nonsense_on_the_socket_is_ignored(self):
        opened, restored = [], threading.Event()
        server = self._server(restored.set, opened.append)

        self._send(server.port, b"open not-json\n")

        self.assertFalse(restored.wait(0.5))
        self.assertEqual(opened, [])

    def test_a_build_with_nowhere_to_put_it_still_comes_back(self):
        restored = threading.Event()
        server = self._server(restored.set, None)

        self._send(server.port, open_message(MAGNET))

        self.assertTrue(restored.wait(2))


class RegisteringTests(unittest.TestCase):
    def test_the_command_names_this_build_and_takes_the_link(self):
        command = associations.launcher_command()
        self.assertIn('"%1"', command)
        self.assertTrue(command.startswith('"'))

    def test_a_lookup_that_throws_is_reported_as_not_registered(self):
        """Settings must render even where the registry cannot be read."""
        with mock.patch.object(associations, "_registered_windows",
                               side_effect=OSError("denied")), \
                mock.patch.object(associations.sys, "platform", "win32"):
            self.assertFalse(associations.is_registered())

    def test_registering_reports_failure_rather_than_raising(self):
        with mock.patch.object(associations, "_register_windows",
                               side_effect=OSError("denied")), \
                mock.patch.object(associations.sys, "platform", "win32"):
            self.assertFalse(associations.register())

    def test_a_system_blinddl_cannot_do_this_on_is_told_apart(self):
        with mock.patch.object(associations.sys, "platform", "darwin"):
            self.assertFalse(associations.supported())
            self.assertFalse(associations.is_registered())
            self.assertFalse(associations.register())

    def test_windows_and_linux_are_supported(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), \
                    mock.patch.object(associations.sys, "platform", platform):
                self.assertTrue(associations.supported())


if __name__ == "__main__":
    unittest.main()
