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

import wx

from .. import soulseek_backend, torrent_engine


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
            "Shows files other people are downloading from your Soulseek shares and torrents you seed. Context Menu stops selected uploads."
        )
        finished_label = wx.StaticText(self, label="&Finished uploads:")
        self.finished_list = self._make_list(
            "Finished uploads",
            "Uploads that completed, failed or were stopped."
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
        menu = wx.Menu()
        stop = menu.Append(wx.ID_ANY, "&Stop selected uploads")
        stop.Enable(any(row.get("active") for row in self._selected(control)))
        menu.Bind(wx.EVT_MENU, lambda e: self.on_stop(e, control), stop)
        control.PopupMenu(menu)
        menu.Destroy()

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
