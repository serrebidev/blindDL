# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible podcast-directory search and historical feed reconstruction."""

import threading
import time
from urllib.parse import urlparse

import wx

from .. import podcast_archiver, ytdlp_backend
from ..downloader import addition_summary
from .item_picker_dialog import ItemPickerDialog


def _is_url(value):
    return urlparse(str(value or "").strip()).scheme.casefold() in (
        "http", "https")


def _archive_source(item):
    captured = str(item.get("archived_at") or "")
    if len(captured) >= 8:
        return f"Archive {captured[:4]}-{captured[4:6]}-{captured[6:8]}"
    return "Current feed"


class PodcastArchiverDialog(wx.Dialog):
    """Find a podcast, reconstruct its feed history, then play or queue it."""

    def __init__(self, parent, initial_value="", auto_start=False):
        super().__init__(
            parent, title="Podcast archiver",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame = parent
        self.results = []
        self.archive = None
        self._cancel_event = None
        self._working = False
        self._closing = False
        self._generation = 0
        self._last_progress = 0.0

        prompt = wx.StaticText(
            self,
            label="Podcast &name, RSS URL, or Apple Podcasts URL:")
        self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.query.SetName("Podcast name or feed URL")
        self.query.SetHelpText(
            "Type a podcast name to search Apple Podcasts, gPodder, fyyd, "
            "and Podverse, or paste an RSS or Apple Podcasts URL to open it "
            "directly. Enter starts.")
        self.query.Bind(wx.EVT_TEXT_ENTER, self.on_find)

        self.find_btn = wx.Button(self, label="&Find or open")
        self.find_btn.SetDefault()
        self.find_btn.Bind(wx.EVT_BUTTON, self.on_find)

        self.include_wayback = wx.CheckBox(
            self, label="Include &Wayback Machine feed history")
        self.include_wayback.SetValue(True)
        self.include_wayback.SetName("Include archived feed history")
        self.include_wayback.SetHelpText(
            "Reads unique old copies of the RSS feed from archive.org. "
            "Turn this off to list only the current feed.")

        snapshots_label = wx.StaticText(self, label="Maximum &snapshots:")
        self.max_snapshots = wx.SpinCtrl(
            self, min=1, max=podcast_archiver.DEFAULT_MAX_SNAPSHOTS,
            initial=podcast_archiver.DEFAULT_MAX_SNAPSHOTS)
        self.max_snapshots.SetName("Maximum archived feed snapshots")
        self.max_snapshots.SetHelpText(
            "The maximum number of unique archived versions to inspect for "
            "each feed URL. Five thousand gives the most complete history.")

        self.podcast_list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.podcast_list.SetName("Podcasts found")
        self.podcast_list.SetHelpText(
            "Choose a podcast and press Enter to reconstruct its episode "
            "history. The list is empty when a feed URL is opened directly.")
        for column, heading in enumerate(
                ("Podcast", "Publisher", "Episodes", "Directory")):
            self.podcast_list.InsertColumn(column, heading)
        self.podcast_list.SetColumnWidth(0, 280)
        self.podcast_list.SetColumnWidth(1, 190)
        self.podcast_list.SetColumnWidth(2, 90)
        self.podcast_list.SetColumnWidth(3, 150)
        self.podcast_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_archive)
        self.podcast_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selected)

        self.status = wx.StaticText(self, label="Enter a podcast name or feed URL.")
        self.status.SetName("Podcast archiver status")

        self.archive_btn = wx.Button(self, label="Build &archive")
        self.archive_btn.Bind(wx.EVT_BUTTON, self.on_archive)
        self.stop_btn = wx.Button(self, label="&Stop")
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)
        self.browse_btn = wx.Button(self, label="&Browse episodes...")
        self.browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)
        self.save_btn = wx.Button(self, label="Save combined &RSS...")
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        close_btn = wx.Button(self, wx.ID_CANCEL, "Close")

        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(self.include_wayback, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        options.Add(snapshots_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        options.Add(self.max_snapshots, 0)

        query_row = wx.BoxSizer(wx.HORIZONTAL)
        query_row.Add(self.query, 1, wx.RIGHT, 8)
        query_row.Add(self.find_btn, 0)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(self.archive_btn, 0, wx.RIGHT, 8)
        actions.Add(self.stop_btn, 0, wx.RIGHT, 8)
        actions.Add(self.browse_btn, 0, wx.RIGHT, 8)
        actions.Add(self.save_btn, 0, wx.RIGHT, 8)
        actions.AddStretchSpacer()
        actions.Add(close_btn, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(query_row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(options, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.podcast_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(actions, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((820, 520))
        self.SetMinSize((620, 400))
        self._update_controls()
        initial_value = str(initial_value or "").strip()
        if initial_value:
            self.query.SetValue(initial_value)
        self.query.SetFocus()
        if initial_value and auto_start:
            wx.CallAfter(self._start_archive, initial_value)

    def _selected_podcast(self):
        row = self.podcast_list.GetFirstSelected()
        return self.results[row] if 0 <= row < len(self.results) else None

    def _on_selected(self, event):
        self._update_controls()
        event.Skip()

    def _set_status(self, message, announce=True):
        self.status.SetLabel(str(message))
        if announce and self.frame is not None:
            self.frame.announce(str(message))

    def _update_controls(self):
        selected = self._selected_podcast() is not None
        self.find_btn.Enable(not self._working)
        self.archive_btn.Enable(not self._working and selected)
        self.stop_btn.Enable(self._working)
        self.browse_btn.Enable(not self._working and self.archive is not None)
        self.save_btn.Enable(not self._working and self.archive is not None)
        self.query.Enable(not self._working)
        self.include_wayback.Enable(not self._working)
        self.max_snapshots.Enable(not self._working)

    def _start_work(self, target, *args):
        if self._working:
            return
        self._working = True
        self._generation += 1
        generation = self._generation
        self._cancel_event = threading.Event()
        self._update_controls()
        threading.Thread(
            target=target, args=(generation, *args), daemon=True,
            name="blinddl-podcast-archiver").start()

    def on_find(self, event=None):
        value = self.query.GetValue().strip()
        if not value:
            self._set_status("Enter a podcast name or feed URL.")
            self.query.SetFocus()
            return
        if _is_url(value):
            self._start_archive(value)
            return
        self.archive = None
        self.results = []
        self.podcast_list.DeleteAllItems()
        self._set_status(f"Searching podcast directories for {value}...")
        self._start_work(self._search_worker, value)

    def _search_worker(self, generation, query):
        try:
            results = podcast_archiver.search_podcasts(query)
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            wx.CallAfter(self._work_failed, generation, str(exc))
            return
        wx.CallAfter(self._search_ready, generation, results)

    def _search_ready(self, generation, results):
        if self._closing or generation != self._generation:
            return
        self._working = False
        self.results = list(results)
        self.podcast_list.DeleteAllItems()
        for row, podcast in enumerate(self.results):
            self.podcast_list.InsertItem(row, podcast["title"])
            self.podcast_list.SetItem(row, 1, podcast.get("artist") or "")
            count = podcast.get("track_count") or 0
            self.podcast_list.SetItem(row, 2, str(count) if count else "")
            self.podcast_list.SetItem(row, 3, podcast.get("source") or "")
        if self.results:
            self.podcast_list.Select(0)
            self.podcast_list.Focus(0)
            self.podcast_list.SetFocus()
            self._set_status(
                f"Found {len(self.results)} podcasts. Choose one and press Enter.")
        else:
            self._set_status("The podcast directories found no matches.")
            self.query.SetFocus()
        self._update_controls()

    def on_archive(self, event=None):
        selected = self._selected_podcast()
        if selected is None:
            value = self.query.GetValue().strip()
            if _is_url(value):
                self._start_archive(value)
            else:
                self._set_status("Choose a podcast from the results first.")
                self.podcast_list.SetFocus()
            return
        self._start_archive(selected["feed_url"])

    def _start_archive(self, feed_url):
        self.archive = None
        self._set_status("Starting podcast archive scan...")
        self._start_work(
            self._archive_worker, feed_url, self.include_wayback.GetValue(),
            self.max_snapshots.GetValue())

    def _progress(self, generation, message, current, total):
        now = time.monotonic()
        if current != total and now - self._last_progress < 0.25:
            return
        self._last_progress = now
        wx.CallAfter(self._show_progress, generation, message, current, total)

    def _show_progress(self, generation, message, current, total):
        if self._closing or generation != self._generation:
            return
        detail = f" ({current} of {total})" if total else ""
        self._set_status(message + detail, announce=False)

    def _archive_worker(self, generation, feed_url, include_wayback,
                        max_snapshots):
        try:
            archive = podcast_archiver.archive_podcast(
                feed_url, include_wayback=include_wayback,
                max_snapshots=max_snapshots,
                cancel_event=self._cancel_event,
                progress=lambda message, current, total: self._progress(
                    generation, message, current, total),
            )
        except podcast_archiver.PodcastArchiveCancelled:
            wx.CallAfter(self._work_stopped, generation)
            return
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            wx.CallAfter(self._work_failed, generation, str(exc))
            return
        wx.CallAfter(self._archive_ready, generation, archive)

    def _archive_ready(self, generation, archive):
        if self._closing or generation != self._generation:
            return
        self._working = False
        self.archive = archive
        warning = f" {len(archive.warnings)} versions could not be read." if archive.warnings else ""
        self._set_status(
            f"Found {len(archive.episodes)} unique episodes from "
            f"{archive.snapshots_loaded} archived feed versions.{warning}")
        self._update_controls()
        self.on_browse()

    def _work_failed(self, generation, error):
        if self._closing or generation != self._generation:
            return
        self._working = False
        self._set_status(f"Podcast archive failed: {error}")
        self._update_controls()
        wx.MessageBox(
            f"Could not build the podcast archive:\n{error}", "blindDL",
            wx.OK | wx.ICON_ERROR, self)

    def _work_stopped(self, generation):
        if self._closing or generation != self._generation:
            return
        self._working = False
        self._set_status("Podcast archive scan stopped.")
        self._update_controls()

    def on_stop(self, event=None):
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._set_status("Stopping the podcast archive scan...")

    def on_browse(self, event=None):
        if self.archive is None:
            self._set_status("Build a podcast archive first.")
            return
        columns = (
            ("Title", 390, lambda item: item.get("title") or "Untitled episode"),
            ("Published", 110, lambda item: item.get("published_date") or "Unknown"),
            ("Duration", 90, lambda item: ytdlp_backend.format_duration(
                item.get("duration_s"))),
            ("Found in", 120, _archive_source),
        )
        dialog = ItemPickerDialog(
            self, self.archive.episodes, self.archive.title,
            columns=columns, dialog_title="Podcast archive episodes")
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            episodes = dialog.selected_items()
        finally:
            dialog.Destroy()
        added = []
        with self.frame.queue.batch_additions():
            for episode in episodes:
                added.append(self.frame.queue.add_ytdlp(
                    episode["url"], episode["title"], audio_only=True,
                    folder=self.archive.title))
        self.frame.announce(
            addition_summary(added, [episode["title"] for episode in episodes]))
        self.frame.show_downloads_tab()

    def on_save(self, event=None):
        if self.archive is None:
            self._set_status("Build a podcast archive first.")
            return
        dialog = wx.FileDialog(
            self, "Save combined podcast RSS", wildcard=(
                "RSS feeds (*.rss;*.xml)|*.rss;*.xml|All files (*.*)|*.*"),
            defaultFile="podcast-archive.xml",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        try:
            podcast_archiver.write_combined_rss(path, self.archive)
        except OSError as exc:
            wx.MessageBox(
                f"Could not save the combined RSS feed:\n{exc}", "blindDL",
                wx.OK | wx.ICON_ERROR, self)
            return
        self._set_status(
            f"Saved {len(self.archive.episodes)} episodes to {path}.")

    def Destroy(self):  # noqa: N802 - wx spelling
        self._closing = True
        self._generation += 1
        if self._cancel_event is not None:
            self._cancel_event.set()
        return super().Destroy()
