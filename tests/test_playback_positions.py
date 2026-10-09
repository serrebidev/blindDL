# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from unittest import mock

from blinddl.gui import media_player

HOUR = 60 * 60 * 1000


class PlaybackPositionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"BLINDDL_APP_DATA_DIR": folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_long_media_resumes_where_it_stopped(self):
        media_player.remember_position("show.mp3", 20 * 60 * 1000, HOUR)
        self.assertEqual(media_player.saved_position("show.mp3"), 1200000)

    def test_short_tracks_start_and_end_are_not_kept(self):
        media_player.remember_position("song.mp3", 120000, 240000)
        media_player.remember_position("show.mp3", 5000, HOUR)
        media_player.remember_position("end.mp3", HOUR - 1000, HOUR)
        for name in ("song.mp3", "show.mp3", "end.mp3"):
            self.assertEqual(media_player.saved_position(name), 0)

    def test_finishing_forgets_the_position(self):
        media_player.remember_position("show.mp3", 20 * 60 * 1000, HOUR)
        media_player.remember_position("show.mp3", 0, 0)
        self.assertEqual(media_player.saved_position("show.mp3"), 0)


if __name__ == "__main__":
    unittest.main()
