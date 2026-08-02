# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible audio/video controls shared by URL, Search, and Library."""

from __future__ import annotations

import ctypes
import os
import sys
from urllib.parse import urlparse

import wx
import wx.media


def _configure_bundled_vlc():
    """Point python-vlc at the native runtime bundled by PyInstaller."""
    roots = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(os.path.abspath(frozen_root))
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    for root in roots:
        if sys.platform == "win32":
            library = os.path.join(root, "libvlc.dll")
            plugins = os.path.join(root, "plugins")
        elif sys.platform == "darwin":
            library = os.path.join(root, "vlc", "lib", "libvlc.dylib")
            core = os.path.join(root, "vlc", "lib", "libvlccore.dylib")
            plugins = os.path.join(root, "vlc", "plugins")
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


_configure_bundled_vlc()

try:
    import vlc
except (ImportError, NotImplementedError, OSError, SystemExit):
    # Native libVLC is optional at runtime; wx.media remains available.
    vlc = None


def _clock(milliseconds):
    seconds = max(0, int(milliseconds or 0) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class MediaPlayerPanel(wx.Panel):
    """VLC-backed playback with a native wx media fallback."""

    def __init__(self, parent, frame, video_height=180):
        super().__init__(parent)
        self.frame = frame
        self._title = ""
        self._loaded = False
        self._updating_position = False
        self._shutting_down = False
        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_events = None

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

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.stop_btn, 0)
        seek_row = wx.BoxSizer(wx.HORIZONTAL)
        seek_row.Add(self.position, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        seek_row.Add(self.time_text, 0, wx.ALIGN_CENTER_VERTICAL)
        volume_row = wx.BoxSizer(wx.HORIZONTAL)
        volume_row.Add(volume_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        volume_row.Add(self.volume, 1, wx.ALIGN_CENTER_VERTICAL)

        sizer.Add(self.now_playing, 0, wx.EXPAND | wx.BOTTOM, 4)
        sizer.Add(self.media, 1, wx.EXPAND | wx.BOTTOM, 6)
        sizer.Add(buttons, 0, wx.BOTTOM, 6)
        sizer.Add(seek_row, 0, wx.EXPAND | wx.BOTTOM, 6)
        sizer.Add(volume_row, 0, wx.EXPAND)
        self.SetSizer(sizer)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self.timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._enable_controls(False)

    def _create_media_surface(self):
        if vlc is not None:
            try:
                self._vlc_instance = vlc.Instance(
                    "--quiet", "--no-video-title-show", "--no-snapshot-preview")
                self._vlc_player = self._vlc_instance.media_player_new()
                self._vlc_events = self._vlc_player.event_manager()
                self._vlc_events.event_attach(
                    vlc.EventType.MediaPlayerEndReached, self._on_vlc_finished)
                self._vlc_events.event_attach(
                    vlc.EventType.MediaPlayerEncounteredError,
                    self._on_vlc_error)
                return wx.Panel(self, style=wx.SIMPLE_BORDER)
            except Exception:  # noqa: BLE001 - fall back when libVLC is absent
                if self._vlc_player is not None:
                    self._vlc_player.release()
                if self._vlc_instance is not None:
                    self._vlc_instance.release()
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
        self.play_btn.Enable(enabled)
        self.stop_btn.Enable(enabled)
        self.position.Enable(enabled)

    def load(self, location, title):
        self.stop(silent=True)
        self._title = title or "Untitled media"
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
            loaded = self._vlc_player.play() != -1
        elif parsed.scheme in ("http", "https"):
            loaded = self.media.LoadURI(location)
        else:
            loaded = self.media.Load(location)
        if not loaded:
            self.now_playing.SetLabel(f"Could not load: {self._title}")
            self.frame.announce(
                "The player could not load this media format or stream."
            )
            return False
        if self._vlc_player is not None or self.media.Play():
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
        self.timer.Start(250)
        self.frame.announce(f"Playing: {self._title}")

    def _on_finished(self, event):
        self._playback_finished()
        event.Skip()

    def _on_vlc_finished(self, event):
        wx.CallAfter(self._playback_finished)

    def _playback_finished(self):
        if self._shutting_down or self.IsBeingDeleted():
            return
        self.timer.Stop()
        self.play_btn.SetLabel("&Play")
        self.position.SetValue(1000)
        self.frame.announce(f"Finished playing: {self._title}")

    def _on_vlc_error(self, event):
        wx.CallAfter(self._playback_error)

    def _playback_error(self):
        if self._shutting_down or self.IsBeingDeleted():
            return
        self.timer.Stop()
        self.play_btn.SetLabel("&Play")
        self.now_playing.SetLabel(f"Could not play: {self._title}")
        self.frame.announce("The player could not decode or open this media.")

    def on_play_pause(self, event):
        if not self._loaded:
            self.frame.announce("Choose media to play first.")
            return
        if self._is_playing():
            self._pause()
            self.play_btn.SetLabel("&Play")
            self.frame.announce("Paused.")
        elif self._play():
            self.play_btn.SetLabel("&Pause")
            self.timer.Start(250)
            self.frame.announce(f"Playing: {self._title}")

    def on_stop(self, event):
        self.stop()

    def stop(self, silent=False):
        self.timer.Stop()
        length = self._length() if self._loaded else 0
        if self._loaded:
            self._stop()
        self.play_btn.SetLabel("&Play")
        self._set_time(0, length)
        if self._loaded and self._title:
            self.now_playing.SetLabel(f"Stopped: {self._title}")
        if not silent and self._title:
            self.frame.announce("Playback stopped.")

    def on_seek(self, event):
        if self._updating_position or not self._loaded:
            return
        length = self._length()
        if length > 0:
            self._seek(int(length * self.position.GetValue() / 1000))
            self._set_time(self._tell(), length)

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
        self.time_text.SetLabel(f"{_clock(current)} / {_clock(length)}")

    def _on_timer(self, event):
        if not self._loaded:
            return
        length = self._length()
        current = self._tell()
        self._set_time(current, length)
        if length > 0 and current >= 0:
            self._updating_position = True
            try:
                self.position.SetValue(min(1000, int(current * 1000 / length)))
            finally:
                self._updating_position = False

    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.timer.Stop()
        if self._loaded:
            self._stop()
        if self._vlc_player is not None:
            self._vlc_events.event_detach(vlc.EventType.MediaPlayerEndReached)
            self._vlc_events.event_detach(
                vlc.EventType.MediaPlayerEncounteredError)
            self._vlc_player.release()
            self._vlc_instance.release()
            self._vlc_player = None
            self._vlc_instance = None

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self.shutdown()
        event.Skip()
