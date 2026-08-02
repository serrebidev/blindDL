# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Downloads tab: live list of queued/active/finished downloads."""

import wx

from ..downloader import ACTIVE_STATUSES, FINISHED_STATUSES, STATUS_DOWNLOADING


class DownloadsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._rows = {}  # item.id -> row index

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Downloads")
        self.list.SetHelpText(
            "Select downloads. Context Menu opens actions.")
        for i, heading in enumerate(("Title", "Status", "Progress", "Speed",
                                     "ETA", "Error")):
            self.list.InsertColumn(i, heading)
        self.list.SetColumnWidth(0, 300)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_downloads_menu)

        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    # -- updates from the queue (main thread) ------------------------------

    def update_item(self, item):
        row = self._rows.get(item.id)
        if row is None:
            row = self.list.GetItemCount()
            self.list.InsertItem(row, item.title)
            self._rows[item.id] = row
        self.list.SetItem(row, 0, item.title)
        self.list.SetItem(row, 1, item.status)
        if (item.status == STATUS_DOWNLOADING and
                item.kind in ("musicdl", "adult") and not item.percent):
            self.list.SetItem(row, 2, "in progress")
        elif item.percent:
            self.list.SetItem(row, 2, f"{item.percent:.0f}%")
        else:
            self.list.SetItem(row, 2, "")
        self.list.SetItem(row, 3, item.speed)
        self.list.SetItem(row, 4, item.eta)
        self.list.SetItem(row, 5, item.error)

    def refresh_all(self):
        self.list.DeleteAllItems()
        self._rows.clear()
        for item in self.frame.queue.items:
            self.update_item(item)

    # -- actions -------------------------------------------------------------

    def _selected_rows(self):
        rows = []
        row = self.list.GetFirstSelected()
        while row != -1:
            rows.append(row)
            row = self.list.GetNextSelected(row)
        return rows

    def _selected_items(self):
        selected_rows = set(self._selected_rows())
        item_ids = [item_id for item_id, row in self._rows.items()
                    if row in selected_rows]
        return [item for item_id in item_ids
                if (item := self.frame.queue._find(item_id)) is not None]

    def _target_context_item(self, event):
        """Make a right-clicked row the target while preserving a group click."""
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            if not self._selected_rows():
                focused = self.list.GetFocusedItem()
                if focused >= 0:
                    self.list.Select(focused)
            return
        row, _flags = self.list.HitTest(self.list.ScreenToClient(position))
        if row < 0 or self.list.IsSelected(row):
            return
        for selected in self._selected_rows():
            self.list.Select(selected, False)
        self.list.Focus(row)
        self.list.Select(row)

    def on_downloads_menu(self, event):
        self._target_context_item(event)
        selected = self._selected_items()
        menu = wx.Menu()
        cancel = menu.Append(wx.ID_ANY, "&Cancel selected")
        clear_finished = menu.Append(wx.ID_ANY, "Clear &finished")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear_selection = menu.Append(wx.ID_ANY, "Clear &selection")
        cancel.Enable(any(item.status in ACTIVE_STATUSES for item in selected))
        clear_finished.Enable(any(
            item.status in FINISHED_STATUSES for item in self.frame.queue.items))
        clear_selection.Enable(bool(selected))
        select_all.Enable(
            self.list.GetSelectedItemCount() < self.list.GetItemCount())
        menu.Bind(wx.EVT_MENU, self.on_cancel, cancel)
        menu.Bind(wx.EVT_MENU, self.on_clear, clear_finished)
        menu.Bind(wx.EVT_MENU, self._select_all, select_all)
        menu.Bind(wx.EVT_MENU, self._clear_selection, clear_selection)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def _select_all(self, event):
        for row in range(self.list.GetItemCount()):
            self.list.Select(row)
        count = self.list.GetSelectedItemCount()
        noun = "download" if count == 1 else "downloads"
        self.frame.announce(f"Selected {count} {noun}.")

    def _clear_selection(self, event):
        for row in self._selected_rows():
            self.list.Select(row, False)
        self.frame.announce("Selection cleared.")

    def on_cancel(self, event):
        items = [item for item in self._selected_items()
                 if item.status in ACTIVE_STATUSES]
        if not items:
            self.frame.announce("No selected downloads can be cancelled.")
            return
        for item in items:
            self.frame.queue.cancel(item.id)
        if len(items) == 1:
            self.frame.announce(f"Cancelled: {items[0].title}")
        else:
            self.frame.announce(f"Cancelled {len(items)} downloads.")

    def on_clear(self, event):
        count = sum(item.status in FINISHED_STATUSES
                    for item in self.frame.queue.items)
        self.frame.queue.remove_finished()
        self.refresh_all()
        self.frame.announce(f"Cleared {count} finished downloads.")
