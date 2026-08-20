# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
from collections import deque
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
        sideb_backend,
        speech,
        soulseek_backend,
        updater,
        ytdlp_backend,
    )
    from blinddl.downloader import (
        DownloadItem,
        FINISHED_STATUSES,
        STATUS_DONE,
        STATUS_DOWNLOADING,
        STATUS_ERROR,
        STATUS_QUEUED,
    )
    from blinddl import config as config_module
    from blinddl.saved_queue import SavedQueue
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
    from blinddl.gui.queue_panel import QueuePanel
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
    from blinddl.gui.feeds_dialog import FeedsDialog
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
    from blinddl.gui.tools_dialog import ExternalToolsDialog
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

    def remove(self, item_id, delete_data=False):
        item = self._find(item_id)
        if (item is None or item.status == STATUS_DOWNLOADING
                or getattr(item, "seeding", False)):
            return False
        if delete_data and not self.can_delete_data(item):
            return False
        self.items.remove(item)
        return True

    def can_delete_data(self, item):
        return bool(getattr(item, "result_path", ""))

    def mark_torrent_stopped(self, key, title=""):
        return True

    def remove_finished(self):
        self.items = [
            item for item in self.items
            if item.status not in FINISHED_STATUSES
            or getattr(item, "seeding", False)
        ]

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
        self.wake_count = 0
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

    def wake(self):
        self.wake_count += 1


class _Frame:
    def __init__(self):
        self.config = _SavingConfig({
            "disabled_music_sources": [],
            "disabled_adult_sources": [],
            "disabled_book_sources": [],
            "disabled_audiobook_sources": [],
            "disabled_archive_sources": [],
            "adult_sites_enabled": True,
            "search_timeout_s": 5,
            "audio_only": True,
            "auto_clear_finished": False,
            "sub_check_hours": 6,
            "cookies_from_browser": None,
            "cookies_file": "",
        })
        self.queue = _Queue()
        self.subs = _Subscriptions()
        self.saved = SavedQueue(state_path="")
        self.queue_panel = mock.Mock()
        self.queue_panel.count_text.return_value = "1 item in the queue."
        self.messages = []
        self.play_calls = []
        self.extra_players = []

    def announce(self, message):
        self.messages.append(message)

    def on_choose_sources(self):
        pass

    def show_downloads_tab(self):
        pass

    def register_player(self, player):
        self.extra_players.append(player)

    def unregister_player(self, player):
        if player in self.extra_players:
            self.extra_players.remove(player)

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
        # Nothing is ticked to start with: an album reached from an artist
        # would otherwise be twenty-five tracks already chosen, where every
        # key except Escape downloads the lot.
        self.assertEqual(dialog.selected_items(), [])
        self.assertFalse(dialog.download_btn.IsEnabled())

        dialog.on_select_all(None)
        self.assertEqual(dialog.selected_items(), items)
        self.assertTrue(dialog.download_btn.IsEnabled())

        dialog.on_clear_selection(None)
        self.assertEqual(dialog.selected_items(), [])

        dialog.item_list.CheckItem(1, True)
        self.app.Yield()
        self.assertEqual(dialog.selected_items(), [items[1]])
        dialog.Destroy()

    def test_the_picker_plays_the_row_you_are_on_without_ticking_it(self):
        # A tick means "download this". Having to tick a track to hear it
        # would be the opposite of what listening before choosing is for.
        items = [
            {"title": "One", "url": "https://example.test/1"},
            {"title": "Two", "url": "https://example.test/2"},
        ]
        panel = SearchPanel(self.host, self.frame)
        dialog = ItemPickerDialog(panel, items, "Album")
        try:
            self.assertIsNotNone(dialog.player)
            self.assertIn(dialog.player, self.frame.extra_players)
            dialog.item_list.Focus(1)
            self.app.Yield()

            with mock.patch(
                "blinddl.gui.item_picker_dialog.threading.Thread"
            ) as thread:
                dialog.on_play_full(None)
            item, full = thread.call_args.kwargs["args"][1:]
            self.assertEqual(item["title"], "Two")
            self.assertTrue(full)
            self.assertEqual(dialog.selected_items(), [])

            with mock.patch(
                "blinddl.gui.item_picker_dialog.threading.Thread"
            ) as thread:
                dialog.on_preview(None)
            self.assertFalse(thread.call_args.kwargs["args"][2])
        finally:
            dialog.Destroy()
            panel.shutdown()
            panel.Destroy()
        # The dialog's player leaves the one-at-a-time rule with the dialog.
        self.assertEqual(self.frame.extra_players, [])

    def test_the_picker_says_why_enter_did_nothing_with_nothing_ticked(self):
        panel = SearchPanel(self.host, self.frame)
        dialog = ItemPickerDialog(panel, [{"title": "One"}], "Album")
        try:
            before = len(self.frame.messages)
            dialog.on_download(None)
            self.assertGreater(len(self.frame.messages), before)
            self.assertIn("Space", self.frame.messages[-1])
        finally:
            dialog.Destroy()
            panel.shutdown()
            panel.Destroy()

    def test_search_results_can_be_kept_for_later(self):
        panel = SearchPanel(self.host, self.frame)
        try:
            panel.result_engine = ENGINE_DEEZER
            panel.results = [
                {"id": "deezer:1", "kind": "deezer", "title": "One",
                 "url": "https://www.deezer.com/track/1"},
                {"id": "deezer:2", "kind": "deezer", "title": "Two",
                 "url": "https://www.deezer.com/track/2"},
            ]
            panel._render_results(ENGINE_DEEZER)
            panel.results_list.Select(0)
            panel.results_list.Select(1)

            panel.on_save_selected(None)
            entries = self.frame.saved.all()
            self.assertEqual([e["result"]["title"] for e in entries],
                             ["One", "Two"])
            self.assertEqual([e["engine"] for e in entries],
                             [ENGINE_DEEZER, ENGINE_DEEZER])

            # Saving the same rows again says so instead of doubling them.
            panel.on_save_selected(None)
            self.assertEqual(len(self.frame.saved.all()), 2)
            self.assertIn("already there", self.frame.messages[-1])
        finally:
            panel.shutdown()
            panel.Destroy()

    def test_the_download_queue_tab_queues_and_then_forgets_its_rows(self):
        # A row that has been handed to the transfer queue must leave this
        # list: a row in both is a row that gets downloaded twice.
        frame = self.frame
        frame.saved.add(
            {"id": "deezer:1", "kind": "deezer", "title": "A track",
             "artist": "X", "source": "Deezer",
             "url": "https://www.deezer.com/track/1"},
            ENGINE_DEEZER,
            folder="X",
        )
        panel = QueuePanel(self.host, frame)
        try:
            self.assertEqual(panel.list.GetItemCount(), 1)
            self.assertEqual(panel._cell(0, 0), "A track")
            panel.list.Select(0)

            panel.on_download_selected(None)
            self.assertEqual(
                frame.queue.calls,
                [("sideb", "https://www.deezer.com/track/1", "A track")],
            )
            self.assertEqual(frame.queue.folders, ["X"])
            self.assertEqual(panel.list.GetItemCount(), 0)
            self.assertEqual(frame.saved.all(), [])
        finally:
            panel.shutdown()
            panel.Destroy()

    def test_an_album_kept_for_later_is_resolved_only_when_it_is_asked_for(self):
        # Shelving a discography must not read a hundred track lists, so an
        # album row stays an album row until it is actually downloaded.
        frame = self.frame
        album = {"id": "deezer:album:7", "kind": "deezer_album",
                 "title": "An album", "artist": "X", "source": "Deezer",
                 "url": "https://www.deezer.com/album/7"}
        frame.saved.add(album, ENGINE_DEEZER)
        panel = QueuePanel(self.host, frame)
        try:
            with mock.patch(
                "blinddl.gui.queue_panel.threading.Thread"
            ) as thread:
                panel.on_download_all(None)
            thread.assert_called_once()
            # Nothing was queued and the row is still there, waiting for the
            # track list that is being fetched off the GUI thread.
            self.assertEqual(frame.queue.calls, [])
            self.assertEqual(panel.list.GetItemCount(), 1)

            key = frame.saved.all()[0]["key"]
            tracks = [{"title": f"T{n}",
                       "url": f"https://www.deezer.com/track/{n}"}
                      for n in (1, 2)]
            panel._collections_ready([(key, album, tracks)], [])
            self.assertEqual(
                [call[0] for call in frame.queue.calls], ["sideb", "sideb"])
            self.assertEqual(
                frame.queue.folders, ["X - An album", "X - An album"])
            self.assertEqual(panel.list.GetItemCount(), 0)
        finally:
            panel.shutdown()
            panel.Destroy()

    def test_removing_from_the_download_queue(self):
        frame = self.frame
        for number in range(3):
            frame.saved.add(
                {"id": str(number), "title": f"T{number}"}, ENGINE_DEEZER)
        panel = QueuePanel(self.host, frame)
        try:
            panel.list.Select(0)
            panel.on_remove_selected(None)
            self.assertEqual(panel.list.GetItemCount(), 2)
            self.assertEqual(
                [e["result"]["title"] for e in frame.saved.all()],
                ["T1", "T2"],
            )
        finally:
            panel.shutdown()
            panel.Destroy()

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

    def test_missing_media_tools_open_an_install_window(self):
        # First run downloads VLC and friends. Doing that with nothing on
        # screen left minutes of silence and no window to tab to.
        holder = SimpleNamespace(_show_external_tools_dialog=mock.Mock())
        with mock.patch.object(updater, "missing_external_tools",
                               return_value=["VideoLAN.VLC"]), \
                mock.patch("blinddl.gui.mainframe.wx.CallAfter",
                           side_effect=lambda callback, *args: callback(*args)):
            MainFrame._external_dependencies_worker(holder)

        holder._show_external_tools_dialog.assert_called_once_with(
            ["VideoLAN.VLC"])

    def test_nothing_missing_opens_no_install_window(self):
        holder = SimpleNamespace(_show_external_tools_dialog=mock.Mock())
        with mock.patch.object(updater, "missing_external_tools",
                               return_value=[]), \
                mock.patch("blinddl.gui.mainframe.wx.CallAfter",
                           side_effect=lambda callback, *args: callback(*args)):
            MainFrame._external_dependencies_worker(holder)

        holder._show_external_tools_dialog.assert_not_called()

    def test_only_one_install_window_is_ever_open(self):
        holder = SimpleNamespace(_closing=False, _tools_dialog=object())
        with mock.patch("blinddl.gui.mainframe.ExternalToolsDialog") as dialog:
            MainFrame._show_external_tools_dialog(holder, ["VideoLAN.VLC"])
        dialog.assert_not_called()

    def test_the_install_window_names_the_tool_being_installed(self):
        with mock.patch("blinddl.gui.tools_dialog.threading.Thread"):
            dialog = ExternalToolsDialog(self.host, ["VideoLAN.VLC"])

        self.assertIn("VLC media player", dialog.intro)
        self.assertIn("VLC media player", dialog.log_text.GetValue())
        self.assertEqual(dialog.close_btn.GetLabel(), "&Hide")
        dialog.Destroy()

    def test_the_install_window_speaks_every_step(self):
        # The log is a read-only text control: a screen reader does not read
        # what arrives in it, so an unspoken step is an invisible one.
        with mock.patch("blinddl.gui.tools_dialog.threading.Thread"):
            dialog = ExternalToolsDialog(self.host, ["VideoLAN.VLC"])

        with mock.patch("blinddl.gui.tools_dialog.speech.announce") as announce:
            dialog._progress("Installing VLC media player (audio preview).")
        self.app.Yield()

        announce.assert_called_once_with(
            "Installing VLC media player (audio preview).")
        self.assertIn("Installing VLC media player",
                      dialog.log_text.GetValue())
        dialog.Destroy()

    def test_hiding_the_install_window_leaves_the_install_running(self):
        finished = []
        with mock.patch("blinddl.gui.tools_dialog.threading.Thread"):
            dialog = ExternalToolsDialog(
                self.host, ["VideoLAN.VLC"], on_finished=finished.append)
        dialog.Close()
        self.app.Yield()

        # What the worker thread does after the window is gone.
        with mock.patch("blinddl.gui.tools_dialog.speech.announce") as announce:
            dialog._log("late line")
            dialog._finished(True)

        self.assertFalse(dialog._alive)
        self.assertEqual(finished, [True])
        announce.assert_called_once_with("Media tools installed. blindDL is ready.")

    def test_a_finished_install_offers_a_close_button(self):
        with mock.patch("blinddl.gui.tools_dialog.threading.Thread"):
            dialog = ExternalToolsDialog(self.host, ["VideoLAN.VLC"])

        with mock.patch("blinddl.gui.tools_dialog.speech.announce"):
            dialog._finished(False)

        self.assertEqual(dialog.close_btn.GetLabel(), "&Close")
        self.assertIn("could not be installed", dialog.log_text.GetValue())
        dialog.Destroy()

    def test_the_install_result_stays_on_the_status_bar(self):
        # The dialog already spoke it; the status bar is where NVDA+End can
        # find it again afterwards.
        holder = SimpleNamespace(
            _closing=False, _tools_dialog=object(), announce=mock.Mock())

        MainFrame._external_tools_finished(holder, True)

        self.assertIsNone(holder._tools_dialog)
        holder.announce.assert_called_once_with(
            "Media tools installed. blindDL is ready.", speak=False)

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

    def test_a_found_release_is_downloaded_automatically(self):
        holder = self._update_holder(_update_downloaded=mock.Mock())
        update = SimpleNamespace(version="9.9.9")

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

    def test_provider_callbacks_are_coalesced_before_reaching_wx(self):
        panel = SimpleNamespace(
            closing=False,
            _site_delivery_lock=threading.Lock(),
            _site_deliveries=deque(),
            _site_delivery_scheduled=False,
            _drain_site_results=mock.Mock(),
        )
        with mock.patch.object(wx, "CallAfter") as call_after:
            for index in range(20):
                SearchPanel._queue_site_results(
                    panel, object(), 0, f"Site {index}", [{"title": str(index)}]
                )

        call_after.assert_called_once_with(panel._drain_site_results)
        self.assertEqual(len(panel._site_deliveries), 20)

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
            _external_dependencies_worker=mock.Mock(),
            _housekeeping_worker=mock.Mock(),
            _start_update_checks=mock.Mock(),
            _apply_soulseek_setting=mock.Mock(),
            _maybe_offer_torrent_associations=mock.Mock(),
        )

        MainFrame._start_background_services(holder)
        MainFrame._start_background_services(holder)

        holder.queue.start.assert_called_once_with()
        holder.subs.start.assert_called_once_with()
        holder._start_update_checks.assert_called_once_with()
        holder._apply_soulseek_setting.assert_called_once_with()
        # The file-type question is scheduled, not asked here: it waits
        # behind the first-run tools window.
        holder._maybe_offer_torrent_associations.assert_not_called()

    def test_a_magnet_handed_in_from_outside_joins_the_queue(self):
        holder = SimpleNamespace(
            _closing=False,
            queue=SimpleNamespace(add_torrent=mock.Mock()),
            show_downloads_tab=mock.Mock(),
            announce=mock.Mock(),
        )
        magnet = ("magnet:?xt=urn:btih:"
                  "0123456789abcdef0123456789abcdef01234567&dn=A+Release")

        MainFrame.open_torrent_link(holder, magnet)

        item, title = holder.queue.add_torrent.call_args[0]
        self.assertEqual(title, "A Release")
        self.assertEqual(item["magnet"], magnet)
        # Shown and said, because the user did this from outside blindDL and
        # would otherwise have to go looking for where it went.
        holder.show_downloads_tab.assert_called_once_with()
        self.assertIn("A Release", holder.announce.call_args[0][0])

    def test_a_torrent_that_cannot_be_read_is_said_not_raised(self):
        holder = SimpleNamespace(
            _closing=False,
            queue=SimpleNamespace(add_torrent=mock.Mock()),
            show_downloads_tab=mock.Mock(),
            announce=mock.Mock(),
        )

        with mock.patch("blinddl.gui.mainframe.wx.MessageBox"):
            MainFrame.open_torrent_link(holder, "C:\\nowhere\\gone.torrent")

        holder.queue.add_torrent.assert_not_called()
        self.assertIn("Could not open", holder.announce.call_args[0][0])

    def test_the_file_type_question_is_asked_once(self):
        holder = SimpleNamespace(
            _closing=False,
            config={"torrent_assoc_prompted": True},
        )
        with mock.patch("blinddl.gui.mainframe.associations.supported",
                           return_value=True), \
                mock.patch("blinddl.gui.mainframe.wx.RichMessageDialog") as dialog:
            MainFrame._maybe_offer_torrent_associations(holder)
        dialog.assert_not_called()

    def test_already_the_handler_means_there_is_nothing_to_ask(self):
        config = _SavingConfig({"torrent_assoc_prompted": False})
        holder = SimpleNamespace(_closing=False, config=config)
        with mock.patch("blinddl.gui.mainframe.associations.supported",
                           return_value=True), \
                mock.patch("blinddl.gui.mainframe.associations.is_registered",
                           return_value=True), \
                mock.patch("blinddl.gui.mainframe.wx.RichMessageDialog") as dialog:
            MainFrame._maybe_offer_torrent_associations(holder)
        dialog.assert_not_called()
        self.assertTrue(config["torrent_assoc_prompted"])

    def test_saying_yes_claims_the_file_types(self):
        config = _SavingConfig({"torrent_assoc_prompted": False})
        holder = SimpleNamespace(
            _closing=False, config=config,
            register_torrent_associations=mock.Mock(),
        )
        answered = mock.Mock(
            ShowCheckBox=mock.Mock(),
            ShowModal=mock.Mock(return_value=wx.ID_YES),
            IsCheckBoxChecked=mock.Mock(return_value=True),
            Destroy=mock.Mock(),
        )
        with mock.patch("blinddl.gui.mainframe.associations.supported",
                           return_value=True), \
                mock.patch("blinddl.gui.mainframe.associations.is_registered",
                           return_value=False), \
                mock.patch("blinddl.gui.mainframe.wx.RichMessageDialog",
                           return_value=answered):
            MainFrame._maybe_offer_torrent_associations(holder)

        # The box is ticked when the question appears, so the ordinary way
        # of answering settles it for good.
        self.assertEqual(answered.ShowCheckBox.call_args[1]["checked"], True)
        holder.register_torrent_associations.assert_called_once_with()
        self.assertTrue(config["torrent_assoc_prompted"])

    def test_unticking_the_box_asks_again_next_time(self):
        config = _SavingConfig({"torrent_assoc_prompted": False})
        holder = SimpleNamespace(
            _closing=False, config=config,
            register_torrent_associations=mock.Mock(),
        )
        answered = mock.Mock(
            ShowCheckBox=mock.Mock(),
            ShowModal=mock.Mock(return_value=wx.ID_NO),
            IsCheckBoxChecked=mock.Mock(return_value=False),
            Destroy=mock.Mock(),
        )
        with mock.patch("blinddl.gui.mainframe.associations.supported",
                           return_value=True), \
                mock.patch("blinddl.gui.mainframe.associations.is_registered",
                           return_value=False), \
                mock.patch("blinddl.gui.mainframe.wx.RichMessageDialog",
                           return_value=answered):
            MainFrame._maybe_offer_torrent_associations(holder)

        holder.register_torrent_associations.assert_not_called()
        self.assertFalse(config["torrent_assoc_prompted"])

    def test_playback_status_gets_its_own_status_field_and_is_never_spoken(self):
        holder = SimpleNamespace(
            _closing=False,
            SetStatusText=mock.Mock(),
            config={"speak_status": True},
        )

        MainFrame.set_playback_status(holder, "Playing: One — 0:05 of 3:20")

        # Field 2, so the queue counts and the last thing that happened both
        # stay where NVDA+End and the other fields already found them.
        holder.SetStatusText.assert_called_once_with(
            "Playing: One — 0:05 of 3:20", 2)

    def test_a_closing_window_is_not_written_to(self):
        holder = SimpleNamespace(
            _closing=True, SetStatusText=mock.Mock(), config={})

        MainFrame.set_playback_status(holder, "Playing: One")

        holder.SetStatusText.assert_not_called()

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

    def test_the_players_play_button_starts_the_selected_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        panel.results = [{"title": "One", "url": "https://example/one"}]
        panel.results_list.SetItemCount(1)
        panel.results_list.Select(0)
        panel.results_list.Focus(0)

        with mock.patch.object(panel, "on_play_full_selected") as play:
            handled = panel.player.play_request()

        self.assertTrue(handled)
        play.assert_called_once_with(None)

    def test_play_with_nothing_loaded_asks_the_panel_before_giving_up(self):
        panel = SimpleNamespace(
            _loaded=False,
            play_request=mock.Mock(return_value=True),
            frame=mock.Mock(),
        )

        media_player.MediaPlayerPanel.on_play_pause(panel, None)

        panel.play_request.assert_called_once_with()
        panel.frame.announce.assert_not_called()

    def test_play_still_says_so_when_the_panel_has_nothing_either(self):
        panel = SimpleNamespace(
            _loaded=False,
            play_request=mock.Mock(return_value=False),
            frame=mock.Mock(),
        )

        media_player.MediaPlayerPanel.on_play_pause(panel, None)

        panel.frame.announce.assert_called_once_with(
            "Choose media to play first.")

    def test_a_book_search_leaves_the_play_button_to_say_so(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_BOOKS

        self.assertFalse(panel.play_selection())

    def test_search_full_playback_ready_uses_shared_player(self):
        panel = SearchPanel(self.host, self.frame)
        panel.full_playback_token = token = object()

        panel._full_playback_ready(
            token, "https://media.example/audio.mp3", "One"
        )

        self.assertEqual(
            self.frame.play_calls,
            [(panel.player, "https://media.example/audio.mp3", "One")],
        )

    def test_artist_scope_choice_saves_and_announces(self):
        panel = SearchPanel(self.host, self.frame)
        panel.kind_choice.SetSelection(
            search_kind.KINDS.index(search_kind.KIND_ARTIST))
        panel.on_kind_changed(None)
        panel.artist_scope_choice.SetSelection(
            search_kind.ARTIST_SCOPES.index(search_kind.ARTIST_SCOPE_ALBUMS))

        panel.on_artist_scope_changed(None)

        self.assertEqual(self.frame.config["artist_scope"], "albums")
        self.assertIn(
            "Artist search set to Albums", self.frame.messages[-1]
        )

    def test_artist_scope_choice_is_off_for_a_track_search(self):
        panel = SearchPanel(self.host, self.frame)
        panel.kind_choice.SetSelection(
            search_kind.KINDS.index(search_kind.KIND_TRACK))
        panel.on_kind_changed(None)

        self.assertFalse(panel.artist_scope_choice.IsEnabled())


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

    def test_full_playback_without_an_arl_falls_back_to_youtube(self):
        item = {
            "kind": "deezer",
            "id": "deezer:3135556",
            "title": "One More Time",
            "artist": "Daft Punk",
        }
        with (
            mock.patch.object(
                ytdlp_backend, "resolve_stream",
                return_value="https://yt.example/full",
            ) as resolve,
            mock.patch.object(
                sideb_backend, "get_deezer_preview_url",
                return_value="https://cdns.example/preview.mp3",
            ) as preview_url,
        ):
            location, title = preview.resolve_full_playback(
                item, audio_only=True, config={}
            )

        self.assertEqual(location, "https://yt.example/full")
        self.assertEqual(title, "One More Time")
        resolve.assert_called_once()
        # The 30-second Deezer clip is for previews, not full playback.
        preview_url.assert_not_called()
        self.assertEqual(
            resolve.call_args.args[0], "ytsearch1:Daft Punk One More Time"
        )

    def test_full_playback_of_a_deezer_result_plays_deezers_own_recording(self):
        item = {
            "kind": "deezer",
            "id": "deezer:3135556",
            "title": "One More Time",
            "artist": "Daft Punk",
            "url": "https://www.deezer.com/track/3135556",
        }
        with (
            mock.patch.object(
                deezer_backend, "playback_file",
                return_value=r"C:\cache\3135556.mp3",
            ) as playback_file,
            mock.patch.object(ytdlp_backend, "resolve_stream") as resolve,
            mock.patch.object(sideb_backend, "get_deezer_preview_url") as clip,
        ):
            location, title = preview.resolve_full_playback(
                item, audio_only=True, config={"deezer_arl": "an-arl"}
            )

        self.assertEqual(location, r"C:\cache\3135556.mp3")
        self.assertEqual(title, "One More Time")
        self.assertEqual(
            playback_file.call_args.args[0],
            "https://www.deezer.com/track/3135556")
        # Deezer holds the recording that was searched for. Hunting YouTube
        # for a match risks the wrong one, and risks being refused outright.
        resolve.assert_not_called()
        clip.assert_not_called()

    def test_deezer_falls_back_to_youtube_when_deezer_refuses_the_track(self):
        item = {
            "kind": "deezer",
            "id": "deezer:3135556",
            "title": "One More Time",
            "artist": "Daft Punk",
        }
        with (
            mock.patch.object(
                deezer_backend, "playback_file",
                side_effect=RuntimeError("region-locked"),
            ),
            mock.patch.object(
                ytdlp_backend, "resolve_stream",
                return_value="https://yt.example/full",
            ) as resolve,
        ):
            location, _title = preview.resolve_full_playback(
                item, audio_only=True, config={"deezer_arl": "an-arl"}
            )

        self.assertEqual(location, "https://yt.example/full")
        resolve.assert_called_once()

    def test_a_deezer_row_from_musicdl_never_plays_its_encrypted_stream(self):
        # musicdl's own Deezer client hands back Deezer's stream URL, which
        # is Blowfish encrypted: a player opens it and produces silence.
        item = {
            "source": "Deezer",
            "title": "One More Time",
            "url": "https://www.deezer.com/track/3135556",
            "song_info": SimpleNamespace(
                download_url="https://cdns-proxy.dzcdn.net/media/1/encrypted"
            ),
        }
        with mock.patch.object(
            sideb_backend, "get_deezer_preview_url",
            return_value="https://cdns.example/preview.mp3",
        ):
            location, _title = preview.resolve_search_result(
                item, audio_only=True, config={}
            )

        self.assertEqual(location, "https://cdns.example/preview.mp3")

    def test_a_deezer_track_is_recognised_wherever_its_id_is_carried(self):
        self.assertEqual(
            preview.deezer_track_url(
                {"kind": "sideb", "id": "sideb:3135556"}),
            "https://www.deezer.com/track/3135556")
        self.assertEqual(
            preview.deezer_track_url(
                {"source": "Deezer",
                 "url": "https://www.deezer.com/fr/track/3135556"}),
            "https://www.deezer.com/track/3135556")
        # An album row is not a track, and has nothing to play on its own.
        self.assertIsNone(preview.deezer_track_url(
            {"kind": "deezer_album", "id": "deezer:album:302127",
             "url": "https://www.deezer.com/album/302127"}))
        self.assertIsNone(preview.deezer_track_url(
            {"source": "YouTube", "url": "https://youtu.be/one"}))

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

    def test_a_deezer_url_plays_full_not_a_preview_clip(self):
        item = {
            "kind": "deezer",
            "id": "deezer:1",
            "title": "One More Time",
            "artist": "Daft Punk",
        }
        with (
            mock.patch.object(
                sideb_backend, "is_deezer_url", return_value=True
            ),
            mock.patch.object(
                deezer_backend, "extract_flat",
                return_value=([item], "One More Time"),
            ),
            mock.patch.object(
                sideb_backend, "get_deezer_preview_url",
                return_value="https://cdns.example/preview.mp3",
            ) as preview_url,
            mock.patch.object(
                ytdlp_backend, "resolve_stream",
                return_value="https://yt.example/full",
            ),
        ):
            location, title = preview.resolve_url(
                "https://www.deezer.com/track/1",
                audio_only=True,
                config={},
            )

        self.assertEqual(location, "https://yt.example/full")
        self.assertEqual(title, "One More Time")
        # The 30-second clip is for the Search tab's Preview, not Play URL.
        preview_url.assert_not_called()

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

    def test_a_growing_selection_says_how_much_is_in_it(self):
        # Selecting rows is silent on Windows, and a screen reader reads the
        # row the cursor lands on whether or not it joined a selection. With
        # nothing to say otherwise, results that can be taken several at a
        # time read as results that can only be taken one at a time.
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": t} for t in ("One", "Two", "Three")]
        panel.results_list.SetItemCount(len(panel.results))

        # One row is what arrowing looks like: the reader is already saying
        # it, so nothing talks over it.
        said = len(self.frame.messages)
        panel.results_list.Select(0)
        panel._announce_selection()
        self.assertEqual(len(self.frame.messages), said)

        panel.results_list.Select(1)
        panel._announce_selection()
        self.assertEqual(self.frame.messages[-1], "2 results selected.")

        panel.results_list.Select(2)
        panel._announce_selection()
        self.assertEqual(self.frame.messages[-1], "3 results selected.")

        panel.shutdown()
        panel.Destroy()

    def test_falling_back_to_one_row_is_worth_saying(self):
        # Losing a selection is exactly what a Shift-less arrow key does by
        # accident, and it is the one thing silence must not hide.
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": t} for t in ("One", "Two")]
        panel.results_list.SetItemCount(len(panel.results))
        panel.results_list.Select(0)
        panel.results_list.Select(1)
        panel._announce_selection()
        self.assertEqual(self.frame.messages[-1], "2 results selected.")

        panel.results_list.Select(0, False)
        panel._announce_selection()
        self.assertEqual(self.frame.messages[-1], "1 result selected.")

        panel.results_list.Select(1, False)
        panel._announce_selection()
        self.assertEqual(self.frame.messages[-1], "Nothing selected.")

        panel.shutdown()
        panel.Destroy()

    def test_control_a_selects_every_result_and_says_so(self):
        # The list control handles Ctrl+A itself and says nothing about it.
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": t} for t in ("One", "Two", "Three")]
        panel.results_list.SetItemCount(len(panel.results))

        skipped = []
        event = SimpleNamespace(
            GetKeyCode=lambda: 1,  # Ctrl+A
            ControlDown=lambda: True,
            Skip=lambda: skipped.append(True),
        )
        panel.on_results_char(event)

        self.assertEqual(panel.results_list.GetSelectedItemCount(), 3)
        self.assertEqual(self.frame.messages[-1], "Selected 3 results.")
        # Kept, so the list's own silent select-all does not run as well.
        self.assertEqual(skipped, [])

        panel.shutdown()
        panel.Destroy()

    def test_a_command_that_speaks_is_not_echoed_by_the_settled_count(self):
        # Select all announces its own result, and the row events its own
        # Select() calls raise must not say the same thing again.
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": t} for t in ("One", "Two")]
        panel.results_list.SetItemCount(len(panel.results))

        panel._select_all(None)
        self.assertEqual(self.frame.messages[-1], "Selected 2 results.")
        said = len(self.frame.messages)
        panel._announce_selection()
        self.assertEqual(len(self.frame.messages), said)

        panel.shutdown()
        panel.Destroy()

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
                media_player._configure_vlc()
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
            _is_playing=lambda: True,
            _report_status=mock.Mock(),
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
            _is_playing=lambda: True,
            _report_status=mock.Mock(),
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

    def test_the_playback_status_line_reads_position_and_length(self):
        self.assertEqual(
            media_player.playback_status("Playing", "One More Time",
                                         83_000, 320_000),
            "Playing: One More Time — 1:23 of 5:20, 3:57 left",
        )
        self.assertEqual(
            media_player.playback_status("Paused", "One More Time", 5_000, 0),
            "Paused: One More Time — 0:05",
        )
        # Nothing playing is nothing to read.
        self.assertEqual(media_player.playback_status("", "One", 0, 0), "")

    def test_the_timer_puts_the_playback_clock_on_the_status_bar(self):
        frame = SimpleNamespace(set_playback_status=mock.Mock())
        panel = SimpleNamespace(
            _loaded=True,
            _length=lambda: 320_000,
            _tell=lambda: 83_000,
            _is_playing=lambda: True,
            _title="One More Time",
            _updating_position=False,
            frame=frame,
            time_text=mock.Mock(),
            position=mock.Mock(),
        )
        panel.time_text.GetLabel.return_value = ""
        panel.position.HasFocus.return_value = True
        panel._set_time = mock.Mock()
        panel._report_status = lambda state: (
            media_player.MediaPlayerPanel._report_status(panel, state))

        media_player.MediaPlayerPanel._on_timer(panel, None)

        frame.set_playback_status.assert_called_once_with(
            "Playing: One More Time — 1:23 of 5:20, 3:57 left")

    def test_stopping_clears_the_playback_status_field(self):
        frame = SimpleNamespace(set_playback_status=mock.Mock())
        panel = SimpleNamespace(frame=frame, _title="One")

        media_player.MediaPlayerPanel._report_status(panel, "")

        frame.set_playback_status.assert_called_once_with("")

    def test_a_frame_without_a_playback_field_is_left_alone(self):
        # Panels are built against hosts that have no status bar at all.
        panel = SimpleNamespace(frame=SimpleNamespace(), _title="One")

        media_player.MediaPlayerPanel._report_status(panel, "Playing")

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

    def test_vlc_playing_event_ignores_a_stale_generation(self):
        panel = SimpleNamespace(
            _shutting_down=False,
            IsBeingDeleted=mock.Mock(return_value=False),
            _load_generation=2,
            _loaded=False,
            _playback_started=mock.Mock(),
        )

        # A playing event from the media just replaced must not announce.
        media_player.MediaPlayerPanel._on_vlc_playing_gui(panel, 1)
        panel._playback_started.assert_not_called()

        media_player.MediaPlayerPanel._on_vlc_playing_gui(panel, 2)
        panel._playback_started.assert_called_once_with()

    def test_media_that_finishes_before_starting_is_ignored(self):
        panel = SimpleNamespace(
            _shutting_down=False,
            IsBeingDeleted=mock.Mock(return_value=False),
            _load_generation=1,
            _loaded=False,
            timer=mock.Mock(),
            play_btn=mock.Mock(),
            position=mock.Mock(),
            frame=mock.Mock(),
        )

        media_player.MediaPlayerPanel._playback_finished(panel, 1)

        panel.timer.Stop.assert_not_called()
        panel.frame.announce.assert_not_called()

    def test_decode_error_reenables_controls(self):
        panel = SimpleNamespace(
            _shutting_down=False,
            IsBeingDeleted=mock.Mock(return_value=False),
            _load_generation=1,
            _title="Broken",
            timer=mock.Mock(),
            play_btn=mock.Mock(),
            now_playing=mock.Mock(),
            frame=mock.Mock(),
            _enable_controls=mock.Mock(),
            _report_status=mock.Mock(),
        )

        media_player.MediaPlayerPanel._playback_error(panel, 1)

        panel._enable_controls.assert_called_once_with(True)
        panel.play_btn.SetLabel.assert_called_once_with("&Play")
        panel.now_playing.SetLabel.assert_called_once_with(
            "Could not play: Broken")

    def test_stale_decode_error_does_not_touch_controls(self):
        panel = SimpleNamespace(
            _shutting_down=False,
            IsBeingDeleted=mock.Mock(return_value=False),
            _load_generation=2,
            _title="New",
            timer=mock.Mock(),
            play_btn=mock.Mock(),
            now_playing=mock.Mock(),
            frame=mock.Mock(),
            _enable_controls=mock.Mock(),
        )

        media_player.MediaPlayerPanel._playback_error(panel, 1)

        panel._enable_controls.assert_not_called()
        panel.timer.Stop.assert_not_called()

    def test_completed_download_only_rescans_visible_library(self):
        frame = SimpleNamespace(
            _closing=False,
            downloads_panel=mock.Mock(),
            queue=mock.Mock(),
            _last_counts=(0, 0, 1, 0),
            notebook=mock.Mock(),
            library_panel=mock.Mock(),
            announce=mock.Mock(),
            sounds=mock.Mock(),
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
            sounds=mock.Mock(),
            config={"auto_clear_finished": True},
        )
        frame.queue.counts.return_value = frame._last_counts
        frame.notebook.GetSelection.return_value = TAB_DOWNLOADS
        frame._schedule_auto_clear = lambda: MainFrame._auto_clear_finished(
            frame)

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

    def test_a_burst_of_finishes_clears_the_rows_once(self):
        # Every clear rewrites the queue file, waits for the disk, and
        # rebuilds both lists row by row. An album used to do that per track.
        frame = SimpleNamespace(
            _closing=False,
            downloads_panel=mock.Mock(),
            queue=mock.Mock(),
            _last_counts=(0, 0, 0, 0),
            notebook=mock.Mock(),
            library_panel=mock.Mock(),
            announce=mock.Mock(),
            sounds=mock.Mock(),
            config={"auto_clear_finished": True},
        )
        frame.queue.counts.return_value = frame._last_counts
        frame.notebook.GetSelection.return_value = TAB_DOWNLOADS
        frame._auto_clear_timer = None
        frame._schedule_auto_clear = lambda: MainFrame._schedule_auto_clear(
            frame)
        frame._auto_clear_finished = lambda: MainFrame._auto_clear_finished(
            frame)

        with mock.patch("blinddl.gui.mainframe.wx.CallLater") as call_later:
            call_later.return_value = SimpleNamespace(
                IsRunning=lambda: True, Restart=mock.Mock())
            for number in range(12):
                MainFrame._on_item_update(
                    frame, SimpleNamespace(status=STATUS_DONE,
                                           title=f"Track {number}",
                                           seeding=False))

        # One timer for the whole burst, restarted by the rest of it.
        call_later.assert_called_once()
        frame.queue.remove_completed.assert_not_called()

        MainFrame._auto_clear_finished(frame)
        frame.queue.remove_completed.assert_called_once_with()
        frame.downloads_panel.refresh_all.assert_called_once_with()
        # Every finish was still spoken as it happened.
        self.assertEqual(frame.announce.call_count, 12)

    def test_a_closing_window_does_not_clear_rows_from_a_late_timer(self):
        frame = SimpleNamespace(
            _closing=True, downloads_panel=mock.Mock(), queue=mock.Mock())

        MainFrame._auto_clear_finished(frame)

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

    # -- the results context menu -------------------------------------------

    def _results_menu_labels(self, panel):
        """Every command the results context menu offers, in menu order."""
        labels = []

        def capture(menu):
            labels.extend(
                item.GetItemLabelText()
                for item in menu.GetMenuItems()
                if not item.IsSeparator()
            )

        with mock.patch.object(panel.results_list, "PopupMenu",
                               side_effect=capture):
            panel.on_results_menu(
                SimpleNamespace(GetPosition=lambda: wx.DefaultPosition))
        return labels

    def _show(self, panel, engine, items, focus=0):
        panel.result_engine = engine
        panel.results = list(items)
        panel.results_list.SetItemCount(len(panel.results))
        if panel.results:
            panel.results_list.Focus(focus)
            panel.results_list.Select(focus)

    def test_soulseek_commands_are_absent_from_a_row_that_is_not_soulseek(self):
        # They used to be appended to every menu and merely greyed out, so a
        # user with Soulseek switched off arrowed past six dead commands to
        # reach Copy URL -- six that could never come back.
        panel = SearchPanel(self.host, self.frame)
        self.frame.config["soulseek_enabled"] = False
        self._show(panel, ENGINE_MUSIC, [
            {"title": "One", "artist": "A", "kind": "music"},
        ])

        labels = self._results_menu_labels(panel)

        for gone in ("Download containing folder", "Browse user's files",
                     "Send user a message", "Add user to friends",
                     "Give user a free slot", "View user profile"):
            self.assertNotIn(gone, labels)
        self.assertIn("Copy URL", labels)
        panel.shutdown()
        panel.Destroy()

    def test_soulseek_commands_are_there_on_a_soulseek_row(self):
        panel = SearchPanel(self.host, self.frame)
        self._show(panel, ENGINE_SOULSEEK_AUDIO, [{
            "title": "One",
            "kind": "soulseek",
            "username": "peer",
            "remote_path": "music\\one.mp3",
        }])

        labels = self._results_menu_labels(panel)

        self.assertIn("Browse user's files", labels)
        self.assertIn("View user profile", labels)
        panel.shutdown()
        panel.Destroy()

    def test_a_catalogue_row_is_offered_its_album_and_its_artist(self):
        panel = SearchPanel(self.host, self.frame)
        self._show(panel, ENGINE_DEEZER, [{
            "title": "One More Time",
            "artist": "Daft Punk",
            "album": "Discovery",
            "kind": "deezer",
            "album_id": "302127",
            "artist_id": "27",
        }])

        labels = self._results_menu_labels(panel)

        self.assertIn("Show album tracks", labels)
        self.assertIn("Show artist's releases", labels)
        # Nothing to go back to yet, so nothing offers it.
        self.assertNotIn("Go back to previous results", labels)
        panel.shutdown()
        panel.Destroy()

    def test_a_row_with_no_catalogue_behind_it_is_offered_neither(self):
        # A file on a music site has an artist's name and no catalogue to
        # look it up in, so the two commands are left out rather than shown
        # dead.
        panel = SearchPanel(self.host, self.frame)
        self._show(panel, ENGINE_MUSIC, [
            {"title": "One", "artist": "A", "kind": "music"},
        ])

        labels = self._results_menu_labels(panel)

        self.assertNotIn("Show album tracks", labels)
        self.assertNotIn("Show artist's releases", labels)
        panel.shutdown()
        panel.Destroy()

    def test_an_album_row_offers_its_track_list_and_a_playlist_does_not(self):
        panel = SearchPanel(self.host, self.frame)
        self._show(panel, ENGINE_DEEZER, [{
            "title": "Discovery",
            "artist": "Daft Punk",
            "kind": "deezer_album",
            "album_id": "302127",
            "artist_id": "27",
        }])
        self.assertIn("Show album tracks", self._results_menu_labels(panel))

        # A playlist is nobody's release: its artist column is the curator,
        # and it is not an album, so it carries neither id.
        self._show(panel, ENGINE_DEEZER, [{
            "title": "Chilled",
            "artist": "Some Curator",
            "kind": "deezer_playlist",
        }])
        labels = self._results_menu_labels(panel)
        self.assertNotIn("Show album tracks", labels)
        self.assertNotIn("Show artist's releases", labels)
        panel.shutdown()
        panel.Destroy()

    def test_a_releases_own_word_for_itself_is_not_shouted(self):
        # "mp3" is a file extension and reads as one in capitals. "Single"
        # is the word Deezer chose for the release, and SINGLE is not how it
        # should be read out.
        from blinddl.gui.search_panel import result_type

        self.assertEqual(result_type({"format": "mp3"}), "MP3")
        self.assertEqual(result_type({"format": "FLAC"}), "FLAC")
        self.assertEqual(result_type({"format": "Single"}), "Single")
        self.assertEqual(result_type({"format": "EP"}), "EP")
        self.assertEqual(
            result_type({"format": "Album, 14 tracks"}), "Album, 14 tracks")

    # -- browsing an artist or album ----------------------------------------

    def test_browsing_an_album_replaces_the_list_with_its_tracks(self):
        panel = SearchPanel(self.host, self.frame)
        row = {
            "title": "One More Time",
            "artist": "Daft Punk",
            "album": "Discovery",
            "kind": "deezer",
            "album_id": "302127",
            "artist_id": "27",
        }
        self._show(panel, ENGINE_DEEZER, [row])
        tracks = [
            {"title": "One More Time", "artist": "Daft Punk", "kind": "deezer"},
            {"title": "Aerodynamic", "artist": "Daft Punk", "kind": "deezer"},
        ]

        with (
            mock.patch.object(
                deezer_backend, "album_items",
                return_value=(tracks, "Discovery")) as album_items,
            mock.patch(
                "blinddl.gui.search_panel.wx.CallAfter",
                side_effect=lambda callback, *args: callback(*args),
            ),
            mock.patch(
                "blinddl.gui.search_panel.threading.Thread",
                side_effect=lambda target, args, **kwargs: SimpleNamespace(
                    start=lambda: target(*args)),
            ),
        ):
            panel.browse_album(row)

        album_items.assert_called_once_with("302127")
        self.assertEqual([item["title"] for item in panel.results],
                         ["One More Time", "Aerodynamic"])
        # The running order of the release is the answer, so it is not then
        # re-ranked by whatever the last search was sorted by.
        self.assertEqual([item["_search_order"] for item in panel.results],
                         [0, 1])
        self.assertIn("Discovery: 2 tracks", self.frame.messages[-1])
        panel.shutdown()
        panel.Destroy()

    def test_browsing_an_artist_lists_their_releases_and_can_be_stepped_back(self):
        panel = SearchPanel(self.host, self.frame)
        row = {
            "title": "One More Time",
            "artist": "Daft Punk",
            "kind": "deezer",
            "album_id": "302127",
            "artist_id": "27",
        }
        self._show(panel, ENGINE_DEEZER, [row])
        releases = [
            {"title": "Discovery", "artist": "Daft Punk",
             "kind": "deezer_album", "album_id": "1", "artist_id": "27"},
            {"title": "Homework", "artist": "Daft Punk",
             "kind": "deezer_album", "album_id": "2", "artist_id": "27"},
        ]

        with (
            mock.patch.object(
                deezer_backend, "artist_albums",
                return_value=(releases, "Daft Punk")),
            mock.patch(
                "blinddl.gui.search_panel.wx.CallAfter",
                side_effect=lambda callback, *args: callback(*args),
            ),
            mock.patch(
                "blinddl.gui.search_panel.threading.Thread",
                side_effect=lambda target, args, **kwargs: SimpleNamespace(
                    start=lambda: target(*args)),
            ),
        ):
            panel.browse_artist(row)

        self.assertEqual([item["title"] for item in panel.results],
                         ["Discovery", "Homework"])
        self.assertIn("Daft Punk: 2 releases", self.frame.messages[-1])

        # The way in has a way out: the search that was showing comes back,
        # with the row that was being read still under the cursor.
        panel.browse_back()
        self.assertEqual([item["title"] for item in panel.results],
                         ["One More Time"])
        self.assertEqual(panel.browse_history, [])
        panel.browse_back()
        self.assertEqual(self.frame.messages[-1],
                         "There is nothing to go back to.")
        panel.shutdown()
        panel.Destroy()

    def test_a_catalogue_that_answers_with_nothing_leaves_the_list_alone(self):
        panel = SearchPanel(self.host, self.frame)
        row = {"title": "One", "artist": "A", "kind": "deezer",
               "album_id": "9", "artist_id": "27"}
        self._show(panel, ENGINE_DEEZER, [row])

        with (
            mock.patch.object(
                deezer_backend, "album_items", return_value=([], "")),
            mock.patch(
                "blinddl.gui.search_panel.wx.CallAfter",
                side_effect=lambda callback, *args: callback(*args),
            ),
            mock.patch(
                "blinddl.gui.search_panel.threading.Thread",
                side_effect=lambda target, args, **kwargs: SimpleNamespace(
                    start=lambda: target(*args)),
            ),
        ):
            panel.browse_album(row)

        self.assertEqual([item["title"] for item in panel.results], ["One"])
        self.assertEqual(panel.browse_history, [])
        self.assertEqual(self.frame.messages[-1],
                         "Nothing listed for that album.")
        self.assertTrue(panel.search_btn.IsEnabled())
        panel.shutdown()
        panel.Destroy()

    def test_a_browse_that_fails_says_so_and_keeps_the_results(self):
        panel = SearchPanel(self.host, self.frame)
        row = {"title": "One", "artist": "A", "kind": "applemusic",
               "album_id": "9", "artist_id": "27"}
        self._show(panel, ENGINE_APPLE_MUSIC, [row])

        with (
            mock.patch.object(
                applemusic_backend, "artist_albums",
                side_effect=RuntimeError("offline")),
            mock.patch(
                "blinddl.gui.search_panel.wx.CallAfter",
                side_effect=lambda callback, *args: callback(*args),
            ),
            mock.patch(
                "blinddl.gui.search_panel.threading.Thread",
                side_effect=lambda target, args, **kwargs: SimpleNamespace(
                    start=lambda: target(*args)),
            ),
            mock.patch("blinddl.gui.search_panel.wx.MessageBox") as box,
        ):
            panel.browse_artist(row)

        box.assert_called_once()
        self.assertEqual([item["title"] for item in panel.results], ["One"])
        self.assertEqual(self.frame.messages[-1],
                         "Could not read that artist.")
        self.assertTrue(panel.search_btn.IsEnabled())
        panel.shutdown()
        panel.Destroy()

    def test_alt_left_and_backspace_step_back_out_of_a_browse(self):
        panel = SearchPanel(self.host, self.frame)
        skipped = []

        def key(code, alt=False):
            return SimpleNamespace(
                GetKeyCode=lambda: code,
                AltDown=lambda: alt,
                Skip=lambda: skipped.append(code),
            )

        # Nothing to go back to: the list keeps the key, since Backspace is
        # the reader's own and must not be eaten for nothing.
        panel.on_results_key(key(wx.WXK_BACK))
        self.assertEqual(skipped, [wx.WXK_BACK])

        with mock.patch.object(panel, "browse_back") as back:
            panel.browse_history = [{"results": []}]
            panel.on_results_key(key(wx.WXK_LEFT, alt=True))
            panel.on_results_key(key(wx.WXK_BACK))
            self.assertEqual(back.call_count, 2)

            # A bare Left arrow still belongs to the list.
            panel.on_results_key(key(wx.WXK_LEFT))
            self.assertEqual(back.call_count, 2)

        self.assertEqual(skipped, [wx.WXK_BACK, wx.WXK_LEFT])
        panel.shutdown()
        panel.Destroy()

    def test_a_new_search_leaves_no_browse_to_go_back_to(self):
        panel = SearchPanel(self.host, self.frame)
        panel.browse_history = [{"results": []}]
        panel.query_text.SetValue("daft punk")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_DEEZER)
        )

        with mock.patch("blinddl.gui.search_panel.threading.Thread"):
            panel.on_search(None)

        self.assertEqual(panel.browse_history, [])
        panel.shutdown()
        panel.Destroy()

    def test_side_b_rows_browse_through_deezers_catalogue(self):
        # Side B reads Deezer's catalogue, so its rows carry Deezer ids and
        # open the same pages a Deezer row does.
        from blinddl.gui.search_panel import _browse_backend

        self.assertIs(_browse_backend({"kind": "sideb"}), deezer_backend)
        self.assertIs(_browse_backend({"kind": "deezer_album"}),
                      deezer_backend)
        self.assertIs(_browse_backend({"kind": "applemusic_album"}),
                      applemusic_backend)
        self.assertIsNone(_browse_backend({"kind": "soulseek"}))
        self.assertIsNone(_browse_backend(None))

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
        panel.shutdown()
        panel.Destroy()

    def test_a_rows_dedup_key_is_worked_out_once_however_often_it_flushes(self):
        # The key costs two regular expressions, and the index it fills was
        # rebuilt from nothing on every flush -- so a search that answered
        # site by site re-keyed every row it had, over and over, on the
        # thread drawing the list being read.
        panel = SearchPanel(self.host, self.frame)
        original = panel._dedup_key
        with mock.patch.object(panel, "_dedup_key", wraps=original) as dedup:
            for number in range(50):
                panel._insert_deduped(
                    {"title": f"Track {number}", "artist": "Artist"}
                )
            with mock.patch.object(panel, "_render_results"):
                for _ in range(5):
                    panel._flush_results()

        self.assertEqual(dedup.call_count, 50)
        panel.shutdown()
        panel.Destroy()

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

    def test_best_match_ranks_by_score_not_by_which_site_replied_first(self):
        """Music fans out over three dozen sites, so arrival order is
        near enough alphabetical by site name."""
        items = [
            {"title": "noise", "source": "FiveSing", "score": 8.0,
             "_search_order": 0},
            {"title": "the answer", "source": "FreeQobuz", "score": 93.0,
             "_search_order": 1},
            {"title": "half an answer", "source": "JioSaavn", "score": 50.0,
             "_search_order": 2},
        ]
        self.assertEqual(
            [item["title"] for item in _sorted_results(
                items, SORT_RELEVANCE, ENGINE_MUSIC,
                search_order.ORDER_RELEVANCE)],
            ["the answer", "half an answer", "noise"],
        )

    def test_most_recent_keeps_the_order_the_sites_replied_in(self):
        """This slot is holding the order that was asked of the sites;
        re-sorting it by score would undo what the user chose."""
        items = [
            {"title": "newest", "score": 20.0, "_search_order": 0},
            {"title": "older", "score": 90.0, "_search_order": 1},
        ]
        self.assertEqual(
            [item["title"] for item in _sorted_results(
                items, SORT_RELEVANCE, ENGINE_BOOKS,
                search_order.ORDER_RECENT)],
            ["newest", "older"],
        )

    def test_rows_without_a_score_are_still_ordered_as_they_arrived(self):
        items = [
            {"title": "second", "_search_order": 1},
            {"title": "first", "_search_order": 0},
        ]
        self.assertEqual(
            [item["title"] for item in _sorted_results(items, SORT_RELEVANCE)],
            ["first", "second"],
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

    def test_artist_albums_scope_asks_only_the_catalogue(self):
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
                          search_order.ORDER_RELEVANCE, search_kind.KIND_ARTIST,
                          search_kind.ARTIST_SCOPE_ALBUMS)
            for thread in threading.enumerate():
                if thread.name == "search-deezer":
                    thread.join(timeout=5)

        musicdl_search.assert_not_called()
        sideb_search.assert_not_called()

    def test_artist_songs_scope_still_asks_every_music_site(self):
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
                          search_order.ORDER_RELEVANCE, search_kind.KIND_ARTIST,
                          search_kind.ARTIST_SCOPE_SONGS)
            for thread in threading.enumerate():
                if thread.name == "search-deezer":
                    thread.join(timeout=5)

        musicdl_search.assert_called_once()
        sideb_search.assert_called_once()
        self.assertEqual(
            deezer.call_args.kwargs["artist_scope"],
            search_kind.ARTIST_SCOPE_SONGS,
        )

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

    def test_stop_search_stays_on_the_page_and_switches_on_with_a_search(self):
        panel = SearchPanel(self.host, self.frame)

        # Always there to be tabbed to, so its place in the row never moves
        # under a screen reader; off until there is a search to stop.
        self.assertTrue(panel.stop_btn.IsShown())
        self.assertFalse(panel.stop_btn.IsEnabled())

        panel.query_text.SetValue("ambient")
        panel.engine_choice.SetSelection(
            panel.visible_engines.index(ENGINE_YOUTUBE))
        with mock.patch.object(panel, "_search"):
            panel.on_search(None)
        self.assertTrue(panel.stop_btn.IsEnabled())

        panel.on_stop_search(None)
        self.assertTrue(panel.stop_btn.IsShown())
        self.assertFalse(panel.stop_btn.IsEnabled())
        self.assertTrue(panel.search_btn.IsEnabled())

    def test_stop_stays_live_while_slow_sites_are_still_answering(self):
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("ambient")
        with mock.patch.object(panel, "_search"):
            panel.on_search(None)
        panel.shown_sources = {"Quick"}

        # Each site runs on its own thread, so the search reports in while
        # the slow ones are still going. Stop belongs to them too.
        panel._search_done(panel.token, [], ENGINE_MUSIC,
                           asked=["Quick", "Slow"])
        self.assertTrue(panel.stop_btn.IsEnabled())

        panel._add_site(panel.token, ENGINE_MUSIC, "Slow",
                        [{"title": "Late", "url": "https://example/late"}])
        self.assertFalse(panel.stop_btn.IsEnabled())

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

        with mock.patch.object(panel, "_queue_collection_items") as queue_collections:
            panel.on_download_selected(None)

        queue_collections.assert_called_once_with([album])
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
            panel._collection_tracks_ready(
                panel.collection_token, [(album, tracks)], []
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

        panel._collection_tracks_ready(
            panel.collection_token, [(album, tracks)], []
        )

        self.assertEqual(self.frame.queue.folders, ["Daft Punk - Discovery"])

        # An album row with no artist named still gets its own folder.
        self.frame.queue.folders.clear()
        panel._collection_tracks_ready(
            panel.collection_token,
            [({ "title": "Untitled", "kind": "deezer_album"}, tracks)],
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

        panel._collection_tracks_ready(panel.collection_token, resolved, [])

        self.assertEqual(
            [(call[0], call[2]) for call in self.frame.queue.calls],
            [("applemusic", "One"), ("applemusic", "Two")],
        )

    def test_an_album_that_cannot_be_read_is_reported_not_silently_dropped(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        token = panel.collection_token = object()

        with mock.patch.object(
            deezer_backend, "extract_flat", side_effect=RuntimeError("gone")
        ), mock.patch.object(wx, "CallAfter") as call_after:
            panel._resolve_collection_tracks(
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

    def test_starting_a_preview_cancels_an_inflight_full_playback(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        panel.results = [{"title": "One", "kind": "deezer"}]
        panel.results_list.SetItemCount(1)
        panel.results_list.Select(0)
        panel.full_playback_token = object()
        panel.play_full_btn.Disable()

        with mock.patch.object(threading, "Thread"):
            panel.on_preview_selected(None)

        self.assertIsNone(panel.full_playback_token)
        self.assertTrue(panel.play_full_btn.IsEnabled())
        self.assertIsNotNone(panel.preview_token)
        self.assertFalse(panel.preview_btn.IsEnabled())

    def test_starting_full_playback_cancels_an_inflight_preview(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_DEEZER
        panel.results = [{"title": "One", "kind": "deezer"}]
        panel.results_list.SetItemCount(1)
        panel.results_list.Select(0)
        panel.preview_token = object()
        panel.preview_btn.Disable()

        with mock.patch.object(threading, "Thread"):
            panel.on_play_full_selected(None)

        self.assertIsNone(panel.preview_token)
        self.assertTrue(panel.preview_btn.IsEnabled())
        self.assertIsNotNone(panel.full_playback_token)
        self.assertFalse(panel.play_full_btn.IsEnabled())

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
        dialog.cookies_choice.SetSelection(2)
        dialog.onlyfans_auth_picker.SetPath("onlyfans.json")
        dialog.justforfans_auth_picker.SetPath("justforfans.json")
        dialog.apply()

        self.assertTrue(config["adult_sites_enabled"])
        self.assertEqual(config["cookies_from_browser"], "chrome")
        self.assertEqual(config["onlyfans_auth_file"], "onlyfans.json")
        self.assertEqual(config["justforfans_auth_file"], "justforfans.json")
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_settings_offers_the_online_metadata_lookup_and_saves_it(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        # On by default: a music file with no album artist, track number or
        # artwork is one a library cannot file.
        self.assertTrue(dialog.metadata_check.GetValue())
        dialog.metadata_check.SetValue(False)
        dialog.apply()

        self.assertFalse(config["music_metadata_lookup"])
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

    def test_automatic_update_is_one_setting(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        # The setting means check, download, verify, and install.
        self.assertTrue(dialog.update_check.GetValue())
        dialog.apply()

        self.assertTrue(config["auto_update"])
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

    def test_one_message_costs_one_look_for_the_screen_reader(self):
        # Auto.speak() and Auto.braille() each walk every installed output
        # asking whether it is running, and the walk costs milliseconds on
        # the thread trying to talk. A search reports fifty sites.
        speech.reset()
        self.addCleanup(speech.reset)

        class _Auto:
            def __init__(self):
                self.probes = 0
                self.spoken = []
                self.brailled = []

            def get_first_available_output(self):
                self.probes += 1
                return self

            def speak(self, text, interrupt=False):
                self.spoken.append(text)
                return True

            def braille(self, text):
                self.brailled.append(text)
                return True

        output = _Auto()
        with mock.patch.object(speech, "_accessible_output",
                               return_value=output):
            for index in range(5):
                speech.speak(f"Searching site {index}.")

        self.assertEqual(output.probes, 1)
        self.assertEqual(len(output.spoken), 5)
        self.assertEqual(len(output.brailled), 5)

    def test_an_output_that_cannot_be_resolved_is_still_spoken_to(self):
        speech.reset()
        self.addCleanup(speech.reset)

        class _Plain:
            def __init__(self):
                self.spoken = []

            def speak(self, text, interrupt=False):
                self.spoken.append(text)
                return True

            def braille(self, text):
                return True

        output = _Plain()
        with mock.patch.object(speech, "_accessible_output",
                               return_value=output):
            self.assertTrue(speech.speak("Twelve results found."))

        self.assertEqual(output.spoken, ["Twelve results found."])

    def test_jaws_is_not_hunted_for_on_every_sentence(self):
        # The test underneath snapshots every process on the machine, and it
        # costs the same whether or not JAWS is installed.
        speech.reset()
        self.addCleanup(speech.reset)
        with mock.patch.object(speech, "_process_running",
                               return_value=False) as running:
            for _ in range(5):
                self.assertFalse(speech._jaws_is_running())

        running.assert_called_once()

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

    def test_settings_pages_are_five_logical_tabs(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertEqual(
            [dialog.notebook.GetPageText(index)
             for index in range(dialog.notebook.GetPageCount())],
            ["Downloads", "Torrents", "Soulseek", "Interface", "Accounts"],
        )
        dialog.Destroy()

    def test_auto_cookies_is_opt_in_and_sets_the_combo_to_auto(self):
        config = _SettingsConfig()
        config["cookies_from_browser"] = ""
        dialog = SettingsDialog(self.host, config)

        # Off by default: no browser is touched, and the combo reads None.
        self.assertFalse(dialog.cookies_auto_check.GetValue())
        self.assertTrue(dialog.cookies_choice.IsEnabled())
        self.assertEqual(dialog.cookies_choice.GetStringSelection(), "None")

        # Turning the opt-in on selects Auto and locks the combo to it.
        dialog.cookies_auto_check.SetValue(True)
        dialog.cookies_auto_check.ProcessEvent(
            wx.CommandEvent(
                wx.wxEVT_CHECKBOX, dialog.cookies_auto_check.GetId()
            )
        )
        self.assertEqual(
            dialog.cookies_choice.GetStringSelection(),
            "Auto (any installed browser)",
        )
        self.assertFalse(dialog.cookies_choice.IsEnabled())
        dialog.apply()

        self.assertEqual(config["cookies_from_browser"], "auto")
        self.assertTrue(config.saved)
        dialog.Destroy()

    def test_auto_cookies_checkbox_off_puts_the_combo_back_to_none(self):
        config = _SettingsConfig()
        config["cookies_from_browser"] = "auto"
        dialog = SettingsDialog(self.host, config)

        self.assertTrue(dialog.cookies_auto_check.GetValue())
        self.assertFalse(dialog.cookies_choice.IsEnabled())
        self.assertEqual(
            dialog.cookies_choice.GetStringSelection(),
            "Auto (any installed browser)",
        )

        dialog.cookies_auto_check.SetValue(False)
        dialog.cookies_auto_check.ProcessEvent(
            wx.CommandEvent(
                wx.wxEVT_CHECKBOX, dialog.cookies_auto_check.GetId()
            )
        )
        self.assertTrue(dialog.cookies_choice.IsEnabled())
        self.assertEqual(dialog.cookies_choice.GetStringSelection(), "None")
        dialog.Destroy()

    def test_file_pickers_name_the_text_box_nvda_focuses(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        expected = {
            "cookies_file_picker": "Cookies file",
            "am_cookies_picker": "Apple Music cookies file",
            "onlyfans_auth_picker": "OnlyFans auth JSON file",
            "justforfans_auth_picker": "JustForFans auth JSON file",
        }
        for attribute, name in expected.items():
            picker = getattr(dialog, attribute)
            self.assertEqual(picker.GetName(), name)
            # The inner text box exists on Windows and macOS, where focus
            # lands in it; wxGTK renders a file picker without one, in which
            # case the picker's own name is all there is to read.
            text = (
                picker.GetTextCtrl() if hasattr(picker, "GetTextCtrl") else None
            )
            if text is not None:
                self.assertEqual(text.GetName(), name)
        dialog.Destroy()

    def test_apple_music_copy_button_says_what_it_copies(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)

        self.assertEqual(
            dialog.am_from_browser.GetName(),
            "Copy Apple Music cookies from browser",
        )
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
        dialog.adult_check_list.CheckItem(pornhub_index, False)
        dialog.apply()

        self.assertIn("pornhub", config["disabled_adult_sources"])
        self.assertNotIn("eporner", config["disabled_adult_sources"])
        dialog.Destroy()

    def test_sources_dialog_uses_accessible_checkbox_lists(self):
        # wx.CheckListBox hides the checked state from NVDA on Windows, so
        # every source list must be a report ListCtrl with a checkbox column.
        config = _SettingsConfig()
        dialog = SourcesDialog(self.host, config)

        for attribute in (
            "check_list",
            "book_check_list",
            "audiobook_check_list",
            "archive_check_list",
            "torrent_check_list",
            "adult_check_list",
        ):
            control = getattr(dialog, attribute)
            self.assertIsInstance(control, wx.ListCtrl, attribute)
            self.assertTrue(control.GetName(), attribute)
            self.assertTrue(hasattr(control, "IsItemChecked"), attribute)
        dialog.Destroy()

    def test_soulseek_user_browser_buttons_say_what_they_act_on(self):
        frame = self.frame
        frame.config = _SettingsConfig()
        dialog = UserBrowserDialog(self.host, frame)

        expected = {
            "browse_button": "Browse this user's files",
            "message_button": "Message this Soulseek user",
            "friend_button": "Add this user as a friend",
            "slot_button": "Give this user a free slot",
            "profile_button": "View this user's profile",
        }
        for attribute, name in expected.items():
            self.assertEqual(getattr(dialog, attribute).GetName(), name)
        dialog.Destroy()

    def test_feeds_dialog_buttons_say_what_they_act_on(self):
        config = _SettingsConfig()
        dialog = FeedsDialog(self.host, config)

        self.assertEqual(dialog.add_btn.GetName(), "Add indexer")
        self.assertEqual(dialog.edit_btn.GetName(), "Edit indexer")
        self.assertEqual(dialog.remove_btn.GetName(), "Remove indexer")
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

    @staticmethod
    def _key_event(key_code, shift=False, source=None):
        event = mock.Mock()
        event.GetKeyCode.return_value = key_code
        event.ShiftDown.return_value = shift
        event.GetEventObject.return_value = source
        return event

    def test_downloads_delete_key_removes_the_selection(self):
        panel = DownloadsPanel(self.host, self.frame)
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        self.frame.queue.items = [done]
        panel.refresh_all()
        panel.finished_list.Select(0)

        panel.on_list_key(self._key_event(
            wx.WXK_DELETE, source=panel.finished_list))

        self.assertEqual(self.frame.queue.items, [])
        self.assertIn("Removed 1 download", self.frame.messages[-1])
        panel.Destroy()

    def test_downloads_shift_delete_deletes_data_after_confirm(self):
        panel = DownloadsPanel(self.host, self.frame)
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        done.result_path = "/media/two.mp3"
        self.frame.queue.items = [done]
        panel.refresh_all()
        panel.finished_list.Select(0)

        with mock.patch("blinddl.gui.downloads_panel.wx.MessageBox",
                        return_value=wx.YES) as box:
            panel.on_list_key(self._key_event(
                wx.WXK_DELETE, shift=True, source=panel.finished_list))

        box.assert_called_once()
        self.assertEqual(self.frame.queue.items, [])
        self.assertIn("Deleted data for 1 download", self.frame.messages[-1])
        panel.Destroy()

    def test_downloads_shift_delete_cancel_deletes_nothing(self):
        panel = DownloadsPanel(self.host, self.frame)
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        done.result_path = "/media/two.mp3"
        self.frame.queue.items = [done]
        panel.refresh_all()
        panel.finished_list.Select(0)

        with mock.patch("blinddl.gui.downloads_panel.wx.MessageBox",
                        return_value=wx.NO):
            panel.on_list_key(self._key_event(
                wx.WXK_DELETE, shift=True, source=panel.finished_list))

        self.assertEqual(self.frame.queue.items, [done])
        panel.Destroy()

    def test_downloads_delete_ignores_a_running_or_seeding_row(self):
        panel = DownloadsPanel(self.host, self.frame)
        running = DownloadItem("Running", "ytdlp", "one")
        running.status = STATUS_DOWNLOADING
        seeding = DownloadItem("Seeding", "torrent", "two")
        seeding.status = STATUS_DONE
        seeding.seeding = True
        self.frame.queue.items = [running, seeding]
        panel.refresh_all()
        panel.list.Select(0)

        panel.on_list_key(self._key_event(wx.WXK_DELETE, source=panel.list))

        self.assertEqual(self.frame.queue.items, [running, seeding])
        self.assertIn("still downloading or seeding", self.frame.messages[-1])
        panel.Destroy()

    def test_downloads_clear_selection_empties_both_lists(self):
        panel = DownloadsPanel(self.host, self.frame)
        queued = DownloadItem("Queued", "ytdlp", "one")
        queued.status = STATUS_QUEUED
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        self.frame.queue.items = [queued, done]
        panel.refresh_all()
        panel.list.Select(0)
        panel.finished_list.Select(0)

        panel._clear_selection(None, panel.list)
        panel._clear_selection(None, panel.finished_list)

        self.assertEqual(panel.list.GetSelectedItemCount(), 0)
        self.assertEqual(panel.finished_list.GetSelectedItemCount(), 0)
        panel.Destroy()

    def test_downloads_clear_finished_keeps_active_and_seeding_rows(self):
        panel = DownloadsPanel(self.host, self.frame)
        queued = DownloadItem("Queued", "ytdlp", "one")
        queued.status = STATUS_QUEUED
        failed = DownloadItem("Failed", "ytdlp", "two")
        failed.status = STATUS_ERROR
        seeding = DownloadItem("Seeding", "torrent", "three")
        seeding.status = STATUS_DONE
        seeding.seeding = True
        self.frame.queue.items = [queued, failed, seeding]
        panel.refresh_all()

        panel.on_clear(None)

        self.assertEqual(self.frame.queue.items, [queued, seeding])
        self.assertIn("Cleared 1 finished download", self.frame.messages[-1])
        panel.Destroy()

    def test_uploads_delete_key_dispatches_remove(self):
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
                soulseek_backend, "uploads_snapshot", return_value=[complete]
            ),
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.uploads",
                return_value=[],
            ),
        ):
            panel = UploadsPanel(self.host, self.frame)
        panel.finished_list.Select(0)

        with mock.patch.object(panel, "_run_action") as run:
            panel.on_list_key(self._key_event(
                wx.WXK_DELETE, source=panel.finished_list))

        run.assert_called_once()
        rows, action = run.call_args.args[:2]
        self.assertEqual([row["key"] for row in rows], ["peer\\done"])
        self.assertEqual(action, panel._remove_row)
        panel.shutdown()
        panel.Destroy()

    def test_uploads_shift_delete_deletes_data_after_confirm(self):
        complete = {
            "key": "peer\\done",
            "title": "Done.flac",
            "service": "Soulseek",
            "peer": "peer",
            "status": "Complete",
            "percent": 100,
            "speed": 0,
            "active": False,
            "path": "/shares/Done.flac",
        }
        with (
            mock.patch.object(
                soulseek_backend, "uploads_snapshot", return_value=[complete]
            ),
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.uploads",
                return_value=[],
            ),
        ):
            panel = UploadsPanel(self.host, self.frame)
        panel.finished_list.Select(0)

        with (
            mock.patch("blinddl.gui.uploads_panel.wx.MessageBox",
                       return_value=wx.YES),
            mock.patch.object(panel, "_run_action") as run,
        ):
            panel.on_list_key(self._key_event(
                wx.WXK_DELETE, shift=True, source=panel.finished_list))

        run.assert_called_once()
        rows, action = run.call_args.args[:2]
        self.assertEqual([row["key"] for row in rows], ["peer\\done"])
        # The action passes delete_data through to the backend.
        with mock.patch.object(
            soulseek_backend, "remove_upload", return_value=True
        ) as remove_upload:
            self.assertTrue(action(complete))
            remove_upload.assert_called_once_with(
                "peer\\done", delete_data=True)
        panel.shutdown()
        panel.Destroy()

    def test_uploads_remove_row_routes_to_torrent_or_soulseek(self):
        panel = UploadsPanel(self.host, self.frame)
        torrent_row = {
            "key": "hash", "title": "Release", "service": "BitTorrent",
        }
        soulseek_row = {
            "key": "peer\\file", "title": "Shared.flac",
            "service": soulseek_backend.SOURCE,
        }
        with (
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.stop_seeding",
                return_value=True,
            ) as stop_seeding,
            mock.patch(
                "blinddl.gui.uploads_panel.torrent_engine.delete_seed",
                return_value=True,
            ) as delete_seed,
            mock.patch.object(
                soulseek_backend, "remove_upload", return_value=True
            ) as remove_upload,
        ):
            self.assertTrue(panel._remove_row(torrent_row))
            stop_seeding.assert_called_once_with("hash")
            self.assertTrue(panel._remove_row(torrent_row, delete_data=True))
            delete_seed.assert_called_once_with("hash")
            self.assertTrue(panel._remove_row(soulseek_row))
            remove_upload.assert_called_once_with(
                "peer\\file", delete_data=False)
        panel.shutdown()
        panel.Destroy()

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

    def test_subscription_interval_is_controlled_from_the_subscriptions_tab(self):
        panel = SubsPanel(self.host, self.frame)
        panel.interval_spin.SetValue(2)

        panel.on_interval_changed(None)

        self.assertEqual(self.frame.config["sub_check_hours"], 2)
        self.assertTrue(self.frame.config.saved)
        self.assertEqual(self.frame.subs.wake_count, 1)
        self.assertEqual(
            self.frame.messages[-1], "Subscriptions will update every 2 hours.")

    def test_adding_a_subscription_only_reads_the_recent_window(self):
        panel = SubsPanel(self.host, self.frame)
        with mock.patch.object(
            ytdlp_backend, "extract_flat", return_value=([], "Channel")
        ) as extract, mock.patch("blinddl.gui.subs_panel.wx.CallAfter"):
            panel._add_worker(
                "https://www.youtube.com/user/EuphoricHardStyleZ", False)

        self.assertEqual(
            extract.call_args.kwargs["limit"],
            ytdlp_backend.SUBSCRIPTION_FEED_LIMIT,
        )


if __name__ == "__main__":
    unittest.main()
