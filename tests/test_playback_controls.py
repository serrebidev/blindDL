# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import logging
import time
import unittest
from types import SimpleNamespace
from unittest import mock

# musicdl creates a file logger at import time. Keep these tests
# self-contained, exactly as the wider GUI tests do.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl.gui import media_player
    from blinddl.gui.mainframe import MainFrame
    from blinddl.gui.media_player import MediaPlayerPanel

# The methods that belong to the panel rather than to one of its widgets,
# bound onto a stand-in so the sleep timer can be driven without a window.
_PANEL_METHODS = (
    "_clear_sleep_fade",
    "_start_sleep_fade",
    "_on_sleep_fade",
    "set_sleep_timer",
    "sleep_timer_active",
)


def _panel(**values):
    panel = SimpleNamespace(
        frame=mock.Mock(),
        volume=mock.Mock(GetValue=lambda: 80),
        sleep_fade_timer=mock.Mock(),
        timer=mock.Mock(),
        play_btn=mock.Mock(),
        position=mock.Mock(),
        now_playing=mock.Mock(),
        _loaded=True,
        _title="One",
        _sleep_deadline=None,
        _sleep_fading=False,
        _sleep_fade_levels=[],
        _set_volume=mock.Mock(),
        _stop=mock.Mock(),
        _pause=mock.Mock(),
        _is_playing=mock.Mock(return_value=True),
        _length=lambda: 0,
        _tell=lambda: 0,
        _set_time=lambda current, length: None,
        _remember=lambda: None,
        _report_status=lambda state: None,
    )
    vars(panel).update(values)
    for name in _PANEL_METHODS:
        setattr(panel, name, getattr(MediaPlayerPanel, name).__get__(panel))
    return panel


class SleepTimerTests(unittest.TestCase):
    def test_setting_the_timer_arms_it_and_says_when(self):
        panel = _panel()
        before = time.monotonic()

        panel.set_sleep_timer(30)

        self.assertGreaterEqual(panel._sleep_deadline, before + 30 * 60)
        self.assertTrue(panel.sleep_timer_active())
        panel.frame.announce.assert_called_once_with(
            "Sleep timer: pausing in 30 minutes.")

    def test_a_timer_the_user_turns_back_off_says_so(self):
        panel = _panel(_sleep_deadline=time.monotonic() + 60)

        panel.set_sleep_timer(0)

        self.assertIsNone(panel._sleep_deadline)
        self.assertFalse(panel.sleep_timer_active())
        panel.frame.announce.assert_called_once_with("Sleep timer off.")

    def test_the_timer_goes_off_when_it_is_due(self):
        panel = _panel(_sleep_deadline=time.monotonic() - 1)

        MediaPlayerPanel._on_timer(panel, None)

        self.assertTrue(panel._sleep_fading)
        panel.sleep_fade_timer.Start.assert_called_once_with(
            media_player.SLEEP_FADE_MS)

    def test_a_timer_that_is_not_due_does_nothing(self):
        panel = _panel(_sleep_deadline=time.monotonic() + 600)

        MediaPlayerPanel._on_timer(panel, None)

        self.assertFalse(panel._sleep_fading)
        panel.sleep_fade_timer.Start.assert_not_called()

    def test_the_sound_fades_to_nothing_and_then_pauses(self):
        # The point of the fade: a player that stops dead in a quiet room is
        # a jolt, and a quiet room is where a sleep timer gets used.
        panel = _panel()

        panel.set_sleep_timer(15)
        panel._sleep_deadline = time.monotonic() - 1
        MediaPlayerPanel._on_timer(panel, None)
        while panel._sleep_fade_levels:
            panel._on_sleep_fade(None)

        levels = [call.args[0] for call in panel._set_volume.call_args_list]
        self.assertEqual(levels[0], 80)
        self.assertEqual(min(levels), 0)
        # The volume control goes back to what the user set, ready for the
        # next thing they play.
        self.assertEqual(levels[-1], 80)
        panel._pause.assert_called_once_with()
        panel.frame.announce.assert_called_with("Sleep timer: playback paused.")
        self.assertFalse(panel._sleep_fading)
        self.assertFalse(panel.sleep_timer_active())

    def test_stop_ends_the_timer_and_a_playlist_does_not(self):
        panel = _panel(_sleep_deadline=time.monotonic() + 600)

        MediaPlayerPanel.stop(panel, silent=True)
        self.assertTrue(panel.sleep_timer_active())

        MediaPlayerPanel.stop(panel)
        self.assertFalse(panel.sleep_timer_active())


class PlaybackMenuTests(unittest.TestCase):
    def _player(self, playing=False, loaded=True):
        return SimpleNamespace(
            is_playing=lambda: playing, has_media=lambda: loaded)

    def _frame(self, players, page):
        """A frame whose tab in front belongs to *page* (a player)."""
        frame = SimpleNamespace(
            _players=lambda: list(players),
            notebook=SimpleNamespace(
                GetCurrentPage=lambda: SimpleNamespace(player=page)),
            announce=mock.Mock(),
        )
        for name in ("_playback_player", "on_play_pause_menu",
                     "on_stop_playback", "on_sleep_timer"):
            setattr(frame, name, getattr(MainFrame, name).__get__(frame))
        return frame

    def test_the_menu_acts_on_whatever_is_playing(self):
        idle = self._player(loaded=True)
        playing = self._player(playing=True)
        frame = self._frame([idle, playing], page=idle)

        self.assertIs(frame._playback_player(), playing)

    def test_a_paused_track_on_the_tab_in_front_is_the_one_acted_on(self):
        elsewhere = self._player(loaded=True)
        front = self._player(loaded=True)
        frame = self._frame([elsewhere, front], page=front)

        self.assertIs(frame._playback_player(), front)

    def test_the_tab_in_front_is_used_when_nothing_has_been_loaded(self):
        front = self._player(loaded=False)
        frame = self._frame([front], page=front)

        self.assertIs(frame._playback_player(), front)

    def test_stop_says_so_when_nothing_is_playing(self):
        front = self._player(loaded=False)
        frame = self._frame([front], page=front)

        frame.on_stop_playback()

        frame.announce.assert_called_once_with("Nothing is playing.")

    def test_the_sleep_timer_reaches_the_player(self):
        front = self._player()
        front.set_sleep_timer = mock.Mock()
        frame = self._frame([front], page=front)

        frame.on_sleep_timer(45)

        front.set_sleep_timer.assert_called_once_with(45)


if __name__ == "__main__":
    unittest.main()
