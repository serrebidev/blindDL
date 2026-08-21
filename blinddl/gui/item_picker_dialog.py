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

    Playing works two ways, because choosing works two ways. Preview and Play
    full song take the track being read, ticked or not, for the listen that
    settles one track. The two ticked buttons play straight through
    everything that is ticked, in the order it was ticked, moving on by
    themselves as each track ends -- which is what a tick was otherwise only
    good for downloading with. Tick four songs off a twenty-track reissue in
    the order you want to hear them, and the dialog plays you your four, in
    your order, before you commit to the download.
    """

    def __init__(self, parent, items, title):
        super().__init__(
            parent,
            title="Choose downloads",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.items = list(items)
        self._changing_selection = False
        # Rows in the order they were ticked, which is the order the ticked
        # tracks play in. The list itself only knows which rows are ticked,
        # never when, and "in the order I chose them" is not a question a row
        # order can answer.
        self._tick_order = []
        # The ticked tracks a run is working through: a snapshot taken when
        # the run started, so ticking something else halfway through changes
        # what will be downloaded rather than what is currently playing.
        self._run = []
        self._run_at = 0
        self._run_full = True
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
            "ticked track. Alt+P previews the focused track and Alt+F plays "
            "its full song, neither of which ticks anything. Alt+I and Alt+L "
            "play the ticked tracks straight through, in the order you "
            "ticked them. Nothing is ticked to start with."
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
        self.item_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_checked)
        self.item_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_unchecked)
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
        self.preview_ticked_btn = wx.Button(self, label="Preview t&icked")
        self.preview_ticked_btn.SetHelpText(
            "Plays a short clip of each ticked track in turn, in the order "
            "you ticked them, moving on by itself as each clip ends."
        )
        self.preview_ticked_btn.Bind(wx.EVT_BUTTON, self.on_preview_ticked)
        self.play_ticked_btn = wx.Button(self, label="P&lay ticked")
        self.play_ticked_btn.SetHelpText(
            "Plays the whole of each ticked track in turn, in the order you "
            "ticked them, moving on by itself as each track ends."
        )
        self.play_ticked_btn.Bind(wx.EVT_BUTTON, self.on_play_ticked)

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
        play_buttons.Add(self.play_full_btn, 0, wx.RIGHT, 8)
        play_buttons.Add(self.preview_ticked_btn, 0, wx.RIGHT, 8)
        play_buttons.Add(self.play_ticked_btn, 0)

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
            self.player.finished_request = self._track_finished
            self.frame.register_player(self.player)
            sizer.Add(
                self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        else:
            for button in (self.preview_btn, self.play_full_btn,
                           self.preview_ticked_btn, self.play_ticked_btn):
                button.Disable()
        sizer.Add(action_buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((760, 640) if self.player is not None else (760, 480))
        self.SetMinSize((520, 360))

        self._update_selection_state()
        if self.items:
            self.item_list.Focus(0)
            self.item_list.Select(0)
        self.item_list.SetFocus()

    # -- selection -----------------------------------------------------------

    def selected_items(self):
        """The ticked items, in the album's own order.

        What gets downloaded is a release, and a release has a running
        order; the order the ticks happened to be made in is not it. Playing
        is the other way round -- see :meth:`ticked_items`.
        """
        return [item for row, item in enumerate(self.items)
                if self.item_list.IsItemChecked(row)]

    def ticked_items(self):
        """The ticked items, in the order they were ticked.

        Reconciled against the list rather than trusted outright: a tick
        event can arrive after the tick itself, so anything ticked that the
        running order has not heard about yet goes on the end, and anything
        no longer ticked drops out.
        """
        ticked = [row for row in range(len(self.items))
                  if self.item_list.IsItemChecked(row)]
        still_ticked = set(ticked)
        ordered = [row for row in self._tick_order if row in still_ticked]
        known = set(ordered)
        ordered.extend(row for row in ticked if row not in known)
        return [self.items[row] for row in ordered]

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
        if selected:
            # Select all gives the rows it adds the album's own order, and
            # leaves anything already ticked by hand where it was put.
            known = set(self._tick_order)
            self._tick_order.extend(
                row for row in range(len(self.items)) if row not in known)
        else:
            self._tick_order = []
        self._update_selection_state()
        self._announce_count()
        self.item_list.SetFocus()

    def _on_checked(self, event):
        row = event.GetIndex()
        if row not in self._tick_order:
            self._tick_order.append(row)
        self._check_changed()
        event.Skip()

    def _on_unchecked(self, event):
        row = event.GetIndex()
        if row in self._tick_order:
            self._tick_order.remove(row)
        self._check_changed()
        event.Skip()

    def _check_changed(self):
        if not self._changing_selection:
            self._update_selection_state()
            self._announce_count()

    def _update_selection_state(self):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        total = len(self.items)
        self.count_text.SetLabel(f"{selected} of {total} selected")
        self.download_btn.Enable(selected > 0)
        self.select_all_btn.Enable(selected < total)
        self.clear_btn.Enable(selected > 0)
        if self.player is not None:
            self.preview_ticked_btn.Enable(selected > 0)
            self.play_ticked_btn.Enable(selected > 0)

    def _announce(self, message):
        if self.frame is not None:
            self.frame.announce(message)

    def _announce_count(self):
        self._announce(self.count_text.GetLabel() + ".")

    # -- playing one of the rows ---------------------------------------------

    def _focused_item(self):
        """The row the reader is on, which is what Preview plays.

        Deliberately not the ticked rows: a tick says "download this", and
        having to tick a track before hearing it would be the opposite of
        what listening first is for. The ticked rows have two buttons of
        their own.
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
        self._play_one(full=False)

    def on_play_full(self, event):
        self._play_one(full=True)

    def on_preview_ticked(self, event):
        self._play_ticked(full=False)

    def on_play_ticked(self, event):
        self._play_ticked(full=True)

    def _play_one(self, full):
        """Play the row being read, and end any run of ticked tracks."""
        self._run = []
        item = self._focused_item()
        if item is None:
            self._announce("Move to a track first.")
            return
        self._start_playback(full, item)

    def _play_ticked(self, full):
        """Play through the ticked tracks, in the order they were ticked."""
        items = self.ticked_items()
        if not items:
            self._announce(
                "Nothing is ticked. Press Space to tick the track you are "
                "on, or choose Select all."
            )
            self.item_list.SetFocus()
            return
        self._run = items
        self._run_at = 0
        self._run_full = full
        noun = "track" if len(items) == 1 else "tracks"
        self._announce(
            f"Playing {len(items)} ticked {noun}, in the order you ticked "
            f"them."
        )
        self._play_run_item()

    def _play_run_item(self):
        self._start_playback(
            self._run_full,
            self._run[self._run_at],
            place=(self._run_at + 1, len(self._run)),
        )

    def _track_finished(self):
        """A track played to its end: move on to the next ticked one.

        Only reached when playback ended by itself. Stopping the player, or
        starting something else, ends the run instead of advancing it --
        Stop has to mean stop.
        """
        if self._closing or not self._run:
            return
        if self._run_at + 1 >= len(self._run):
            noun = "track" if len(self._run) == 1 else "tracks"
            self._announce(f"Finished the {len(self._run)} ticked {noun}.")
            self._run = []
            return
        self._run_at += 1
        self._play_run_item()

    def _start_playback(self, full, item, place=None):
        if self.player is None:
            return
        token = object()
        where = f"track {place[0]} of {place[1]}, " if place else ""
        if full:
            self._full_playback_token = token
            # A preview still resolving must not land on top of this and
            # replace the whole song with a clip of it.
            self._preview_token = None
            self.play_full_btn.Disable()
            self.preview_btn.Enable()
            self._announce(f"Loading {where}{item.get('title') or 'track'}")
        else:
            self._preview_token = token
            self._full_playback_token = None
            self.preview_btn.Disable()
            self.play_full_btn.Enable()
            self._announce(
                f"Preparing preview of {where}"
                f"{item.get('title') or 'track'}")
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
        if self._run:
            # One track that will not play must not end a run of twelve, and
            # a dialog box in the middle of one would stop the run just as
            # surely as the failure did. Say which track was lost, and carry
            # on to the next.
            lost = self._run[self._run_at].get("title") or "that track"
            self._announce(f"Skipping {lost}: it could not be played.")
            self._track_finished()
            return
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
        self._run = []
        if self.player is not None:
            player, self.player = self.player, None
            player.finished_request = None
            player.shutdown()
            if self.frame is not None:
                self.frame.unregister_player(player)
        return super().Destroy()

    def _on_context_menu(self, event):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        menu = wx.Menu()
        preview_item = play_full_item = None
        preview_ticked_item = play_ticked_item = None
        if self.player is not None:
            preview_item = menu.Append(wx.ID_ANY, "&Preview")
            play_full_item = menu.Append(wx.ID_ANY, "Play &full song")
            menu.Enable(preview_item.GetId(), self._focused_item() is not None)
            menu.Enable(
                play_full_item.GetId(), self._focused_item() is not None)
            preview_ticked_item = menu.Append(wx.ID_ANY, "Preview t&icked")
            play_ticked_item = menu.Append(wx.ID_ANY, "P&lay ticked")
            menu.Enable(preview_ticked_item.GetId(), selected > 0)
            menu.Enable(play_ticked_item.GetId(), selected > 0)
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
            menu.Bind(wx.EVT_MENU, self.on_preview_ticked, preview_ticked_item)
            menu.Bind(wx.EVT_MENU, self.on_play_ticked, play_ticked_item)
        self.item_list.PopupMenu(menu)
        menu.Destroy()
