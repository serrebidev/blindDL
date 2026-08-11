# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import wx

from blinddl.single_instance import RestoreServer, notify_existing


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_second_checker_detects_the_first_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            name = f"blindDL-test-{uuid.uuid4()}"
            first = wx.SingleInstanceChecker(name, folder)
            second = wx.SingleInstanceChecker(name, folder)
            try:
                self.assertFalse(first.IsAnotherRunning())
                self.assertTrue(second.IsAnotherRunning())
            finally:
                del second
                del first

    def test_relaunch_signal_restores_the_existing_instance(self):
        restored = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "instance.json"
            server = RestoreServer(restored.set, path).start()
            try:
                self.assertTrue(notify_existing(path, timeout=1))
                self.assertTrue(restored.wait(2))
            finally:
                server.stop()
            self.assertFalse(path.exists())

    def test_stale_endpoint_does_not_claim_that_restore_succeeded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "instance.json"
            path.write_text('{"port": 1}', encoding="utf-8")
            self.assertFalse(notify_existing(path, timeout=0.1))


if __name__ == "__main__":
    unittest.main()
