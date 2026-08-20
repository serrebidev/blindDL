# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Download queue tab: results kept aside, played from, and fetched later.

The Downloads tab is transfers already under way. This one is the step
before that: what you found and meant to keep, waiting for you to decide.
Rows stay until they are downloaded or removed, they survive restarts, and
they can be played -- a preview or the whole song -- without leaving the
list, which is what makes it possible to choose between them at all.
"""

from __future__ import annotations

import threading

import wx

from .. import preview, ytdlp_backend
from ..downloader import addition_summary
from .media_player import MediaPlayerPanel
from .search_panel import (
    ResultsList,
    collection_tracks,
    is_collection_item,
    queue_collection_tracks,
    queue_result,
    result_type,
)

COLUMN_HEADINGS = ("Title", "Type", "Artist / channel", "Source", "Duration")


class QueuePanel(wx.Panel):
    """The saved-results list, its player, and the actions over both."""

    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.store = frame.saved
        self.closing = False
        self.entries = []
        self._play_token = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label="&Download queue:")
        self.list = ResultsList(self)
        self.list.cell_provider = self._cell
        self.list.SetName("Download queue")
        self.list.SetHelpText(
            "Results kept for later. Enter downloads what you have selected, "
            "Delete removes it from the queue, and the buttons below play a "
            "clip or the whole track without downloading anything."
        )
        for column, heading in enumerate(COLUMN_HEADINGS):
            self.list.InsertColumn(column, heading)
        self.list.SetColumnWidth(0, 320)
        self.list.SetColumnWidth(1, 120)
        self.list.SetColumnWidth(2, 180)
        self.list.SetColumnWidth(3, 120)
        self.list.SetColumnWidth(4, 90)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_download_selected)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_menu)
        self.list.Bind(wx.EVT_KEY_DOWN, self.on_key)

        self.preview_btn = wx.Button(self, label="&Preview")
        self.preview_btn.Bind(wx.EVT_BUTTON, self.on_preview)
        self.play_full_btn = wx.Button(self, label="Play &full song")
        self.play_full_btn.Bind(wx.EVT_BUTTON, self.on_play_full)
        self.download_btn = wx.Button(self, label="&Download selected")
        self.download_btn.Bind(wx.EVT_BUTTON, self.on_download_selected)
        self.download_all_btn = wx.Button(self, label="Download &all")
        self.download_all_btn.Bind(wx.EVT_BUTTON, self.on_download_all)
        self.remove_btn = wx.Button(self, label="&Remove selected")
        self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove_selected)
        self.clear_btn = wx.Button(self, label="&Empty the queue")
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear)

        self.player = MediaPlayerPanel(self, frame, video_height=140)
        self.player.play_request = self.play_selection

        play_row = wx.BoxSizer(wx.HORIZONTAL)
        play_row.Add(self.preview_btn, 0, wx.RIGHT, 8)
        play_row.Add(self.play_full_btn, 0)
        action_row = wx.BoxSizer(wx.HORIZONTAL)
        action_row.Add(self.download_btn, 0, wx.RIGHT, 8)
        action_row.Add(self.download_all_btn, 0, wx.RIGHT, 8)
        action_row.Add(self.remove_btn, 0, wx.RIGHT, 8)
        action_row.Add(self.clear_btn, 0)

        sizer.Add(label, 0, wx.ALL, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(play_row, 0, wx.ALL, 8)
        sizer.Add(action_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)
        self.refresh(announce=False)

    # -- the list ------------------------------------------------------------

    def refresh(self, announce=True):
        """Re-read the store, keeping the reader where it was if it can."""
        if self.closing:
            return
        focused = self.list.GetFocusedItem()
        self.entries = self.store.all()
        self.list.SetItemCount(len(self.entries))
        self.list.Refresh()
        empty = not self.entries
        for button in (self.download_btn, self.download_all_btn,
                       self.remove_btn, self.clear_btn, self.preview_btn,
                       self.play_full_btn):
            button.Enable(not empty)
        if self.entries:
            index = min(max(focused, 0), len(self.entries) - 1)
            self.list.Focus(index)
            self.list.Select(index)
        if announce:
            self.frame.announce(self.count_text())

    def count_text(self):
        count = len(self.entries)
        if not count:
            return "The download queue is empty."
        noun = "item" if count == 1 else "items"
        return f"{count} {noun} in the download queue."

    def _cell(self, row, column):
        if not (0 <= row < len(self.entries)):
            return ""
        result = self.entries[row]["result"]
        if column == 0:
            return str(result.get("title") or "")
        if column == 1:
            return result_type(result)
        if column == 2:
            return str(result.get("artist") or result.get("uploader")
                       or result.get("author") or "")
        if column == 3:
            return str(result.get("source") or "")
        if column == 4:
            return ytdlp_backend.format_duration(
                result.get("duration_s", result.get("duration")))
        return ""

    def _selected_rows(self):
        rows = []
        row = self.list.GetFirstSelected()
        while row != -1:
            rows.append(row)
            row = self.list.GetNextSelected(row)
        return rows

    def _selected_entries(self):
        return [self.entries[row] for row in self._selected_rows()
                if row < len(self.entries)]

    def _focused_entry(self):
        row = self.list.GetFocusedItem()
        if 0 <= row < len(self.entries):
            return self.entries[row]
        return None

    # -- playing -------------------------------------------------------------

    def play_selection(self):
        if self.closing or not self.entries:
            return False
        self.on_play_full(None)
        return True

    def on_preview(self, event):
        self._play(full=False)

    def on_play_full(self, event):
        self._play(full=True)

    def _play(self, full):
        entry = self._focused_entry()
        if entry is None:
            self.frame.announce("The download queue is empty.")
            return
        try:
            result = self.store.result_of(entry)
        except Exception as exc:  # noqa: BLE001 - reported against this row
            self.frame.announce(f"Could not read that saved result: {exc}")
            return
        if is_collection_item(result):
            self.frame.announce(
                "An album or playlist has no single track to play. Download "
                "it to choose which of its tracks to keep."
            )
            return
        token = self._play_token = object()
        self.preview_btn.Disable()
        self.play_full_btn.Disable()
        title = result.get("title") or "track"
        self.frame.announce(
            f"Loading: {title}" if full else f"Preparing preview: {title}")
        threading.Thread(
            target=self._resolve_play,
            args=(token, result, full),
            daemon=True,
            name="blinddl-queue-play",
        ).start()

    def _resolve_play(self, token, result, full):
        resolve = (preview.resolve_full_playback if full
                   else preview.resolve_search_result)
        try:
            location, title = resolve(result, True, self.frame.config)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._play_failed, token, str(exc))
            return
        wx.CallAfter(self._play_ready, token, location, title)

    def _play_ready(self, token, location, title):
        if self.closing or token is not self._play_token:
            return
        self.preview_btn.Enable()
        self.play_full_btn.Enable()
        self.frame.play_media(self.player, location, title)

    def _play_failed(self, token, error):
        if self.closing or token is not self._play_token:
            return
        self.preview_btn.Enable()
        self.play_full_btn.Enable()
        self.frame.announce("Could not play that.")
        wx.MessageBox(f"Could not play that:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    # -- downloading ---------------------------------------------------------

    def on_download_selected(self, event):
        entries = self._selected_entries()
        if not entries:
            self.frame.announce("Select something in the queue first.")
            return
        self._download(entries)

    def on_download_all(self, event):
        if not self.entries:
            self.frame.announce("The download queue is empty.")
            return
        self._download(list(self.entries))

    def _download(self, entries):
        """Queue what can be queued now; resolve albums off the GUI thread.

        A row leaves the download queue once it has been handed to the
        transfer queue: it is now in the Downloads tab, and a row in both
        lists is a row that gets downloaded twice.
        """
        added = []
        titles = []
        collections = []
        problems = []
        done_keys = []
        with self.frame.queue.batch_additions():
            for entry in entries:
                try:
                    result = self.store.result_of(entry)
                except Exception as exc:  # noqa: BLE001 - one bad row only
                    problems.append(str(exc))
                    continue
                if is_collection_item(result):
                    collections.append((entry["key"], result))
                    continue
                try:
                    added.append(queue_result(
                        self.frame.queue, result, entry["engine"],
                        folder=entry["folder"]))
                except (KeyError, TypeError, ValueError) as exc:
                    problems.append(f"{result.get('title') or 'A row'}: {exc}")
                    continue
                titles.append(result.get("title") or "")
                done_keys.append(entry["key"])
        if done_keys:
            self.store.remove(done_keys)
        self.refresh(announce=False)
        message = addition_summary(added, titles) if added else ""
        if collections:
            noun = "album" if len(collections) == 1 else "albums"
            message = (message + " " if message else "") + (
                f"Reading {len(collections)} {noun}...")
            threading.Thread(
                target=self._resolve_collections,
                args=(list(collections),),
                daemon=True,
                name="blinddl-queue-collections",
            ).start()
        if problems:
            failed = "row" if len(problems) == 1 else "rows"
            message = (message + " " if message else "") + (
                f"{len(problems)} {failed} could not be queued.")
        self.frame.announce(message or "Nothing to download.")

    def _resolve_collections(self, collections):
        resolved = []
        errors = []
        for key, collection in collections:
            try:
                tracks = collection_tracks(collection, self.frame.config)
            except Exception as exc:  # noqa: BLE001 - reported to the user
                errors.append(f"{collection.get('title')}: {exc}")
                continue
            if tracks:
                resolved.append((key, collection, tracks))
            else:
                errors.append(f"{collection.get('title')}: no tracks listed")
        wx.CallAfter(self._collections_ready, resolved, errors)

    def _collections_ready(self, resolved, errors):
        if self.closing:
            return
        added = []
        titles = []
        with self.frame.queue.batch_additions():
            for _key, collection, tracks in resolved:
                batch, names = queue_collection_tracks(
                    self.frame.queue, collection, tracks)
                added.extend(batch)
                titles.extend(names)
        if resolved:
            self.store.remove([key for key, _c, _t in resolved])
            self.refresh(announce=False)
        message = addition_summary(added, titles) if added else ""
        if errors:
            failed = "item" if len(errors) == 1 else "items"
            message = (message + " " if message else "") + (
                f"{len(errors)} {failed} could not be read.")
        self.frame.announce(message or "Nothing could be queued.")

    # -- removing ------------------------------------------------------------

    def on_remove_selected(self, event):
        entries = self._selected_entries()
        if not entries:
            self.frame.announce("Select something in the queue first.")
            return
        removed = self.store.remove([entry["key"] for entry in entries])
        self.refresh(announce=False)
        noun = "item" if removed == 1 else "items"
        self.frame.announce(
            f"Removed {removed} {noun}. {self.count_text()}")

    def on_clear(self, event):
        if not self.entries:
            self.frame.announce("The download queue is empty.")
            return
        if wx.MessageBox(
            f"Remove all {len(self.entries)} items from the download queue?",
            "blindDL",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        removed = self.store.clear()
        self.refresh(announce=False)
        noun = "item" if removed == 1 else "items"
        self.frame.announce(f"Removed {removed} {noun}.")

    # -- keys and menu -------------------------------------------------------

    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_DELETE:
            self.on_remove_selected(None)
            return
        event.Skip()

    def on_menu(self, event):
        menu = wx.Menu()
        has_rows = bool(self.entries)
        has_selection = bool(self._selected_rows())
        entries = (
            ("&Preview", self.on_preview, has_rows),
            ("Play &full song", self.on_play_full, has_rows),
            (None, None, False),
            ("&Download selected", self.on_download_selected, has_selection),
            ("Download &all", self.on_download_all, has_rows),
            (None, None, False),
            ("&Remove selected\tDelete", self.on_remove_selected,
             has_selection),
            ("&Empty the queue", self.on_clear, has_rows),
        )
        for label, handler, enabled in entries:
            if label is None:
                menu.AppendSeparator()
                continue
            item = menu.Append(wx.ID_ANY, label)
            menu.Enable(item.GetId(), enabled)
            menu.Bind(wx.EVT_MENU, handler, item)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def shutdown(self):
        self.closing = True
        self._play_token = None
        self.player.shutdown()
