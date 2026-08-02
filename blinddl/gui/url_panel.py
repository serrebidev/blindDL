# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Download-from-URL tab: paste any media/playlist/channel URL."""

import threading

import wx

from .. import adult_backend, preview, sideb_backend, ytdlp_backend
from .item_picker_dialog import ItemPickerDialog
from .media_player import MediaPlayerPanel


class UrlPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.play_token = None
        self.closing = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(self, label="&URL:")
        self.url_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.url_text.SetName("Media URL")
        self.url_text.Bind(wx.EVT_TEXT_ENTER, self.on_download)

        self.format_radio = wx.RadioBox(
            self, label="Media format", choices=["Audio", "Video"],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS,
        )
        self.format_radio.SetSelection(0 if frame.config["audio_only"] else 1)

        self.download_btn = wx.Button(self, label="&Download")
        self.download_btn.Bind(wx.EVT_BUTTON, self.on_download)
        self.play_btn = wx.Button(self, label="&Play URL")
        self.play_btn.SetHelpText(
            "Plays this URL without adding it to the download queue.")
        self.play_btn.Bind(wx.EVT_BUTTON, self.on_play_url)
        self.player = MediaPlayerPanel(self, frame, video_height=220)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(self.download_btn, 0, wx.RIGHT, 8)
        actions.Add(self.play_btn, 0)

        sizer.Add(label, 0, wx.ALL, 8)
        sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.format_radio, 0, wx.ALL, 8)
        sizer.Add(actions, 0, wx.ALL, 8)
        sizer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)

    def focus_input(self):
        self.url_text.SetFocus()

    def shutdown(self):
        self.closing = True
        self.play_token = None
        self.player.shutdown()

    def on_play_url(self, event):
        if self.closing:
            return
        url = self.url_text.GetValue().strip()
        if not url:
            self.frame.announce("Enter a URL.")
            return
        audio_only = self.format_radio.GetSelection() == 0
        token = self.play_token = object()
        self.play_btn.Disable()
        self.frame.announce("Preparing URL for playback...")
        threading.Thread(
            target=self._resolve_playback,
            args=(token, url, audio_only),
            daemon=True,
            name="blinddl-url-playback",
        ).start()

    def _resolve_playback(self, token, url, audio_only):
        try:
            location, title = preview.resolve_url(
                url, audio_only, self.frame.config)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._playback_failed, token, str(exc))
            return
        wx.CallAfter(self._playback_ready, token, location, title)

    def _playback_ready(self, token, location, title):
        if self.closing or token is not self.play_token:
            return
        self.play_btn.Enable()
        self.frame.play_media(self.player, location, title)

    def _playback_failed(self, token, error):
        if self.closing or token is not self.play_token:
            return
        self.play_btn.Enable()
        self.frame.announce("Could not play that URL.")
        wx.MessageBox(
            f"Could not play that URL:\n{error}", "blindDL",
            wx.OK | wx.ICON_ERROR, self,
        )

    def on_download(self, event):
        if self.closing:
            return
        url = self.url_text.GetValue().strip()
        if not url:
            self.frame.announce("Enter a URL.")
            return
        audio_only = self.format_radio.GetSelection() == 0
        self.download_btn.Disable()
        self.frame.announce("Reading URL...")
        threading.Thread(target=self._inspect, args=(url, audio_only),
                         daemon=True).start()

    def _inspect(self, url, audio_only):
        adult_error = None
        if adult_backend.is_supported_url(url):
            if not self.frame.config["adult_sites_enabled"]:
                wx.CallAfter(
                    self._inspect_failed,
                    "Adult sites are disabled. Enable them in Settings.",
                )
                return
            try:
                items, title = adult_backend.inspect_url(
                    url, config=self.frame.config)
                wx.CallAfter(self._inspect_done, items, title, False, "adult")
                return
            except Exception as exc:  # noqa: BLE001 - yt-dlp may still cope
                adult_error = str(exc)
        sideb_error = None
        if sideb_backend.is_deezer_url(url):
            # Deezer gets the full Side B treatment (tags, cover, lyrics);
            # if that fails, yt-dlp still gets its turn below.
            try:
                items, title = sideb_backend.extract_flat(
                    url, self.frame.config)
                wx.CallAfter(self._inspect_done, items, title, audio_only,
                             "sideb")
                return
            except Exception as exc:  # noqa: BLE001 - yt-dlp may still cope
                sideb_error = str(exc)
        try:
            items, title = ytdlp_backend.extract_flat(
                url, cookies_from_browser=
                self.frame.config["cookies_from_browser"])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = str(exc)
            if sideb_error:
                error = f"Side B: {sideb_error}\nyt-dlp: {error}"
            if adult_error:
                error = f"Adult API: {adult_error}\nyt-dlp: {error}"
            wx.CallAfter(self._inspect_failed, error)
            return
        wx.CallAfter(self._inspect_done, items, title, audio_only, "ytdlp")

    def _inspect_failed(self, error):
        if self.closing:
            return
        self.download_btn.Enable()
        self.frame.announce("Could not read URL.")
        wx.MessageBox(f"Could not read that URL:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)
        self.url_text.SetFocus()

    def _inspect_done(self, items, title, audio_only, engine):
        if self.closing:
            return
        self.download_btn.Enable()
        if not items:
            self.frame.announce("No items found.")
            return
        if len(items) > 1:
            dialog = ItemPickerDialog(self, items, title)
            if dialog.ShowModal() != wx.ID_OK:
                dialog.Destroy()
                self.frame.announce("Cancelled.")
                self.url_text.SetFocus()
                return
            items = dialog.selected_items()
            dialog.Destroy()
            if not items:
                self.frame.announce("No items selected.")
                self.url_text.SetFocus()
                return
        for item in items:
            if engine == "sideb":
                self.frame.queue.add_sideb(item["url"], item["title"])
            elif engine == "adult":
                self.frame.queue.add_adult(item, item["title"])
            else:
                self.frame.queue.add_ytdlp(item["url"], item["title"],
                                           audio_only=audio_only)
        self.frame.announce(
            f"Queued {len(items)} from {title}."
            if len(items) > 1 else f"Queued: {items[0]['title']}")
        self.url_text.Clear()
        self.frame.show_downloads_tab()
