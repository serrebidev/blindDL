# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Unified Soulseek uploads and BitTorrent seeding view.

Running transfers and finished ones are kept in separate lists, for the
same reason the Downloads tab does it: what is still being sent is the
question the tab is opened to answer, and a list that has to be arrowed
past every completed transfer to reach it answers something else.
"""

import threading
import os

import wx

from .. import soulseek_backend, torrent_engine
from ..runtime import open_folder


def _size(value):
    amount = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return ""


COLUMNS = ("Title", "Service", "Peer", "Status", "Progress", "Speed", "Ratio")


class UploadsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._rows = []
        self._finished_rows = []
        self._signature = None
        self._alive = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        active_label = wx.StaticText(self, label="&Uploads:")
        self.list = self._make_list(
            "Uploads and torrent seeding",
            "Shows files other people are downloading from your Soulseek "
            "shares and torrents you seed. Select one or more items; Context "
            "Menu opens actions. Delete removes the selection; Shift Delete "
            "deletes its data."
        )
        finished_label = wx.StaticText(self, label="&Finished uploads:")
        self.finished_list = self._make_list(
            "Finished uploads",
            "Uploads that completed, failed or were stopped. Select one or "
            "more items; Context Menu opens actions. Delete removes the "
            "selection; Shift Delete deletes its data."
        )
        sizer.Add(active_label, 0, wx.LEFT | wx.TOP, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(finished_label, 0, wx.LEFT, 8)
        sizer.Add(self.finished_list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        # Torrent status has no GUI event source. A low-frequency timer only
        # checks while this page is visible; Soulseek pushes updates directly.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.timer.Start(2000)
        self.refresh()

    def _make_list(self, name, help_text):
        control = wx.ListCtrl(self, style=wx.LC_REPORT)
        control.SetName(name)
        control.SetHelpText(help_text)
        for index, heading in enumerate(COLUMNS):
            control.InsertColumn(index, heading)
        control.SetColumnWidth(0, 300)
        control.SetColumnWidth(1, 100)
        control.SetColumnWidth(2, 150)
        control.SetColumnWidth(3, 120)
        control.Bind(wx.EVT_CONTEXT_MENU, self.on_menu)
        control.Bind(wx.EVT_KEY_DOWN, self.on_list_key)
        return control

    def shutdown(self):
        self._alive = False
        self.timer.Stop()

    def handle_soulseek_event(self, event):
        if (
            self._alive
            and self.IsShownOnScreen()
            and event.get("type") == "uploads"
        ):
            self.refresh(event.get("uploads", []))

    def on_timer(self, event=None):
        if self._alive and self.IsShownOnScreen():
            self.refresh()

    def refresh(self, soulseek_rows=None):
        soulseek_rows = (
            soulseek_backend.uploads_snapshot()
            if soulseek_rows is None
            else soulseek_rows
        )
        rows = list(soulseek_rows) + list(torrent_engine.uploads())
        rows.sort(
            key=lambda row: (
                not row.get("active", False),
                str(row.get("service", "")).casefold(),
                str(row.get("title", "")).casefold(),
            )
        )
        active_rows = [row for row in rows if row.get("active")]
        # A finished upload is only a record that it happened, so the
        # automatic clear-out in Settings simply stops listing them. Nothing
        # is lost: the transfer itself is over either way.
        finished_rows = [] if self._auto_clear() else [
            row for row in rows if not row.get("active")
        ]
        signature = tuple(
            (
                row.get("service"),
                row.get("key"),
                row.get("status"),
                round(float(row.get("percent") or 0), 1),
                int(row.get("speed") or 0),
                round(float(row.get("ratio") or 0), 2),
            )
            for row in active_rows + finished_rows
        )
        if signature == self._signature:
            return
        self._signature = signature
        self._rows = active_rows
        self._finished_rows = finished_rows
        self._fill(self.list, active_rows)
        self._fill(self.finished_list, finished_rows)

    def _auto_clear(self):
        try:
            return bool(self.frame.config["auto_clear_finished"])
        except (KeyError, TypeError):
            return False

    def _fill(self, control, uploads):
        control.Freeze()
        try:
            control.DeleteAllItems()
            for upload in uploads:
                row = control.InsertItem(
                    control.GetItemCount(), upload.get("title", "")
                )
                control.SetItem(row, 1, upload.get("service", ""))
                control.SetItem(row, 2, upload.get("peer", ""))
                control.SetItem(row, 3, upload.get("status", ""))
                percent = upload.get("percent")
                control.SetItem(
                    row,
                    4,
                    f"{float(percent):.0f}%" if percent is not None else "",
                )
                speed = float(upload.get("speed") or 0)
                control.SetItem(row, 5, f"{_size(speed)}/s" if speed else "")
                ratio = upload.get("ratio")
                control.SetItem(
                    row, 6, f"{float(ratio):.2f}" if ratio is not None else ""
                )
        finally:
            control.Thaw()

    def _selected(self, control=None):
        control = control if control is not None else self.list
        source = (self._finished_rows if control is self.finished_list
                  else self._rows)
        selected = []
        row = control.GetFirstSelected()
        while row != -1:
            if row < len(source):
                selected.append(source[row])
            row = control.GetNextSelected(row)
        return selected

    def on_menu(self, event):
        control = (self.finished_list if event.GetEventObject() is
                   self.finished_list else self.list)
        self._target_context_item(event, control)
        selected = self._selected(control)
        menu = wx.Menu()
        resume = menu.Append(wx.ID_ANY, "&Start or resume selected uploads")
        pause = menu.Append(wx.ID_ANY, "&Pause selected uploads")
        stop = menu.Append(wx.ID_ANY, "&Stop selected uploads")
        menu.AppendSeparator()
        show = menu.Append(wx.ID_ANY, "Show data in &folder")
        remove = menu.Append(wx.ID_ANY, "&Remove from list")
        delete_data = menu.Append(wx.ID_ANY, "&Delete with data...")
        clear = menu.Append(wx.ID_ANY, "Clear &finished uploads")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear_selection = menu.Append(wx.ID_ANY, "Clear &selection")
        resume.Enable(any(row.get("paused") for row in selected))
        pause.Enable(any(row.get("active") and not row.get("paused")
                         for row in selected))
        stop.Enable(any(row.get("active") for row in selected))
        show.Enable(len(selected) == 1 and bool(selected[0].get("path")))
        remove.Enable(bool(selected))
        delete_data.Enable(bool(selected) and all(
            row.get("service") == "BitTorrent" or row.get("path")
            for row in selected
        ))
        clear.Enable(bool(self._finished_rows))
        select_all.Enable(
            control.GetSelectedItemCount() < control.GetItemCount())
        clear_selection.Enable(bool(selected))
        menu.Bind(wx.EVT_MENU, lambda e: self.on_resume(e, control), resume)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_pause(e, control), pause)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_stop(e, control), stop)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_show_folder(e, control), show)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_remove(e, control), remove)
        menu.Bind(wx.EVT_MENU, lambda e: self.on_delete_data(e, control),
                  delete_data)
        menu.Bind(wx.EVT_MENU, self.on_clear_finished, clear)
        menu.Bind(wx.EVT_MENU, lambda e: self._select_all(control), select_all)
        menu.Bind(wx.EVT_MENU, lambda e: self._clear_selection(control),
                  clear_selection)
        control.PopupMenu(menu)
        menu.Destroy()

    def _selected_rows(self, control):
        rows = []
        row = control.GetFirstSelected()
        while row != -1:
            rows.append(row)
            row = control.GetNextSelected(row)
        return rows

    def _target_context_item(self, event, control):
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

    def _select_all(self, control):
        for row in range(control.GetItemCount()):
            control.Select(row)
        count = control.GetSelectedItemCount()
        self.frame.announce(f"Selected {count} upload{'s' if count != 1 else ''}.")

    def _clear_selection(self, control):
        for row in self._selected_rows(control):
            control.Select(row, False)
        self.frame.announce("Selection cleared.")

    def _run_action(self, rows, action, message):
        def work():
            succeeded = 0
            errors = []
            for row in rows:
                try:
                    succeeded += bool(action(row))
                except Exception as exc:  # noqa: BLE001 - reported in the GUI
                    errors.append(str(exc))
            def finish():
                self._signature = None
                self.refresh()
                suffix = f" First error: {errors[0]}" if errors else ""
                self.frame.announce(message(succeeded) + suffix)
            wx.CallAfter(finish)
        threading.Thread(target=work, daemon=True,
                         name="blinddl-upload-action").start()

    @staticmethod
    def _service_action(row, torrent_action, soulseek_action):
        if row.get("service") == "BitTorrent":
            return torrent_action(row.get("key", ""))
        if row.get("service") == soulseek_backend.SOURCE:
            return soulseek_action(row.get("key", ""))
        return False

    def on_pause(self, event=None, control=None):
        rows = [row for row in self._selected(control)
                if row.get("active") and not row.get("paused")]
        self._run_action(
            rows,
            lambda row: self._service_action(
                row, torrent_engine.pause_seeding, soulseek_backend.pause_upload),
            lambda count: f"Paused {count} upload{'s' if count != 1 else ''}.",
        )

    def on_resume(self, event=None, control=None):
        rows = [row for row in self._selected(control) if row.get("paused")]
        self._run_action(
            rows,
            lambda row: self._service_action(
                row, torrent_engine.resume_seeding,
                soulseek_backend.resume_upload),
            lambda count: f"Resumed {count} upload{'s' if count != 1 else ''}.",
        )

    def on_show_folder(self, event=None, control=None):
        rows = self._selected(control)
        if len(rows) == 1 and rows[0].get("path"):
            path = os.path.abspath(rows[0]["path"])
            open_folder(path if os.path.isdir(path) else os.path.dirname(path))

    def on_remove(self, event=None, control=None):
        rows = self._selected(control)
        if not rows:
            self.frame.announce("Select an upload to remove.")
            return
        self._run_action(
            rows, self._remove_row,
            lambda count: f"Removed {count} upload{'s' if count != 1 else ''}.",
        )

    def _remove_row(self, row, delete_data=False):
        if row.get("service") == "BitTorrent":
            key = row.get("key", "")
            removed = (torrent_engine.delete_seed(key) if delete_data
                       else torrent_engine.stop_seeding(key))
            if removed:
                self.frame.queue.mark_torrent_stopped(key, row.get("title", ""))
            return removed
        if row.get("service") == soulseek_backend.SOURCE:
            return soulseek_backend.remove_upload(
                row.get("key", ""), delete_data=delete_data)
        return False

    def on_delete_data(self, event=None, control=None):
        selected = self._selected(control)
        if not selected:
            self.frame.announce("Select an upload first.")
            return
        # Deleting a Soulseek upload's data removes the shared source file,
        # which needs a local path; BitTorrent data lives with the seed.
        rows = [row for row in selected
                if row.get("service") == "BitTorrent" or row.get("path")]
        if not rows:
            self.frame.announce("No selected uploads have known data to delete.")
            return
        answer = wx.MessageBox(
            f"Permanently delete the data for {len(rows)} selected "
            f"upload{'s' if len(rows) != 1 else ''}? For Soulseek, this "
            "deletes the shared source file.",
            "Delete upload data", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self,
        )
        if answer != wx.YES:
            return
        self._run_action(
            rows, lambda row: self._remove_row(row, delete_data=True),
            lambda count: f"Deleted data for {count} upload{'s' if count != 1 else ''}.",
        )

    def on_list_key(self, event):
        """Delete removes the selection; Shift Delete deletes its data."""
        if event.GetKeyCode() not in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            event.Skip()
            return
        control = event.GetEventObject()
        if control is not self.list and control is not self.finished_list:
            control = self.list
        if event.ShiftDown():
            self.on_delete_data(None, control)
        else:
            self.on_remove(None, control)

    def on_clear_finished(self, event=None):
        rows = list(self._finished_rows)
        self._run_action(
            rows, self._remove_row,
            lambda count: f"Cleared {count} finished upload{'s' if count != 1 else ''}.",
        )

    def on_stop(self, event=None, control=None):
        rows = [row for row in self._selected(control) if row.get("active")]
        if not rows:
            self.frame.announce("No active uploads are selected.")
            return
        stopped = 0
        for row in rows:
            if row.get("service") == "BitTorrent":
                key = row.get("key", "")
                did_stop = torrent_engine.stop_seeding(key)
                if did_stop:
                    self.frame.queue.mark_torrent_stopped(
                        key, row.get("title", "")
                    )
                stopped += bool(did_stop)
            elif row.get("service") == soulseek_backend.SOURCE:
                threading.Thread(
                    target=self._stop_soulseek,
                    args=(row.get("key", ""),),
                    daemon=True,
                    name="blinddl-stop-soulseek-upload",
                ).start()
                stopped += 1
        self.frame.announce(f"Stopping {stopped} upload{'s' if stopped != 1 else ''}.")
        self.refresh()

    def _stop_soulseek(self, key):
        try:
            soulseek_backend.stop_upload(key)
        except Exception as exc:  # noqa: BLE001 - shown on status bar
            wx.CallAfter(self.frame.announce, f"Could not stop Soulseek upload: {exc}")
        finally:
            wx.CallAfter(self.refresh)
