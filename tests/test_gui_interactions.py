# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import copy
from contextlib import nullcontext
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
    from blinddl import (
        adult_backend,
        archive_backend,
        musicdl_backend,
        preview,
        search_order,
        soulseek_backend,
        ytdlp_backend,
    )
    from blinddl.downloader import (
        DownloadItem,
        STATUS_DONE,
        STATUS_QUEUED,
    )
    from blinddl import config as config_module
    from blinddl.config import DEFAULTS
    from blinddl.gui.downloads_panel import DownloadsPanel
    from blinddl.gui.item_picker_dialog import ItemPickerDialog
    from blinddl.gui.library_panel import LibraryPanel, discover_media
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
        ENGINE_ARCHIVE_AUDIO,
        ENGINE_ARCHIVE_VIDEO,
        ENGINE_AUDIOBOOKS,
        ENGINE_BOOKS,
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
        _order_phrase,
        _sort_for_order,
        _soulseek_media_kind,
        _sorted_results,
    )
    from blinddl.gui.settings_dialog import SettingsDialog
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


class _SettingsConfig(dict):
    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))
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
        for row, item in enumerate(panel.results):
            panel.results_list.InsertItem(row, item["title"])

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

    def test_library_refresh_runs_off_thread_and_coalesces_requests(self):
        file_list = mock.Mock()
        file_list.GetFirstSelected.return_value = -1
        panel = SimpleNamespace(
            _alive=True,
            _refreshing=False,
            _pending_refresh=None,
            _announce_refresh=False,
            items=[],
            list=file_list,
            frame=SimpleNamespace(config={"download_dir": "C:\\Media"}),
            _discover=mock.Mock(),
        )
        panel._start_refresh = lambda: LibraryPanel._start_refresh(panel)
        with mock.patch("blinddl.gui.library_panel.threading.Thread") as worker:
            LibraryPanel.refresh(panel, announce=False)
            LibraryPanel.refresh(panel, announce=False)

        worker.assert_called_once()
        worker.return_value.start.assert_called_once_with()
        self.assertTrue(panel._refreshing)
        self.assertEqual(panel._pending_refresh, ("C:\\Media", None))

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

        self.assertEqual(
            [
                panel.engine_choice.GetString(index)
                for index in range(panel.engine_choice.GetCount())
            ],
            ENGINE_LABELS[:GENERAL_ENGINE_COUNT],
        )

        self.frame.config["adult_sites_enabled"] = True
        panel.refresh_engine_choices()
        self.assertEqual(
            [
                panel.engine_choice.GetString(index)
                for index in range(panel.engine_choice.GetCount())
            ],
            ENGINE_LABELS[:15],
        )

        panel.engine_choice.SetSelection(ENGINE_TRANS)
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
            [ENGINE_LABELS[index] for index in range(GENERAL_ENGINE_COUNT)]
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

    def test_search_order_choice_is_persistent_and_repeats_the_query(self):
        panel = SearchPanel(self.host, self.frame)
        panel.query_text.SetValue("dragnet")
        panel.order_choice.SetSelection(
            search_order.ORDERS.index(search_order.ORDER_RECENT)
        )

        with mock.patch.object(panel, "on_search") as search:
            panel.on_order_changed(None)

        self.assertEqual(self.frame.config["search_order"], search_order.ORDER_RECENT)
        search.assert_called_once_with(None)

    def test_search_status_names_sources_that_cannot_honour_order(self):
        self.assertEqual(
            _order_phrase(search_order.ORDER_POPULAR, ["Bandcamp"], 2),
            "1 site cannot sort by most popular, so it answered by best "
            "match: Bandcamp.",
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
        panel.engine_choice.SetSelection(ENGINE_BOOKS)
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

        panel.engine_choice.SetSelection(ENGINE_MUSIC)
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
        panel.engine_choice.SetSelection(ENGINE_ARCHIVE_VIDEO)
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
            panel.results_list.DeleteAllItems()
            panel.results_list.InsertItem(0, item["title"])
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

    def test_failed_cookie_export_removes_secure_temporary_file(self):
        config = _SettingsConfig()
        dialog = SettingsDialog(self.host, config)
        descriptor, path = tempfile.mkstemp(suffix=".txt")

        with (
            mock.patch("tempfile.mkstemp", return_value=(descriptor, path)),
            mock.patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("locked")),
            mock.patch.object(wx, "MessageBox"),
        ):
            dialog._on_am_copy_cookies(None)

        self.assertFalse(os.path.exists(path))
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
            panel.results_list.DeleteAllItems()
            panel.results_list.InsertItem(0, item["title"])
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
        panel._insert_result_row(0, item, ENGINE_SOULSEEK_AUDIO)
        panel._apply_engine_columns(ENGINE_SOULSEEK_AUDIO)

        self.assertEqual(panel.results_list.GetColumn(3).GetText(), "Folder")
        self.assertEqual(panel.results_list.GetItemText(0, 3), "Music\\Album")
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
        panel = SimpleNamespace(list=native_list, _rows={}, _values={})

        DownloadsPanel.update_item(panel, item)
        native_list.SetItem.reset_mock()
        DownloadsPanel.update_item(panel, item)

        native_list.SetItem.assert_not_called()
        item.percent = 25
        DownloadsPanel.update_item(panel, item)
        native_list.SetItem.assert_called_once_with(0, 2, "25%")

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
