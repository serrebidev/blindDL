# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
from contextlib import nullcontext
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import wx

# musicdl creates a file logger at import time. Keep GUI tests self-contained.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl import (
        adult_backend,
        applemusic_backend,
        archive_backend,
        browser_cookies,
        deezer_backend,
        musicdl_backend,
        preview,
        search_kind,
        search_order,
        speech,
        soulseek_backend,
        updater,
        ytdlp_backend,
    )
    from blinddl.downloader import (
        DownloadItem,
        STATUS_DONE,
        STATUS_ERROR,
        STATUS_QUEUED,
    )
    from blinddl import config as config_module
    from blinddl.config import DEFAULTS
    from blinddl.gui.downloads_panel import DownloadsPanel
    from blinddl.gui.item_picker_dialog import ItemPickerDialog
    from blinddl.gui.library_panel import (
        LibraryPanel,
        discover_library,
        discover_media,
        library_roots,
    )
    from blinddl.gui.mainframe import MainFrame, TAB_DOWNLOADS, TAB_LIBRARY
    from blinddl.gui.messages_panel import MessagesPanel
    from blinddl.gui import media_player
    from blinddl.gui.search_panel import (
        ADULT_ENGINE_CATEGORIES,
        ARCHIVE_COLUMN_HEADINGS,
        ARCHIVE_SORT_LABELS,
        AUDIOBOOK_COLUMN_HEADINGS,
        BOOK_COLUMN_HEADINGS,
        BOOK_SORT_LABELS,
        COLUMN_HEADINGS,
        ENGINE_ADULT,
        ENGINE_APPLE_MUSIC,
        ENGINE_ARCHIVE_AUDIO,
        ENGINE_ARCHIVE_VIDEO,
        ENGINE_AUDIOBOOKS,
        ENGINE_BOOKS,
        ENGINE_DEEZER,
        ENGINE_LABELS,
        ENGINE_MUSIC,
        ENGINE_SOULSEEK_AUDIO,
        ENGINE_SOULSEEK_BOOKS,
        ENGINE_SOULSEEK_TORRENTS,
        ENGINE_SOULSEEK_VIDEO,
        ENGINE_SOUNDCLOUD,
        ENGINE_STRAIGHT,
        ENGINE_TORRENTS,
        ENGINE_TRANS,
        ENGINE_YOUTUBE,
        GENERAL_ENGINE_COUNT,
        GENERAL_ENGINES,
        SORT_ARTIST,
        SORT_LABELS,
        SORT_LONGEST,
        SORT_NAME,
        SORT_RELEVANCE,
        SORT_SHORTEST,
        SORT_SITE,
        SOULSEEK_COLUMN_HEADINGS,
        SOULSEEK_SORT_LABELS,
        SearchPanel,
        _kind_capable_sources,
        _kind_phrase,
        _order_capable_sources,
        _order_phrase,
        _sort_for_order,
        _soulseek_media_kind,
        _sorted_results,
    )
    from blinddl.gui.settings_dialog import DEEZER_FORMAT_CHOICES, SettingsDialog
    from blinddl.gui.soulseek_user_dialog import UserBrowserDialog
    from blinddl.gui.sources_dialog import SourcesDialog
    from blinddl.gui.subs_panel import (
        SUBS_SORT_CHECKED,
        SUBS_SORT_ENABLED,
        SUBS_SORT_SITE,
        SUBS_SORT_STALE,
        SUBS_SORT_TITLE,
        SUBS_SORT_TRACKED,
        SubsPanel,
        _sorted_subscriptions,
    )
    from blinddl.gui.uploads_panel import UploadsPanel
    from blinddl.gui.tray import TrayIcon, app_icon
    from blinddl.gui.update_dialog import UpdateDialog
    from blinddl.gui.url_panel import UrlPanel


class _Clipboard:
    """In-memory clipboard so tests never contend with the user's clipboard."""

    def __init__(self):
        self.text = ""

    def Open(self):
        return True

    def SetData(self, data):
        self.text = data.GetText()
        return True

    def Flush(self):
        return True

    def Close(self):
        return None


class _Queue:
    def __init__(self):
        self.items = []
        self.calls = []
        self.folders = []

    def add_ytdlp(self, url, title, audio_only=None, folder=""):
        self.calls.append(("ytdlp", url, title, audio_only))
        self.folders.append(folder)

    def add_musicdl(self, song, title, folder=""):
        self.calls.append(("musicdl", song, title))
        self.folders.append(folder)

    def add_sideb(self, url, title, folder=""):
        self.calls.append(("sideb", url, title))
        self.folders.append(folder)

    def add_applemusic(self, url, title, folder=""):
        self.calls.append(("applemusic", url, title))
        self.folders.append(folder)

    def add_adult(self, payload, title, folder=""):
        self.calls.append(("adult", payload, title))
        self.folders.append(folder)

    def add_book(self, payload, title):
        self.calls.append(("book", payload, title))

    def add_audiobook(self, payload, title):
        self.calls.append(("audiobook", payload, title))

    def add_archive(self, payload, title):
        self.calls.append(("archive", payload, title))

    def add_soulseek(self, payload, title):
        self.calls.append(("soulseek", payload, title))

    def batch_additions(self):
        return nullcontext()

    def _find(self, item_id):
        return next((item for item in self.items if item.id == item_id), None)

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is not None:
            item.cancel_event.set()

    def remove_finished(self):
        self.items = [item for item in self.items if item.status != STATUS_DONE]

    def remove_completed(self):
        self.items = [item for item in self.items if item.status != STATUS_DONE]


class _SettingsConfig(dict):
    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))
        self.saved = False

    def save(self):
        self.saved = True


class _SavingConfig(dict):
    """A config holding only the keys one test cares about."""

    def __init__(self, values):
        super().__init__(values)
        self.saved = False

    def save(self):
        self.saved = True


class _Subscriptions:
    def __init__(self):
        self.rows = [
            {
                "id": "one",
                "title": "One",
                "url": "one",
                "enabled": True,
                "seen_ids": [],
            },
            {
                "id": "two",
                "title": "Two",
                "url": "two",
                "enabled": True,
                "seen_ids": [],
            },
        ]

    def snapshot(self):
        return [dict(row) for row in self.rows]

    def set_enabled(self, sub_id, enabled):
        next(row for row in self.rows if row["id"] == sub_id)["enabled"] = enabled

    def set_order(self, sub_id, order):
        next(row for row in self.rows if row["id"] == sub_id)["order"] = order


class _Frame:
    def __init__(self):
        self.config = {
            "disabled_music_sources": [],
            "disabled_adult_sources": [],
            "disabled_book_sources": [],
            "disabled_audiobook_sources": [],
            "disabled_archive_sources": [],
            "adult_sites_enabled": True,
            "search_timeout_s": 5,
            "audio_only": True,
            "auto_clear_finished": False,
        }
        self.queue = _Queue()
        self.subs = _Subscriptions()
        self.messages = []
        self.play_calls = []

    def announce(self, message):
        self.messages.append(message)

    def on_choose_sources(self):
        pass

    def show_downloads_tab(self):
        pass

    def play_media(self, player, location, title):
        self.play_calls.append((player, location, title))


class GuiInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.host = wx.Frame(None)
        self.frame = _Frame()

    def tearDown(self):
        self.host.Destroy()
        self.app.Yield()

    def test_item_picker_all_clear_and_specific_selection(self):
        items = [
            {"title": "One", "artist": "Artist", "duration_s": 60},
            {"title": "Two", "artist": "Artist", "duration_s": 90},
            {"title": "Three", "artist": "Artist", "duration_s": 120},
        ]
        dialog = ItemPickerDialog(self.host, items, "Album")
        self.assertEqual(dialog.selected_items(), items)

        dialog.on_clear_selection(None)
        self.assertEqual(dialog.selected_items(), [])
        self.assertFalse(dialog.download_btn.IsEnabled())

        dialog.item_list.CheckItem(1, True)
        self.app.Yield()
        self.assertEqual(dialog.selected_items(), [items[1]])
        dialog.Destroy()

    def test_high_contrast_tray_icon_installs_and_can_be_removed(self):
        icon = app_icon(32)
        self.assertTrue(icon.IsOk())
        tray = TrayIcon(self.host, lambda: None, lambda: None)
        try:
            self.assertTrue(tray.is_available())
        finally:
            tray.dispose()

    def test_url_enter_does_not_start_a_second_inspection(self):
        panel = UrlPanel(self.host, self.frame)
        panel.url_text.SetValue("https://example.test/media")
        panel.download_btn.Disable()

        with mock.patch("blinddl.gui.url_panel.threading.Thread") as thread:
            panel.on_download(None)

        thread.assert_not_called()
        panel.shutdown()
        panel.Destroy()

    def test_a_playlist_downloads_into_a_folder_of_its_own(self):
        panel = UrlPanel(self.host, self.frame)
        entries = [
            {"url": "https://example.test/one", "title": "One"},
            {"url": "https://example.test/two", "title": "Two"},
        ]
        picker = mock.Mock()
        picker.ShowModal.return_value = wx.ID_OK
        picker.selected_items.return_value = entries[:1]

        with mock.patch("blinddl.gui.url_panel.ItemPickerDialog",
                        return_value=picker):
            panel._inspect_done(entries, "Best of 2026", True, "ytdlp")

        # Picking one track still files it under the playlist it came from.
        self.assertEqual(self.frame.queue.folders, ["Best of 2026"])

        # A single video is not a collection and stays where it always went.
        self.frame.queue.folders.clear()
        panel._inspect_done(
            [{"url": "https://example.test/solo", "title": "Solo"}],
            "Solo", True, "ytdlp")
        self.assertEqual(self.frame.queue.folders, [""])
        panel.shutdown()
        panel.Destroy()

    def test_apple_music_fallback_error_is_labelled_correctly(self):
        panel = UrlPanel(self.host, self.frame)
        self.frame.config["cookies_from_browser"] = None
        errors = []

        with (
            mock.patch(
                "blinddl.gui.url_panel.wx.CallAfter",
                side_effect=lambda callback, *args: callback(*args),
            ),
            mock.patch(
                "blinddl.gui.url_panel.adult_backend.is_supported_url",
                return_value=False,
            ),
            mock.patch(
                "blinddl.gui.url_panel.sideb_backend.is_deezer_url",
                return_value=False,
            ),
            mock.patch(
                "blinddl.gui.url_panel.applemusic_backend.is_apple_music_url",
                return_value=True,
            ),
            mock.patch(
                "blinddl.gui.url_panel.applemusic_backend.extract_flat",
                side_effect=RuntimeError("cookies expired"),
            ),
            mock.patch(
                "blinddl.gui.url_panel.ytdlp_backend.extract_flat",
                side_effect=RuntimeError("unsupported URL"),
            ),
            mock.patch.object(panel, "_inspect_failed", side_effect=errors.append),
        ):
            panel._inspect("https://music.apple.com/album/test", True)

        self.assertEqual(
            errors,
            ["Apple Music: cookies expired\nyt-dlp: unsupported URL"],
        )
        panel.shutdown()
        panel.Destroy()

    def test_busy_update_dialog_vetoes_title_bar_close(self):
        with mock.patch("blinddl.gui.update_dialog.threading.Thread"):
            dialog = UpdateDialog(self.host)
        event = mock.Mock()
        event.CanVeto.return_value = True

        dialog._on_close(event)

        event.Veto.assert_called_once_with()
        self.assertTrue(dialog._alive)
        dialog._busy = False
        dialog.Destroy()

    def test_destroyed_update_dialog_ignores_queued_log_writes(self):
        with mock.patch("blinddl.gui.update_dialog.threading.Thread"):
            dialog = UpdateDialog(self.host)
        dialog._alive = False

        with mock.patch.object(dialog.log_text, "AppendText") as append:
            dialog._append_log("late message\n")

        append.assert_not_called()
        dialog.Destroy()

    def test_update_dialog_speaks_download_progress(self):
        # The log is a read-only text control, which a screen reader does
        # not read on its own: unspoken progress is no progress at all.
        with mock.patch("blinddl.gui.update_dialog.threading.Thread"):
            dialog = UpdateDialog(self.host)

        with mock.patch("blinddl.gui.update_dialog.speech.announce") as announce:
            dialog._progress("blindDL 9.9.9: 40 percent of 88 MB.")

        announce.assert_called_once_with("blindDL 9.9.9: 40 percent of 88 MB.")
        dialog._busy = False
        dialog.Destroy()

    def test_window_is_never_hidden_without_an_installed_tray_icon(self):
        tray = SimpleNamespace(is_available=lambda: False)
        holder = SimpleNamespace(
            tray=tray,
            Show=mock.Mock(),
            Hide=mock.Mock(),
            Raise=mock.Mock(),
            IsIconized=mock.Mock(return_value=False),
            Iconize=mock.Mock(),
            announce=mock.Mock(),
        )

        hidden = MainFrame._hide_to_tray(holder)

        self.assertFalse(hidden)
        holder.Show.assert_called_once()
        holder.Hide.assert_not_called()
        holder.Raise.assert_called_once()

    def test_close_to_tray_is_deferred_until_after_windows_close_event(self):
        hide = mock.Mock()
        holder = SimpleNamespace(
            _closing=False,
            _quitting=False,
            tray=object(),
            config={"minimize_to_tray": True},
            _hide_to_tray=hide,
        )
        event = mock.Mock()
        event.CanVeto.return_value = True

        with mock.patch(
            "blinddl.gui.mainframe.wx.CallLater",
            side_effect=lambda _delay, callback, *args: callback(*args),
        ) as call_later:
            MainFrame.on_close(holder, event)

        event.Veto.assert_called_once_with()
        call_later.assert_called_once_with(100, hide)
        hide.assert_called_once_with()

    @staticmethod
    def _update_holder(**overrides):
        config = {
            "auto_update": True,
            "auto_install_update": False,
            "update_check_hours": 12,
            "last_update_check": 0,
        }
        config.update(overrides.pop("config", {}))
        holder = SimpleNamespace(
            _closing=False,
            _update_checking=False,
            _pending_update=None,
            _pending_update_announced=False,
            config=config,
            announce=mock.Mock(),
            _auto_update_worker=mock.Mock(),
            _announce_update_progress=mock.Mock(),
        )
        holder.config = _SavingConfig(config)
        for name, value in overrides.items():
            setattr(holder, name, value)
        return holder

    def test_the_startup_check_does_not_wait_for_the_interval(self):
        # A release that landed while blindDL was closed should be found
        # when it opens, not up to twelve hours afterwards.
        holder = self._update_holder()
        holder.config["last_update_check"] = time.time()

        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder, force=True)

        thread.assert_called_once()
        self.assertTrue(holder._update_checking)

    def test_later_checks_wait_for_the_twelve_hour_interval(self):
        holder = self._update_holder()
        holder.config["last_update_check"] = time.time() - 3600

        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder)
        thread.assert_not_called()

        holder.config["last_update_check"] = time.time() - 13 * 3600
        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder)
        thread.assert_called_once()

    def test_a_check_already_running_is_not_started_twice(self):
        holder = self._update_holder(_update_checking=True)
        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder, force=True)
        thread.assert_not_called()

        # Nor while a downloaded update is still waiting to be installed.
        holder = self._update_holder(_pending_update=("update", "package"))
        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder, force=True)
        thread.assert_not_called()

        holder = self._update_holder(config={"auto_update": False})
        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._maybe_auto_update(holder, force=True)
        thread.assert_not_called()

    def test_a_found_release_is_only_named_unless_auto_install_is_on(self):
        holder = self._update_holder()
        update = SimpleNamespace(version="9.9.9")

        with (
            mock.patch.object(updater, "check_for_app_update",
                              return_value=update),
            mock.patch.object(updater, "download_app_update") as download,
            mock.patch.object(wx, "CallAfter",
                              side_effect=lambda fn, *a: fn(*a)),
        ):
            MainFrame._check_for_release(holder, lambda _line: None)

        download.assert_not_called()
        self.assertIn("9.9.9", holder.announce.call_args.args[0])
        self.assertIn("Help", holder.announce.call_args.args[0])

    def test_a_check_that_cannot_reach_github_says_nothing(self):
        # This runs on its own every twelve hours; a passing network fault
        # is not news, and saying so would interrupt whatever is being read.
        holder = self._update_holder()
        with (
            mock.patch.object(updater, "check_for_app_update",
                              side_effect=OSError("no route to host")),
            mock.patch.object(wx, "CallAfter",
                              side_effect=lambda fn, *a: fn(*a)),
        ):
            MainFrame._check_for_release(holder, lambda _line: None)
        holder.announce.assert_not_called()

    def test_auto_install_downloads_and_reports_a_download_that_fails(self):
        update = SimpleNamespace(version="9.9.9")
        holder = self._update_holder(
            config={"auto_install_update": True},
            _update_downloaded=mock.Mock(),
        )

        with (
            mock.patch.object(updater, "check_for_app_update",
                              return_value=update),
            mock.patch.object(updater, "download_app_update",
                              return_value="package.exe") as download,
            mock.patch.object(wx, "CallAfter",
                              side_effect=lambda fn, *a: fn(*a)),
        ):
            MainFrame._check_for_release(holder, lambda _line: None)

        download.assert_called_once()
        holder._update_downloaded.assert_called_once_with(update, "package.exe")

        # A download that fails after an update was found is worth saying:
        # by then something was expected to happen.
        holder = self._update_holder(config={"auto_install_update": True})
        with (
            mock.patch.object(updater, "check_for_app_update",
                              return_value=update),
            mock.patch.object(updater, "download_app_update",
                              side_effect=RuntimeError("checksum failed")),
            mock.patch.object(wx, "CallAfter",
                              side_effect=lambda fn, *a: fn(*a)),
        ):
            MainFrame._check_for_release(holder, lambda _line: None)
        self.assertIn("checksum failed", holder.announce.call_args.args[0])

    def test_an_update_waits_for_the_downloads_to_finish(self):
        update = SimpleNamespace(version="9.9.9")
        holder = self._update_holder(
            _pending_update=(update, "package.exe"),
            queue=SimpleNamespace(counts=lambda: (1, 2, 0, 0)),
            _start_update_idle_timer=mock.Mock(),
            _stop_update_idle_timer=mock.Mock(),
        )

        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._install_pending_update(holder)
            # A second look does not repeat itself at the user.
            MainFrame._install_pending_update(holder)

        thread.assert_not_called()
        holder._start_update_idle_timer.assert_called()
        self.assertEqual(holder.announce.call_count, 1)
        self.assertIn("once the downloads finish",
                      holder.announce.call_args.args[0])
        # The package is still there, waiting for a quieter moment.
        self.assertIsNotNone(holder._pending_update)

    def test_an_update_installs_once_the_queue_is_quiet(self):
        update = SimpleNamespace(version="9.9.9")
        holder = self._update_holder(
            _pending_update=(update, "package.exe"),
            queue=SimpleNamespace(counts=lambda: (0, 0, 5, 1)),
            _start_update_idle_timer=mock.Mock(),
            _stop_update_idle_timer=mock.Mock(),
            _install_update_worker=mock.Mock(),
        )

        with mock.patch.object(threading, "Thread") as thread:
            MainFrame._install_pending_update(holder)

        holder._stop_update_idle_timer.assert_called_once_with()
        self.assertEqual(thread.call_args.kwargs["args"],
                         (update, "package.exe"))
        # Cleared before the worker starts, so a tick landing meanwhile
        # cannot start the same install twice.
        self.assertIsNone(holder._pending_update)

    def test_installing_closes_blinddl_through_the_ordinary_shutdown(self):
        update = SimpleNamespace(version="9.9.9")
        holder = self._update_holder(
            _quitting=False, Close=mock.Mock()
        )

        MainFrame._update_started(holder, update, True)

        # Closing this way is what writes the queue, torrents and Soulseek
        # shares down before the window goes.
        self.assertTrue(holder._quitting)
        holder.Close.assert_called_once_with()
        self.assertIn("restart", holder.announce.call_args.args[0])

        # macOS opens a disk image instead, so blindDL stays where it is.
        holder = self._update_holder(_quitting=False, Close=mock.Mock())
        MainFrame._update_started(holder, update, False)
        holder.Close.assert_not_called()
        self.assertIn("Finish the install", holder.announce.call_args.args[0])

    def test_check_for_updates_lives_in_the_help_menu(self):
        holder = mock.MagicMock()

        MainFrame._build_menus(holder)

        menubar = holder.SetMenuBar.call_args[0][0]
        labels = [menubar.GetMenuLabelText(index)
                  for index in range(menubar.GetMenuCount())]
        self.assertEqual(labels, ["File", "Tools", "Help"])
        help_menu = menubar.GetMenu(labels.index("Help"))
        help_labels = [item.GetItemLabelText() for item in help_menu.GetMenuItems()
                       if not item.IsSeparator()]
        self.assertIn("Check for updates...", help_labels)
        # Tools is what blindDL fetches media with; updating blindDL itself
        # is not one of those things and no longer sits there.
        tools_menu = menubar.GetMenu(labels.index("Tools"))
        tools_labels = [item.GetItemLabelText()
                        for item in tools_menu.GetMenuItems()
                        if not item.IsSeparator()]
        self.assertFalse([label for label in tools_labels
                          if "update" in label.casefold()])
        # The shortcut moved with it rather than being left behind.
        item = help_menu.FindItemById(holder.ID_UPDATE)
        self.assertIn("CTRL+U", item.GetItemLabel().upper())

    def test_exit_menu_leaves_alt_f4_to_the_window_close_path(self):
        # A menu accelerator is matched before Windows' own close handling, so
        # labelling Exit with Alt+F4 would quit outright instead of closing the
        # window -- and closing is the gesture that hides in the tray.
        holder = mock.MagicMock()

        MainFrame._build_menus(holder)

        menubar = holder.SetMenuBar.call_args[0][0]
        item = menubar.FindItemById(wx.ID_EXIT)
        self.assertNotIn("F4", item.GetItemLabel().upper())
        accel = item.GetAccel()
        self.assertEqual(accel.GetFlags(), wx.ACCEL_CTRL)
        self.assertEqual(accel.GetKeyCode(), ord("Q"))

    def test_minimize_to_tray_finishes_after_native_iconize_event(self):
        finish = mock.Mock()
        holder = SimpleNamespace(
            _closing=False,
            tray=object(),
            config={"tray_on_minimize": True},
            _finish_minimize_to_tray=finish,
        )
        event = mock.Mock()
        event.IsIconized.return_value = True

        with mock.patch(
            "blinddl.gui.mainframe.wx.CallAfter",
            side_effect=lambda callback, *args: callback(*args),
        ) as call_after:
            MainFrame.on_iconize(holder, event)

        event.Skip.assert_called_once_with()
        call_after.assert_called_once_with(finish)
        finish.assert_called_once_with()

    def test_background_services_start_only_once_after_window_creation(self):
        holder = SimpleNamespace(
            _closing=False,
            _background_started=False,
            queue=SimpleNamespace(start=mock.Mock()),
            subs=SimpleNamespace(start=mock.Mock()),
            config={"soulseek_enabled": True},
            _start_update_checks=mock.Mock(),
            _apply_soulseek_setting=mock.Mock(),
        )

        MainFrame._start_background_services(holder)
        MainFrame._start_background_services(holder)

        holder.queue.start.assert_called_once_with()
        holder.subs.start.assert_called_once_with()
        holder._start_update_checks.assert_called_once_with()
        holder._apply_soulseek_setting.assert_called_once_with()

    def test_search_queues_every_selected_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = 1
        panel.results = [
            {"title": "One", "url": "https://example/one"},
            {"title": "Two", "url": "https://example/two"},
        ]
        panel.results_list.SetItemCount(len(panel.results))
        for row in range(len(panel.results)):
            panel.results_list.Select(row)

        panel.on_download_selected(None)
        self.assertEqual(len(self.frame.queue.calls), 2)
        self.assertEqual(self.frame.messages[-1], "Queued 2 downloads.")

    def test_search_preview_ready_uses_shared_player(self):
        panel = SearchPanel(self.host, self.frame)
        panel.preview_token = token = object()

        panel._preview_ready(token, "https://media.example/audio.mp3", "One")

        self.assertEqual(
            self.frame.play_calls,
            [(panel.player, "https://media.example/audio.mp3", "One")],
        )

    def test_music_search_preview_uses_direct_download_url(self):
        item = {
            "title": "One",
            "song_info": SimpleNamespace(
                download_url=[{"url": "https://media.example/one.mp3"}]
            ),
        }

        location, title = preview.resolve_search_result(
            item, audio_only=True, config={}
        )

        self.assertEqual(location, "https://media.example/one.mp3")
        self.assertEqual(title, "One")

    def test_direct_media_url_bypasses_ytdlp_extraction(self):
        with mock.patch.object(ytdlp_backend, "resolve_stream") as resolve:
            location, title = preview.resolve_url(
                "https://media.example/live/video.mp4?token=one",
                audio_only=False,
                config={},
            )

        self.assertEqual(location, "https://media.example/live/video.mp4?token=one")
        self.assertEqual(title, location)
        resolve.assert_not_called()

    def test_preview_accepts_the_real_config_object(self):
        # The app hands preview a Config, not a dict. Dicts have .get, so a
        # dict here would hide a missing Config.get entirely.
        with mock.patch.object(
            config_module, "app_data_dir", return_value=tempfile.mkdtemp()
        ):
            config = config_module.Config()
        item = {
            "kind": "adult",
            "title": "One",
            "source": "EPorner",
            "url": "https://www.eporner.com/video-one/",
        }

        with mock.patch.object(
            ytdlp_backend, "resolve_stream", return_value="https://cdn.example/one.mp4"
        ):
            location, title = preview.resolve_search_result(
                item, audio_only=False, config=config
            )

        self.assertEqual(location, "https://cdn.example/one.mp4")
        self.assertEqual(title, "One")

    def test_result_url_prefers_page_url_then_falls_back(self):
        self.assertEqual(
            preview.result_url(
                {
                    "url": "https://www.eporner.com/video-one/",
                    "direct_url": "https://cdn.example/one.mp4",
                }
            ),
            "https://www.eporner.com/video-one/",
        )
        self.assertEqual(
            preview.result_url({"direct_url": "https://cdn.example/one.mp4"}),
            "https://cdn.example/one.mp4",
        )
        self.assertEqual(
            preview.result_url(
                {
                    "song_info": SimpleNamespace(
                        download_url=[{"url": "https://media.example/one.mp3"}]
                    )
                }
            ),
            "https://media.example/one.mp3",
        )
        self.assertIsNone(preview.result_url({"title": "No URL"}))

    def test_copy_url_puts_selected_result_links_on_the_clipboard(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ADULT
        panel.results = [
            {"title": "One", "url": "https://www.eporner.com/video-one/"},
            {"title": "Two", "url": "https://www.eporner.com/video-two/"},
            {"title": "Three"},
        ]
        panel.results_list.SetItemCount(len(panel.results))

        clipboard = _Clipboard()
        with mock.patch.object(wx, "TheClipboard", clipboard):
            panel.results_list.Select(0)
            panel.on_copy_url(None)
            self.assertEqual(clipboard.text, "https://www.eporner.com/video-one/")
            self.assertEqual(self.frame.messages[-1], "Copied 1 URL.")

            panel.results_list.Select(1)
            panel.results_list.Select(2)
            panel.on_copy_url(None)
            self.assertEqual(
                clipboard.text,
                "https://www.eporner.com/video-one/\n"
                "https://www.eporner.com/video-two/",
            )
        self.assertEqual(self.frame.messages[-1], "Copied 2 URLs. 1 had no URL.")

    def test_copy_url_reports_when_no_result_has_a_link(self):
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": "One"}]
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)

        panel.on_copy_url(None)

        self.assertEqual(self.frame.messages[-1], "No URL for that result.")

    def test_bundled_vlc_runtime_paths_are_configured(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "libvlc.dll").touch()
            (root / "plugins").mkdir()
            with (
                mock.patch.object(media_player.sys, "platform", "win32"),
                mock.patch.object(media_player.sys, "_MEIPASS", str(root), create=True),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("PYTHON_VLC_LIB_PATH", None)
                os.environ.pop("PYTHON_VLC_MODULE_PATH", None)
                media_player._configure_bundled_vlc()
                self.assertEqual(
                    os.environ["PYTHON_VLC_LIB_PATH"],
                    str(root / "libvlc.dll"),
                )
                self.assertEqual(
                    os.environ["PYTHON_VLC_MODULE_PATH"],
                    str(root / "plugins"),
                )

    def test_media_timer_skips_unchanged_accessible_values(self):
        panel = SimpleNamespace(
            _loaded=True,
            _length=lambda: 10_000,
            _tell=lambda: 1_000,
            _updating_position=False,
            time_text=mock.Mock(),
            position=mock.Mock(),
        )
        panel.time_text.GetLabel.return_value = "0:01 / 0:10"
        panel.position.GetValue.return_value = 100
        panel.position.HasFocus.return_value = False
        panel._set_time = lambda current, length: media_player.MediaPlayerPanel._set_time(
            panel, current, length
        )

        media_player.MediaPlayerPanel._on_timer(panel, None)

        panel.time_text.SetLabel.assert_not_called()
        panel.position.SetValue.assert_not_called()

    def test_media_timer_does_not_move_a_focused_seek_slider(self):
        panel = SimpleNamespace(
            _loaded=True,
            _length=lambda: 10_000,
            _tell=lambda: 5_000,
            _updating_position=False,
            time_text=mock.Mock(),
            position=mock.Mock(),
        )
        panel.time_text.GetLabel.return_value = "0:04 / 0:10"
        panel.position.HasFocus.return_value = True
        panel._set_time = lambda current, length: media_player.MediaPlayerPanel._set_time(
            panel, current, length
        )

        media_player.MediaPlayerPanel._on_timer(panel, None)

        panel.time_text.SetLabel.assert_called_once_with("0:05 / 0:10")
        panel.position.SetValue.assert_not_called()

    def test_media_players_share_one_vlc_runtime(self):
        runtime = object()
        fake_vlc = SimpleNamespace(Instance=mock.Mock(return_value=runtime))
        with (
            mock.patch.object(media_player, "vlc", fake_vlc),
            mock.patch.object(media_player, "_shared_vlc_instance", None),
        ):
            first = media_player._get_vlc_instance()
            second = media_player._get_vlc_instance()

        self.assertIs(first, runtime)
        self.assertIs(second, runtime)
        fake_vlc.Instance.assert_called_once_with(
            "--quiet", "--no-video-title-show", "--no-snapshot-preview"
        )

    def test_completed_download_only_rescans_visible_library(self):
        frame = SimpleNamespace(
            _closing=False,
            downloads_panel=mock.Mock(),
            queue=mock.Mock(),
            _last_counts=(0, 0, 1, 0),
            notebook=mock.Mock(),
            library_panel=mock.Mock(),
            announce=mock.Mock(),
            config={"auto_clear_finished": False},
        )
        frame.queue.counts.return_value = frame._last_counts
        item = SimpleNamespace(status=STATUS_DONE, title="Finished", seeding=False)

        frame.notebook.GetSelection.return_value = TAB_DOWNLOADS
        MainFrame._on_item_update(frame, item)
        frame.library_panel.refresh.assert_not_called()

        frame.notebook.GetSelection.return_value = TAB_LIBRARY
        MainFrame._on_item_update(frame, item)
        frame.library_panel.refresh.assert_called_once_with(announce=False)

    def test_automatic_clearing_keeps_failures_and_drops_finishes(self):
        frame = SimpleNamespace(
            _closing=False,
            downloads_panel=mock.Mock(),
            queue=mock.Mock(),
            _last_counts=(0, 0, 0, 0),
            notebook=mock.Mock(),
            library_panel=mock.Mock(),
            announce=mock.Mock(),
            config={"auto_clear_finished": True},
        )
        frame.queue.counts.return_value = frame._last_counts
        frame.notebook.GetSelection.return_value = TAB_DOWNLOADS

        MainFrame._on_item_update(
            frame, SimpleNamespace(status=STATUS_DONE, title="Got it",
                                   seeding=False))
        frame.queue.remove_completed.assert_called_once_with()

        # A torrent that is still seeding keeps its row, and so does a
        # failure: its error is only readable while the row is there.
        frame.queue.remove_completed.reset_mock()
        MainFrame._on_item_update(
            frame, SimpleNamespace(status=STATUS_DONE, title="Seeding",
                                   seeding=True))
        MainFrame._on_item_update(
            frame, SimpleNamespace(status=STATUS_ERROR, title="Broke",
                                   seeding=False, error="404"))
        frame.queue.remove_completed.assert_not_called()

    def test_library_discovers_audio_and_video_recursively(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "song.mp3").write_bytes(b"audio")
            (root / "notes.txt").write_text("not media", encoding="utf-8")
            nested = root / "Videos"
            nested.mkdir()
            (nested / "clip.MP4").write_bytes(b"video")

            items = discover_media(root)

        self.assertEqual([item["title"] for item in items], ["song", "clip"])
        self.assertEqual([item["kind"] for item in items], ["Audio", "Video"])
        self.assertEqual(items[1]["folder"], "Videos")

    def test_library_roots_includes_shared_folders_and_dedupes(self):
        media = os.path.abspath("C:\\Media")
        music = os.path.abspath("D:\\Music")
        roots = library_roots(
            {
                "download_dir": "C:\\Media",
                "soulseek_shared_folders": ["D:\\Music", "C:\\Media", "", "D:\\Music"],
            }
        )
        self.assertEqual([root["path"] for root in roots], [media, music])

    def test_library_discovers_every_file_and_subfolder_of_shared_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / "Media"
            media.mkdir()
            (media / "song.mp3").write_bytes(b"audio")
            (media / "notes.txt").write_text("note", encoding="utf-8")
            nested = media / "Nested"
            nested.mkdir()
            (nested / "deep.mkv").write_bytes(b"video")
            shared = root / "Shared"
            shared.mkdir()
            (shared / "cover.jpg").write_bytes(b"image")
            inner = shared / "Inner"
            inner.mkdir()
            (inner / "track.ogg").write_bytes(b"audio")

            result = discover_library(
                library_roots(
                    {
                        "download_dir": str(media),
                        "soulseek_shared_folders": [str(shared)],
                    }
                )
            )
            media_items = discover_media(media)

        names = result["names"]
        by_folder = {
            names[norm]: {record["name"]: record["kind"] for record in records}
            for norm, records in result["files"].items()
            if records
        }
        self.assertEqual(by_folder["Media"], {"notes.txt": "File", "song.mp3": "Audio"})
        self.assertEqual(by_folder["Nested"], {"deep.mkv": "Video"})
        self.assertEqual(by_folder["Shared"], {"cover.jpg": "File"})
        self.assertEqual(by_folder["Inner"], {"track.ogg": "Audio"})
        # The whole subfolder tree is indexed, not only the shared roots.
        self.assertEqual([names[n] for n in result["dirs"][result["roots"][0]]], ["Nested"])
        # discover_media still returns only media, for existing callers.
        self.assertEqual(sorted(item["title"] for item in media_items), ["deep", "song"])

    def test_library_refresh_runs_off_thread_and_coalesces_requests(self):
        file_list = mock.Mock()
        file_list.GetFirstSelected.return_value = -1
        panel = SimpleNamespace(
            _alive=True,
            _refreshing=False,
            _pending_refresh=None,
            _announce_refresh=False,
            list=file_list,
            frame=SimpleNamespace(config={"download_dir": "C:\\Media"}),
            _selected_norm=lambda: "",
            _selected_entry=lambda: None,
            _discover=mock.Mock(),
        )
        panel._start_refresh = lambda: LibraryPanel._start_refresh(panel)
        with mock.patch("blinddl.gui.library_panel.threading.Thread") as worker:
            LibraryPanel.refresh(panel, announce=False)
            LibraryPanel.refresh(panel, announce=False)

        worker.assert_called_once()
        worker.return_value.start.assert_called_once_with()
        self.assertTrue(panel._refreshing)
        roots, folder, file_path = panel._pending_refresh
        self.assertEqual(
            [root["path"] for root in roots], [os.path.abspath("C:\\Media")]
        )
        self.assertEqual(folder, "")
        self.assertIsNone(file_path)

    def test_library_supports_multi_selection_and_deletes_selected_files(self):
        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with tempfile.TemporaryDirectory() as folder:
            paths = [os.path.join(folder, name) for name in ("one.mp3", "two.mp3")]
            for path in paths:
                Path(path).write_bytes(b"audio")
            panel = LibraryPanel(self.host, self.frame)
            panel._visible = [
                {
                    "type": "file", "name": os.path.basename(path),
                    "path": path, "kind": "Audio", "size": 5,
                }
                for path in paths
            ]
            panel.roots = [os.path.normcase(os.path.abspath(folder))]
            panel._root_norms = set(panel.roots)
            panel.list.DeleteAllItems()
            for row, path in enumerate(paths):
                panel.list.InsertItem(row, os.path.basename(path))
                panel.list.Select(row)

            self.assertEqual(len(panel._selected_entries()), 2)
            with (
                mock.patch.object(wx, "MessageBox", return_value=wx.YES),
                mock.patch(
                    "blinddl.gui.library_panel.threading.Thread",
                    ImmediateThread,
                ),
                mock.patch.object(wx, "CallAfter", side_effect=lambda fn: fn()),
                mock.patch.object(panel, "refresh") as refresh,
            ):
                panel._on_delete(None)

            self.assertFalse(any(os.path.exists(path) for path in paths))
            refresh.assert_called_once_with(announce=False)
            panel.shutdown()
            panel.Destroy()

    def test_search_queues_adult_api_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ADULT
        item = {
            "title": "Example",
            "url": "https://xvideos.com/video.1",
            "provider": "xvideos",
            "kind": "adult",
        }
        panel.results = [item]
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)

        panel.on_download_selected(None)

        self.assertEqual(self.frame.queue.calls, [("adult", item, "Example")])

    def test_adult_search_respects_master_setting(self):
        panel = SearchPanel(self.host, self.frame)
        self.frame.config["adult_sites_enabled"] = False
        panel.query_text.SetValue("example")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_ADULT)
        )

        panel.on_search(None)

        self.assertEqual(
            self.frame.messages[-1],
            "Adult sites are disabled. Enable them in Settings.",
        )
        self.assertTrue(panel.search_btn.IsEnabled())

    def test_a_late_result_announces_the_site_it_came_from(self):
        panel = SearchPanel(self.host, self.frame)
        panel.token = token = object()
        panel.done = True
        panel.asked = ["Bandcamp"]

        panel._add_site(
            token,
            ENGINE_MUSIC,
            "Bandcamp",
            [{
                "title": "Late Arrival",
                "artist": "Someone",
                "source": "Bandcamp",
                "kind": "music",
            }],
        )

        self.assertIn("Bandcamp", self.frame.messages[-1])

    def test_search_dedup_uses_one_key_lookup_per_unique_result(self):
        panel = SearchPanel(self.host, self.frame)
        original = panel._dedup_key
        with mock.patch.object(panel, "_dedup_key", wraps=original) as dedup:
            for number in range(500):
                panel._insert_deduped(
                    {"title": f"Track {number}", "artist": "Artist"}
                )

        self.assertEqual(dedup.call_count, 500)

    def test_provider_results_are_coalesced_before_rendering(self):
        panel = SearchPanel(self.host, self.frame)
        panel.token = token = object()
        with mock.patch.object(panel, "_render_results") as render:
            panel._add_site(
                token, ENGINE_MUSIC, "One", [{"title": "One", "artist": "A"}]
            )
            panel._add_site(
                token, ENGINE_MUSIC, "Two", [{"title": "Two", "artist": "B"}]
            )
            render.assert_not_called()

            panel._flush_results()

        render.assert_called_once()

    def test_adult_combo_choices_follow_master_setting(self):
        self.frame.config["adult_sites_enabled"] = False
        panel = SearchPanel(self.host, self.frame)

        general = [ENGINE_LABELS[index] for index in GENERAL_ENGINES]
        self.assertEqual(
            [
                panel.engine_choice.GetString(index)
                for index in range(panel.engine_choice.GetCount())
            ],
            general,
        )

        self.frame.config["adult_sites_enabled"] = True
        panel.refresh_engine_choices()
        self.assertEqual(
            [
                panel.engine_choice.GetString(index)
                for index in range(panel.engine_choice.GetCount())
            ],
            general
            + [ENGINE_LABELS[index] for index in ADULT_ENGINE_CATEGORIES],
        )

        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_TRANS)
        )
        self.frame.config["adult_sites_enabled"] = False
        panel.refresh_engine_choices()
        self.assertEqual(panel.engine_choice.GetCount(), GENERAL_ENGINE_COUNT)
        self.assertEqual(panel.engine_choice.GetSelection(), ENGINE_MUSIC)

        self.frame.config["soulseek_enabled"] = True
        panel.refresh_engine_choices()
        self.assertEqual(
            [
                panel.engine_choice.GetString(index)
                for index in range(panel.engine_choice.GetCount())
            ],
            general
            + [
                ENGINE_LABELS[ENGINE_SOULSEEK_AUDIO],
                ENGINE_LABELS[ENGINE_SOULSEEK_VIDEO],
                ENGINE_LABELS[ENGINE_SOULSEEK_BOOKS],
                ENGINE_LABELS[ENGINE_SOULSEEK_TORRENTS],
            ],
        )

    def test_search_sort_choices_and_ordering(self):
        panel = SearchPanel(self.host, self.frame)
        self.assertEqual(
            [
                panel.sort_choice.GetString(index)
                for index in range(panel.sort_choice.GetCount())
            ],
            SORT_LABELS,
        )
        items = [
            {
                "title": "Zulu",
                "artist": "Beta",
                "source": "Site B",
                "duration_s": 90,
                "_search_order": 0,
            },
            {
                "title": "Alpha",
                "artist": "Gamma",
                "source": "Site A",
                "duration_s": None,
                "_search_order": 1,
            },
            {
                "title": "Bravo",
                "artist": "Alpha",
                "source": "Site B",
                "duration_s": 30,
                "_search_order": 2,
            },
        ]

        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_RELEVANCE)],
            ["Zulu", "Alpha", "Bravo"],
        )
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_NAME)],
            ["Alpha", "Bravo", "Zulu"],
        )
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_SITE)],
            ["Alpha", "Bravo", "Zulu"],
        )
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_ARTIST)],
            ["Bravo", "Zulu", "Alpha"],
        )
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_SHORTEST)],
            ["Bravo", "Zulu", "Alpha"],
        )
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_LONGEST)],
            ["Zulu", "Bravo", "Alpha"],
        )

    def test_search_order_is_saved_without_searching_on_every_step(self):
        # Arrowing through the choices used to fire a search per step and
        # throw the focus into the results at the end of each one, so the
        # list could not be walked to the option wanted.
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("dragnet")
        panel.order_choice.SetSelection(
            search_order.ORDERS.index(search_order.ORDER_RECENT)
        )

        with mock.patch.object(panel, "on_search") as search:
            panel.on_order_changed(None)

        self.assertEqual(self.frame.config["search_order"], search_order.ORDER_RECENT)
        search.assert_not_called()
        self.assertEqual(
            self.frame.messages[-1],
            "Search order set to Most recent. Press Enter to search.",
        )

    def test_a_setting_change_only_offers_enter_when_there_is_a_query(self):
        panel = SearchPanel(self.host, self.frame)
        panel.order_choice.SetSelection(
            search_order.ORDERS.index(search_order.ORDER_POPULAR)
        )

        with mock.patch.object(panel, "on_search"):
            panel.on_order_changed(None)

        # Nothing typed yet, so there is nothing for Enter to search.
        self.assertEqual(
            self.frame.messages[-1], "Search order set to Most popular."
        )

    def test_enter_searches_from_any_control_on_the_search_row(self):
        panel = SearchPanel(self.host, self.frame)
        controls = (
            panel.engine_choice,
            panel.kind_choice,
            panel.order_choice,
            panel.sort_choice,
        )
        # A key event cannot be built in-process convincingly enough to send
        # through the control -- wx keeps the key code on the C++ side -- so
        # the handler is driven directly here and the binding itself is
        # covered by pressing real keys against a running window.
        self.assertTrue(controls)
        for control in controls:
            event = mock.Mock()
            event.GetKeyCode.return_value = wx.WXK_RETURN
            event.GetEventObject.return_value = control
            with mock.patch.object(panel, "on_search") as search:
                panel.on_row_key(event)
            search.assert_called_once_with(None)
            # Skipped as well, so an open dropdown still commits the choice
            # the Enter was meant to pick.
            event.Skip.assert_called_once_with()

        # Arrow keys are left alone: they are how the choice is walked.
        event = mock.Mock()
        event.GetKeyCode.return_value = wx.WXK_DOWN
        event.GetEventObject.return_value = panel.order_choice
        with mock.patch.object(panel, "on_search") as search:
            panel.on_row_key(event)
        search.assert_not_called()
        event.Skip.assert_called_once_with()

    def test_enter_on_an_open_list_picks_the_item_without_searching(self):
        # Walking a combo box open and pressing Enter chooses what is being
        # read. Searching on that Enter takes the focus into the results
        # before the choice has even been made.
        panel = SearchPanel(self.host, self.frame)
        event = mock.Mock()
        event.GetKeyCode.return_value = wx.WXK_RETURN
        event.GetEventObject.return_value = panel.order_choice

        panel.on_dropdown_opened(
            SimpleNamespace(GetEventObject=lambda: panel.order_choice,
                            Skip=lambda: None))
        with mock.patch.object(panel, "on_search") as search:
            panel.on_row_key(event)
        search.assert_not_called()
        # Still skipped, so the native list commits the choice as usual.
        event.Skip.assert_called_once_with()

        # Closing the list hands Enter back to the search.
        panel.on_dropdown_closed(
            SimpleNamespace(GetEventObject=lambda: panel.order_choice,
                            Skip=lambda: None))
        event = mock.Mock()
        event.GetKeyCode.return_value = wx.WXK_RETURN
        event.GetEventObject.return_value = panel.order_choice
        with mock.patch.object(panel, "on_search") as search:
            panel.on_row_key(event)
        search.assert_called_once_with(None)

    def test_sorting_rearranges_the_list_without_searching_again(self):
        # Sort by is a display control; the rows it reorders have already
        # arrived, so it never goes back to the sites.
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_MUSIC
        panel.results = [
            {"title": "Zulu", "_search_order": 0},
            {"title": "Alpha", "_search_order": 1},
        ]
        panel._render_results(ENGINE_MUSIC)
        panel.sort_choice.SetSelection(SORT_NAME)

        with mock.patch.object(panel, "on_search") as search:
            panel.on_sort_changed(None)

        search.assert_not_called()
        self.assertEqual(panel.results_list.GetItemText(0), "Alpha")

    def test_search_status_names_sources_that_cannot_honour_order(self):
        self.assertEqual(
            _order_phrase(search_order.ORDER_POPULAR, ["Bandcamp"], 2),
            "1 site cannot sort by most popular, so it answered by best "
            "match: Bandcamp.",
        )

    def test_search_type_is_offered_only_where_a_music_field_exists(self):
        panel = SearchPanel(self.host, self.frame)
        panel.current_kind = search_kind.KIND_ALBUM

        for engine in (ENGINE_MUSIC, ENGINE_DEEZER, ENGINE_APPLE_MUSIC):
            panel._apply_engine_controls(engine)
            self.assertTrue(panel.kind_choice.IsEnabled())
            self.assertEqual(
                panel.kind_choice.GetSelection(),
                search_kind.KINDS.index(search_kind.KIND_ALBUM),
            )

        # A book library or a torrent indexer has no album or artist field,
        # so the control is switched off rather than left offering choices
        # that could not change the answer.
        for engine in (ENGINE_BOOKS, ENGINE_TORRENTS, ENGINE_ARCHIVE_AUDIO):
            panel._apply_engine_controls(engine)
            self.assertFalse(panel.kind_choice.IsEnabled())
            self.assertEqual(
                panel.kind_choice.GetSelection(),
                search_kind.KINDS.index(search_kind.KIND_BEST),
            )
            panel.engine_choice.SetSelection(
                panel.visible_engines.index(engine)
            )
            self.assertEqual(panel._selected_kind(), search_kind.KIND_BEST)

    def test_search_type_is_saved_without_searching_on_every_step(self):
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("discovery")
        panel.kind_choice.SetSelection(
            search_kind.KINDS.index(search_kind.KIND_ALBUM)
        )

        with mock.patch.object(panel, "on_search") as search:
            panel.on_kind_changed(None)

        self.assertEqual(self.frame.config["search_kind"], search_kind.KIND_ALBUM)
        search.assert_not_called()
        self.assertEqual(
            self.frame.messages[-1],
            "Search type set to Album. Press Enter to search.",
        )

    def test_album_search_asks_only_the_sites_that_have_albums(self):
        panel = SearchPanel(self.host, self.frame)
        stop = threading.Event()
        token = panel.token = object()

        with (
            mock.patch.object(musicdl_backend, "search") as musicdl_search,
            mock.patch.object(panel, "_sideb_search") as sideb_search,
            mock.patch.object(deezer_backend, "search", return_value=[]),
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search("discovery", ENGINE_MUSIC, token, stop, ["netease"],
                          search_order.ORDER_RELEVANCE, search_kind.KIND_ALBUM)
            # Wait for the Deezer worker this branch starts on its own.
            for thread in threading.enumerate():
                if thread.name == "search-deezer":
                    thread.join(timeout=5)

        # The musicdl sites and Side B match song titles only; asking them
        # would answer an album search with several hundred tracks.
        musicdl_search.assert_not_called()
        sideb_search.assert_not_called()

    def test_track_search_still_asks_every_music_site(self):
        panel = SearchPanel(self.host, self.frame)
        stop = threading.Event()
        token = panel.token = object()

        with (
            mock.patch.object(
                musicdl_backend, "search", return_value=([], [], [])
            ) as musicdl_search,
            mock.patch.object(panel, "_sideb_search") as sideb_search,
            mock.patch.object(deezer_backend, "search", return_value=[]) as deezer,
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search("discovery", ENGINE_MUSIC, token, stop, ["netease"],
                          search_order.ORDER_RELEVANCE, search_kind.KIND_TRACK)
            for thread in threading.enumerate():
                if thread.name == "search-deezer":
                    thread.join(timeout=5)

        musicdl_search.assert_called_once()
        sideb_search.assert_called_once()
        self.assertEqual(deezer.call_args.kwargs["kind"], search_kind.KIND_TRACK)

    def test_search_status_says_which_sites_could_search_by_type(self):
        # Album leaves the sites that cannot answer it out of the search...
        self.assertEqual(
            _kind_phrase(search_kind.KIND_ALBUM, ["Deezer"], ["Netease", "QQ"]),
            "Only Deezer can search by album, so the other 2 sites were not "
            "asked.",
        )
        # ...while the types that only narrow the matching still ask them.
        self.assertEqual(
            _kind_phrase(search_kind.KIND_ARTIST, ["Deezer"], ["Netease"]),
            "1 site cannot search by artist, so it answered by best match.",
        )
        self.assertEqual(
            _kind_phrase(search_kind.KIND_TRACK, [], ["Bandcamp"]),
            "No site here can search by track title; showing best match.",
        )
        self.assertEqual(_kind_phrase(search_kind.KIND_BEST, [], ["Bandcamp"]), "")

    def test_only_the_catalogue_sites_can_search_a_named_field(self):
        able, unable = _kind_capable_sources(
            ENGINE_MUSIC, ["netease", "Deezer", "Deezer (Side B)"],
            search_kind.KIND_ALBUM,
        )
        self.assertEqual((able, unable),
                         (["Deezer"], ["netease", "Deezer (Side B)"]))
        # Best match is what every site does when asked for nothing, so
        # nothing is ever named as unable to do it.
        self.assertEqual(
            _kind_capable_sources(
                ENGINE_MUSIC, ["netease"], search_kind.KIND_BEST),
            (["netease"], []),
        )
        self.assertEqual(
            _kind_capable_sources(
                ENGINE_APPLE_MUSIC, ["Apple Music"], search_kind.KIND_ALBUM),
            (["Apple Music"], []),
        )
        self.assertEqual(
            _kind_capable_sources(
                ENGINE_BOOKS, ["Anna's Archive"], search_kind.KIND_ALBUM),
            ([], ["Anna's Archive"]),
        )

    def test_query_order_is_not_confused_with_a_books_original_year(self):
        self.assertEqual(
            _sort_for_order(ENGINE_BOOKS, search_order.ORDER_RECENT),
            SORT_RELEVANCE,
        )
        self.assertEqual(
            _sort_for_order(ENGINE_ARCHIVE_AUDIO, search_order.ORDER_RECENT),
            SORT_RELEVANCE,
        )

    def test_search_sort_change_preserves_selected_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_MUSIC
        panel.results = [
            {"title": "Zulu", "source": "Two", "_search_order": 0},
            {"title": "Alpha", "source": "One", "_search_order": 1},
        ]
        panel._render_results(ENGINE_MUSIC)
        panel.results_list.Select(0)
        panel.results_list.Focus(0)
        panel.sort_choice.SetSelection(SORT_NAME)

        panel.on_sort_changed(None)

        self.assertEqual(panel.results_list.GetItemText(0), "Alpha")
        self.assertEqual(panel.results_list.GetItemText(1), "Zulu")
        self.assertTrue(panel.results_list.IsSelected(1))
        self.assertEqual(panel.results_list.GetFocusedItem(), 1)
        self.assertEqual(self.frame.messages[-1], "Sorted 2 results by Name.")

    def test_search_sort_reindexes_late_duplicate_replacement(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_MUSIC
        panel._insert_deduped(
            {"title": "Zulu", "artist": "Artist", "format": "MP3"}
        )
        panel._insert_deduped(
            {"title": "Alpha", "artist": "Artist", "format": "MP3"}
        )
        panel._render_results(ENGINE_MUSIC)
        panel.sort_choice.SetSelection(SORT_NAME)
        panel.on_sort_changed(None)

        panel._insert_deduped(
            {"title": "Zulu", "artist": "Artist", "format": "FLAC"}
        )

        self.assertEqual([item["title"] for item in panel.results], ["Alpha", "Zulu"])
        self.assertEqual(panel.results[1]["format"], "FLAC")

    def test_each_adult_combo_choice_routes_its_category(self):
        panel = SearchPanel(self.host, self.frame)
        stop = threading.Event()
        for engine, category in ADULT_ENGINE_CATEGORIES.items():
            token = object()
            panel.token = token
            with (
                mock.patch.object(
                    adult_backend, "search", return_value=([], [], [])
                ) as search,
                mock.patch.object(wx, "CallAfter"),
            ):
                panel._search("example", engine, token, stop, ["pornhub"])

            self.assertEqual(search.call_args.kwargs["category"], category)

    def test_soulseek_sections_are_exclusive_searches(self):
        self.frame.config["soulseek_enabled"] = True
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("ambient")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_SOULSEEK_AUDIO)
        )
        panel.on_engine_changed(wx.CommandEvent())

        self.assertEqual(
            [
                panel.sort_choice.GetString(index)
                for index in range(panel.sort_choice.GetCount())
            ],
            SOULSEEK_SORT_LABELS,
        )
        panel._apply_engine_columns(ENGINE_SOULSEEK_AUDIO)
        self.assertEqual(
            [
                panel.results_list.GetColumn(index).GetText()
                for index in range(panel.results_list.GetColumnCount())
            ],
            list(SOULSEEK_COLUMN_HEADINGS),
        )

        with mock.patch.object(threading, "Thread") as worker:
            panel.on_search(None)

        self.assertEqual(worker.call_args.kwargs["args"][1], ENGINE_SOULSEEK_AUDIO)
        self.assertIn("Soulseek music and audio", self.frame.messages[-1])

        token = panel.token = object()
        stop = threading.Event()
        with (
            mock.patch.object(soulseek_backend, "search", return_value=[]) as search,
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search(
                "ambient",
                ENGINE_SOULSEEK_AUDIO,
                token,
                stop,
                [],
                search_order.ORDER_RELEVANCE,
            )
        self.assertEqual(search.call_args.args[2], "audio")

        sizes = [
            {"title": "Large", "size_bytes": 200, "_search_order": 0},
            {"title": "Small", "size_bytes": 10, "_search_order": 1},
        ]
        self.assertEqual(
            [
                item["title"]
                for item in _sorted_results(sizes, SORT_SHORTEST, ENGINE_SOULSEEK_AUDIO)
            ],
            ["Small", "Large"],
        )

    def test_soulseek_search_streams_results_silently_and_stops(self):
        self.frame.config["soulseek_enabled"] = True
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("ambient")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_SOULSEEK_AUDIO)
        )
        panel.on_engine_changed(wx.CommandEvent())

        with mock.patch.object(threading, "Thread"):
            panel.on_search(None)

        # The search has no deadline and never mentions a timeout.
        self.assertTrue(panel._soulseek_streaming)
        self.assertIn("arrive as they come", self.frame.messages[-1])
        self.assertNotIn("seconds", self.frame.messages[-1])

        # A streamed batch lands in the list without an announcement, so the
        # screen reader is not flooded while the search keeps running.
        batch = [
            {
                "title": "Track",
                "kind": "soulseek",
                "username": "peer",
                "remote_path": "Music\\Track.mp3",
                "format": "MP3",
                "file_size": "1.0 MB",
                "size_bytes": 1024,
                "has_free_slots": True,
                "average_speed": 100,
                "queue_size": 0,
                "seeders": 1,
                "leechers": 0,
            }
        ]
        messages_before = len(self.frame.messages)
        panel._add_soulseek_batch(panel.token, batch)
        self.assertEqual(len(panel.results), 1)
        self.assertEqual(len(self.frame.messages), messages_before)

        with mock.patch.object(panel.stop, "set") as stop_set:
            panel.on_stop_search(None)
        stop_set.assert_called_once_with()
        self.assertFalse(panel._soulseek_streaming)
        self.assertEqual(self.frame.messages[-1], "Search stopped.")

    def test_deezer_choice_searches_and_downloads_only_deezer(self):
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("ambient")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_DEEZER)
        )
        panel.on_engine_changed(wx.CommandEvent())

        self.assertEqual(panel.engine_choice.GetString(0), "Music sites")
        self.assertEqual(panel.engine_choice.GetString(1), "Deezer")

        deezer_items = [
            {
                "title": "One",
                "kind": "deezer",
                "url": "https://www.deezer.com/track/1",
                "source": "Deezer",
            }
        ]
        token = panel.token = object()
        stop = threading.Event()
        with (
            mock.patch.object(
                deezer_backend, "search", return_value=deezer_items
            ) as search,
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search(
                "ambient",
                ENGINE_DEEZER,
                token,
                stop,
                [],
                search_order.ORDER_RELEVANCE,
            )

        self.assertEqual(search.call_args.args, ("ambient", self.frame.config))

        # Deezer has no release date, so "most recent" reports it unable.
        self.assertEqual(
            _order_capable_sources(
                ENGINE_DEEZER,
                ["Deezer"],
                search_order.ORDER_RECENT,
                self.frame.config,
            ),
            ([], ["Deezer"]),
        )

        # A Deezer result downloads through the Deezer-capable queue path.
        self.frame.queue.calls = []
        panel.result_engine = ENGINE_DEEZER
        panel.results = deezer_items
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)
        panel.on_download_selected(None)
        self.assertEqual(
            self.frame.queue.calls,
            [("sideb", deezer_items[0]["url"], deezer_items[0]["title"])],
        )

    def test_apple_music_choice_searches_and_downloads(self):
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("ambient")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_APPLE_MUSIC)
        )
        panel.on_engine_changed(wx.CommandEvent())

        apple_items = [
            {
                "title": "One",
                "kind": "applemusic",
                "url": "https://music.apple.com/us/song/1",
                "source": "Apple Music",
            }
        ]
        token = panel.token = object()
        stop = threading.Event()
        with (
            mock.patch.object(
                applemusic_backend, "search", return_value=apple_items
            ) as search,
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search(
                "ambient",
                ENGINE_APPLE_MUSIC,
                token,
                stop,
                [],
                search_order.ORDER_RELEVANCE,
            )

        self.assertEqual(search.call_args.args, ("ambient", self.frame.config))

        # An Apple Music result downloads through its own queue path.
        self.frame.queue.calls = []
        panel.result_engine = ENGINE_APPLE_MUSIC
        panel.results = apple_items
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)
        panel.on_download_selected(None)
        self.assertEqual(
            self.frame.queue.calls,
            [("applemusic", apple_items[0]["url"], apple_items[0]["title"])],
        )

    def test_music_archive_and_adult_searches_never_call_soulseek(self):
        self.frame.config["soulseek_enabled"] = True
        panel = SearchPanel(self.host, self.frame)
        stop = threading.Event()
        token = panel.token = object()

        with (
            mock.patch.object(archive_backend, "search", return_value=([], [], [])),
            mock.patch.object(adult_backend, "search", return_value=([], [], [])),
            mock.patch.object(musicdl_backend, "search", return_value=([], [], [])),
            mock.patch.object(soulseek_backend, "search") as soulseek_search,
            mock.patch.object(threading, "Thread"),
            mock.patch.object(wx, "CallAfter"),
        ):
            panel._search(
                "music",
                ENGINE_MUSIC,
                token,
                stop,
                ["example"],
                search_order.ORDER_RELEVANCE,
            )
            panel._search(
                "radio",
                ENGINE_ARCHIVE_AUDIO,
                token,
                stop,
                ["audio_music"],
                search_order.ORDER_RELEVANCE,
            )
            panel._search(
                "movie",
                ENGINE_ARCHIVE_VIDEO,
                token,
                stop,
                ["movies"],
                search_order.ORDER_RELEVANCE,
            )
            panel._search(
                "adult",
                ENGINE_STRAIGHT,
                token,
                stop,
                ["pornhub"],
                search_order.ORDER_RELEVANCE,
            )

        soulseek_search.assert_not_called()

    def test_book_engine_relabels_columns_and_sort_choices(self):
        panel = SearchPanel(self.host, self.frame)
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_BOOKS)
        )
        panel.on_engine_changed(wx.CommandEvent())

        self.assertEqual(
            [
                panel.sort_choice.GetString(index)
                for index in range(panel.sort_choice.GetCount())
            ],
            BOOK_SORT_LABELS,
        )
        # Nothing to play, so the preview button must not offer itself.
        self.assertFalse(panel.preview_btn.IsEnabled())

        panel._apply_engine_columns(ENGINE_BOOKS)
        self.assertEqual(
            [
                panel.results_list.GetColumn(index).GetText()
                for index in range(panel.results_list.GetColumnCount())
            ],
            list(BOOK_COLUMN_HEADINGS),
        )

        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_MUSIC)
        )
        panel.on_engine_changed(wx.CommandEvent())
        panel._apply_engine_columns(ENGINE_MUSIC)
        self.assertEqual(
            [
                panel.results_list.GetColumn(index).GetText()
                for index in range(panel.results_list.GetColumnCount())
            ],
            list(COLUMN_HEADINGS),
        )
        self.assertTrue(panel.preview_btn.IsEnabled())

    def test_media_engines_relabel_their_own_columns(self):
        panel = SearchPanel(self.host, self.frame)
        for engine, headings in (
            (ENGINE_AUDIOBOOKS, AUDIOBOOK_COLUMN_HEADINGS),
            (ENGINE_ARCHIVE_AUDIO, ARCHIVE_COLUMN_HEADINGS),
            (ENGINE_ARCHIVE_VIDEO, ARCHIVE_COLUMN_HEADINGS),
        ):
            panel._apply_engine_columns(engine)
            self.assertEqual(
                [
                    panel.results_list.GetColumn(index).GetText()
                    for index in range(panel.results_list.GetColumnCount())
                ],
                list(headings),
            )
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_ARCHIVE_VIDEO)
        )
        panel.on_engine_changed(wx.CommandEvent())
        self.assertEqual(
            [
                panel.sort_choice.GetString(index)
                for index in range(panel.sort_choice.GetCount())
            ],
            ARCHIVE_SORT_LABELS,
        )

    def test_year_sorts_replace_duration_sorts_for_books(self):
        items = [
            {"title": "Middle", "year": "1900", "_search_order": 0},
            {"title": "Newest", "year": "2001", "_search_order": 1},
            {"title": "Undated", "year": "", "_search_order": 2},
            {"title": "Oldest", "year": "1851", "_search_order": 3},
        ]

        self.assertEqual(
            [
                item["title"]
                for item in _sorted_results(items, SORT_SHORTEST, ENGINE_BOOKS)
            ],
            ["Oldest", "Middle", "Newest", "Undated"],
        )
        self.assertEqual(
            [
                item["title"]
                for item in _sorted_results(items, SORT_LONGEST, ENGINE_ARCHIVE_VIDEO)
            ],
            ["Newest", "Middle", "Oldest", "Undated"],
        )

    def test_search_queues_book_and_audiobook_results(self):
        panel = SearchPanel(self.host, self.frame)
        for engine, kind in ((ENGINE_BOOKS, "book"), (ENGINE_AUDIOBOOKS, "audiobook")):
            self.frame.queue.calls = []
            panel.result_engine = engine
            item = {"title": "One", "kind": kind, "identifier": "one"}
            panel.results = [item]
            panel.results_list.SetItemCount(len(panel.results))
            panel.results_list.Select(0)

            panel.on_download_selected(None)

            self.assertEqual(self.frame.queue.calls, [(kind, item, "One")])

    def test_archive_item_with_one_file_queues_without_asking(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ARCHIVE_AUDIO
        item = {
            "title": "Dragnet",
            "kind": "archive",
            "identifier": "dragnet",
            "video": False,
        }
        files = [
            {
                "title": "Episode 1",
                "file_name": "ep1.mp3",
                "identifier": "dragnet",
                "direct_url": "https://archive.org/download/dragnet/ep1.mp3",
            }
        ]

        panel._archive_files_ready(panel.archive_token, item, files)

        self.assertEqual(len(self.frame.queue.calls), 1)
        kind, payload, title = self.frame.queue.calls[0]
        self.assertEqual((kind, title), ("archive", "Episode 1"))
        # The show's name rides along so the episode lands in its own folder.
        self.assertEqual(payload["collection_title"], "Dragnet")

    def test_archive_item_with_many_files_offers_a_picker(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ARCHIVE_AUDIO
        item = {
            "title": "Dragnet",
            "kind": "archive",
            "identifier": "dragnet",
            "video": False,
        }
        files = [
            {
                "title": f"Episode {number}",
                "file_name": f"ep{number}.mp3",
                "identifier": "dragnet",
                "direct_url": f"https://x/ep{number}.mp3",
            }
            for number in (1, 2, 3)
        ]

        with (
            mock.patch.object(ItemPickerDialog, "ShowModal", return_value=wx.ID_OK),
            mock.patch.object(
                ItemPickerDialog, "selected_items", return_value=files[:2]
            ),
        ):
            panel._archive_files_ready(panel.archive_token, item, files)

        self.assertEqual(
            [call[2] for call in self.frame.queue.calls], ["Episode 1", "Episode 2"]
        )
        self.assertEqual(self.frame.messages[-1], "Queued 2 downloads.")

    def test_album_row_is_resolved_to_its_tracks_before_queueing(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        album = {
            "title": "Discovery",
            "kind": "deezer_album",
            "url": "https://www.deezer.com/album/7",
            "format": "Album, 2 tracks",
        }
        panel.results = [album]
        panel.results_list.SetItemCount(1)
        panel.results_list.Select(0)

        with mock.patch.object(panel, "_queue_album_items") as queue_albums:
            panel.on_download_selected(None)

        queue_albums.assert_called_once_with([album])
        # Nothing reached the queue directly: an album has to be read first.
        self.assertEqual(self.frame.queue.calls, [])

    def test_album_download_queues_every_track_the_user_keeps(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        album = {
            "title": "Discovery",
            "kind": "deezer_album",
            "url": "https://www.deezer.com/album/7",
        }
        tracks = [
            {"title": f"Track {number}",
             "url": f"https://www.deezer.com/track/{number}"}
            for number in (1, 2, 3)
        ]

        with (
            mock.patch.object(ItemPickerDialog, "ShowModal", return_value=wx.ID_OK),
            mock.patch.object(
                ItemPickerDialog, "selected_items", return_value=tracks[:2]
            ),
        ):
            panel._album_tracks_ready(
                panel.album_token, [(album, tracks)], []
            )

        self.assertEqual(
            [(call[0], call[2]) for call in self.frame.queue.calls],
            [("sideb", "Track 1"), ("sideb", "Track 2")],
        )
        self.assertEqual(self.frame.messages[-1], "Queued 2 downloads.")

    def test_an_album_lands_in_a_folder_named_for_artist_and_album(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        album = {
            "title": "Discovery",
            "artist": "Daft Punk",
            "kind": "deezer_album",
            "url": "https://www.deezer.com/album/7",
        }
        tracks = [{"title": "One", "url": "https://www.deezer.com/track/1"}]

        panel._album_tracks_ready(panel.album_token, [(album, tracks)], [])

        self.assertEqual(self.frame.queue.folders, ["Daft Punk - Discovery"])

        # An album row with no artist named still gets its own folder.
        self.frame.queue.folders.clear()
        panel._album_tracks_ready(
            panel.album_token,
            [({"title": "Untitled", "kind": "deezer_album"}, tracks)],
            [],
        )
        self.assertEqual(self.frame.queue.folders, ["Untitled"])

    def test_an_artist_search_files_its_downloads_under_the_artist(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        panel.kind_used = search_kind.KIND_ARTIST
        panel.results = [{
            "title": "One More Time",
            "artist": "Daft Punk",
            "kind": "deezer",
            "url": "https://www.deezer.com/track/1",
        }]

        with mock.patch.object(panel, "_selected_indices", return_value=[0]):
            panel.on_download_selected(None)
        self.assertEqual(self.frame.queue.folders, ["Daft Punk"])

        # A best-match search is not about anyone in particular.
        self.frame.queue.folders.clear()
        panel.kind_used = search_kind.KIND_BEST
        with mock.patch.object(panel, "_selected_indices", return_value=[0]):
            panel.on_download_selected(None)
        self.assertEqual(self.frame.queue.folders, [""])

    def test_several_albums_are_queued_whole_without_a_picker(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_APPLE_MUSIC
        resolved = [
            ({"title": "First", "kind": "applemusic_album"},
             [{"title": "One", "url": "https://music.apple.com/us/song/1"}]),
            ({"title": "Second", "kind": "applemusic_album"},
             [{"title": "Two", "url": "https://music.apple.com/us/song/2"}]),
        ]

        panel._album_tracks_ready(panel.album_token, resolved, [])

        self.assertEqual(
            [(call[0], call[2]) for call in self.frame.queue.calls],
            [("applemusic", "One"), ("applemusic", "Two")],
        )

    def test_an_album_that_cannot_be_read_is_reported_not_silently_dropped(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        token = panel.album_token = object()

        with mock.patch.object(
            deezer_backend, "extract_flat", side_effect=RuntimeError("gone")
        ), mock.patch.object(wx, "CallAfter") as call_after:
            panel._resolve_album_tracks(
                token,
                [{"title": "Discovery", "kind": "deezer_album", "url": "u"}],
            )

        _method, _token, resolved, errors = call_after.call_args.args
        self.assertEqual(resolved, [])
        self.assertEqual(errors, ["Discovery: gone"])

    def test_albums_cannot_be_previewed(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        panel.results = [{"title": "Discovery", "kind": "deezer_album"}]
        panel.results_list.SetItemCount(1)
        panel.results_list.Select(0)

        panel.on_preview_selected(None)

        self.assertIn("no single track to play", self.frame.messages[-1])

    def test_books_cannot_be_previewed(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_BOOKS
        panel.results = [{"title": "One"}]
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)

        panel.on_preview_selected(None)

        self.assertIn("cannot be previewed", self.frame.messages[-1])

    def test_settings_adult_sites_default_off_and_auth_paths_save(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertFalse(dialog.adult_sites_check.GetValue())
        self.assertFalse(dialog.onlyfans_auth_picker.IsEnabled())
        self.assertFalse(dialog.justforfans_auth_picker.IsEnabled())
        dialog.adult_sites_check.SetValue(True)
        dialog.cookies_choice.SetSelection(1)
        dialog.onlyfans_auth_picker.SetPath("onlyfans.json")
        dialog.justforfans_auth_picker.SetPath("justforfans.json")
        dialog.apply()

        self.assertTrue(config["adult_sites_enabled"])
        self.assertEqual(config["cookies_from_browser"], "chrome")
        self.assertEqual(config["onlyfans_auth_file"], "onlyfans.json")
        self.assertEqual(config["justforfans_auth_file"], "justforfans.json")
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_settings_opens_on_the_download_folder_not_the_ok_button(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)
        folder_box = dialog.first_control()

        # ShowModal sends this once the dialog's default button has taken
        # the focus, which is what used to leave Settings on OK -- past
        # every setting in it. Where the focus really lands cannot be read
        # back from a dialog that was never shown: wxGTK leaves it where it
        # was and wxOSX reports nothing at all, so what is checked here is
        # the control this asks for.
        with mock.patch.object(folder_box, "SetFocus") as set_focus:
            dialog.InitDialog()

        set_focus.assert_called_once_with()
        self.assertEqual(dialog.notebook.GetSelection(), 0)
        self.assertEqual(dialog.notebook.GetPageText(0), "Downloads")
        # The edit box inside the picker carries the name too, so the
        # control the focus lands in is not an unlabelled one.
        self.assertEqual(folder_box.GetName(), "Download folder")
        # And the box is asked for outright, because wxGTK's default picker
        # is a bare Browse button with no way to read or type the path.
        self.assertIsNotNone(dialog.dir_picker.GetTextCtrl())
        dialog.Destroy()

    def test_automatic_install_is_off_by_default_and_follows_the_check(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        # Checking is on by default; installing without being asked is not.
        self.assertTrue(dialog.update_check.GetValue())
        self.assertFalse(dialog.auto_install_check.GetValue())
        self.assertTrue(dialog.auto_install_check.IsEnabled())

        # Nothing to install automatically if nothing is looking.
        dialog.update_check.SetValue(False)
        dialog.update_check.ProcessEvent(
            wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.update_check.GetId())
        )
        self.assertFalse(dialog.auto_install_check.IsEnabled())

        dialog.update_check.SetValue(True)
        dialog.auto_install_check.SetValue(True)
        dialog.apply()

        self.assertTrue(config["auto_install_update"])
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_speak_status_checkbox_defaults_on_and_saves(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertTrue(dialog.speak_status_check.GetValue())
        dialog.speak_status_check.SetValue(False)
        dialog.apply()

        self.assertFalse(config["speak_status"])
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_status_messages_are_spoken_once_and_only_when_wanted(self):
        holder = SimpleNamespace(
            _closing=False,
            config={"speak_status": True},
            SetStatusText=mock.Mock(),
        )
        with mock.patch.object(speech, "announce") as spoken:
            MainFrame.announce(holder, "12 results found.")
            MainFrame.announce(holder, "Download failed.", speak=False)
            holder.config["speak_status"] = False
            MainFrame.announce(holder, "Settings saved.")

        # The status bar still carries everything, so NVDA+End reads the
        # last thing that happened whether or not it was said out loud.
        self.assertEqual(
            [call.args[0] for call in holder.SetStatusText.call_args_list],
            ["12 results found.", "Download failed.", "Settings saved."],
        )
        spoken.assert_called_once_with("12 results found.")

    def test_the_same_status_message_is_not_spoken_twice(self):
        speech.reset()
        self.addCleanup(speech.reset)
        with mock.patch.object(speech, "speak", return_value=True) as spoke:
            self.assertTrue(speech.announce("Still searching 5 sites."))
            self.assertFalse(speech.announce("Still searching 5 sites."))
            self.assertTrue(speech.announce("12 results found."))

        self.assertEqual(
            [call.args[0] for call in spoke.call_args_list],
            ["Still searching 5 sites.", "12 results found."],
        )

    def test_speech_stays_silent_when_it_has_nobody_to_talk_to(self):
        speech.reset()
        self.addCleanup(speech.reset)
        with mock.patch.dict(os.environ, {"BLINDDL_NO_SPEECH": "1"}):
            self.assertFalse(speech.speak("Anything at all."))
        self.assertFalse(speech.speak(""))

    def test_start_maximized_checkbox_saves(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertFalse(dialog.start_maximized_check.GetValue())
        dialog.start_maximized_check.SetValue(True)
        dialog.apply()

        self.assertTrue(config["start_maximized"])
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_accounts_page_has_arl_paste_button(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertEqual(dialog.arl_paste_btn.GetLabel(), "&Paste")
        self.assertEqual(dialog.arl_text.GetName(), "Deezer ARL cookie")
        dialog.Destroy()

    def test_deezer_format_defaults_to_flac_and_saves(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        # FLAC is the first choice and the default.
        self.assertEqual(dialog.deezer_format_choice.GetSelection(), 0)
        self.assertEqual(dialog.deezer_format_choice.GetStringSelection(),
                         DEEZER_FORMAT_CHOICES[0][0])
        dialog.deezer_format_choice.SetSelection(1)
        dialog.apply()

        self.assertEqual(config["deezer_format"], "mp3_320")
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_failed_cookie_export_removes_secure_temporary_file(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)
        descriptor, path = tempfile.mkstemp(suffix=".txt")

        with (
            mock.patch("tempfile.mkstemp", return_value=(descriptor, path)),
            mock.patch(
                "blinddl.browser_cookies.export_apple_music_cookies",
                side_effect=browser_cookies.CookieExportError(["firefox: locked"]),
            ),
            mock.patch.object(wx, "MessageBox") as message_box,
        ):
            dialog._on_am_copy_cookies(None)

        self.assertFalse(os.path.exists(path))
        message_box.assert_called_once()
        dialog.Destroy()

    def test_soulseek_settings_save_credentials_sharing_and_extra_folders(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertEqual(dialog.notebook.GetPageText(2), "Soulseek")
        self.assertFalse(dialog.soulseek_enabled_check.GetValue())
        self.assertTrue(dialog.soulseek_share_library_check.GetValue())
        self.assertFalse(dialog.soulseek_username_text.IsEnabled())
        self.assertEqual(
            dialog.soulseek_account_button.GetLabel(), "Sign in or sign &up"
        )
        self.assertFalse(dialog.soulseek_account_button.IsEnabled())

        dialog.soulseek_enabled_check.SetValue(True)
        dialog.soulseek_enabled_check.ProcessEvent(
            wx.CommandEvent(wx.wxEVT_CHECKBOX, dialog.soulseek_enabled_check.GetId())
        )
        dialog.soulseek_username_text.SetValue("listener")
        dialog.soulseek_password_text.SetValue("secret")
        self.assertTrue(dialog.soulseek_account_button.IsEnabled())
        with (
            mock.patch.object(soulseek_backend, "verify_account") as verify,
            mock.patch.object(
                wx, "CallAfter", side_effect=lambda function, *args: function(*args)
            ),
            mock.patch.object(wx, "MessageBox"),
        ):
            dialog._check_soulseek_account("listener", "secret")
        verify.assert_called_once_with("listener", "secret")
        self.assertIn(
            "Signed in as listener", dialog.soulseek_account_status.GetLabel()
        )
        dialog.soulseek_folders_list.Append("C:\\Media\\One")
        dialog.soulseek_folders_list.Append("D:\\Media\\Two")
        dialog.soulseek_slots_spin.SetValue(4)
        dialog.apply()

        self.assertTrue(config["soulseek_enabled"])
        self.assertEqual(config["soulseek_username"], "listener")
        self.assertEqual(config["soulseek_password"], "secret")
        self.assertEqual(
            config["soulseek_shared_folders"],
            ["C:\\Media\\One", "D:\\Media\\Two"],
        )
        self.assertEqual(config["soulseek_upload_slots"], 4)
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_soulseek_messages_exposes_friends_and_private_transcript(self):
        self.frame.config = _SettingsConfig()
        self.frame.config["soulseek_enabled"] = True
        with (
            mock.patch.object(
                soulseek_backend,
                "friends_snapshot",
                return_value=[{"username": "alice", "status": "Online"}],
            ),
            mock.patch.object(
                soulseek_backend,
                "private_messages_snapshot",
                return_value=[
                    {
                        "timestamp": 123,
                        "user": "alice",
                        "message": "hello",
                        "outgoing": False,
                    }
                ],
            ),
        ):
            panel = MessagesPanel(self.host, self.frame)

        self.assertEqual(panel.friends_list.GetItemText(0), "alice")
        self.assertEqual(panel.friends_list.GetItemText(0, 1), "Online")
        self.assertEqual(panel.list.GetItemText(0, 1), "From")
        self.assertEqual(panel.list.GetItemText(0, 3), "hello")

        panel.recipient_text.SetValue("bob")
        panel._friend_changed(
            "bob",
            True,
            [
                {"username": "alice", "status": "Online"},
                {"username": "bob", "status": "Away"},
            ],
        )
        self.assertEqual(self.frame.config["soulseek_friends"], ["bob"])
        self.assertEqual(panel.friends_list.GetItemCount(), 2)
        self.assertIn("Added Soulseek friend bob", self.frame.messages[-1])
        panel.Destroy()

    def test_soulseek_chat_and_messages_tabs_follow_enabled_setting(self):
        class OptionalPanel(wx.Panel):
            def __init__(self, parent, frame):
                super().__init__(parent)
                self.stopped = False

            def shutdown(self):
                self.stopped = True

        holder = SimpleNamespace(
            config=_SettingsConfig(),
            notebook=wx.Notebook(self.host),
            chat_panel=None,
            messages_panel=None,
        )
        holder.config["soulseek_enabled"] = True
        with (
            mock.patch("blinddl.gui.mainframe.ChatPanel", OptionalPanel),
            mock.patch("blinddl.gui.mainframe.MessagesPanel", OptionalPanel),
        ):
            MainFrame._sync_soulseek_tabs(holder)
            self.assertEqual(holder.notebook.GetPageCount(), 2)
            self.assertEqual(holder.notebook.GetPageText(0), "Chat")
            self.assertEqual(holder.notebook.GetPageText(1), "Messages")

            holder.config["soulseek_enabled"] = False
            MainFrame._sync_soulseek_tabs(holder)

        self.assertEqual(holder.notebook.GetPageCount(), 0)
        self.assertIsNone(holder.chat_panel)
        self.assertIsNone(holder.messages_panel)

    def test_soulseek_result_queues_its_own_backend_in_soulseek_sections(self):
        panel = SearchPanel(self.host, self.frame)
        item = {
            "title": "Shared file.flac",
            "kind": "soulseek",
            "username": "peer",
            "remote_path": "Music\\Shared file.flac",
        }
        for engine in (
            ENGINE_SOULSEEK_AUDIO,
            ENGINE_SOULSEEK_VIDEO,
            ENGINE_SOULSEEK_BOOKS,
            ENGINE_SOULSEEK_TORRENTS,
        ):
            self.frame.queue.calls = []
            panel.result_engine = engine
            panel.results = [item]
            panel.results_list.SetItemCount(len(panel.results))
            panel.results_list.Select(0)

            panel.on_download_selected(None)

            self.assertEqual(
                self.frame.queue.calls, [("soulseek", item, item["title"])]
            )

    def test_only_explicit_soulseek_sections_map_to_peer_file_types(self):
        self.assertEqual(_soulseek_media_kind(ENGINE_SOULSEEK_AUDIO), "audio")
        self.assertEqual(_soulseek_media_kind(ENGINE_SOULSEEK_VIDEO), "video")
        self.assertEqual(_soulseek_media_kind(ENGINE_SOULSEEK_BOOKS), "book")
        self.assertEqual(_soulseek_media_kind(ENGINE_SOULSEEK_TORRENTS), "torrent")
        for engine in (
            ENGINE_MUSIC,
            ENGINE_SOUNDCLOUD,
            ENGINE_AUDIOBOOKS,
            ENGINE_ARCHIVE_AUDIO,
            ENGINE_ARCHIVE_VIDEO,
            ENGINE_STRAIGHT,
            ENGINE_BOOKS,
            ENGINE_TORRENTS,
            ENGINE_YOUTUBE,
        ):
            self.assertIsNone(_soulseek_media_kind(engine))

    def test_soulseek_columns_show_the_remote_folder(self):
        panel = SearchPanel(self.host, self.frame)
        item = {
            "title": "Track.flac",
            "kind": "soulseek",
            "username": "peer",
            "folder": "Music\\Album",
            "availability": "free slot",
            "file_size": "1.0 MiB",
        }
        panel.results = [item]
        panel.result_engine = ENGINE_SOULSEEK_AUDIO
        panel.results_list.SetItemCount(1)
        panel._apply_engine_columns(ENGINE_SOULSEEK_AUDIO)

        self.assertEqual(panel.results_list.GetColumn(3).GetText(), "Folder")
        self.assertEqual(panel.results_list.GetItemText(0, 3), "Music\\Album")
        panel.Destroy()

    def test_results_list_draws_rows_on_demand(self):
        """A big search must not cost per-row work on the GUI thread.

        An all-sites music search asks 57 sources for a page each, so filling
        the list row by row once per answering site used to freeze the app for
        the length of the search. The list is virtual now: it holds the rows
        in ``results`` and asks for text only for what it draws.
        """
        panel = SearchPanel(self.host, self.frame)
        self.assertTrue(panel.results_list.IsVirtual())

        panel.result_engine = ENGINE_MUSIC
        panel.results = [
            {
                "title": f"Track {i}",
                "artist": "Artist",
                "source": "Netease",
                "file_size": "8 MB",
                "format": "MP3",
                "kind": "music",
            }
            for i in range(5000)
        ]
        panel._render_results(ENGINE_MUSIC)

        # Publishing 5000 rows is a count, not 5000 insertions.
        self.assertEqual(panel.results_list.GetItemCount(), 5000)
        self.assertEqual(panel.results_list.GetItemText(4999, 0), "Track 4999")
        self.assertEqual(panel.results_list.GetItemText(0, 2), "Artist")

        # A new search empties the list rather than leaving a stale count.
        panel.results = []
        panel._render_results(ENGINE_MUSIC)
        self.assertEqual(panel.results_list.GetItemCount(), 0)
        panel.Destroy()

    def test_results_list_keeps_selection_across_a_resort(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_MUSIC
        first = {"title": "Zulu", "kind": "music"}
        second = {"title": "Alpha", "kind": "music"}
        panel.results = [first, second]
        panel._render_results(ENGINE_MUSIC)
        panel.results_list.Select(0)

        # Re-render with the rows the other way round, as a sort would.
        panel.results = [second, first]
        panel._render_results(ENGINE_MUSIC, selected=[first], focused=first)

        self.assertTrue(panel.results_list.IsSelected(1))
        self.assertFalse(panel.results_list.IsSelected(0))
        self.assertEqual(panel.results_list.GetFocusedItem(), 1)
        panel.Destroy()

    def test_user_browser_tree_filter_and_folder_download(self):
        frame = self.frame
        frame.config = _SettingsConfig()
        dialog = UserBrowserDialog(self.host, frame)
        dialog.username_text.SetValue("peer")
        dialog._loaded(
            [
                {"name": "Music", "locked": False, "files": []},
                {
                    "name": "Music\\Album",
                    "locked": False,
                    "files": [
                        {
                            "title": "Track.flac",
                            "kind": "soulseek",
                            "username": "peer",
                            "remote_path": "Music\\Album\\Track.flac",
                            "folder": "Music\\Album",
                            "file_size": "1.0 MiB",
                            "format": "FLAC",
                            "locked": False,
                        }
                    ],
                },
            ]
        )
        album = dialog._tree_items["music\\album"]
        dialog.tree.SelectItem(album)
        dialog._render()
        self.assertEqual(dialog.list.GetItemText(0), "Track.flac")

        dialog.filter_text.SetValue("track")
        dialog._render()
        self.assertEqual(dialog.list.GetItemCount(), 1)
        dialog._queue_files(
            dialog._files_in_folder("Music\\Album"), "Music\\Album"
        )
        payload = frame.queue.calls[-1][1]
        self.assertEqual(payload["target_relative_path"], "Album\\Track.flac")
        dialog.Destroy()

    def test_uploads_panel_combines_soulseek_and_torrent_rows(self):
        soul = {
            "key": "peer\\file",
            "title": "Shared.flac",
            "service": "Soulseek",
            "peer": "peer",
            "status": "Uploading",
            "percent": 50,
            "speed": 1024,
            "active": True,
        }
        torrent = {
            "key": "hash",
            "title": "Release",
            "service": "BitTorrent",
            "peer": "2 peers",
            "status": "Seeding",
            "ratio": 1.5,
            "speed": 2048,
            "active": True,
        }
        with (
            mock.patch.object(
                soulseek_backend, "uploads_snapshot", return_value=[soul]
            ),
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.uploads",
                return_value=[torrent],
            ),
        ):
            panel = UploadsPanel(self.host, self.frame)

        self.assertEqual(panel.list.GetItemCount(), 2)
        self.assertEqual(
            {panel.list.GetItemText(row, 1) for row in range(2)},
            {"Soulseek", "BitTorrent"},
        )
        panel.shutdown()
        panel.Destroy()

    def test_finished_uploads_are_separated_and_can_be_cleared(self):
        sending = {
            "key": "peer\\sending",
            "title": "Sending.flac",
            "service": "Soulseek",
            "peer": "peer",
            "status": "Uploading",
            "percent": 50,
            "speed": 1024,
            "active": True,
        }
        complete = {
            "key": "peer\\done",
            "title": "Done.flac",
            "service": "Soulseek",
            "peer": "peer",
            "status": "Complete",
            "percent": 100,
            "speed": 0,
            "active": False,
        }
        with (
            mock.patch.object(
                soulseek_backend, "uploads_snapshot",
                return_value=[sending, complete],
            ),
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.uploads",
                return_value=[],
            ),
        ):
            panel = UploadsPanel(self.host, self.frame)

            self.assertEqual(panel.list.GetItemCount(), 1)
            self.assertEqual(panel.list.GetItemText(0, 0), "Sending.flac")
            self.assertEqual(panel.finished_list.GetItemCount(), 1)
            self.assertEqual(panel.finished_list.GetItemText(0, 0), "Done.flac")

            # With the automatic clear-out on, a finished upload is not listed.
            self.frame.config["auto_clear_finished"] = True
            panel._signature = None
            panel.refresh()

        self.assertEqual(panel.list.GetItemCount(), 1)
        self.assertEqual(panel.finished_list.GetItemCount(), 0)
        self.frame.config["auto_clear_finished"] = False
        panel.shutdown()
        panel.Destroy()

    def test_hidden_uploads_panel_ignores_progress_pushes(self):
        with mock.patch(
            "blinddl.gui.uploads_panel.torrent_engine.uploads", return_value=[]
        ):
            panel = UploadsPanel(self.host, self.frame)
        with (
            mock.patch.object(panel, "IsShownOnScreen", return_value=False),
            mock.patch.object(panel, "refresh") as refresh,
        ):
            panel.handle_soulseek_event({"type": "uploads", "uploads": [{}]})

        refresh.assert_not_called()
        panel.shutdown()
        panel.Destroy()

    def test_sources_dialog_lists_general_adult_providers_together(self):
        config = _SettingsConfig()
        config["adult_sites_enabled"] = True
        dialog = SourcesDialog(self.host, config)

        self.assertEqual(dialog.adult_check_list.GetName(), "Adult sites")
        self.assertIn("eporner", dialog.adult_sources)
        self.assertIn("pornhub", dialog.adult_sources)

        pornhub_index = dialog.adult_sources.index("pornhub")
        dialog.adult_check_list.Check(pornhub_index, False)
        dialog.apply()

        self.assertIn("pornhub", config["disabled_adult_sources"])
        self.assertNotIn("eporner", config["disabled_adult_sources"])
        dialog.Destroy()

    def test_search_shutdown_stops_timer_and_ignores_late_results(self):
        panel = SearchPanel(self.host, self.frame)
        panel.token = token = object()
        panel.stop = threading.Event()
        panel.timer.Start(1000)

        panel.shutdown()
        panel._add_site(token, 1, "YouTube", [{"title": "Too late"}])

        self.assertTrue(panel.closing)
        self.assertTrue(panel.stop.is_set())
        self.assertFalse(panel.timer.IsRunning())
        self.assertEqual(panel.results, [])

    def test_download_progress_only_writes_changed_cells(self):
        item = DownloadItem("Track", "ytdlp", "https://example/track")
        native_list = mock.Mock()
        native_list.GetItemCount.return_value = 0
        panel = SimpleNamespace(
            list=native_list, finished_list=mock.Mock(), _rows={}, _values={})

        DownloadsPanel.update_item(panel, item)
        native_list.SetItem.reset_mock()
        DownloadsPanel.update_item(panel, item)

        native_list.SetItem.assert_not_called()
        item.percent = 25
        DownloadsPanel.update_item(panel, item)
        native_list.SetItem.assert_called_once_with(0, 2, "25%")

    def test_finished_downloads_live_in_their_own_list(self):
        # What is still running is the whole of the first list, so it is
        # never arrowed through to reach what is still going.
        panel = DownloadsPanel(self.host, self.frame)
        queued = DownloadItem("Queued", "ytdlp", "one")
        queued.status = STATUS_QUEUED
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        self.frame.queue.items = [queued, done]
        panel.refresh_all()

        self.assertEqual(panel.list.GetItemCount(), 1)
        self.assertEqual(panel.list.GetItemText(0, 0), "Queued")
        self.assertEqual(panel.finished_list.GetItemCount(), 1)
        self.assertEqual(panel.finished_list.GetItemText(0, 0), "Done")

        # Finishing moves the row across without leaving a copy behind.
        queued.status = STATUS_DONE
        panel.update_item(queued)
        self.assertEqual(panel.list.GetItemCount(), 0)
        self.assertEqual(panel.finished_list.GetItemCount(), 2)
        panel.Destroy()

    def test_downloads_cancel_multiple_and_clear_finished(self):
        panel = DownloadsPanel(self.host, self.frame)
        queued = DownloadItem("Queued", "ytdlp", "one")
        queued.status = STATUS_QUEUED
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        self.frame.queue.items = [queued, done]
        panel.refresh_all()
        panel.list.Select(0)
        panel.finished_list.Select(0)

        panel.on_cancel(None)
        self.assertTrue(queued.cancel_event.is_set())
        panel.on_clear(None)
        self.assertEqual(self.frame.queue.items, [queued])

    def test_subscriptions_bulk_disable_and_selection_helpers(self):
        panel = SubsPanel(self.host, self.frame)
        panel._select_all(None)
        self.assertEqual(panel.list.GetSelectedItemCount(), 2)
        panel.on_disable(None)
        self.assertFalse(any(row["enabled"] for row in self.frame.subs.rows))
        panel._clear_selection(None)
        self.assertEqual(panel.list.GetSelectedItemCount(), 0)

    def test_subscription_view_sorting_covers_each_persisted_mode(self):
        rows = [
            {
                "title": "Zulu",
                "url": "https://youtube.com/z",
                "enabled": False,
                "last_checked": None,
                "seen_ids": ["1"],
            },
            {
                "title": "Alpha",
                "url": "https://bandcamp.com/a",
                "enabled": True,
                "last_checked": "2026-08-08 10:00",
                "seen_ids": ["1", "2", "3"],
            },
            {
                "title": "Beta",
                "url": "https://youtube.com/b",
                "enabled": True,
                "last_checked": "2026-08-07 10:00",
                "seen_ids": ["1", "2"],
            },
        ]

        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_TITLE)],
            ["Alpha", "Beta", "Zulu"],
        )
        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_SITE)],
            ["Alpha", "Beta", "Zulu"],
        )
        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_CHECKED)],
            ["Alpha", "Beta", "Zulu"],
        )
        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_STALE)],
            ["Zulu", "Beta", "Alpha"],
        )
        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_TRACKED)],
            ["Alpha", "Beta", "Zulu"],
        )
        self.assertEqual(
            [row["title"] for row in _sorted_subscriptions(rows, SUBS_SORT_ENABLED)],
            ["Alpha", "Beta", "Zulu"],
        )

    def test_subscription_feed_order_updates_selected_rows(self):
        panel = SubsPanel(self.host, self.frame)
        panel.list.Select(0)

        panel._set_feed_order(search_order.ORDER_POPULAR)

        self.assertEqual(self.frame.subs.rows[0]["order"], search_order.ORDER_POPULAR)
        self.assertIn("Most popular", self.frame.messages[-1])


if __name__ == "__main__":
    unittest.main()
