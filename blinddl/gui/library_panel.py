# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Library tab: discover and play completed audio, video and book downloads."""

from __future__ import annotations

import os
from pathlib import Path
import threading

import wx

from ..book_backend import BOOK_SUBFOLDER
from ..runtime import open_file, open_folder
from .media_player import MediaPlayerPanel

AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogv",
    ".ts",
    ".webm",
    ".wmv",
}
BOOK_EXTENSIONS = {
    ".azw3",
    ".djvu",
    ".epub",
    ".fb2",
    ".mobi",
    ".pdf",
    ".txt",
}
# A .txt is a book only inside the folder books are downloaded to; anywhere
# else it is far more likely to be the user's own notes.
BOOK_FOLDER_ONLY_EXTENSIONS = {".txt"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | BOOK_EXTENSIONS
KIND_BOOK = "Book"


def _kind_for(extension):
    if extension in AUDIO_EXTENSIONS:
        return "Audio"
    if extension in VIDEO_EXTENSIONS:
        return "Video"
    return KIND_BOOK


def discover_media(root):
    """Return media and book file records below *root*, by relative path."""
    base = Path(root)
    if not base.is_dir():
        return []
    records = []
    for folder, _directories, filenames in os.walk(base):
        for filename in filenames:
            path = Path(folder) / filename
            extension = path.suffix.lower()
            if extension not in MEDIA_EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
                relative_parent = path.parent.relative_to(base)
            except OSError:
                continue
            if (extension in BOOK_FOLDER_ONLY_EXTENSIONS and
                    relative_parent.parts[:1] != (BOOK_SUBFOLDER,)):
                continue
            records.append(
                {
                    "path": str(path),
                    "title": path.stem,
                    "kind": _kind_for(extension),
                    "folder": ""
                    if str(relative_parent) == "."
                    else str(relative_parent),
                    "size": size,
                }
            )
    return sorted(
        records,
        key=lambda item: os.path.relpath(item["path"], base).casefold(),
    )


def _format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


class LibraryPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.items = []
        self._alive = True
        self._refreshing = False
        self._pending_refresh = None
        self._announce_refresh = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.SetName("Downloaded media library")
        self.list.SetHelpText(
            "Choose a download and press Enter to play it. Books open in your "
            "usual reader. Context Menu opens actions."
        )
        for index, heading in enumerate(("Title", "Type", "Folder", "Size")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 340)
        self.list.SetColumnWidth(1, 80)
        self.list.SetColumnWidth(2, 250)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_play_selected)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)

        self.play_btn = wx.Button(self, label="&Play or open selected")
        self.refresh_btn = wx.Button(self, label="&Refresh library")
        self.play_btn.Bind(wx.EVT_BUTTON, self.on_play_selected)
        self.refresh_btn.Bind(wx.EVT_BUTTON, self.on_refresh)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.refresh_btn, 0)

        self.player = MediaPlayerPanel(self, frame, video_height=220)

        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)
        self.refresh(announce=False)

    def refresh(self, announce=True):
        if not self._alive:
            return
        selected_path = None
        selected = self.list.GetFirstSelected()
        if 0 <= selected < len(self.items):
            selected_path = self.items[selected]["path"]
        self._announce_refresh = self._announce_refresh or announce
        # Keep only the newest request while a walk is running. A burst of
        # completed downloads therefore causes one follow-up scan, not one
        # recursive walk per file.
        self._pending_refresh = (
            self.frame.config["download_dir"],
            selected_path,
        )
        self._start_refresh()

    def _start_refresh(self):
        if not self._alive or self._refreshing or self._pending_refresh is None:
            return
        root, selected_path = self._pending_refresh
        self._pending_refresh = None
        self._refreshing = True
        threading.Thread(
            target=self._discover,
            args=(root, selected_path),
            daemon=True,
            name="blinddl-library-scan",
        ).start()

    def _discover(self, root, selected_path):
        items = discover_media(root)
        wx.CallAfter(self._refresh_finished, items, selected_path)

    def _refresh_finished(self, items, selected_path):
        if not self._alive:
            return
        self._refreshing = False
        if self._pending_refresh is not None:
            self._start_refresh()
            return
        announce = self._announce_refresh
        self._announce_refresh = False
        self.items = items
        self.list.Freeze()
        selected_row = -1
        try:
            self.list.DeleteAllItems()
            for row, item in enumerate(self.items):
                self.list.InsertItem(row, item["title"])
                self.list.SetItem(row, 1, item["kind"])
                self.list.SetItem(row, 2, item["folder"])
                self.list.SetItem(row, 3, _format_size(item["size"]))
                if item["path"] == selected_path:
                    selected_row = row
        finally:
            self.list.Thaw()
        if selected_row >= 0:
            self.list.Select(selected_row)
            self.list.Focus(selected_row)
        if announce:
            count = len(self.items)
            noun = "file" if count == 1 else "files"
            self.frame.announce(f"Library refreshed: {count} {noun}.")

    def on_refresh(self, event):
        self.refresh()

    def _selected_item(self):
        row = self.list.GetFirstSelected()
        return self.items[row] if 0 <= row < len(self.items) else None

    def on_play_selected(self, event):
        item = self._selected_item()
        if item is None:
            self.frame.announce("Select a library item to play first.")
            return
        if not os.path.isfile(item["path"]):
            self.frame.announce("That file no longer exists. Refreshing library.")
            self.refresh(announce=False)
            return
        if item["kind"] == KIND_BOOK:
            # blindDL has no reader of its own; the book goes to whichever
            # application the user already reads books in.
            self.frame.announce(f"Opening {item['title']} in your reader.")
            open_file(item["path"])
            return
        self.frame.play_media(self.player, item["path"], item["title"])

    def on_context_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            row, _flags = self.list.HitTest(self.list.ScreenToClient(position))
            if row >= 0:
                self.list.Select(row)
                self.list.Focus(row)
        menu = wx.Menu()
        selected = self._selected_item()
        play = menu.Append(
            wx.ID_ANY,
            "&Open" if (selected is not None
                        and selected["kind"] == KIND_BOOK) else "&Play")
        open_location = menu.Append(wx.ID_ANY, "Open file &location")
        refresh_item = menu.Append(wx.ID_ANY, "&Refresh library")
        play.Enable(selected is not None)
        open_location.Enable(selected is not None)
        menu.Bind(wx.EVT_MENU, self.on_play_selected, play)
        menu.Bind(wx.EVT_MENU, self._on_open_location, open_location)
        menu.Bind(wx.EVT_MENU, self.on_refresh, refresh_item)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def _on_open_location(self, event):
        item = self._selected_item()
        if item is not None:
            open_folder(os.path.dirname(item["path"]))

    def shutdown(self):
        self._alive = False
        self._pending_refresh = None
        self.player.shutdown()
