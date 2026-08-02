# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
import logging
import threading
import unittest
from unittest import mock

import wx

# musicdl creates a file logger at import time. Keep GUI tests self-contained.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl import adult_backend
    from blinddl.downloader import (
        DownloadItem,
        STATUS_DONE,
        STATUS_QUEUED,
    )
    from blinddl.config import DEFAULTS
    from blinddl.gui.downloads_panel import DownloadsPanel
    from blinddl.gui.item_picker_dialog import ItemPickerDialog
    from blinddl.gui.search_panel import (
        ADULT_ENGINE_CATEGORIES,
        ENGINE_ADULT,
        ENGINE_LABELS,
        ENGINE_MUSIC,
        ENGINE_TRANS,
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
            "adult_sites_enabled": True,
            "search_timeout_s": 5,
            "audio_only": True,
        }
        self.queue = _Queue()
        self.subs = _Subscriptions()
        self.messages = []

    def announce(self, message):
        self.messages.append(message)

    def on_choose_sources(self):
        pass


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
            ENGINE_LABELS[:2],
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
        self.assertEqual(panel.engine_choice.GetCount(), 2)
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
