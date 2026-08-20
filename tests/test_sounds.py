# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The two download sounds: which file plays, and how often."""

import os
import tempfile
import unittest
from unittest import mock

import wx  # noqa: F401 - wx.adv needs an initialized wx to import cleanly

from blinddl.gui import sounds


class SoundFileTests(unittest.TestCase):
    def test_both_shipped_sounds_are_present_and_playable_wave_files(self):
        import wave

        for event in sounds.EVENTS:
            path = sounds.bundled_path(event)
            self.assertTrue(os.path.isfile(path), event)
            with wave.open(path) as handle:
                self.assertGreater(handle.getnframes(), 0, event)

    def test_switching_sounds_off_leaves_nothing_to_play(self):
        config = {"sounds_enabled": False}
        self.assertEqual(sounds.sound_path(config, sounds.COMPLETE), "")

    def test_a_chosen_file_replaces_the_shipped_one(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen = os.path.join(directory, "mine.wav")
            with open(chosen, "wb") as handle:
                handle.write(b"RIFF")
            config = {"sounds_enabled": True,
                      "sound_download_complete": chosen}
            self.assertEqual(
                sounds.sound_path(config, sounds.COMPLETE), chosen)

    def test_a_chosen_file_that_has_gone_falls_back_rather_than_silent(self):
        # A path that no longer resolves is a moved file, not a request to
        # stop being told how downloads went.
        config = {"sounds_enabled": True,
                  "sound_download_complete": "C:/gone/missing.wav"}
        self.assertEqual(
            sounds.sound_path(config, sounds.COMPLETE),
            sounds.bundled_path(sounds.COMPLETE),
        )


class DownloadSoundsTests(unittest.TestCase):
    """A burst of finishes is one sound, and a failure in it is the sound."""

    def setUp(self):
        self.config = {"sounds_enabled": True}
        self.played = []
        self.timers = []

    def _sounds(self):
        player = sounds.DownloadSounds(self.config)
        return player

    def _run(self, outcomes):
        played = []
        pending = []

        def call_later(_delay, callback, *args):
            timer = mock.Mock()
            pending.append((timer, callback, args))
            return timer

        with mock.patch.object(sounds.wx, "CallLater", call_later), \
                mock.patch.object(sounds, "play",
                                  side_effect=lambda _c, event:
                                  played.append(event)):
            player = self._sounds()
            for failed in outcomes:
                player.report(failed=failed)
            # Only the last timer is left running; the earlier ones were
            # stopped as each new finish arrived.
            pending[-1][1](*pending[-1][2])
        return played, pending

    def test_twenty_tracks_finishing_together_play_one_sound(self):
        played, pending = self._run([False] * 20)
        self.assertEqual(played, [sounds.COMPLETE])
        # Every earlier timer was stopped rather than left to fire.
        for timer, _callback, _args in pending[:-1]:
            timer.Stop.assert_called_once_with()

    def test_a_failure_anywhere_in_the_burst_is_what_it_sounds_like(self):
        played, _pending = self._run([False, False, True, False, False])
        self.assertEqual(played, [sounds.FAILED])

    def test_one_finish_on_its_own_still_answers(self):
        played, _pending = self._run([False])
        self.assertEqual(played, [sounds.COMPLETE])

    def test_a_sound_still_waiting_is_dropped_on_the_way_out(self):
        played = []
        timer = mock.Mock()
        with mock.patch.object(sounds.wx, "CallLater", return_value=timer), \
                mock.patch.object(sounds, "play",
                                  side_effect=lambda _c, e: played.append(e)):
            player = self._sounds()
            player.report(failed=False)
            player.shutdown()
        timer.Stop.assert_called_once_with()
        self.assertEqual(played, [])


if __name__ == "__main__":
    unittest.main()
