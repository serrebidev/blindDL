# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Main window: notebook tabs, menus, status bar, queue/subscription wiring."""

import os
import sys
import threading
import time

import wx

from .. import (
    APP_NAME,
    soulseek_backend,
    speech,
    torrent_engine,
    updater,
)
from ..config import Config
from ..downloader import DownloadQueue, STATUS_DONE, STATUS_ERROR
from ..runtime import open_folder
from ..subscriptions import SubscriptionStore
from .chat_panel import ChatPanel
from .downloads_panel import DownloadsPanel
from .feeds_dialog import FeedsDialog
from .library_panel import LibraryPanel
from .messages_panel import MessagesPanel
from .search_panel import SearchPanel
from .settings_dialog import SettingsDialog
from .soulseek_user_dialog import UserBrowserDialog, UserProfileDialog
from .sources_dialog import SourcesDialog
from .subs_panel import SubsPanel
from .tray import TrayIcon, app_icon
from .update_dialog import UpdateDialog
from .url_panel import UrlPanel
from .uploads_panel import UploadsPanel

# How often the update clock is looked at, rather than setting one timer for
# the whole interval: blindDL lives in the tray for days at a time, and a
# timer that long stops being accurate the first time the machine sleeps.
UPDATE_TICK_MS = 30 * 60 * 1000
# How often a downloaded update looks to see whether the queue has gone
# quiet enough for blindDL to restart into it.
UPDATE_IDLE_TICK_MS = 60 * 1000

TAB_URL = 0
TAB_SEARCH = 1
TAB_DOWNLOADS = 2
TAB_UPLOADS = 3
TAB_LIBRARY = 4
TAB_SUBS = 5


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(950, 650))
        self.SetIcon(app_icon(32))

        self._closing = False
        self._background_started = False
        # Set when the user asks to leave outright -- File > Exit or the
        # tray's own Exit -- so that path is never turned into a hide.
        self._quitting = False
        self.tray = None
        self.config = Config()
        if self.config["start_maximized"]:
            self.Maximize(True)
        # Restored rows must exist before the panels are built, but workers
        # wait until those panels can safely receive their first update.
        self.queue = DownloadQueue(
            self.config, self._queue_notify, start_workers=False
        )
        self.subs = SubscriptionStore(self.config, self.queue, notify=self._subs_notify)
        self._last_counts = None
        self.chat_panel = None
        self.messages_panel = None
        # Update checking: one repeating clock, one worker at a time, and a
        # verified package waiting for the queue to go quiet.
        self._update_timer = None
        self._update_idle_timer = None
        self._update_checking = False
        self._pending_update = None
        self._pending_update_announced = False

        self._build_ui()
        self.downloads_panel.refresh_all()
        self._build_menus()
        self._bind_shortcuts()
        soulseek_backend.add_listener(self._queue_soulseek_event)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ICONIZE, self.on_iconize)
        self._apply_tray_setting()
        # MainFrame is constructed before __main__ can show it. Defer all
        # background services briefly so Windows can paint a responsive window
        # before Soulseek loads a large share index.
        self._background_start_timer = wx.CallLater(
            250, self._start_background_services
        )

    def _start_background_services(self):
        """Start persistent work after the first window has been presented."""
        if self._closing or self._background_started:
            return
        self._background_started = True
        self.queue.start()
        self.subs.start()
        threading.Thread(
            target=self._external_dependencies_worker,
            daemon=True,
            name="blinddl-external-tools",
        ).start()
        self._start_update_checks()
        if self.config["soulseek_enabled"]:
            self._apply_soulseek_setting()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self):
        self.notebook = wx.Notebook(self)
        self.url_panel = UrlPanel(self.notebook, self)
        self.search_panel = SearchPanel(self.notebook, self)
        self.downloads_panel = DownloadsPanel(self.notebook, self)
        self.uploads_panel = UploadsPanel(self.notebook, self)
        self.library_panel = LibraryPanel(self.notebook, self)
        self.subs_panel = SubsPanel(self.notebook, self)
        self.notebook.AddPage(self.url_panel, "URL")
        self.notebook.AddPage(self.search_panel, "Search")
        self.notebook.AddPage(self.downloads_panel, "Downloads")
        self.notebook.AddPage(self.uploads_panel, "Uploads")
        self.notebook.AddPage(self.library_panel, "Library")
        self.notebook.AddPage(self.subs_panel, "Subscriptions")
        self.CreateStatusBar(2)
        self.SetStatusWidths([-3, -1])
        self._sync_soulseek_tabs()
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_tab_changed)

        self.announce("Ready.")

    def _build_menus(self):
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_OPEN, "&Open downloads\tCtrl+O")
        file_menu.Append(wx.ID_PREFERENCES, "&Settings...\tCtrl+,")
        file_menu.AppendSeparator()
        # Deliberately not labelled Alt+F4: wx would turn that into a menu
        # accelerator, and the accelerator table is searched before Windows'
        # own close handling. Alt+F4 would then quit outright instead of
        # closing the window, which is the gesture that hides to the tray.
        file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")

        tools_menu = wx.Menu()
        self.ID_SOURCES = wx.NewIdRef()
        tools_menu.Append(self.ID_SOURCES, "Search &sites...\tCtrl+Shift+S")
        self.ID_FEEDS = wx.NewIdRef()
        tools_menu.Append(self.ID_FEEDS, "My torrent &indexers...")
        tools_menu.AppendSeparator()
        self.ID_ADD_SUB = wx.NewIdRef()
        tools_menu.Append(self.ID_ADD_SUB, "&Add subscription...")
        self.ID_CHECK_SUBS = wx.NewIdRef()
        tools_menu.Append(self.ID_CHECK_SUBS, "Check &subscriptions\tCtrl+Shift+C")

        # Checking for updates is about blindDL itself rather than about the
        # media it fetches, which is what everything left in Tools is, so it
        # sits with About under Help where an application usually keeps it.
        help_menu = wx.Menu()
        self.ID_UPDATE = wx.NewIdRef()
        help_menu.Append(self.ID_UPDATE, "Check for &updates...\tCtrl+U")
        help_menu.AppendSeparator()
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
        ids = [wx.NewIdRef() for _ in range(10)]
        entries = [
            (wx.ACCEL_CTRL, ord("1"), ids[0]),
            (wx.ACCEL_CTRL, ord("2"), ids[1]),
            (wx.ACCEL_CTRL, ord("3"), ids[2]),
            (wx.ACCEL_CTRL, ord("4"), ids[3]),
            (wx.ACCEL_CTRL, ord("5"), ids[4]),
            (wx.ACCEL_CTRL, ord("6"), ids[5]),
            (wx.ACCEL_CTRL, ord("7"), ids[6]),
            (wx.ACCEL_CTRL, ord("8"), ids[7]),
            (wx.ACCEL_CTRL, ord("F"), ids[8]),
            (wx.ACCEL_CTRL, ord("L"), ids[9]),
        ]
        self.SetAcceleratorTable(
            wx.AcceleratorTable([wx.AcceleratorEntry(*e) for e in entries])
        )
        for tab, bind_id in enumerate(ids[:6]):
            self.Bind(wx.EVT_MENU, lambda e, t=tab: self.show_tab(t), id=bind_id)
        self.Bind(
            wx.EVT_MENU,
            lambda event: self._show_optional_tab(self.chat_panel, "Chat"),
            id=ids[6],
        )
        self.Bind(
            wx.EVT_MENU,
            lambda event: self._show_optional_tab(self.messages_panel, "Messages"),
            id=ids[7],
        )
        self.Bind(wx.EVT_MENU, lambda e: self._focus_search(), id=ids[8])
        self.Bind(wx.EVT_MENU, lambda e: self._focus_url(), id=ids[9])

    # -- helpers used by panels ----------------------------------------------

    def announce(self, message, speak=True):
        """Say a message and leave it on the status bar.

        The status bar is still written either way, so NVDA+End reads the
        last thing that happened as it always did. Speaking it as well is
        what makes a finished search or a failed download arrive on its own
        instead of having to be checked for; Settings, Window turns that off
        for anyone who would rather it did not. Repeats are not spoken twice
        -- a status tick with nothing new to say rewrites the same sentence.
        """
        if self._closing:
            return
        self.SetStatusText(message, 0)
        if speak and self.config["speak_status"]:
            speech.announce(message)

    def show_tab(self, index):
        self.notebook.SetSelection(index)
        panel = self.notebook.GetPage(index)
        if hasattr(panel, "focus_input"):
            panel.focus_input()
        elif hasattr(panel, "list"):
            panel.list.SetFocus()

    def show_downloads_tab(self):
        self.show_tab(TAB_DOWNLOADS)

    def _show_optional_tab(self, panel, name):
        if panel is None:
            self.announce(f"{name} is available when Soulseek is enabled in Settings.")
            return
        for index in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(index) is panel:
                self.show_tab(index)
                return

    def open_soulseek_user(self, username=""):
        dialog = UserBrowserDialog(self, self, username)
        dialog.ShowModal()
        if dialog:
            dialog.Destroy()

    def message_soulseek_user(self, username):
        username = str(username or "").strip()
        if not username or self.messages_panel is None:
            self.announce("Enter a Soulseek username, and enable Soulseek if needed.")
            return
        self._show_optional_tab(self.messages_panel, "Messages")
        self.messages_panel.recipient_text.SetValue(username)
        self.messages_panel.message_text.SetFocus()
        self.announce(f"Message recipient: {username}.")

    def _user_action(self, action, username, finished, thread_name):
        username = str(username or "").strip()
        if not username:
            self.announce("Enter a Soulseek username.")
            return

        def worker():
            try:
                result = action(username, self.config)
            except Exception as exc:  # noqa: BLE001 - presented to the user
                wx.CallAfter(self.announce, f"Soulseek user action failed: {exc}")
                return
            wx.CallAfter(finished, username, result)

        threading.Thread(target=worker, daemon=True, name=thread_name).start()

    def add_soulseek_friend(self, username):
        self._user_action(
            soulseek_backend.add_friend,
            username,
            self._friend_added,
            "blinddl-soulseek-add-friend",
        )

    def _friend_added(self, username, friends):
        saved = list(self.config.get("soulseek_friends", []) or [])
        if username.casefold() not in {value.casefold() for value in saved}:
            saved.append(username)
            self.config["soulseek_friends"] = saved
            self.config.save()
        if self.messages_panel is not None:
            self.messages_panel._show_friends(friends)
        self.announce(f"Added Soulseek friend {username}.")

    def give_soulseek_free_slot(self, username):
        self._user_action(
            soulseek_backend.give_free_slot,
            username,
            self._free_slot_added,
            "blinddl-soulseek-free-slot",
        )

    def _free_slot_added(self, username, priority_users):
        self.config["soulseek_priority_users"] = list(priority_users)
        self.config.save()
        self.announce(f"Gave {username} Soulseek upload priority for a free slot.")

    def view_soulseek_profile(self, username):
        self.announce(f"Loading Soulseek profile for {username}...")
        self._user_action(
            soulseek_backend.user_profile,
            username,
            self._profile_loaded,
            "blinddl-soulseek-user-profile",
        )

    def _profile_loaded(self, username, profile):
        if self._closing:
            return
        dialog = UserProfileDialog(self, profile)
        dialog.ShowModal()
        dialog.Destroy()

    def _sync_soulseek_tabs(self):
        enabled = bool(self.config["soulseek_enabled"])
        if enabled and self.chat_panel is None:
            self.chat_panel = ChatPanel(self.notebook, self)
            self.messages_panel = MessagesPanel(self.notebook, self)
            self.notebook.AddPage(self.chat_panel, "Chat")
            self.notebook.AddPage(self.messages_panel, "Messages")
            return
        if enabled:
            return
        for attribute in ("messages_panel", "chat_panel"):
            panel = getattr(self, attribute)
            if panel is None:
                continue
            panel.shutdown()
            for index in range(self.notebook.GetPageCount()):
                if self.notebook.GetPage(index) is panel:
                    self.notebook.RemovePage(index)
                    break
            panel.Destroy()
            setattr(self, attribute, None)

    def play_media(self, player, location, title):
        """Start one player and stop any other tab's active playback."""
        for other in (
            self.url_panel.player,
            self.search_panel.player,
            self.library_panel.player,
        ):
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
        if (item.status == STATUS_DONE and not item.seeding
                and self.config["auto_clear_finished"]):
            # Only the clean finishes go. A failed or cancelled download
            # keeps its row so the error stays readable.
            self.queue.remove_completed()
            self.downloads_panel.refresh_all()
        counts = self.queue.counts()
        if counts != self._last_counts:
            self._last_counts = counts
            active, queued, done, failed = counts
            self.SetStatusText(
                f"{active} active, {queued} queued, {done} done, "
                f"{failed} failed/cancelled",
                1,
            )
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

    def _queue_soulseek_event(self, event):
        if not self._closing:
            wx.CallAfter(self._on_soulseek_event, event)

    def _on_soulseek_event(self, event):
        if self._closing:
            return
        if self.chat_panel is not None:
            self.chat_panel.handle_soulseek_event(event)
        if self.messages_panel is not None:
            self.messages_panel.handle_soulseek_event(event)
        self.uploads_panel.handle_soulseek_event(event)
        message = event.get("message", {})
        if event.get("type") == "private_message" and not message.get("outgoing"):
            self.announce(f"Private Soulseek message from {message.get('user', '')}.")
        elif event.get("type") == "room_message" and not message.get("outgoing"):
            self.announce(
                f"Soulseek room message from {message.get('user', '')} "
                f"in {message.get('room', '')}."
            )

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
        auto_update_was_enabled = bool(self.config["auto_update"])
        dialog = SettingsDialog(self, self.config)
        if dialog.ShowModal() == wx.ID_OK:
            dialog.apply()
            self._sync_soulseek_tabs()
            self.search_panel.refresh_engine_choices()
            self.library_panel.refresh(announce=False)
            self.queue.set_concurrency(self.config["max_concurrent"])
            self.subs.wake()
            self._apply_tray_setting()
            self._apply_torrent_setting()
            self._apply_soulseek_setting()
            self.announce("Settings saved.")
            if self.config["auto_update"] and not auto_update_was_enabled:
                self._maybe_auto_update(force=True)
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
            "Install libtorrent",
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        )
        if answer != wx.YES:
            self.config["torrent_engine"] = False
            self.config.save()
            self.announce("Left off. Torrents will keep opening in your own client.")
            return
        self.announce("Installing libtorrent; this takes about a minute...")
        threading.Thread(
            target=self._install_torrent_engine, daemon=True, name="blinddl-libtorrent"
        ).start()

    def _install_torrent_engine(self):
        ok = torrent_engine.install()
        if ok:
            wx.CallAfter(
                self.announce,
                "libtorrent installed. blindDL now downloads torrents itself.",
            )
            return
        self.config["torrent_engine"] = False
        self.config.save()
        wx.CallAfter(self._report_engine_failure)

    def _report_engine_failure(self):
        if self._closing:
            return
        self.announce("libtorrent could not be installed.")
        wx.MessageBox(
            torrent_engine.install_hint(),
            "Install libtorrent",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _apply_soulseek_setting(self):
        """Connect, reconnect, or stop the optional persistent peer client."""
        threading.Thread(
            target=self._configure_soulseek,
            daemon=True,
            name="blinddl-soulseek-configure",
        ).start()

    def _configure_soulseek(self):
        try:
            soulseek_backend.configure(self.config)
        except Exception as exc:  # noqa: BLE001 - shown on the status bar
            if not self._closing:
                wx.CallAfter(self.announce, f"Soulseek unavailable: {exc}")
            return
        if self.config["soulseek_enabled"] and not self._closing:
            wx.CallAfter(
                self.announce,
                "Soulseek connected; shared folders are being indexed.",
            )

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
        dialog = UpdateDialog(
            self, on_changed=lambda: self.announce("Tools updated. Restart blindDL.")
        )
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
            "Tabs: Ctrl+1-6, or Ctrl+7-8 for Soulseek Chat and Messages "
            "when enabled. URL: Ctrl+L. Search: Ctrl+F.\n"
            "Play from URL or Search without downloading, or use Library "
            "to play completed downloads.\n"
            "Search also finds free books, audiobooks, old-time radio, "
            "movies and TV. Downloaded books open in your usual reader "
            "from the Library tab.\n"
            "Search type narrows a music search to a track title, an album "
            "or an artist. Album lists whole releases; Enter on one "
            "downloads the tracks you keep.\n"
            "Status messages are spoken as they appear; Settings, Window "
            "turns that off.\n"
            "blindDL can download and install a new release when it starts "
            "and every 12 hours; see Settings, "
            "Window. Help, Check for updates does it now.\n"
            "Torrent results open in your own BitTorrent client, or download "
            "here when Settings, Torrents says so. Add your Prowlarr or "
            "Jackett in Tools, My torrent indexers to search private "
            "trackers too.",
            f"About {APP_NAME}",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

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
        wanted = bool(
            self.config["minimize_to_tray"] or self.config["tray_on_minimize"]
        )
        if wanted and self.tray is None:
            self.tray = TrayIcon(
                self, on_restore=self.restore_from_tray, on_exit=self.on_exit
            )
            if not self.tray.is_available():
                self.tray.dispose()
                self.tray = None
                self.announce(
                    "Windows could not install the blindDL tray icon, so the "
                    "window will remain visible when minimized or closed."
                )
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
        pending_hide = getattr(self, "_tray_hide_timer", None)
        if pending_hide is not None and pending_hide.IsRunning():
            pending_hide.Stop()
        if self.IsIconized():
            self.Iconize(False)
        self.Show()
        self.Raise()
        page = self.notebook.GetSelection()
        if page != wx.NOT_FOUND:
            self.show_tab(page)

    def _hide_to_tray(self):
        if self.tray is None or not self.tray.is_available():
            self.Show()
            if self.IsIconized():
                self.Iconize(False)
            self.Raise()
            self.announce(
                "The system tray icon is unavailable, so blindDL was left open."
            )
            return False
        self.Hide()
        self.tray.notify_hidden()
        self.announce(
            f"{APP_NAME} is still running in the system tray. Click the blue "
            "B icon, press Windows plus B, or launch blindDL again to restore it."
        )
        return True

    def on_iconize(self, event):
        """Minimizing hides the window in the tray when that is switched on."""
        event.Skip()
        if (
            self._closing
            or not event.IsIconized()
            or self.tray is None
            or not self.config["tray_on_minimize"]
        ):
            return
        # Let Windows finish its native minimize transition before changing
        # the state again. Doing both inside EVT_ICONIZE races Explorer: the
        # taskbar can restore the button after Hide() has removed it.
        wx.CallAfter(self._finish_minimize_to_tray)

    def _finish_minimize_to_tray(self):
        if (
            self._closing
            or not self.config["tray_on_minimize"]
            or not self.IsIconized()
        ):
            return
        self.Iconize(False)
        self._hide_to_tray()

    def on_close(self, event):
        if self._closing:
            return
        if (
            not self._quitting
            and self.tray is not None
            and self.config["minimize_to_tray"]
            and event.CanVeto()
        ):
            event.Veto()
            # Windows may make a vetoed close window visible again after this
            # handler returns. Hiding on the next event-loop turn avoids that
            # close-transition race.
            self._tray_hide_timer = wx.CallLater(100, self._hide_to_tray)
            return
        self._closing = True
        for timer_name in (
            "_background_start_timer",
            "_tray_hide_timer",
            "_update_timer",
            "_update_idle_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None and timer.IsRunning():
                timer.Stop()
        soulseek_backend.remove_listener(self._queue_soulseek_event)
        if self.chat_panel is not None:
            self.chat_panel.shutdown()
        if self.messages_panel is not None:
            self.messages_panel.shutdown()
        self.search_panel.shutdown()
        self.url_panel.shutdown()
        self.library_panel.shutdown()
        self.uploads_panel.shutdown()
        self.subs.stop()
        # Active rows become resumable Queued rows on the next start, and
        # completed seeds are recorded before libtorrent is shut down.
        self.queue.shutdown()
        # Seeding stops here, so this is the last chance to write down how
        # far each torrent got.
        torrent_engine.shutdown()
        # This writes Soulseek share and transfer caches and closes listening
        # ports before the native window disappears.
        soulseek_backend.shutdown()
        if self.tray is not None:
            self.tray.dispose()
            self.tray = None
        self.Destroy()

    # -- automatic dependency updates -------------------------------------------

    def _external_dependencies_worker(self):
        """Install large native Windows tools without blocking the window."""
        if sys.platform != "win32":
            return
        try:
            missing = updater.missing_external_tools()
            if not missing:
                return
            if updater.ensure_external_tools(lambda _line: None):
                wx.CallAfter(
                    self.announce,
                    "Download and playback tools installed in the background.",
                )
            else:
                wx.CallAfter(
                    self.announce,
                    "Some download tools could not be installed with WinGet. "
                    "Use Help, Check for updates to try again.",
                )
        except Exception:  # noqa: BLE001 - background best effort
            return

    def _start_update_checks(self):
        """Check for an update now, then keep checking on the saved interval."""
        self._update_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_update_tick, self._update_timer)
        self._update_timer.Start(UPDATE_TICK_MS)
        # Startup is the one check that does not wait for the interval. A
        # release that landed while blindDL was closed should be found when
        # it opens, not up to twelve hours afterwards.
        self._maybe_auto_update(force=True)

    def _on_update_tick(self, event):
        self._maybe_auto_update()

    def _maybe_auto_update(self, force=False):
        if self._closing or not self.config["auto_update"]:
            return
        # A check that is still running has not finished paying for itself;
        # starting a second one would only duplicate the network calls.
        if self._update_checking or self._pending_update is not None:
            return
        if not force:
            interval = max(1, int(self.config["update_check_hours"])) * 3600
            if time.time() - float(self.config["last_update_check"]) < interval:
                return
        self.config["last_update_check"] = time.time()
        self.config.save()
        self._update_checking = True
        threading.Thread(
            target=self._auto_update_worker, daemon=True, name="blinddl-updater"
        ).start()

    def _auto_update_worker(self):
        lines = []

        def log(line):
            lines.append(line)

        try:
            if getattr(sys, "frozen", False):
                self._check_for_release(log)
                return
            updater.ensure_deno(log)
            changed = updater.run_full_update(log)
            if changed:
                wx.CallAfter(self.announce, "Tools updated. Restart blindDL.")
        except Exception:  # noqa: BLE001 - background best effort
            return
        finally:
            self._update_checking = False

    def _check_for_release(self, log):
        """Look for a newer blindDL, and fetch it when asked to.

        A check that cannot reach GitHub says nothing: this runs on its own
        every twelve hours, and a passing network fault is not news. A
        download that fails after an update *was* found is worth saying,
        because by then something was expected to happen.
        """
        try:
            update = updater.check_for_app_update(log)
        except Exception:  # noqa: BLE001 - a network blip must not nag
            return
        if update is None:
            return
        wx.CallAfter(
            self.announce, f"Downloading blindDL {update.version}..."
        )
        try:
            package = updater.download_app_update(
                update, log, progress=self._announce_update_progress
            )
        except Exception as exc:  # noqa: BLE001 - the user was expecting this
            wx.CallAfter(
                self.announce, f"Automatic update failed: {exc}"
            )
            return
        wx.CallAfter(self._update_downloaded, update, package)

    def _announce_update_progress(self, line):
        """Say one download-progress line from the updater's own thread.

        An update that says "Downloading blindDL 1.2.3..." and then goes
        quiet for two minutes cannot be told from one that has stalled,
        so the percentages are spoken as they arrive.
        """
        wx.CallAfter(self.announce, line)

    def _update_downloaded(self, update, package):
        if self._closing:
            return
        self._pending_update = (update, package)
        self._pending_update_announced = False
        self._install_pending_update()

    def _install_pending_update(self):
        """Install the downloaded update once nothing is mid-download.

        Finishing an update restarts blindDL. The queue does turn active
        rows back into resumable ones on the way out, so nothing is lost
        either way, but pulling the window away from someone in the middle
        of a download is not something to do unasked.
        """
        if self._closing or self._pending_update is None:
            return
        update, package = self._pending_update
        active, queued, _done, _failed = self.queue.counts()
        if active or queued:
            if not self._pending_update_announced:
                self._pending_update_announced = True
                self.announce(
                    f"blindDL {update.version} is ready and installs once "
                    "the downloads finish."
                )
            self._start_update_idle_timer()
            return
        self._stop_update_idle_timer()
        self._pending_update = None
        self.announce(f"Installing blindDL {update.version}...")
        threading.Thread(
            target=self._install_update_worker,
            args=(update, package),
            daemon=True,
            name="blinddl-auto-install",
        ).start()

    def _install_update_worker(self, update, package):
        lines = []
        try:
            exit_to_update = updater.install_app_update(
                update, package, lines.append
            )
        except Exception as exc:  # noqa: BLE001 - shown on the status bar
            wx.CallAfter(self.announce, f"Automatic update failed: {exc}")
            return
        wx.CallAfter(self._update_started, update, exit_to_update)

    def _update_started(self, update, exit_to_update):
        if self._closing:
            return
        if exit_to_update:
            # Closing this way runs the ordinary shutdown, so the queue,
            # torrents and Soulseek shares are all written down first.
            self.announce(
                f"Installing blindDL {update.version}; it will restart."
            )
            self._quitting = True
            self.Close()
            return
        self.announce(
            f"blindDL {update.version} was downloaded. Finish the install "
            "to use it."
        )

    def _start_update_idle_timer(self):
        if self._update_idle_timer is None:
            self._update_idle_timer = wx.Timer(self)
            self.Bind(
                wx.EVT_TIMER, self._on_update_idle_tick, self._update_idle_timer
            )
        if not self._update_idle_timer.IsRunning():
            self._update_idle_timer.Start(UPDATE_IDLE_TICK_MS)

    def _stop_update_idle_timer(self):
        if self._update_idle_timer is not None:
            self._update_idle_timer.Stop()

    def _on_update_idle_tick(self, event):
        self._install_pending_update()
