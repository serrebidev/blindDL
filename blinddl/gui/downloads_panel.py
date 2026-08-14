# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Downloads tab: queued and running downloads, and a finished section.

The two are separate lists rather than one list sorted by status. A queue
that mixes them makes the one question the tab is opened to answer -- what
is still going -- into a search through everything that ever finished, and
arrow keys are the only way through a list. Here the running downloads are
the whole of the first list, and the finished ones are out of the way in
the second until they are wanted.
"""

import os

import wx

from .. import torrent_engine
from ..downloader import (
    ACTIVE_STATUSES,
    FINISHED_STATUSES,
    PAUSABLE_KINDS,
    STATUS_DOWNLOADING,
    STATUS_PAUSED,
    STATUS_QUEUED,
)
from ..runtime import open_file, open_folder

COLUMNS = ("Title", "Status", "Progress", "Speed", "ETA", "Error")


class DownloadsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._rows = {}  # item.id -> (list control, row index)
        self._values = {}  # item.id -> last values written to that row

        sizer = wx.BoxSizer(wx.VERTICAL)

        active_label = wx.StaticText(self, label="&Downloads:")
        self.list = self._make_list(
            "Downloads",
            "Queued and running downloads. Select one or more items; Context "
            "Menu opens actions.")
        finished_label = wx.StaticText(self, label="&Finished downloads:")
        self.finished_list = self._make_list(
            "Finished downloads",
            "Downloads that finished, failed or were cancelled. Select one "
            "or more items; Context Menu opens actions.")

        sizer.Add(active_label, 0, wx.LEFT | wx.TOP, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(finished_label, 0, wx.LEFT, 8)
        sizer.Add(self.finished_list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def _make_list(self, name, help_text):
        control = wx.ListCtrl(self, style=wx.LC_REPORT)
        control.SetName(name)
        control.SetHelpText(help_text)
        for index, heading in enumerate(COLUMNS):
            control.InsertColumn(index, heading)
        control.SetColumnWidth(0, 300)
        control.Bind(wx.EVT_CONTEXT_MENU, self.on_downloads_menu)
        return control

    # -- updates from the queue (main thread) ------------------------------

    def update_item(self, item):
        target = (self.finished_list if item.status in FINISHED_STATUSES
                  else self.list)
        known = self._rows.get(item.id)
        if known is not None and known[0] is not target:
            # The download just crossed into the finished section. Taking a
            # row out of the middle of a list renumbers everything under it,
            # so both lists are rebuilt -- a download crosses this line once.
            self.refresh_all()
            return
        if known is None:
            row = target.GetItemCount()
            target.InsertItem(row, item.title)
            self._rows[item.id] = (target, row)
        else:
            row = known[1]
        if (item.status == STATUS_DOWNLOADING and
                item.kind in ("musicdl", "adult", "soulseek") and
                not item.percent):
            progress = "in progress"
        elif item.percent:
            progress = f"{item.percent:.0f}%"
        else:
            progress = ""
        values = (
            item.title,
            item.status,
            progress,
            item.speed,
            item.eta,
            item.error,
        )
        previous = self._values.get(item.id)
        # InsertItem already wrote a new row's title.
        start = 1 if previous is None else 0
        for column in range(start, len(values)):
            if previous is None or previous[column] != values[column]:
                target.SetItem(row, column, values[column])
        self._values[item.id] = values

    def refresh_all(self):
        self.list.DeleteAllItems()
        self.finished_list.DeleteAllItems()
        self._rows.clear()
        self._values.clear()
        for item in self.frame.queue.items:
            self.update_item(item)

    # -- actions -------------------------------------------------------------

    def _event_list(self, event):
        """The list a context menu was asked for, defaulting to the top one."""
        control = event.GetEventObject()
        if control is self.finished_list:
            return self.finished_list
        return self.list

    def _selected_rows(self, control):
        rows = []
        row = control.GetFirstSelected()
        while row != -1:
            rows.append(row)
            row = control.GetNextSelected(row)
        return rows

    def _selected_items(self, control):
        selected_rows = set(self._selected_rows(control))
        item_ids = [item_id for item_id, (owner, row) in self._rows.items()
                    if owner is control and row in selected_rows]
        return [item for item_id in item_ids
                if (item := self.frame.queue._find(item_id)) is not None]

    def _target_context_item(self, event, control):
        """Make a right-clicked row the target while preserving a group click."""
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            if not self._selected_rows(control):
                focused = control.GetFocusedItem()
                if focused >= 0:
                    control.Select(focused)
            return
        row, _flags = control.HitTest(control.ScreenToClient(position))
        if row < 0 or control.IsSelected(row):
            return
        for selected in self._selected_rows(control):
            control.Select(selected, False)
        control.Focus(row)
        control.Select(row)

    def on_downloads_menu(self, event):
        control = self._event_list(event)
        self._target_context_item(event, control)
        selected = self._selected_items(control)
        menu = wx.Menu()
        start = menu.Append(wx.ID_ANY, "&Start or resume selected")
        pause = menu.Append(wx.ID_ANY, "&Pause selected")
        cancel = menu.Append(wx.ID_ANY, "&Cancel selected")
        restart = menu.Append(wx.ID_ANY, "&Restart selected")
        stop_seeding = menu.Append(wx.ID_ANY, "Stop &seeding")
        menu.AppendSeparator()
        open_result = menu.Append(wx.ID_ANY, "&Open downloaded item")
        show_result = menu.Append(wx.ID_ANY, "Show in &folder")
        menu.AppendSeparator()
        remove = menu.Append(wx.ID_ANY, "&Remove from list")
        delete_data = menu.Append(wx.ID_ANY, "&Delete with data...")
        clear_finished = menu.Append(wx.ID_ANY, "Clear &finished")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear_selection = menu.Append(wx.ID_ANY, "Clear &selection")
        start.Enable(any(item.status == STATUS_PAUSED for item in selected))
        pause.Enable(any(
            item.status == STATUS_QUEUED or (
                item.status == STATUS_DOWNLOADING and item.kind in PAUSABLE_KINDS
            ) for item in selected
        ))
        cancel.Enable(any(item.status in ACTIVE_STATUSES for item in selected))
        restart.Enable(any(
            item.status in FINISHED_STATUSES and not item.seeding
            for item in selected
        ))
        stop_seeding.Enable(bool(self._seeding_keys(selected)))
        paths = [self._result_path(item) for item in selected]
        open_result.Enable(len(selected) == 1 and bool(paths[0])
                           if selected else False)
        show_result.Enable(len(selected) == 1 and bool(paths[0])
                           if selected else False)
        removable = [item for item in selected
                     if item.status != STATUS_DOWNLOADING and not item.seeding]
        remove.Enable(bool(removable))
        delete_data.Enable(bool(removable) and all(
            self.frame.queue.can_delete_data(item) for item in removable
        ))
        clear_finished.Enable(any(
            item.status in FINISHED_STATUSES and not item.seeding
            for item in self.frame.queue.items))
        clear_selection.Enable(bool(selected))
        select_all.Enable(
            control.GetSelectedItemCount() < control.GetItemCount())
        menu.Bind(wx.EVT_MENU, lambda e: self.on_resume(e, control), start)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_pause(e, control), pause)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_cancel(e, control), cancel)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_restart(e, control), restart)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_stop_seeding(e, control),
                  stop_seeding)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_open(e, control), open_result)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_show_folder(e, control),
                  show_result)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_remove(e, control), remove)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_delete_data(e, control),
                  delete_data)
        menu.Bind(wx.EVT_MENU, self.on_clear, clear_finished)
        menu.Bind(wx.EVT_MENU, lambda e: self._select_all(e, control),
                  select_all)
        menu.Bind(wx.EVT_MENU, lambda e: self._clear_selection(e, control),
                  clear_selection)
        control.PopupMenu(menu)
        menu.Destroy()

    @staticmethod
    def _result_path(item):
        path = str(getattr(item, "result_path", "") or "")
        return path if path and os.path.exists(path) else ""

    def on_resume(self, event, control=None):
        control = control if control is not None else self.list
        items = [item for item in self._selected_items(control)
                 if item.status == STATUS_PAUSED]
        count = sum(bool(self.frame.queue.resume(item.id)) for item in items)
        self.frame.announce(f"Resumed {count} download{'s' if count != 1 else ''}.")

    def on_pause(self, event, control=None):
        control = control if control is not None else self.list
        items = self._selected_items(control)
        count = sum(bool(self.frame.queue.pause(item.id)) for item in items)
        self.frame.announce(f"Pausing {count} download{'s' if count != 1 else ''}.")

    def on_restart(self, event, control=None):
        control = control if control is not None else self.finished_list
        items = self._selected_items(control)
        count = sum(bool(self.frame.queue.retry(item.id)) for item in items)
        self.frame.announce(f"Restarted {count} download{'s' if count != 1 else ''}.")

    def on_open(self, event, control=None):
        items = self._selected_items(control or self.finished_list)
        if len(items) == 1 and (path := self._result_path(items[0])):
            open_file(path)

    def on_show_folder(self, event, control=None):
        items = self._selected_items(control or self.finished_list)
        if len(items) == 1 and (path := self._result_path(items[0])):
            open_folder(path if os.path.isdir(path) else os.path.dirname(path))

    def on_remove(self, event, control=None):
        control = control if control is not None else self.finished_list
        items = self._selected_items(control)
        count = sum(bool(self.frame.queue.remove(item.id)) for item in items)
        self.refresh_all()
        self.frame.announce(f"Removed {count} download{'s' if count != 1 else ''}.")

    def on_delete_data(self, event, control=None):
        control = control if control is not None else self.finished_list
        items = [item for item in self._selected_items(control)
                 if self.frame.queue.can_delete_data(item)]
        if not items:
            self.frame.announce("No selected downloads have known data to delete.")
            return
        answer = wx.MessageBox(
            f"Permanently delete the downloaded data for {len(items)} selected "
            f"download{'s' if len(items) != 1 else ''}?",
            "Delete download data", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self,
        )
        if answer != wx.YES:
            return
        count = sum(bool(self.frame.queue.remove(item.id, delete_data=True))
                    for item in items)
        self.refresh_all()
        self.frame.announce(
            f"Deleted data for {count} download{'s' if count != 1 else ''}.")

    def _select_all(self, event, control=None):
        control = control if control is not None else self.list
        for row in range(control.GetItemCount()):
            control.Select(row)
        count = control.GetSelectedItemCount()
        noun = "download" if count == 1 else "downloads"
        self.frame.announce(f"Selected {count} {noun}.")

    def _clear_selection(self, event, control=None):
        control = control if control is not None else self.list
        for row in self._selected_rows(control):
            control.Select(row, False)
        self.frame.announce("Selection cleared.")

    def on_cancel(self, event, control=None):
        control = control if control is not None else self.list
        items = [item for item in self._selected_items(control)
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

    def _seeding_keys(self, items):
        """Info hashes of the selected torrents that are still seeding.

        A torrent's row says Done the moment its files are complete, but the
        engine keeps sharing it until the ratio or time limit in Settings is
        reached. This is how that is called off early.
        """
        active = {key for key, _title, _ratio, _rate in torrent_engine.seeding()}
        keys = []
        for item in items:
            if item.kind != "torrent" or not isinstance(item.payload, dict):
                continue
            key = str(item.payload.get("infohash") or "").lower()
            if key in active:
                keys.append((key, item.title))
        return keys

    def on_stop_seeding(self, event, control=None):
        control = control if control is not None else self.list
        stopping = self._seeding_keys(self._selected_items(control))
        if not stopping:
            self.frame.announce("None of the selected downloads are seeding.")
            return
        stopped = []
        for key, title in stopping:
            if torrent_engine.stop_seeding(key):
                self.frame.queue.mark_torrent_stopped(key, title)
                stopped.append(title)
        if len(stopped) == 1:
            self.frame.announce(f"Stopped seeding: {stopped[0]}")
        else:
            self.frame.announce(f"Stopped seeding {len(stopped)} torrents.")

    def on_clear(self, event):
        count = sum(item.status in FINISHED_STATUSES and not item.seeding
                    for item in self.frame.queue.items)
        self.frame.queue.remove_finished()
        self.refresh_all()
        self.frame.announce(f"Cleared {count} finished downloads.")
