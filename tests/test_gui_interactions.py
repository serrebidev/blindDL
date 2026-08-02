# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import logging
import unittest
from unittest import mock

import wx

# musicdl creates a file logger at import time. Keep GUI tests self-contained.
with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
    from blinddl.downloader import (
        DownloadItem,
        STATUS_DONE,
        STATUS_QUEUED,
    )
    from blinddl.gui.downloads_panel import DownloadsPanel
    from blinddl.gui.item_picker_dialog import ItemPickerDialog
    from blinddl.gui.search_panel import SearchPanel
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

    def _find(self, item_id):
        return next((item for item in self.items if item.id == item_id), None)

    def cancel(self, item_id):
        item = self._find(item_id)
        if item is not None:
            item.cancel_event.set()

    def remove_finished(self):
        self.items = [item for item in self.items if item.status != STATUS_DONE]


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
