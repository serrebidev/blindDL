# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import wx

# musicdl creates a file logger at import time. Keep GUI tests self-contained.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl import adult_backend, preview, ytdlp_backend
    from blinddl.downloader import (
        DownloadItem,
        STATUS_DONE,
        STATUS_QUEUED,
    )
    from blinddl import config as config_module
    from blinddl.config import DEFAULTS
    from blinddl.gui.downloads_panel import DownloadsPanel
    from blinddl.gui.item_picker_dialog import ItemPickerDialog
    from blinddl.gui.library_panel import discover_media
    from blinddl.gui.mainframe import MainFrame, TAB_DOWNLOADS, TAB_LIBRARY
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
        ENGINE_ARCHIVE_AUDIO,
        ENGINE_ARCHIVE_VIDEO,
        ENGINE_AUDIOBOOKS,
        ENGINE_BOOKS,
        ENGINE_LABELS,
        ENGINE_MUSIC,
        ENGINE_TRANS,
        GENERAL_ENGINE_COUNT,
        SORT_ARTIST,
        SORT_LABELS,
        SORT_LONGEST,
        SORT_NAME,
        SORT_RELEVANCE,
        SORT_SHORTEST,
        SORT_SITE,
        SearchPanel,
        _sorted_results,
    )
    from blinddl.gui.settings_dialog import SettingsDialog
    from blinddl.gui.sources_dialog import SourcesDialog
    from blinddl.gui.subs_panel import SubsPanel


def _clipboard_text():
    data = wx.TextDataObject()
    wx.TheClipboard.Open()
    try:
        wx.TheClipboard.GetData(data)
    finally:
        wx.TheClipboard.Close()
    return data.GetText()


class _Queue:
    def __init__(self):
        self.items = []
        self.calls = []

    def add_ytdlp(self, url, title, audio_only=None):
        self.calls.append(("ytdlp", url, title, audio_only))

    def add_musicdl(self, song, title):
        self.calls.append(("musicdl", song, title))

    def add_sideb(self, url, title):
        self.calls.append(("sideb", url, title))

    def add_adult(self, payload, title):
        self.calls.append(("adult", payload, title))

    def add_book(self, payload, title):
        self.calls.append(("book", payload, title))

    def add_audiobook(self, payload, title):
        self.calls.append(("audiobook", payload, title))

    def add_archive(self, payload, title):
        self.calls.append(("archive", payload, title))

    def _find(self, item_id):
        return next((item for item in self.items if item.id == item_id), None)

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is not None:
            item.cancel_event.set()

    def remove_finished(self):
        self.items = [item for item in self.items if item.status != STATUS_DONE]


class _SettingsConfig(dict):
    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))
        self.saved = False

    def save(self):
        self.saved = True


class _Subscriptions:
    def __init__(self):
        self.rows = [
            {"id": "one", "title": "One", "url": "one", "enabled": True,
             "seen_ids": []},
            {"id": "two", "title": "Two", "url": "two", "enabled": True,
             "seen_ids": []},
        ]

    def snapshot(self):
        return [dict(row) for row in self.rows]

    def set_enabled(self, sub_id, enabled):
        next(row for row in self.rows if row["id"] == sub_id)["enabled"] = enabled


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
        }
        self.queue = _Queue()
        self.subs = _Subscriptions()
        self.messages = []
        self.play_calls = []

    def announce(self, message):
        self.messages.append(message)

    def on_choose_sources(self):
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

    def test_search_queues_every_selected_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = 1
        panel.results = [
            {"title": "One", "url": "https://example/one"},
            {"title": "Two", "url": "https://example/two"},
        ]
        for row, item in enumerate(panel.results):
            panel.results_list.InsertItem(row, item["title"])
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
                download_url=[{"url": "https://media.example/one.mp3"}]),
        }

        location, title = preview.resolve_search_result(
            item, audio_only=True, config={})

        self.assertEqual(location, "https://media.example/one.mp3")
        self.assertEqual(title, "One")

    def test_direct_media_url_bypasses_ytdlp_extraction(self):
        with mock.patch.object(ytdlp_backend, "resolve_stream") as resolve:
            location, title = preview.resolve_url(
                "https://media.example/live/video.mp4?token=one",
                audio_only=False,
                config={},
            )

        self.assertEqual(
            location, "https://media.example/live/video.mp4?token=one")
        self.assertEqual(title, location)
        resolve.assert_not_called()

    def test_preview_accepts_the_real_config_object(self):
        # The app hands preview a Config, not a dict. Dicts have .get, so a
        # dict here would hide a missing Config.get entirely.
        with mock.patch.object(config_module, "app_data_dir",
                               return_value=tempfile.mkdtemp()):
            config = config_module.Config()
        item = {
            "kind": "adult",
            "title": "One",
            "source": "EPorner",
            "url": "https://www.eporner.com/video-one/",
        }

        with mock.patch.object(ytdlp_backend, "resolve_stream",
                               return_value="https://cdn.example/one.mp4"):
            location, title = preview.resolve_search_result(
                item, audio_only=False, config=config)

        self.assertEqual(location, "https://cdn.example/one.mp4")
        self.assertEqual(title, "One")

    def test_result_url_prefers_page_url_then_falls_back(self):
        self.assertEqual(
            preview.result_url({"url": "https://www.eporner.com/video-one/",
                                "direct_url": "https://cdn.example/one.mp4"}),
            "https://www.eporner.com/video-one/",
        )
        self.assertEqual(
            preview.result_url({"direct_url": "https://cdn.example/one.mp4"}),
            "https://cdn.example/one.mp4",
        )
        self.assertEqual(
            preview.result_url({"song_info": SimpleNamespace(
                download_url=[{"url": "https://media.example/one.mp3"}])}),
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
        for row, item in enumerate(panel.results):
            panel.results_list.InsertItem(row, item["title"])

        panel.results_list.Select(0)
        panel.on_copy_url(None)
        self.assertEqual(_clipboard_text(), "https://www.eporner.com/video-one/")
        self.assertEqual(self.frame.messages[-1], "Copied 1 URL.")

        panel.results_list.Select(1)
        panel.results_list.Select(2)
        panel.on_copy_url(None)
        self.assertEqual(
            _clipboard_text(),
            "https://www.eporner.com/video-one/\n"
            "https://www.eporner.com/video-two/",
        )
        self.assertEqual(self.frame.messages[-1], "Copied 2 URLs. 1 had no URL.")

    def test_copy_url_reports_when_no_result_has_a_link(self):
        panel = SearchPanel(self.host, self.frame)
        panel.results = [{"title": "One"}]
        panel.results_list.InsertItem(0, "One")
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
                mock.patch.object(
                    media_player.sys, "_MEIPASS", str(root), create=True),
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

    def test_completed_download_only_rescans_visible_library(self):
        frame = SimpleNamespace(
            _closing=False,
            downloads_panel=mock.Mock(),
            queue=mock.Mock(),
            _last_counts=(0, 0, 1, 0),
            notebook=mock.Mock(),
            library_panel=mock.Mock(),
            announce=mock.Mock(),
        )
        frame.queue.counts.return_value = frame._last_counts
        item = SimpleNamespace(status=STATUS_DONE, title="Finished")

        frame.notebook.GetSelection.return_value = TAB_DOWNLOADS
        MainFrame._on_item_update(frame, item)
        frame.library_panel.refresh.assert_not_called()

        frame.notebook.GetSelection.return_value = TAB_LIBRARY
        MainFrame._on_item_update(frame, item)
        frame.library_panel.refresh.assert_called_once_with(announce=False)

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

    def test_search_queues_adult_api_result(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ADULT
        item = {
            "title": "Example", "url": "https://xvideos.com/video.1",
            "provider": "xvideos", "kind": "adult",
        }
        panel.results = [item]
        panel.results_list.InsertItem(0, item["title"])
        panel.results_list.Select(0)

        panel.on_download_selected(None)

        self.assertEqual(self.frame.queue.calls, [("adult", item, "Example")])

    def test_adult_search_respects_master_setting(self):
        panel = SearchPanel(self.host, self.frame)
        self.frame.config["adult_sites_enabled"] = False
        panel.query_text.SetValue("example")
        panel.engine_choice.SetSelection(ENGINE_ADULT)

        panel.on_search(None)

        self.assertEqual(
            self.frame.messages[-1],
            "Adult sites are disabled. Enable them in Settings.",
        )
        self.assertTrue(panel.search_btn.IsEnabled())

    def test_adult_combo_choices_follow_master_setting(self):
        self.frame.config["adult_sites_enabled"] = False
        panel = SearchPanel(self.host, self.frame)

        self.assertEqual(
            [panel.engine_choice.GetString(index)
             for index in range(panel.engine_choice.GetCount())],
            ENGINE_LABELS[:GENERAL_ENGINE_COUNT],
        )

        self.frame.config["adult_sites_enabled"] = True
        panel.refresh_engine_choices()
        self.assertEqual(
            [panel.engine_choice.GetString(index)
             for index in range(panel.engine_choice.GetCount())],
            ENGINE_LABELS,
        )

        panel.engine_choice.SetSelection(ENGINE_TRANS)
        self.frame.config["adult_sites_enabled"] = False
        panel.refresh_engine_choices()
        self.assertEqual(panel.engine_choice.GetCount(), GENERAL_ENGINE_COUNT)
        self.assertEqual(panel.engine_choice.GetSelection(), ENGINE_MUSIC)

    def test_search_sort_choices_and_ordering(self):
        panel = SearchPanel(self.host, self.frame)
        self.assertEqual(
            [panel.sort_choice.GetString(index)
             for index in range(panel.sort_choice.GetCount())],
            SORT_LABELS,
        )
        items = [
            {"title": "Zulu", "artist": "Beta", "source": "Site B",
             "duration_s": 90, "_search_order": 0},
            {"title": "Alpha", "artist": "Gamma", "source": "Site A",
             "duration_s": None, "_search_order": 1},
            {"title": "Bravo", "artist": "Alpha", "source": "Site B",
             "duration_s": 30, "_search_order": 2},
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
        self.assertEqual(
            self.frame.messages[-1], "Sorted 2 results by Name.")

    def test_each_adult_combo_choice_routes_its_category(self):
        panel = SearchPanel(self.host, self.frame)
        stop = threading.Event()
        for engine, category in ADULT_ENGINE_CATEGORIES.items():
            token = object()
            panel.token = token
            with (mock.patch.object(
                    adult_backend, "search", return_value=([], [], []))
                  as search,
                  mock.patch.object(wx, "CallAfter")):
                panel._search("example", engine, token, stop, ["pornhub"])

            self.assertEqual(search.call_args.kwargs["category"], category)

    def test_book_engine_relabels_columns_and_sort_choices(self):
        panel = SearchPanel(self.host, self.frame)
        panel.engine_choice.SetSelection(ENGINE_BOOKS)
        panel.on_engine_changed(wx.CommandEvent())

        self.assertEqual(
            [panel.sort_choice.GetString(index)
             for index in range(panel.sort_choice.GetCount())],
            BOOK_SORT_LABELS,
        )
        # Nothing to play, so the preview button must not offer itself.
        self.assertFalse(panel.preview_btn.IsEnabled())

        panel._apply_engine_columns(ENGINE_BOOKS)
        self.assertEqual(
            [panel.results_list.GetColumn(index).GetText()
             for index in range(panel.results_list.GetColumnCount())],
            list(BOOK_COLUMN_HEADINGS),
        )

        panel.engine_choice.SetSelection(ENGINE_MUSIC)
        panel.on_engine_changed(wx.CommandEvent())
        panel._apply_engine_columns(ENGINE_MUSIC)
        self.assertEqual(
            [panel.results_list.GetColumn(index).GetText()
             for index in range(panel.results_list.GetColumnCount())],
            list(COLUMN_HEADINGS),
        )
        self.assertTrue(panel.preview_btn.IsEnabled())

    def test_media_engines_relabel_their_own_columns(self):
        panel = SearchPanel(self.host, self.frame)
        for engine, headings in (
                (ENGINE_AUDIOBOOKS, AUDIOBOOK_COLUMN_HEADINGS),
                (ENGINE_ARCHIVE_AUDIO, ARCHIVE_COLUMN_HEADINGS),
                (ENGINE_ARCHIVE_VIDEO, ARCHIVE_COLUMN_HEADINGS)):
            panel._apply_engine_columns(engine)
            self.assertEqual(
                [panel.results_list.GetColumn(index).GetText()
                 for index in range(panel.results_list.GetColumnCount())],
                list(headings),
            )
        panel.engine_choice.SetSelection(ENGINE_ARCHIVE_VIDEO)
        panel.on_engine_changed(wx.CommandEvent())
        self.assertEqual(
            [panel.sort_choice.GetString(index)
             for index in range(panel.sort_choice.GetCount())],
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
            [item["title"]
             for item in _sorted_results(items, SORT_SHORTEST, ENGINE_BOOKS)],
            ["Oldest", "Middle", "Newest", "Undated"],
        )
        self.assertEqual(
            [item["title"]
             for item in _sorted_results(items, SORT_LONGEST,
                                         ENGINE_ARCHIVE_VIDEO)],
            ["Newest", "Middle", "Oldest", "Undated"],
        )

    def test_search_queues_book_and_audiobook_results(self):
        panel = SearchPanel(self.host, self.frame)
        for engine, kind in ((ENGINE_BOOKS, "book"),
                             (ENGINE_AUDIOBOOKS, "audiobook")):
            self.frame.queue.calls = []
            panel.result_engine = engine
            item = {"title": "One", "kind": kind, "identifier": "one"}
            panel.results = [item]
            panel.results_list.DeleteAllItems()
            panel.results_list.InsertItem(0, item["title"])
            panel.results_list.Select(0)

            panel.on_download_selected(None)

            self.assertEqual(self.frame.queue.calls, [(kind, item, "One")])

    def test_archive_item_with_one_file_queues_without_asking(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ARCHIVE_AUDIO
        item = {"title": "Dragnet", "kind": "archive", "identifier": "dragnet",
                "video": False}
        files = [{"title": "Episode 1", "file_name": "ep1.mp3",
                  "identifier": "dragnet",
                  "direct_url": "https://archive.org/download/dragnet/ep1.mp3"}]

        panel._archive_files_ready(panel.archive_token, item, files)

        self.assertEqual(len(self.frame.queue.calls), 1)
        kind, payload, title = self.frame.queue.calls[0]
        self.assertEqual((kind, title), ("archive", "Episode 1"))
        # The show's name rides along so the episode lands in its own folder.
        self.assertEqual(payload["collection_title"], "Dragnet")

    def test_archive_item_with_many_files_offers_a_picker(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_ARCHIVE_AUDIO
        item = {"title": "Dragnet", "kind": "archive", "identifier": "dragnet",
                "video": False}
        files = [
            {"title": f"Episode {number}", "file_name": f"ep{number}.mp3",
             "identifier": "dragnet", "direct_url": f"https://x/ep{number}.mp3"}
            for number in (1, 2, 3)
        ]

        with mock.patch.object(ItemPickerDialog, "ShowModal",
                               return_value=wx.ID_OK), \
                mock.patch.object(ItemPickerDialog, "selected_items",
                                  return_value=files[:2]):
            panel._archive_files_ready(panel.archive_token, item, files)

        self.assertEqual([call[2] for call in self.frame.queue.calls],
                         ["Episode 1", "Episode 2"])
        self.assertEqual(self.frame.messages[-1], "Queued 2 downloads.")

    def test_books_cannot_be_previewed(self):
        panel = SearchPanel(self.host, self.frame)
        panel.result_engine = ENGINE_BOOKS
        panel.results = [{"title": "One"}]
        panel.results_list.InsertItem(0, "One")
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

    def test_downloads_cancel_multiple_and_clear_finished(self):
        panel = DownloadsPanel(self.host, self.frame)
        queued = DownloadItem("Queued", "ytdlp", "one")
        queued.status = STATUS_QUEUED
        done = DownloadItem("Done", "ytdlp", "two")
        done.status = STATUS_DONE
        self.frame.queue.items = [queued, done]
        panel.refresh_all()
        panel.list.Select(0)
        panel.list.Select(1)

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


if __name__ == "__main__":
    unittest.main()
