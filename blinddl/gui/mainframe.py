# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Main window: notebook tabs, menus, status bar, queue/subscription wiring."""

import os
import sys
import threading
import time

import wx

from .. import APP_NAME, musicdl_backend, torrent_engine, updater
from ..config import Config
from ..downloader import DownloadQueue, STATUS_DONE, STATUS_ERROR
from ..runtime import open_folder
from ..subscriptions import SubscriptionStore
from .downloads_panel import DownloadsPanel
from .feeds_dialog import FeedsDialog
from .library_panel import LibraryPanel
from .search_panel import SearchPanel
from .settings_dialog import SettingsDialog
from .sources_dialog import SourcesDialog
from .subs_panel import SubsPanel
from .tray import TrayIcon
from .update_dialog import UpdateDialog
from .url_panel import UrlPanel

TAB_URL = 0
TAB_SEARCH = 1
TAB_DOWNLOADS = 2
TAB_LIBRARY = 3
TAB_SUBS = 4


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(950, 650))

        self._closing = False
        # Set when the user asks to leave outright -- File > Exit or the
        # tray's own Exit -- so that path is never turned into a hide.
        self._quitting = False
        self.tray = None
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
        self.Bind(wx.EVT_ICONIZE, self.on_iconize)
        self._apply_tray_setting()
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
        self.library_panel = LibraryPanel(self.notebook, self)
        self.subs_panel = SubsPanel(self.notebook, self)
        self.notebook.AddPage(self.url_panel, "URL")
        self.notebook.AddPage(self.search_panel, "Search")
        self.notebook.AddPage(self.downloads_panel, "Downloads")
        self.notebook.AddPage(self.library_panel, "Library")
        self.notebook.AddPage(self.subs_panel, "Subscriptions")
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_tab_changed)

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
        self.ID_FEEDS = wx.NewIdRef()
        tools_menu.Append(self.ID_FEEDS, "My torrent &indexers...")
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
        self.Bind(wx.EVT_MENU, self.on_exit, id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, self.on_choose_sources, id=self.ID_SOURCES)
        self.Bind(wx.EVT_MENU, self.on_edit_feeds, id=self.ID_FEEDS)
        self.Bind(wx.EVT_MENU, self.on_add_subscription, id=self.ID_ADD_SUB)
        self.Bind(wx.EVT_MENU, self.on_check_updates, id=self.ID_UPDATE)
        self.Bind(wx.EVT_MENU, self.on_check_subs, id=self.ID_CHECK_SUBS)
        self.Bind(wx.EVT_MENU, self.on_about, id=wx.ID_ABOUT)

    def _bind_shortcuts(self):
        ids = [wx.NewIdRef() for _ in range(7)]
        entries = [
            (wx.ACCEL_CTRL, ord("1"), ids[0]),
            (wx.ACCEL_CTRL, ord("2"), ids[1]),
            (wx.ACCEL_CTRL, ord("3"), ids[2]),
            (wx.ACCEL_CTRL, ord("4"), ids[3]),
            (wx.ACCEL_CTRL, ord("5"), ids[4]),
            (wx.ACCEL_CTRL, ord("F"), ids[5]),
            (wx.ACCEL_CTRL, ord("L"), ids[6]),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(
            [wx.AcceleratorEntry(*e) for e in entries]))
        for tab, bind_id in enumerate(ids[:5]):
            self.Bind(wx.EVT_MENU, lambda e, t=tab: self.show_tab(t), id=bind_id)
        self.Bind(wx.EVT_MENU, lambda e: self._focus_search(), id=ids[5])
        self.Bind(wx.EVT_MENU, lambda e: self._focus_url(), id=ids[6])

    # -- helpers used by panels ----------------------------------------------

    def announce(self, message):
        """Put a message on the status bar (NVDA: Insert+End reads it)."""
        if self._closing:
            return
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

    def play_media(self, player, location, title):
        """Start one player and stop any other tab's active playback."""
        for other in (
                self.url_panel.player,
                self.search_panel.player,
                self.library_panel.player):
            if other is not player:
                other.stop(silent=True)
        player.load(location, title)

    def on_tab_changed(self, event):
        if event.GetSelection() == TAB_LIBRARY:
            self.library_panel.refresh(announce=False)
        event.Skip()

    def _focus_search(self):
        self.notebook.SetSelection(TAB_SEARCH)
        self.search_panel.focus_input()

    def _focus_url(self):
        self.notebook.SetSelection(TAB_URL)
        self.url_panel.focus_input()

    # -- queue / subscription notifications (from worker threads) -------------

    def _queue_notify(self, item):
        if self._closing:
            return
        wx.CallAfter(self._on_item_update, item)

    def _on_item_update(self, item):
        if self._closing:
            return
        self.downloads_panel.update_item(item)
        counts = self.queue.counts()
        if counts != self._last_counts:
            self._last_counts = counts
            active, queued, done, failed = counts
            self.SetStatusText(
                f"{active} active, {queued} queued, {done} done, "
                f"{failed} failed/cancelled", 1)
        if item.status == STATUS_DONE:
            if self.notebook.GetSelection() == TAB_LIBRARY:
                self.library_panel.refresh(announce=False)
            self.announce(f"Finished: {item.title}")
        elif item.status == STATUS_ERROR:
            self.announce(f"Download failed: {item.title}. {item.error}")

    def _subs_notify(self, message):
        if self._closing:
            return
        wx.CallAfter(self.announce, message)
        wx.CallAfter(self._refresh_subscriptions)

    def _refresh_subscriptions(self):
        """Refresh only while the native subscription controls still exist."""
        if self._closing:
            return
        self.subs_panel.refresh()

    # -- menu handlers ---------------------------------------------------------

    def on_open_folder(self, event):
        path = self.config["download_dir"]
        os.makedirs(path, exist_ok=True)
        open_folder(path)

    def on_settings(self, event):
        dialog = SettingsDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self.search_panel.refresh_engine_choices()
            self.library_panel.refresh(announce=False)
            self.queue.set_concurrency(self.config["max_concurrent"])
            self.subs.wake()
            self._apply_tray_setting()
            self._apply_torrent_setting()
            self.announce("Settings saved.")
        dialog.Destroy()

    def _apply_torrent_setting(self):
        """Follow up on the torrent engine setting once Settings is closed.

        Releases contain libtorrent. Source checkouts can still add it on
        demand, which keeps contributor setup flexible.
        """
        if not self.config["torrent_engine"]:
            return
        if torrent_engine.available():
            # Rate limits, seeding limits and the rest reach a session that
            # is already running; a new one picks them up when it starts.
            if torrent_engine.running():
                torrent_engine.engine(self.config)
            return
        if getattr(sys, "frozen", False):
            self.config["torrent_engine"] = False
            self.config.save()
            self._report_engine_failure()
            return
        answer = wx.MessageBox(
            "Downloading torrents in blindDL needs the libtorrent package, "
            "which is not installed yet.\n\nInstall it now? It is a few "
            "megabytes and takes about a minute.",
            "Install libtorrent", wx.YES_NO | wx.ICON_QUESTION, self)
        if answer != wx.YES:
            self.config["torrent_engine"] = False
            self.config.save()
            self.announce(
                "Left off. Torrents will keep opening in your own client.")
            return
        self.announce("Installing libtorrent; this takes about a minute...")
        threading.Thread(target=self._install_torrent_engine, daemon=True,
                         name="blinddl-libtorrent").start()

    def _install_torrent_engine(self):
        ok = torrent_engine.install()
        if ok:
            wx.CallAfter(self.announce,
                         "libtorrent installed. blindDL now downloads "
                         "torrents itself.")
            return
        self.config["torrent_engine"] = False
        self.config.save()
        wx.CallAfter(self._report_engine_failure)

    def _report_engine_failure(self):
        if self._closing:
            return
        self.announce("libtorrent could not be installed.")
        wx.MessageBox(torrent_engine.install_hint(), "Install libtorrent",
                      wx.OK | wx.ICON_INFORMATION, self)

    def on_choose_sources(self, event=None):
        dialog = SourcesDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self.announce(dialog.summary())
        dialog.Destroy()

    def on_edit_feeds(self, event=None):
        dialog = FeedsDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self.announce(dialog.summary())
        dialog.Destroy()

    def on_check_updates(self, event):
        dialog = UpdateDialog(self, on_changed=lambda: self.announce(
            "Tools updated. Restart blindDL."))
        result = dialog.ShowModal()
        dialog.Destroy()
        if result == wx.ID_OK:
            self._quitting = True
            self.Close()

    def on_check_subs(self, event):
        self.announce("Checking all subscriptions...")
        threading.Thread(target=self._check_subs_worker, daemon=True).start()

    def on_add_subscription(self, event):
        self.show_tab(TAB_SUBS)
        self.subs_panel.on_add(event)

    def _check_subs_worker(self):
        self.subs.check_all()
        wx.CallAfter(self._refresh_subscriptions)
        wx.CallAfter(self.announce, "Subscription check complete.")

    def on_about(self, event):
        from .. import __version__
        wx.MessageBox(
            f"blindDL {__version__}\n\n"
            "Accessible media downloader.\n"
            "MIT licensed. Copyright (c) 2024-2026 "
            "serrebidev and contributors.\n\n"
            "Tabs: Ctrl+1-5. URL: Ctrl+L. Search: Ctrl+F.\n"
            "Play from URL or Search without downloading, or use Library "
            "to play completed downloads.\n"
            "Search also finds free books, audiobooks, old-time radio, "
            "movies and TV. Downloaded books open in your usual reader "
            "from the Library tab.\n"
            "Torrent results open in your own BitTorrent client, or download "
            "here when Settings, Torrents says so. Add your Prowlarr or "
            "Jackett in Tools, My torrent indexers to search private "
            "trackers too.",
            f"About {APP_NAME}", wx.OK | wx.ICON_INFORMATION, self)

    # -- closing and the system tray --------------------------------------------

    def on_exit(self, event=None):
        """File > Exit: leave for good, whatever closing is set to do."""
        self._quitting = True
        self.Close()

    def _apply_tray_setting(self):
        """Add or remove the tray icon to match the current settings.

        One icon serves both ways of getting there, so it exists while either
        closing or minimizing is set to hide the window.
        """
        wanted = bool(self.config["minimize_to_tray"] or
                      self.config["tray_on_minimize"])
        if wanted and self.tray is None:
            self.tray = TrayIcon(self, on_restore=self.restore_from_tray,
                                 on_exit=self.on_exit)
        elif not wanted and self.tray is not None:
            # Nothing can bring the window back once the icon is gone, so it
            # only leaves while the window is on screen.
            self.restore_from_tray()
            self.tray.dispose()
            self.tray = None

    def restore_from_tray(self):
        """Bring the window back and put the user where they left off."""
        if self._closing:
            return
        self.Show()
        if self.IsIconized():
            self.Iconize(False)
        self.Raise()
        page = self.notebook.GetSelection()
        if page != wx.NOT_FOUND:
            self.show_tab(page)

    def _hide_to_tray(self):
        self.Hide()
        self.announce(
            f"{APP_NAME} is in the system tray. Windows plus B reaches it; "
            "downloads keep running.")

    def on_iconize(self, event):
        """Minimizing hides the window in the tray when that is switched on."""
        event.Skip()
        if (self._closing or not event.IsIconized() or
                self.tray is None or not self.config["tray_on_minimize"]):
            return
        # Undo the iconize first: a window that is hidden while minimized
        # comes back minimized, and the taskbar keeps a button for it.
        self.Iconize(False)
        self._hide_to_tray()

    def on_close(self, event):
        if self._closing:
            return
        if (not self._quitting and self.tray is not None and
                self.config["minimize_to_tray"] and event.CanVeto()):
            event.Veto()
            self._hide_to_tray()
            return
        self._closing = True
        self.search_panel.shutdown()
        self.url_panel.shutdown()
        self.library_panel.shutdown()
        self.subs.stop()
        # Seeding stops here, so this is the last chance to write down how
        # far each torrent got.
        torrent_engine.shutdown()
        if self.tray is not None:
            self.tray.dispose()
            self.tray = None
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
            if getattr(sys, "frozen", False):
                update = updater.check_for_app_update(log)
                if update is not None:
                    wx.CallAfter(
                        self.announce,
                        f"BlindDL {update.version} is available. Use Tools, "
                        "Check for updates to install it.",
                    )
                return
            updater.ensure_deno(log)
            changed = updater.run_full_update(log)
        except Exception:  # noqa: BLE001 - background best effort
            return
        if changed:
            wx.CallAfter(self.announce,
                         "Tools updated. Restart blindDL.")
