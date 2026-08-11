# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Unified Soulseek uploads and BitTorrent seeding view."""

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


class UploadsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._rows = []
        self._signature = None
        self._alive = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Uploads and torrent seeding")
        self.list.SetHelpText(
            "Shows files other people download from your Soulseek shares and torrents you seed. Context Menu stops selected uploads."
        )
        for index, heading in enumerate(
            ("Title", "Service", "Peer", "Status", "Progress", "Speed", "Ratio")
        ):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 300)
        self.list.SetColumnWidth(1, 100)
        self.list.SetColumnWidth(2, 150)
        self.list.SetColumnWidth(3, 120)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_menu)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        # Torrent status has no GUI event source. A low-frequency timer only
        # checks while this page is visible; Soulseek pushes updates directly.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.timer.Start(2000)
        self.refresh()

    def shutdown(self):
        self._alive = False
        self.timer.Stop()

    def handle_soulseek_event(self, event):
        if self._alive and event.get("type") == "uploads":
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
        signature = tuple(
            (
                row.get("service"),
                row.get("key"),
                row.get("status"),
                round(float(row.get("percent") or 0), 1),
                int(row.get("speed") or 0),
                round(float(row.get("ratio") or 0), 2),
            )
            for row in rows
        )
        if signature == self._signature:
            return
        self._signature = signature
        self._rows = rows
        self.list.DeleteAllItems()
        for upload in rows:
            row = self.list.InsertItem(self.list.GetItemCount(), upload.get("title", ""))
            self.list.SetItem(row, 1, upload.get("service", ""))
            self.list.SetItem(row, 2, upload.get("peer", ""))
            self.list.SetItem(row, 3, upload.get("status", ""))
            percent = upload.get("percent")
            self.list.SetItem(row, 4, f"{float(percent):.0f}%" if percent is not None else "")
            speed = float(upload.get("speed") or 0)
            self.list.SetItem(row, 5, f"{_size(speed)}/s" if speed else "")
            ratio = upload.get("ratio")
            self.list.SetItem(row, 6, f"{float(ratio):.2f}" if ratio is not None else "")

    def _selected(self):
        selected = []
        row = self.list.GetFirstSelected()
        while row != -1:
            if row < len(self._rows):
                selected.append(self._rows[row])
            row = self.list.GetNextSelected(row)
        return selected

    def on_menu(self, event):
        menu = wx.Menu()
        stop = menu.Append(wx.ID_ANY, "&Stop selected uploads")
        stop.Enable(any(row.get("active") for row in self._selected()))
        menu.Bind(wx.EVT_MENU, self.on_stop, stop)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def on_stop(self, event=None):
        rows = [row for row in self._selected() if row.get("active")]
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
