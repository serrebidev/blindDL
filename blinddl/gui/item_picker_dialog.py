# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible item selection for URLs that contain multiple downloads."""

import threading

import wx

from .. import preview, ytdlp_backend
from .media_player import MediaPlayerPanel


class ItemPickerDialog(wx.Dialog):
    """Let the user choose which items from an expanded URL to queue.

    Nothing starts ticked. This list is most often an album reached from an
    artist, and arriving at one with all twenty-five tracks already chosen
    means the only safe key is Escape: every other way out downloads the lot.
    Ticking what you want is the shorter path in the common case, and Select
    all is one button away for the other one.

    The dialog carries a player for the same reason. A track list is
    something to listen through before choosing, and this was the one list in
    blindDL that could not be played from -- so an album's tracks had to be
    queued blind, or the dialog closed and the album browsed again from the
    results list to hear any of it.
    """

    def __init__(self, parent, items, title):
        super().__init__(
            parent,
            title="Choose downloads",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.items = list(items)
        self._changing_selection = False
        # The main window, when this dialog was opened from a panel that has
        # one. Playback needs its config and its "stop the other players"
        # rule; without it the dialog is still a perfectly good picker.
        self.frame = getattr(parent, "frame", None)
        self.player = None
        self._preview_token = None
        self._full_playback_token = None
        self._closing = False

        prompt = wx.StaticText(self, label=f"Choose from {title}.")
        self.item_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.item_list.SetName("Items to download")
        self.item_list.SetHelpText(
            "Space ticks the track you are on, and Enter downloads every "
            "ticked track. Nothing is ticked to start with."
        )
        self.item_list.EnableCheckBoxes()
        for column, heading in enumerate(
                ("Title", "Artist or channel", "Duration")):
            self.item_list.InsertColumn(column, heading)

        for row, item in enumerate(self.items):
            self.item_list.InsertItem(row, item.get("title") or "Unknown title")
            self.item_list.SetItem(
                row, 1, item.get("artist") or item.get("uploader") or "")
            duration = item.get("duration_s", item.get("duration"))
            self.item_list.SetItem(
                row, 2, ytdlp_backend.format_duration(duration))

        self.item_list.SetColumnWidth(0, 380)
        self.item_list.SetColumnWidth(1, 200)
        self.item_list.SetColumnWidth(2, 90)
        self.item_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_check_changed)
        self.item_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_check_changed)
        self.item_list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)

        self.count_text = wx.StaticText(self)

        self.preview_btn = wx.Button(self, label="&Preview")
        self.preview_btn.SetHelpText(
            "Plays a short clip of the track you are on."
        )
        self.preview_btn.Bind(wx.EVT_BUTTON, self.on_preview)
        self.play_full_btn = wx.Button(self, label="Play &full song")
        self.play_full_btn.SetHelpText(
            "Plays the whole of the track you are on, rather than a clip."
        )
        self.play_full_btn.Bind(wx.EVT_BUTTON, self.on_play_full)

        self.select_all_btn = wx.Button(self, label="Select &all")
        self.select_all_btn.Bind(wx.EVT_BUTTON, self.on_select_all)
        self.clear_btn = wx.Button(self, label="&Clear selection")
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_selection)

        self.download_btn = wx.Button(self, wx.ID_OK, "&Download selected")
        self.download_btn.SetDefault()
        self.download_btn.Bind(wx.EVT_BUTTON, self.on_download)
        cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")

        play_buttons = wx.BoxSizer(wx.HORIZONTAL)
        play_buttons.Add(self.preview_btn, 0, wx.RIGHT, 8)
        play_buttons.Add(self.play_full_btn, 0)

        selection_buttons = wx.BoxSizer(wx.HORIZONTAL)
        selection_buttons.Add(self.select_all_btn, 0, wx.RIGHT, 8)
        selection_buttons.Add(self.clear_btn, 0)

        action_buttons = wx.BoxSizer(wx.HORIZONTAL)
        action_buttons.AddStretchSpacer()
        action_buttons.Add(self.download_btn, 0, wx.RIGHT, 8)
        action_buttons.Add(cancel_btn, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, 8)
        sizer.Add(self.item_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.count_text, 0, wx.ALL, 8)
        sizer.Add(play_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(selection_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        if self.frame is not None:
            self.player = MediaPlayerPanel(self, self.frame, video_height=120)
            self.player.play_request = self.play_focused
            self.frame.register_player(self.player)
            sizer.Add(
                self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        else:
            self.preview_btn.Disable()
            self.play_full_btn.Disable()
        sizer.Add(action_buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((720, 620) if self.player is not None else (720, 480))
        self.SetMinSize((520, 360))

        self._update_selection_state()
        if self.items:
            self.item_list.Focus(0)
            self.item_list.Select(0)
        self.item_list.SetFocus()

    # -- selection -----------------------------------------------------------

    def selected_items(self):
        """Return selected item dictionaries in their original order."""
        return [item for row, item in enumerate(self.items)
                if self.item_list.IsItemChecked(row)]

    def on_select_all(self, event):
        self._set_all_selected(True)

    def on_clear_selection(self, event):
        self._set_all_selected(False)

    def on_download(self, event):
        """Close with the ticked tracks, or say why nothing happened.

        With nothing ticked, a disabled default button would swallow Enter in
        silence -- and silence is indistinguishable from a dialog that has
        stopped responding.
        """
        if not self.selected_items():
            self._announce(
                "Nothing is ticked. Press Space to tick the track you are "
                "on, or choose Select all."
            )
            self.item_list.SetFocus()
            return
        self.EndModal(wx.ID_OK)

    def _set_all_selected(self, selected):
        self._changing_selection = True
        self.item_list.Freeze()
        try:
            for row in range(len(self.items)):
                self.item_list.CheckItem(row, selected)
        finally:
            self.item_list.Thaw()
            self._changing_selection = False
        self._update_selection_state()
        self._announce_count()
        self.item_list.SetFocus()

    def _on_check_changed(self, event):
        if not self._changing_selection:
            self._update_selection_state()
            self._announce_count()
        event.Skip()

    def _update_selection_state(self):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        total = len(self.items)
        self.count_text.SetLabel(f"{selected} of {total} selected")
        self.download_btn.Enable(selected > 0)
        self.select_all_btn.Enable(selected < total)
        self.clear_btn.Enable(selected > 0)

    def _announce(self, message):
        if self.frame is not None:
            self.frame.announce(message)

    def _announce_count(self):
        self._announce(self.count_text.GetLabel() + ".")

    # -- playing one of the rows ---------------------------------------------

    def _focused_item(self):
        """The row the reader is on, which is what plays.

        Deliberately not the ticked rows: a tick says "download this", and
        having to tick a track before hearing it would be the opposite of
        what listening first is for.
        """
        row = self.item_list.GetFocusedItem()
        if 0 <= row < len(self.items):
            return self.items[row]
        return None

    def play_focused(self):
        """The player's own Play button, with nothing loaded yet."""
        if self.player is None:
            return False
        self.on_play_full(None)
        return True

    def on_preview(self, event):
        self._start_playback(full=False)

    def on_play_full(self, event):
        self._start_playback(full=True)

    def _start_playback(self, full):
        if self.player is None:
            return
        item = self._focused_item()
        if item is None:
            self._announce("Move to a track first.")
            return
        token = object()
        if full:
            self._full_playback_token = token
            # A preview still resolving must not land on top of this and
            # replace the whole song with a clip of it.
            self._preview_token = None
            self.play_full_btn.Disable()
            self.preview_btn.Enable()
            self._announce(f"Loading: {item.get('title') or 'track'}")
        else:
            self._preview_token = token
            self._full_playback_token = None
            self.preview_btn.Disable()
            self.play_full_btn.Enable()
            self._announce(f"Preparing preview: {item.get('title') or 'track'}")
        threading.Thread(
            target=self._resolve_playback,
            args=(token, item, full),
            daemon=True,
            name="blinddl-picker-play",
        ).start()

    def _resolve_playback(self, token, item, full):
        resolve = (preview.resolve_full_playback if full
                   else preview.resolve_search_result)
        try:
            location, title = resolve(item, True, self.frame.config)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._playback_failed, token, full, str(exc))
            return
        wx.CallAfter(self._playback_ready, token, full, location, title)

    def _current_token(self, full):
        return self._full_playback_token if full else self._preview_token

    def _playback_ready(self, token, full, location, title):
        if self._closing or token is not self._current_token(full):
            return
        self.preview_btn.Enable()
        self.play_full_btn.Enable()
        self.frame.play_media(self.player, location, title)

    def _playback_failed(self, token, full, error):
        if self._closing or token is not self._current_token(full):
            return
        self.preview_btn.Enable()
        self.play_full_btn.Enable()
        noun = "that song" if full else "that preview"
        self._announce(f"Could not play {noun}.")
        wx.MessageBox(
            f"Could not play {noun}:\n{error}",
            "blindDL",
            wx.OK | wx.ICON_ERROR,
            self,
        )

    # -- teardown ------------------------------------------------------------

    def Destroy(self):  # noqa: N802 - wx spelling
        """Stop and give up the player before the dialog goes away.

        Every caller destroys this dialog after ShowModal returns, so this is
        the one place all of them pass through. A player left running would
        keep playing with no window left to stop it from.
        """
        self._closing = True
        self._preview_token = None
        self._full_playback_token = None
        if self.player is not None:
            player, self.player = self.player, None
            player.shutdown()
            if self.frame is not None:
                self.frame.unregister_player(player)
        return super().Destroy()

    def _on_context_menu(self, event):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        menu = wx.Menu()
        preview_item = play_full_item = None
        if self.player is not None:
            preview_item = menu.Append(wx.ID_ANY, "&Preview")
            play_full_item = menu.Append(wx.ID_ANY, "Play &full song")
            menu.Enable(preview_item.GetId(), self._focused_item() is not None)
            menu.Enable(
                play_full_item.GetId(), self._focused_item() is not None)
            menu.AppendSeparator()
        download_item = menu.Append(wx.ID_OK, "Download selected")
        menu.Enable(download_item.GetId(), selected > 0)
        menu.AppendSeparator()
        select_all_item = menu.Append(wx.ID_ANY, "Select all")
        menu.Enable(select_all_item.GetId(), selected < len(self.items))
        clear_item = menu.Append(wx.ID_ANY, "Clear selection")
        menu.Enable(clear_item.GetId(), selected > 0)
        menu.Bind(wx.EVT_MENU, lambda _event: self.EndModal(wx.ID_OK),
                  download_item)
        menu.Bind(wx.EVT_MENU, self.on_select_all, select_all_item)
        menu.Bind(wx.EVT_MENU, self.on_clear_selection, clear_item)
        if preview_item is not None:
            menu.Bind(wx.EVT_MENU, self.on_preview, preview_item)
            menu.Bind(wx.EVT_MENU, self.on_play_full, play_full_item)
        self.item_list.PopupMenu(menu)
        menu.Destroy()
