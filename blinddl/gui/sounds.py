# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The two short sounds that say how a download ended.

A finished download is announced in words, and words arrive in the middle of
whatever else the screen reader is reading -- so the one thing worth knowing
at a glance, whether it worked, is the thing that gets interrupted or
missed. A sound says that much on its own, from another window, without
taking the reader's turn.

Bursts are collapsed rather than played through: an album finishes twenty
tracks in a few seconds, and twenty chimes says nothing that one chime does
not. Whatever happened in the burst is summed up by its worst outcome, so a
run with a failure in it never sounds like a clean one.
"""

from __future__ import annotations

import os
import sys

import wx
import wx.adv

# The names of the two events, which are also the file names shipped for
# them and the suffix of the settings that override those files.
COMPLETE = "download_complete"
FAILED = "download_failed"
EVENTS = (COMPLETE, FAILED)

# How long a run of finishes is gathered before one sound is played for all
# of it. Long enough to swallow an album landing at once, short enough that
# a single download still sounds as though it answered.
BURST_MS = 1200


def bundled_path(event):
    """The sound blindDL ships for *event*, wherever this build keeps it.

    A frozen build unpacks its data next to the executable's temporary root;
    a source checkout has it beside this package.
    """
    name = f"{event}.wav"
    roots = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(os.path.join(os.path.abspath(frozen_root), "sounds"))
    roots.append(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "sounds")
    )
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def sound_path(config, event):
    """The file *event* should play, or "" when it should stay silent.

    A path in settings replaces the shipped sound. One that has since been
    moved or deleted falls back to the shipped sound rather than to silence:
    a missing file is not a request not to be told.
    """
    if not config.get("sounds_enabled", True):
        return ""
    chosen = str(config.get(f"sound_{event}") or "").strip()
    if chosen and os.path.isfile(chosen):
        return chosen
    return bundled_path(event)


def play(config, event):
    """Play one event's sound now, if there is one to play.

    Never raises: a sound card that is busy, missing, or has been taken by
    another application must not turn into a failed download.
    """
    path = sound_path(config, event)
    if not path:
        return False
    try:
        silence = wx.LogNull()
        try:
            sound = wx.adv.Sound(path)
            if not sound.IsOk():
                return False
            # Kept on the module so the object outlives this call: an
            # asynchronous sound that is garbage collected stops.
            global _playing
            _playing = sound
            return bool(sound.Play(wx.adv.SOUND_ASYNC))
        finally:
            del silence
    except Exception:  # noqa: BLE001 - a sound is never worth an error
        return False


_playing = None


class DownloadSounds:
    """Collapses a burst of finished downloads into one sound.

    Every finish is reported here as it happens; the sound for it is held
    back until BURST_MS has gone by with nothing else arriving. A failure
    anywhere in that run is what the run is reported as, because a chime
    saying "all done" over a failed track is worse than no sound at all.
    """

    def __init__(self, config):
        self.config = config
        self._timer = None
        self._pending = None

    def report(self, failed):
        """Note that one download has just finished, successfully or not."""
        if failed or self._pending == FAILED:
            self._pending = FAILED
        else:
            self._pending = COMPLETE
        if self._timer is not None:
            self._timer.Stop()
        self._timer = wx.CallLater(BURST_MS, self._flush)

    def _flush(self):
        self._timer = None
        event, self._pending = self._pending, None
        if event is not None:
            play(self.config, event)

    def shutdown(self):
        """Drop a sound still waiting to be played, on the way out."""
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None
        self._pending = None
