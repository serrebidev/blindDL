# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible audio/video controls shared by URL, Search, and Library."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import sys
import threading
import time
from bisect import bisect_left
from collections.abc import Callable
from urllib.parse import urlparse

import wx
import wx.media

from .. import describe


def _configure_vlc():
    """Point python-vlc at a bundled or OS-installed native runtime."""
    roots = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(os.path.abspath(frozen_root))
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    if sys.platform == "win32":
        roots.extend(
            os.path.join(os.environ.get(name, ""), "VideoLAN", "VLC")
            for name in ("ProgramFiles", "ProgramFiles(x86)")
        )
    elif sys.platform == "darwin":
        roots.extend([
            "/Applications/VLC.app/Contents/MacOS",
            os.path.expanduser("~/Applications/VLC.app/Contents/MacOS"),
        ])
    for root in roots:
        if sys.platform == "win32":
            library = os.path.join(root, "libvlc.dll")
            plugins = os.path.join(root, "plugins")
        elif sys.platform == "darwin":
            bundled = os.path.join(root, "vlc")
            vlc_root = bundled if os.path.isdir(bundled) else root
            library = os.path.join(vlc_root, "lib", "libvlc.dylib")
            core = os.path.join(vlc_root, "lib", "libvlccore.dylib")
            plugins = os.path.join(vlc_root, "plugins")
            if not os.path.isfile(core):
                continue
            try:
                ctypes.CDLL(core, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                continue
        else:
            continue
        if os.path.isfile(library) and os.path.isdir(plugins):
            os.environ.setdefault("PYTHON_VLC_LIB_PATH", library)
            os.environ.setdefault("PYTHON_VLC_MODULE_PATH", plugins)
            return


vlc = None


def refresh_vlc_runtime():
    """Load VLC after a background WinGet installation completes."""
    global vlc
    if vlc is not None:
        return True
    _configure_vlc()
    try:
        vlc = importlib.import_module("vlc")
    except (ImportError, NotImplementedError, OSError, SystemExit):
        # Native libVLC is optional at runtime; wx.media remains available.
        vlc = None
    return vlc is not None


refresh_vlc_runtime()


_shared_vlc_instance = None
_shared_vlc_lock = threading.Lock()
PLAYBACK_TIMER_MS = 1000

# Playback speeds offered, as multiples of normal.
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

# Sleep timer: what it offers, and how long the sound takes to go. A player
# that stops dead is a jolt, and silence is where a sleep timer gets used.
SLEEP_CHOICES = ((15, "15 minutes"), (30, "30 minutes"),
                 (45, "45 minutes"), (60, "60 minutes"))
SLEEP_FADE_MS = 100
SLEEP_FADE_STEPS = 25

# Audio description read out during playback. The sound is dropped to this
# share of the volume while a description is read over it, which is the
# honest compromise: real description goes in the gaps in the dialogue,
# and choosing which line matters more than which sentence is an editorial
# judgement nothing here can make.
DESCRIPTION_DUCK_LEVEL = 0.3
DESCRIPTION_OFF, DESCRIPTION_DUCK, DESCRIPTION_PAUSE = "off", "duck", "pause"
DESCRIPTION_MODES = (
    ("Read it out, pausing the video", DESCRIPTION_PAUSE),
    ("Read it out over the sound, quietly", DESCRIPTION_DUCK),
    ("Do not read it out", DESCRIPTION_OFF),
)
DESCRIPTION_MODE_LABELS = {value: label for label, value in DESCRIPTION_MODES}
# How often the pause is looked at while a description is being read.
DESCRIPTION_TICK_MS = 200
# A description whose moment has been passed by more than this is one the
# user skipped over -- by seeking or by playing something else -- and the
# rest of that moment is not worth reading out.
DESCRIPTION_LATE_S = 4.0

# Where playback stopped is kept only for long media -- podcasts, mixes,
# audiobooks -- and only once it is past the first and short of the last
# half-minute, where starting over or finishing is what anyone would want.
RESUME_MIN_LENGTH_MS = 10 * 60 * 1000
RESUME_MARGIN_MS = 30 * 1000
RESUME_KEEP = 200
_positions_lock = threading.Lock()


def worth_resuming(current, length):
    """True when stopping at ``current`` of ``length`` ms should be kept."""
    return (length >= RESUME_MIN_LENGTH_MS
            and RESUME_MARGIN_MS <= current <= length - RESUME_MARGIN_MS)


def _positions_path():
    from ..config import app_data_dir
    return os.path.join(app_data_dir(), "playback_positions.json")


def _read_positions():
    try:
        with open(_positions_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_positions(data):
    path = _positions_path()
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
    except OSError:
        pass


def saved_position(location):
    """Milliseconds to resume ``location`` from, or 0."""
    value = _read_positions().get(str(location), 0)
    return int(value) if isinstance(value, (int, float)) else 0


def remember_position(location, current, length):
    """Keep or forget where ``location`` stopped; newest entries win."""
    key = str(location)
    with _positions_lock:
        data = _read_positions()
        keep = worth_resuming(current, length)
        if data.pop(key, None) is None and not keep:
            return
        if keep:
            data[key] = int(current)
            while len(data) > RESUME_KEEP:
                data.pop(next(iter(data)))
        _write_positions(data)


def _get_vlc_instance():
    """Create one libVLC runtime shared by every player surface."""
    global _shared_vlc_instance
    if not refresh_vlc_runtime():
        return None
    with _shared_vlc_lock:
        if _shared_vlc_instance is None:
            _shared_vlc_instance = vlc.Instance(
                "--quiet", "--no-video-title-show", "--no-snapshot-preview"
            )
        return _shared_vlc_instance


def _clock(milliseconds):
    seconds = max(0, int(milliseconds or 0) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def playback_status(state, title, current, length):
    """The status-bar line for one moment of playback.

    "Playing" on its own says nothing a listener did not already know. What
    is worth reading off the status bar is how far in the track is and how
    much of it is left, which is exactly what a sighted user reads off the
    seek bar.
    """
    if not state:
        return ""
    line = f"{state}: {title}" if title else state
    if length > 0:
        return (f"{line} — {_clock(current)} of {_clock(length)}, "
                f"{_clock(length - current)} left")
    return f"{line} — {_clock(current)}"


class MediaPlayerPanel(wx.Panel):
    """VLC-backed playback with a native wx media fallback."""

    @property
    def play_request(self):
        return self._play_request

    @play_request.setter
    def play_request(self, value):
        # Panels attach their play_request after construction, by which point
        # the controls have already been disabled with nothing loaded. Re-run
        # the enablement so a newly attached hook turns Play back on.
        self._play_request = value
        button = getattr(self, "play_btn", None)
        if button is not None:
            self._enable_controls(self._loaded)

    def is_playing(self):
        """True while this player is the one making sound."""
        return self._loaded and self._is_playing()

    def has_media(self):
        """True once something has been loaded, playing or not."""
        return self._loaded

    def __init__(self, parent, frame, video_height=180):
        super().__init__(parent)
        self.frame = frame
        # Set by a panel that has something to play when nothing is loaded
        # yet, so the Play button starts the selected result instead of
        # answering that there is nothing to play. Returns True when it
        # took the request on.
        self._play_request = None
        # Set by an owner that is playing through a list rather than one
        # track, and called when a track reaches its own end -- never when
        # playback was stopped or replaced. That distinction is the whole
        # point: a list should carry on by itself, and stop when stopped.
        self.finished_request: Callable[[], None] | None = None
        self._title = ""
        self._location = ""
        self._loaded = False
        self._updating_position = False
        self._shutting_down = False
        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_events = None
        # Increments on every load(); libVLC events carry the value they saw
        # so a stale event from a media just replaced is ignored.
        self._load_generation = 0
        # When the sleep timer should pause playback, as a reading of the
        # clock rather than a countdown left to run, so a paused player and
        # a machine that slept both still fire it at the right moment.
        # None is off.
        self._sleep_deadline = None
        self._sleep_fading = False
        self._sleep_fade_levels = []
        # Audio description for this media, as (seconds, words), and where
        # playback has got to in it. Only ever read out while playing: a
        # description that talks over a paused picture is a reading of
        # nothing.
        self._description = []
        self._description_at = 0
        self._description_busy = False
        self._description_until = 0.0
        self._description_volume = None
        self._description_paused = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.now_playing = wx.StaticText(self, label="Nothing playing.")
        self.now_playing.SetName("Now playing")

        self.media = self._create_media_surface()
        self.media.SetName("Audio and video display")
        self.media.SetMinSize((-1, video_height))
        if self._vlc_player is None:
            self.media.Bind(wx.media.EVT_MEDIA_LOADED, self._on_loaded)
            self.media.Bind(wx.media.EVT_MEDIA_FINISHED, self._on_finished)

        self.play_btn = wx.Button(self, label="&Play")
        self.play_btn.SetName("Play or pause")
        self.play_btn.SetHelpText(
            "Plays or pauses. With nothing loaded, plays whatever is "
            "selected in the list above."
        )
        self.stop_btn = wx.Button(self, label="&Stop")
        self.play_btn.Bind(wx.EVT_BUTTON, self.on_play_pause)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)

        self.position = wx.Slider(self, value=0, minValue=0, maxValue=1000)
        self.position.SetName("Playback position")
        self.position.SetHelpText(
            "Use Left and Right Arrow to seek through the current media."
        )
        self.position.Bind(wx.EVT_SLIDER, self.on_seek)
        self.time_text = wx.StaticText(self, label="0:00 / 0:00")
        self.time_text.SetName("Playback time")

        volume_label = wx.StaticText(self, label="&Volume:")
        self.volume = wx.Slider(
            self,
            value=80,
            minValue=0,
            maxValue=100,
            style=wx.SL_HORIZONTAL,
        )
        self.volume.SetName("Volume")
        self.volume.SetHelpText("Use Left and Right Arrow to change volume.")
        self.volume.Bind(wx.EVT_SLIDER, self.on_volume)
        self._set_volume(80)

        speed_label = wx.StaticText(self, label="Speed:")
        self.speed = wx.Choice(
            self, choices=[f"{value:g}x" for value in SPEEDS])
        self.speed.SetName("Playback speed")
        self.speed.SetHelpText(
            "How fast media plays. Remembered for everything played after.")
        self.speed.SetSelection(SPEEDS.index(self._rate()))
        self.speed.Bind(wx.EVT_CHOICE, self.on_speed)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.stop_btn, 0)
        seek_row = wx.BoxSizer(wx.HORIZONTAL)
        seek_row.Add(self.position, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        seek_row.Add(self.time_text, 0, wx.ALIGN_CENTER_VERTICAL)
        volume_row = wx.BoxSizer(wx.HORIZONTAL)
        volume_row.Add(volume_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        volume_row.Add(self.volume, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        volume_row.Add(speed_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        volume_row.Add(self.speed, 0, wx.ALIGN_CENTER_VERTICAL)

        sizer.Add(self.now_playing, 0, wx.EXPAND | wx.BOTTOM, 4)
        sizer.Add(self.media, 1, wx.EXPAND | wx.BOTTOM, 6)
        sizer.Add(buttons, 0, wx.BOTTOM, 6)
        sizer.Add(seek_row, 0, wx.EXPAND | wx.BOTTOM, 6)
        sizer.Add(volume_row, 0, wx.EXPAND)
        self.SetSizer(sizer)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self.timer)
        # The fade before a sleep-timer pause has its own timer: it runs
        # several times a second for a couple of seconds, where the one
        # above ticks once a second for as long as anything plays.
        self.sleep_fade_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_sleep_fade, self.sleep_fade_timer)
        # Reading a description pauses the picture for as long as the words
        # take, and no screen reader will say when it has finished, so this
        # is what starts playback again afterwards.
        self.description_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_description_tick,
                  self.description_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._enable_controls(False)

    def _create_media_surface(self):
        if vlc is not None:
            try:
                self._vlc_instance = _get_vlc_instance()
                self._vlc_player = self._vlc_instance.media_player_new()
                self._vlc_events = self._vlc_player.event_manager()
                self._vlc_events.event_attach(
                    vlc.EventType.MediaPlayerEndReached, self._on_vlc_finished)
                self._vlc_events.event_attach(
                    vlc.EventType.MediaPlayerPlaying, self._on_vlc_playing)
                self._vlc_events.event_attach(
                    vlc.EventType.MediaPlayerEncounteredError,
                    self._on_vlc_error)
                return wx.Panel(self, style=wx.SIMPLE_BORDER)
            except Exception:  # noqa: BLE001 - fall back when libVLC is absent
                if self._vlc_player is not None:
                    self._vlc_player.release()
                self._vlc_player = None
                self._vlc_instance = None
                self._vlc_events = None
        return wx.media.MediaCtrl(self, style=wx.SIMPLE_BORDER)

    def _attach_video_surface(self):
        if self._vlc_player is None:
            return
        handle = int(self.media.GetHandle())
        if sys.platform == "win32":
            self._vlc_player.set_hwnd(handle)
        elif sys.platform == "darwin":
            self._vlc_player.set_nsobject(handle)
        else:
            self._vlc_player.set_xwindow(handle)

    def _enable_controls(self, enabled):
        # Play stays usable with nothing loaded when a panel has given it
        # something to start: that is the whole point of the hook.
        self.play_btn.Enable(enabled or self.play_request is not None)
        self.stop_btn.Enable(enabled)
        self.position.Enable(enabled)

    def load(self, location, title):
        self.stop(silent=True)
        self._load_generation += 1
        self._title = title or "Untitled media"
        self._location = str(location)
        # A description belongs to the file it describes, so it is looked
        # for beside whatever is being played rather than remembered from
        # the last thing that was.
        self._description = describe.load_description(location)
        self._description_at = 0
        self._loaded = False
        self.now_playing.SetLabel(f"Loading: {self._title}")
        self._enable_controls(False)
        location = str(location)
        parsed = urlparse(location)
        if self._vlc_player is not None:
            self._attach_video_surface()
            if parsed.scheme in ("http", "https"):
                media = self._vlc_instance.media_new(location)
            else:
                media = self._vlc_instance.media_new_path(os.path.abspath(location))
            self._vlc_player.set_media(media)
            media.release()
            if self._vlc_player.play() == -1:
                self.now_playing.SetLabel(f"Could not load: {self._title}")
                self.frame.announce(
                    "The player could not load this media format or stream."
                )
                return False
            # libVLC opens media asynchronously: play() returning 0 only
            # means the request was accepted, not that audio is already
            # coming out. A stream can spend seconds in its Opening state
            # (YouTube's googlevideo URLs are the worst offender), and
            # announcing "Playing" during that gap left the user with a
            # player that claimed to play while staying silent. The
            # MediaPlayerPlaying event is what says playback really began.
            self.frame.announce(f"Loading: {self._title}")
            return True
        if parsed.scheme in ("http", "https"):
            loaded = self.media.LoadURI(location)
        else:
            loaded = self.media.Load(location)
        if not loaded:
            self.now_playing.SetLabel(f"Could not load: {self._title}")
            self.frame.announce(
                "The player could not load this media format or stream."
            )
            return False
        if self.media.Play():
            self._playback_started()
        else:
            self.frame.announce(f"Loading: {self._title}")
        return True

    def _on_loaded(self, event):
        if self._loaded and self._is_playing():
            event.Skip()
            return
        if self.media.Play():
            self._playback_started()
        else:
            self.frame.announce("The media loaded, but playback could not start.")
        event.Skip()

    def _playback_started(self):
        self._loaded = True
        self._enable_controls(True)
        self.now_playing.SetLabel(f"Now playing: {self._title}")
        self.play_btn.SetLabel("&Pause")
        self.timer.Start(PLAYBACK_TIMER_MS)
        self.speed.SetSelection(SPEEDS.index(self._rate()))
        self._set_rate(self._rate())
        message = f"Playing: {self._title}"
        resume = saved_position(self._location)
        if resume:
            self._seek(resume)
            message += f", resumed at {_clock(resume)}"
        self._sync_description_position()
        if self._description:
            mode = self.description_mode()
            message += (
                f". Audio description: {DESCRIPTION_MODE_LABELS[mode].lower()}."
                if mode != DESCRIPTION_OFF
                else ". This one has an audio description; it is switched off.")
        self._report_status("Playing")
        self.frame.announce(message)

    def _rate(self):
        config = getattr(self.frame, "config", None)
        value = config.get("playback_rate", 1.0) if config is not None else 1.0
        return value if value in SPEEDS else 1.0

    def on_speed(self, event):
        rate = SPEEDS[self.speed.GetSelection()]
        config = getattr(self.frame, "config", None)
        if config is not None:
            config["playback_rate"] = rate
            config.save()
        if self._loaded:
            self._set_rate(rate)

    def _set_rate(self, rate):
        if self._vlc_player is not None:
            self._vlc_player.set_rate(rate)
        else:
            self.media.SetPlaybackRate(rate)

    def _remember(self):
        """Keep where long media stopped, so it can start from there next."""
        # A stopped player reads 0; that says nothing about where the
        # listener got to, so it must not wipe what Stop already kept.
        current = self._tell() if self._loaded else 0
        if current and self._location:
            remember_position(self._location, current, self._length())

    def _on_finished(self, event):
        self._playback_finished()
        event.Skip()

    def _on_vlc_finished(self, event):
        wx.CallAfter(self._playback_finished, self._load_generation)

    def _on_vlc_playing(self, event):
        wx.CallAfter(self._on_vlc_playing_gui, self._load_generation)

    def _on_vlc_playing_gui(self, generation):
        if self._shutting_down or self.IsBeingDeleted():
            return
        if generation != self._load_generation:
            return
        if self._loaded:
            return
        self._playback_started()

    def _playback_finished(self, generation=None):
        if self._shutting_down or self.IsBeingDeleted():
            return
        if generation is not None and generation != self._load_generation:
            return
        if not self._loaded:
            # Playback was never announced as started, so this finished
            # event belongs to a media that was just replaced (its Playing
            # event never had a chance to fire, or already fired for the
            # previous load). There is nothing to report as finished.
            return
        self.timer.Stop()
        location = getattr(self, "_location", "")
        if location:
            remember_position(location, 0, 0)
        self.play_btn.SetLabel("&Play")
        self.position.SetValue(1000)
        self._report_status("")
        self.frame.announce(f"Finished playing: {self._title}")
        if self.finished_request is not None:
            self.finished_request()

    def _on_vlc_error(self, event):
        wx.CallAfter(self._playback_error, self._load_generation)

    def _playback_error(self, generation=None):
        if self._shutting_down or self.IsBeingDeleted():
            return
        if generation is not None and generation != self._load_generation:
            return
        self.timer.Stop()
        self.play_btn.SetLabel("&Play")
        # Playback is over (it never really began), so the controls come
        # back on. load() disables them while the stream is still Opening;
        # without this they stay dead after a decode error and the only
        # way to retry is to start a whole new selection.
        self._enable_controls(True)
        self.now_playing.SetLabel(f"Could not play: {self._title}")
        self._report_status("")
        self.frame.announce("The player could not decode or open this media.")

    def on_play_pause(self, event):
        if not self._loaded:
            # Nothing is loaded, but the tab this player belongs to may have
            # a selected result that Play is obviously meant to start. Only
            # say there is nothing to play when there really is not.
            if self.play_request is not None and self.play_request():
                return
            self.frame.announce("Choose media to play first.")
            return
        if self._is_playing():
            self._pause()
            # Nothing moves while paused, so the position clock has nothing
            # left to read: it used to keep asking libVLC where it was, once
            # a second, for as long as the player sat there -- including
            # after the window was hidden to the tray. Seeking still writes
            # the time itself, and resuming below starts the clock again.
            self.timer.Stop()
            self.play_btn.SetLabel("&Play")
            self._report_status("Paused")
            self.frame.announce("Paused.")
        elif self._play():
            self.play_btn.SetLabel("&Pause")
            self.timer.Start(PLAYBACK_TIMER_MS)
            self._report_status("Playing")
            self.frame.announce(f"Playing: {self._title}")

    def on_stop(self, event):
        self.stop()

    def stop(self, silent=False):
        self.timer.Stop()
        self._clear_sleep_fade()
        self._end_description()
        if not silent:
            # An explicit Stop is also the user saying they do not want to
            # be paused later. A load() in between -- the next item of a
            # list -- is not, so a sleep timer outlives a running playlist.
            self._sleep_deadline = None
        length = self._length() if self._loaded else 0
        self._remember()
        if self._loaded:
            self._stop()
        self.play_btn.SetLabel("&Play")
        self._set_time(0, length)
        self._report_status("")
        if self._loaded and self._title:
            self.now_playing.SetLabel(f"Stopped: {self._title}")
        if not silent and self._title:
            self.frame.announce("Playback stopped.")

    def set_sleep_timer(self, minutes):
        """Pause playback after *minutes*, fading the sound out first.

        Zero turns the timer off. The deadline is a reading of the clock
        rather than a countdown, so pausing, seeking, or playing the next
        item of a playlist neither spends it early nor loses it -- which is
        the whole point of setting one before an audiobook chapter
        you are not going to stay awake for.
        """
        minutes = int(minutes or 0)
        self._clear_sleep_fade()
        if minutes <= 0:
            self._sleep_deadline = None
            self.frame.announce("Sleep timer off.")
            return
        self._sleep_deadline = time.monotonic() + minutes * 60
        self.frame.announce(f"Sleep timer: pausing in {minutes} minutes.")

    def sleep_timer_active(self):
        return self._sleep_deadline is not None

    def _clear_sleep_fade(self):
        self.sleep_fade_timer.Stop()
        self._sleep_fading = False
        self._sleep_fade_levels = []

    def _start_sleep_fade(self):
        """Take the volume down to nothing, then pause."""
        if self._sleep_fading:
            return
        self._sleep_deadline = None
        self._sleep_fading = True
        # The fade starts from the slider's own value, so it lasts the same
        # whatever the listening volume, and the control goes back to
        # reading what the user set when the sound is restored.
        volume = max(1, self.volume.GetValue())
        self._sleep_fade_levels = [
            round(volume * (SLEEP_FADE_STEPS - step) / SLEEP_FADE_STEPS)
            for step in range(SLEEP_FADE_STEPS + 1)
        ]
        self.sleep_fade_timer.Start(SLEEP_FADE_MS)

    def _on_sleep_fade(self, event):
        if not self._sleep_fade_levels:
            self._clear_sleep_fade()
            return
        self._set_volume(self._sleep_fade_levels.pop(0))
        if self._sleep_fade_levels:
            return
        self._clear_sleep_fade()
        self._set_volume(self.volume.GetValue())
        if self._is_playing():
            self._pause()
        # Paused, so the position clock has nothing left to read: this is
        # what on_play_pause does on the way down, and for the same reason.
        self.timer.Stop()
        self.play_btn.SetLabel("&Play")
        self._report_status("Paused")
        self.frame.announce("Sleep timer: playback paused.")

    # -- audio description -----------------------------------------------------

    def description_mode(self):
        """How this player reads audio description out, from Settings."""
        config = getattr(self.frame, "config", None)
        value = config.get("description_mode") if config is not None else None
        known = {mode for _label, mode in DESCRIPTION_MODES}
        return value if value in known else DESCRIPTION_PAUSE

    def has_description(self):
        """True when something beside this media describes it."""
        return bool(self._description)

    def _sync_description_position(self):
        """Line the description up with wherever playback has been moved to."""
        if not self._description:
            return
        now = self._tell() / 1000.0
        self._description_at = bisect_left(
            [at for at, _text in self._description], now)

    def _read_due_description(self):
        """Read out the next description whose moment has arrived."""
        if not self._description or self._description_busy:
            return
        mode = self.description_mode()
        if mode == DESCRIPTION_OFF or not self._is_playing():
            return
        now = self._tell() / 1000.0
        while self._description_at < len(self._description):
            at, text = self._description[self._description_at]
            if at > now:
                return
            self._description_at += 1
            # A moment that far behind is one the user has already gone
            # past -- by seeking, or by playing something else -- and
            # reading it out now would describe a picture that is gone.
            if now - at <= DESCRIPTION_LATE_S:
                self._read_description(text, mode)
                return

    def _read_description(self, text, mode):
        """Say one description, holding the picture for as long as it takes.

        No screen reader will say when it has finished, so the pause lasts
        as long as the words take at an ordinary reading rate. That is an
        estimate, and it is the honest one available: the alternative is
        either talking over the description or never carrying on.
        """
        self._description_busy = True
        self._description_until = (
            time.monotonic() + describe.reading_seconds(text))
        self._description_paused = False
        if mode == DESCRIPTION_DUCK:
            self._description_volume = self.volume.GetValue()
            self._set_volume(max(1, int(
                self._description_volume * DESCRIPTION_DUCK_LEVEL)))
        elif self._is_playing():
            self._pause()
            self._description_paused = True
        self.frame.announce(text)
        self.description_timer.Start(DESCRIPTION_TICK_MS)

    def _on_description_tick(self, event):
        if time.monotonic() < self._description_until:
            return
        self._end_description()

    def _end_description(self):
        """Stop holding the picture and put the sound back as it was."""
        self.description_timer.Stop()
        self._description_busy = False
        if self._description_volume is not None:
            self._set_volume(self._description_volume)
            self._description_volume = None
        if self._description_paused:
            self._description_paused = False
            if not self._is_playing():
                self._play()
                self.play_btn.SetLabel("&Pause")
                self.timer.Start(PLAYBACK_TIMER_MS)

    def on_seek(self, event):
        if self._updating_position or not self._loaded:
            return
        length = self._length()
        if length > 0:
            self._seek(int(length * self.position.GetValue() / 1000))
            self._set_time(self._tell(), length)
            self._report_status(
                "Playing" if self._is_playing() else "Paused")
            self._sync_description_position()

    def on_volume(self, event):
        self._set_volume(self.volume.GetValue())

    def _is_playing(self):
        if self._vlc_player is not None:
            return bool(self._vlc_player.is_playing())
        return self.media.GetState() == wx.media.MEDIASTATE_PLAYING

    def _play(self):
        if self._vlc_player is not None:
            self._vlc_player.set_pause(0)
            return self._vlc_player.play() != -1
        return self.media.Play()

    def _pause(self):
        if self._vlc_player is not None:
            self._vlc_player.set_pause(1)
        else:
            self.media.Pause()

    def _stop(self):
        if self._vlc_player is not None:
            self._vlc_player.stop()
        else:
            self.media.Stop()

    def _length(self):
        if self._vlc_player is not None:
            return max(0, self._vlc_player.get_length())
        return max(0, self.media.Length())

    def _tell(self):
        if self._vlc_player is not None:
            return max(0, self._vlc_player.get_time())
        return max(0, self.media.Tell())

    def _seek(self, milliseconds):
        if self._vlc_player is not None:
            self._vlc_player.set_time(milliseconds)
        else:
            self.media.Seek(milliseconds)

    def _set_volume(self, value):
        if self._vlc_player is not None:
            self._vlc_player.audio_set_volume(value)
        else:
            self.media.SetVolume(value / 100.0)

    def _set_time(self, current, length):
        label = f"{_clock(current)} / {_clock(length)}"
        if label != self.time_text.GetLabel():
            self.time_text.SetLabel(label)

    def _report_status(self, state):
        """Put where playback has got to on the main window's status bar.

        Written, never spoken: it changes once a second, and a screen
        reader is asked for the status bar rather than told it.
        """
        report = getattr(self.frame, "set_playback_status", None)
        if report is None:
            return
        if not state:
            report("")
            return
        report(playback_status(
            state, self._title, self._tell(), self._length()))

    def _on_timer(self, event):
        if not self._loaded:
            return
        if (self._sleep_deadline is not None
                and time.monotonic() >= self._sleep_deadline):
            self._start_sleep_fade()
        else:
            self._read_due_description()
        length = self._length()
        current = self._tell()
        self._set_time(current, length)
        self._report_status("Playing" if self._is_playing() else "Paused")
        # Do not send unsolicited accessibility value-change events while a
        # screen-reader user is positioned on the seek control. Their own
        # arrow-key changes still go through on_seek immediately.
        if length > 0 and current >= 0 and not self.position.HasFocus():
            value = min(1000, int(current * 1000 / length))
            if value == self.position.GetValue():
                return
            self._updating_position = True
            try:
                self.position.SetValue(value)
            finally:
                self._updating_position = False

    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.timer.Stop()
        self._clear_sleep_fade()
        self.description_timer.Stop()
        self._report_status("")
        self._remember()
        if self._loaded:
            self._stop()
        if self._vlc_player is not None:
            self._vlc_events.event_detach(vlc.EventType.MediaPlayerEndReached)
            self._vlc_events.event_detach(vlc.EventType.MediaPlayerPlaying)
            self._vlc_events.event_detach(
                vlc.EventType.MediaPlayerEncounteredError)
            self._vlc_player.release()
            self._vlc_player = None
            self._vlc_instance = None

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self.shutdown()
        event.Skip()
