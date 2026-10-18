# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import sys
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from blinddl import awake, downloader
from blinddl.downloader import STATUS_DOWNLOADING, STATUS_PAUSED, STATUS_QUEUED


class _Stop(Exception):
    """Ends the keeper thread's loop in a test."""


class AwakeTests(unittest.TestCase):
    def setUp(self):
        # Another test's queue may be holding the computer awake, and a
        # hold already taken is what this suite is about.
        awake.release()

    def tearDown(self):
        awake.release()

    def test_the_hold_is_taken_once_however_often_it_is_asked_for(self):
        started = []

        def start_child():
            started.append("child")
            return mock.Mock()

        with mock.patch.object(awake, "_windows_keeper"), \
                mock.patch.object(awake.threading, "Thread") as thread, \
                mock.patch.object(awake, "_start_child", start_child), \
                mock.patch.object(awake.shutil, "which", return_value="/bin/x"):
            self.assertTrue(awake.hold())
            self.assertTrue(awake.hold())

        self.assertTrue(awake.held())
        # One hold, whichever way this system takes it: a thread that keeps
        # renewing it on Windows, a child process that watches blindDL
        # everywhere else. Never two.
        self.assertEqual(thread.call_count + len(started), 1)

    def test_releasing_twice_is_harmless(self):
        with mock.patch.object(awake, "_windows_keeper"), \
                mock.patch.object(awake.threading, "Thread"):
            awake.hold()
            awake.release()
            awake.release()

        self.assertFalse(awake.held())

    @unittest.skipUnless(sys.platform == "win32", "Windows hold")
    def test_the_windows_hold_is_renewed_and_then_let_go(self):
        # SetThreadExecutionState belongs to the calling thread and lapses
        # with it, so the keeper renews it while it is wanted. This is the
        # only thing between a sleeping laptop and a finished playlist.
        calls = []

        def record(active):
            calls.append(active)
            if not active:
                raise _Stop
            return True

        with mock.patch.object(awake, "_set_windows_state", record), \
                mock.patch.object(awake.time, "sleep", awake.release), \
                mock.patch.object(awake.threading, "Thread"):
            awake.hold()
            with self.assertRaises(_Stop):
                awake._windows_keeper()

        self.assertEqual(calls, [True, False])

    def test_a_child_process_watches_blinddl_rather_than_outliving_it(self):
        # An inhibitor left running after a crash would stop a machine from
        # ever sleeping again, which is a worse fault than the one the hold
        # exists to cure.
        with mock.patch.object(awake.sys, "platform", "darwin"), \
                mock.patch.object(awake.shutil, "which", return_value="/bin/x"):
            command = awake._child_command()
        self.assertEqual(command[0], "/bin/x")
        self.assertEqual(command[-1], str(awake.os.getpid()))

        with mock.patch.object(awake.sys, "platform", "linux"), \
                mock.patch.object(awake.shutil, "which", return_value="/bin/x"):
            command = awake._child_command()
        self.assertIn("--what=idle:sleep", command)
        self.assertIn(str(awake.os.getpid()), command[-1])

    def test_a_system_with_no_way_to_ask_says_so(self):
        with mock.patch.object(awake, "_child_command", return_value=None), \
                mock.patch.object(awake.sys, "platform", "linux"):
            self.assertFalse(awake.supported())
            self.assertFalse(awake.hold())
        self.assertFalse(awake.held())


class QueueAwakeTests(unittest.TestCase):
    def _queue(self, downloading=0, queued=0, paused=0, keep_awake=True):
        return SimpleNamespace(
            config={"keep_awake": keep_awake},
            _status_counts={
                STATUS_DOWNLOADING: downloading,
                STATUS_QUEUED: queued,
                STATUS_PAUSED: paused,
            },
            _cond=nullcontext(),
        )

    def test_a_download_in_flight_is_worth_staying_awake_for(self):
        self.assertTrue(
            downloader.DownloadQueue._wants_awake_hold(self._queue(downloading=1)))
        self.assertTrue(
            downloader.DownloadQueue._wants_awake_hold(self._queue(queued=3)))

    def test_an_idle_queue_and_a_paused_one_are_not(self):
        self.assertFalse(downloader.DownloadQueue._wants_awake_hold(self._queue()))
        self.assertFalse(
            downloader.DownloadQueue._wants_awake_hold(self._queue(paused=5)))

    def test_the_setting_turns_the_hold_off(self):
        self.assertFalse(downloader.DownloadQueue._wants_awake_hold(
            self._queue(downloading=2, keep_awake=False)))

    def test_asking_twice_asks_the_system_once(self):
        queue = SimpleNamespace(_awake_hold=False)
        with mock.patch.object(downloader, "awake") as module:
            downloader.DownloadQueue._set_awake_hold(queue, True)
            downloader.DownloadQueue._set_awake_hold(queue, True)
            downloader.DownloadQueue._set_awake_hold(queue, False)
            downloader.DownloadQueue._set_awake_hold(queue, False)

        module.hold.assert_called_once_with()
        module.release.assert_called_once_with()
        self.assertFalse(queue._awake_hold)


if __name__ == "__main__":
    unittest.main()
