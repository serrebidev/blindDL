# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Main window: notebook tabs, menus, status bar, queue/subscription wiring."""

import os
import threading
import time

import wx

from .. import APP_NAME, musicdl_backend, updater
from ..config import Config
from ..downloader import DownloadQueue, STATUS_DONE, STATUS_ERROR
from ..runtime import open_folder
from ..subscriptions import SubscriptionStore
from .downloads_panel import DownloadsPanel
from .search_panel import SearchPanel
from .settings_dialog import SettingsDialog
from .sources_dialog import SourcesDialog
from .subs_panel import SubsPanel
from .update_dialog import UpdateDialog
from .url_panel import UrlPanel

TAB_URL = 0
TAB_SEARCH = 1
TAB_DOWNLOADS = 2
TAB_SUBS = 3


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(950, 650))

        self.config = Config()
        self.queue = DownloadQueue(self.config, self._queue_notify)
        self.subs = SubscriptionStore(self.config, self.queue,
                                      notify=self._subs_notify)
        self._last_counts = None

        self._build_ui()
        self._build_menus()
        self._bind_shortcuts()

        self.subs.start()
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self._maybe_auto_update()
        # Build the music-site clients now so the first search does not pay
        # for it, and clear last session's search scratch files.
        threading.Thread(target=musicdl_backend.warm_up, daemon=True).start()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self):
        self.notebook = wx.Notebook(self)
        self.url_panel = UrlPanel(self.notebook, self)
        self.search_panel = SearchPanel(self.notebook, self)
        self.downloads_panel = DownloadsPanel(self.notebook, self)
        self.subs_panel = SubsPanel(self.notebook, self)
        self.notebook.AddPage(self.url_panel, "URL")
        self.notebook.AddPage(self.search_panel, "Search")
        self.notebook.AddPage(self.downloads_panel, "Downloads")
        self.notebook.AddPage(self.subs_panel, "Subscriptions")

        self.CreateStatusBar(2)
        self.SetStatusWidths([-3, -1])
        self.announce("Ready.")

    def _build_menus(self):
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_OPEN, "&Open downloads\tCtrl+O")
        file_menu.Append(wx.ID_PREFERENCES, "&Settings...\tCtrl+,")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_EXIT, "E&xit\tAlt+F4")

        tools_menu = wx.Menu()
        self.ID_SOURCES = wx.NewIdRef()
        tools_menu.Append(self.ID_SOURCES, "Search &sites...\tCtrl+Shift+S")
        tools_menu.AppendSeparator()
        self.ID_ADD_SUB = wx.NewIdRef()
        tools_menu.Append(self.ID_ADD_SUB, "&Add subscription...")
        self.ID_CHECK_SUBS = wx.NewIdRef()
        tools_menu.Append(self.ID_CHECK_SUBS,
                          "Check &subscriptions\tCtrl+Shift+C")
        tools_menu.AppendSeparator()
        self.ID_UPDATE = wx.NewIdRef()
        tools_menu.Append(self.ID_UPDATE, "Check for &updates...\tCtrl+U")

        help_menu = wx.Menu()
        help_menu.Append(wx.ID_ABOUT, "&About blindDL")

        menubar = wx.MenuBar()
        menubar.Append(file_menu, "&File")
        menubar.Append(tools_menu, "&Tools")
        menubar.Append(help_menu, "&Help")
        self.SetMenuBar(menubar)

        self.Bind(wx.EVT_MENU, self.on_open_folder, id=wx.ID_OPEN)
        self.Bind(wx.EVT_MENU, self.on_settings, id=wx.ID_PREFERENCES)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, self.on_choose_sources, id=self.ID_SOURCES)
        self.Bind(wx.EVT_MENU, self.on_add_subscription, id=self.ID_ADD_SUB)
        self.Bind(wx.EVT_MENU, self.on_check_updates, id=self.ID_UPDATE)
        self.Bind(wx.EVT_MENU, self.on_check_subs, id=self.ID_CHECK_SUBS)
        self.Bind(wx.EVT_MENU, self.on_about, id=wx.ID_ABOUT)

    def _bind_shortcuts(self):
        ids = [wx.NewIdRef() for _ in range(6)]
        entries = [
            (wx.ACCEL_CTRL, ord("1"), ids[0]),
            (wx.ACCEL_CTRL, ord("2"), ids[1]),
            (wx.ACCEL_CTRL, ord("3"), ids[2]),
            (wx.ACCEL_CTRL, ord("4"), ids[3]),
            (wx.ACCEL_CTRL, ord("F"), ids[4]),
            (wx.ACCEL_CTRL, ord("L"), ids[5]),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(
            [wx.AcceleratorEntry(*e) for e in entries]))
        for tab, bind_id in enumerate(ids[:4]):
            self.Bind(wx.EVT_MENU, lambda e, t=tab: self.show_tab(t), id=bind_id)
        self.Bind(wx.EVT_MENU, lambda e: self._focus_search(), id=ids[4])
        self.Bind(wx.EVT_MENU, lambda e: self._focus_url(), id=ids[5])

    # -- helpers used by panels ----------------------------------------------

    def announce(self, message):
        """Put a message on the status bar (NVDA: Insert+End reads it)."""
        self.SetStatusText(message, 0)

    def show_tab(self, index):
        self.notebook.SetSelection(index)
        panel = self.notebook.GetPage(index)
        if hasattr(panel, "focus_input"):
            panel.focus_input()
        elif hasattr(panel, "list"):
            panel.list.SetFocus()

    def show_downloads_tab(self):
        self.show_tab(TAB_DOWNLOADS)

    def _focus_search(self):
        self.notebook.SetSelection(TAB_SEARCH)
        self.search_panel.focus_input()

    def _focus_url(self):
        self.notebook.SetSelection(TAB_URL)
        self.url_panel.focus_input()

    # -- queue / subscription notifications (from worker threads) -------------

    def _queue_notify(self, item):
        wx.CallAfter(self._on_item_update, item)

    def _on_item_update(self, item):
        self.downloads_panel.update_item(item)
        counts = self.queue.counts()
        if counts != self._last_counts:
            self._last_counts = counts
            active, queued, done, failed = counts
            self.SetStatusText(
                f"{active} active, {queued} queued, {done} done, "
                f"{failed} failed/cancelled", 1)
        if item.status == STATUS_DONE:
            self.announce(f"Finished: {item.title}")
        elif item.status == STATUS_ERROR:
            self.announce(f"Download failed: {item.title}. {item.error}")

    def _subs_notify(self, message):
        wx.CallAfter(self.announce, message)
        wx.CallAfter(self.subs_panel.refresh)

    # -- menu handlers ---------------------------------------------------------

    def on_open_folder(self, event):
        path = self.config["download_dir"]
        os.makedirs(path, exist_ok=True)
        open_folder(path)

    def on_settings(self, event):
        dialog = SettingsDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self.queue.set_concurrency(self.config["max_concurrent"])
            self.subs.wake()
            self.announce("Settings saved.")
        dialog.Destroy()

    def on_choose_sources(self, event=None):
        dialog = SourcesDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self.announce(dialog.summary())
        dialog.Destroy()

    def on_check_updates(self, event):
        dialog = UpdateDialog(self, on_changed=lambda: self.announce(
            "Tools updated. Restart blindDL."))
        dialog.ShowModal()
        dialog.Destroy()

    def on_check_subs(self, event):
        self.announce("Checking all subscriptions...")
        threading.Thread(target=self._check_subs_worker, daemon=True).start()

    def on_add_subscription(self, event):
        self.show_tab(TAB_SUBS)
        self.subs_panel.on_add(event)

    def _check_subs_worker(self):
        self.subs.check_all()
        wx.CallAfter(self.subs_panel.refresh)
        wx.CallAfter(self.announce, "Subscription check complete.")

    def on_about(self, event):
        from .. import __version__
        wx.MessageBox(
            f"blindDL {__version__}\n\n"
            "Accessible media downloader.\n"
            "MIT licensed. Copyright (c) 2024-2026 "
            "serrebidev and contributors.\n\n"
            "Tabs: Ctrl+1-4. URL: Ctrl+L. Search: Ctrl+F.",
            f"About {APP_NAME}", wx.OK | wx.ICON_INFORMATION, self)

    def on_close(self, event):
        self.subs.stop()
        self.Destroy()

    # -- automatic dependency updates -------------------------------------------

    def _maybe_auto_update(self):
        if not self.config["auto_update"]:
            return
        interval = int(self.config["update_check_hours"]) * 3600
        if time.time() - float(self.config["last_update_check"]) < interval:
            return
        self.config["last_update_check"] = time.time()
        self.config.save()
        threading.Thread(target=self._auto_update_worker, daemon=True,
                         name="blinddl-updater").start()

    def _auto_update_worker(self):
        lines = []

        def log(line):
            lines.append(line)

        try:
            updater.ensure_deno(log)
            changed = updater.run_full_update(log)
        except Exception:  # noqa: BLE001 - background best effort
            return
        if changed:
            wx.CallAfter(self.announce,
                         "Tools updated. Restart blindDL.")
