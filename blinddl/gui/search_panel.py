# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Search tab: musicdl across all supported music sites, or yt-dlp/YouTube."""

import threading
import time

import wx

from .. import musicdl_backend, sideb_backend, ytdlp_backend

ENGINE_MUSIC = "Music sites"
ENGINE_YOUTUBE = "YouTube/web"


class SearchPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.results = []
        self.result_engine = 0
        self.token = None  # identifies the current search
        self.stop = None  # set to silence a superseded search's late sites
        self.shown_sources = set()
        self.asked = []  # sites this search went out to
        self.done = False  # True once the current search hit its deadline
        self.started_at = 0.0
        # Refreshes the status bar while slow sites are still working.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        query_label = wx.StaticText(self, label="&Search:")
        self.query_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.query_text.SetName("Search query")
        self.query_text.Bind(wx.EVT_TEXT_ENTER, self.on_search)

        engine_label = wx.StaticText(self, label="S&ource:")
        self.engine_choice = wx.Choice(self, choices=[ENGINE_MUSIC, ENGINE_YOUTUBE])
        self.engine_choice.SetName("Search source")
        self.engine_choice.SetSelection(0)

        self.search_btn = wx.Button(self, label="&Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self.on_search)

        self.results_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.results_list.SetName("Search results")
        self.results_list.SetHelpText(
            "Select results. Enter downloads; Context Menu opens actions.")
        for i, heading in enumerate(("Title", "Artist / channel", "Source",
                                     "Duration", "Size")):
            self.results_list.InsertColumn(i, heading)
        self.results_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_download_selected)
        self.results_list.Bind(wx.EVT_CONTEXT_MENU, self.on_results_menu)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(engine_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.engine_choice, 0, wx.RIGHT, 12)
        top.Add(self.search_btn, 0)

        sizer.Add(query_label, 0, wx.ALL, 8)
        sizer.Add(self.query_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(top, 0, wx.ALL, 8)
        sizer.Add(self.results_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT |
                  wx.BOTTOM, 8)
        self.SetSizer(sizer)

    def focus_input(self):
        self.query_text.SetFocus()

    # -- search -----------------------------------------------------------

    def on_search(self, event):
        query = self.query_text.GetValue().strip()
        if not query:
            self.frame.announce("Type a search first.")
            return
        engine = self.engine_choice.GetSelection()
        sources = musicdl_backend.enabled_sources(
            self.frame.config["disabled_music_sources"])
        if engine == 0 and not sources:
            self.frame.announce("No music sites selected. Use Tools, Search sites.")
            return
        self.search_btn.Disable()

        # Everything below is tagged with this token, so results still
        # trickling in from a previous search are ignored.
        self.token = object()
        if self.stop is not None:
            self.stop.set()
        self.stop = threading.Event()
        self.results = []
        self.result_engine = engine
        self.shown_sources = set()
        self.asked = []
        self.done = False
        self.started_at = time.time()
        self.timer.Stop()
        self.results_list.DeleteAllItems()

        if engine == 0:
            # Side B's Deezer catalog search goes out next to the musicdl
            # sites and reports through the same per-site callback.
            count = len(sources) + 1
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} music {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        else:
            self.frame.announce("Searching YouTube...")
        threading.Thread(target=self._search, args=(query, engine, self.token,
                                                    self.stop, sources),
                         daemon=True).start()

    def _search(self, query, engine, token, stop, sources):
        asked = []
        try:
            if engine == 0:
                def on_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                threading.Thread(target=self._sideb_search,
                                 args=(query, token, engine, stop),
                                 daemon=True, name="search-sideb").start()
                items, _answered, asked = musicdl_backend.search(
                    query, self.frame.config["search_timeout_s"],
                    on_site=on_site, stop=stop, sources=sources)
                asked.append(sideb_backend.SIDEB_SOURCE)
                # on_site already delivered these; nothing left to hand over.
                items = []
            else:
                items = ytdlp_backend.search(query)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._search_failed, token, str(exc))
            return
        wx.CallAfter(self._search_done, token, items, engine, asked)

    def _sideb_search(self, query, token, engine, stop):
        try:
            items = sideb_backend.search(query, self.frame.config)
        except Exception:  # noqa: BLE001 - one failing site must not kill the rest
            items = []
        if stop.is_set():
            return
        wx.CallAfter(self._add_site, token, engine,
                     sideb_backend.SIDEB_SOURCE, items)

    def _search_failed(self, token, error):
        if token is not self.token:
            return
        self.search_btn.Enable()
        self.frame.announce("Search failed.")
        wx.MessageBox(f"Search failed:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _add_site(self, token, engine, source, items):
        """Append one site's results. Runs on the GUI thread."""
        if token is not self.token or source in self.shown_sources:
            return
        self.shown_sources.add(source)
        if not items:
            return
        for item in items:
            row = self.results_list.GetItemCount()
            self.results_list.InsertItem(row, item["title"])
            if engine == 0:
                self.results_list.SetItem(row, 1, item.get("artist", ""))
                self.results_list.SetItem(row, 2, item.get("source", ""))
                self.results_list.SetItem(
                    row, 3, ytdlp_backend.format_duration(item.get("duration_s")))
                self.results_list.SetItem(row, 4, item.get("file_size", ""))
            else:
                self.results_list.SetItem(row, 1, item.get("uploader", ""))
                self.results_list.SetItem(row, 2, "YouTube")
                self.results_list.SetItem(
                    row, 3, ytdlp_backend.format_duration(item.get("duration")))
            self.results.append(item)
        if self.done:
            # A late site: say so on the status bar, but leave focus alone.
            self.frame.announce(
                f"{self._result_count()}, latest from {source}. "
                f"{self._pending_phrase()}")
            if not self._pending():
                self.timer.Stop()

    def _pending(self):
        """Sites that were asked but have not reported back yet."""
        return [s for s in self.asked if s not in self.shown_sources]

    def _pending_phrase(self):
        pending = self._pending()
        if not pending:
            return "All sites finished."
        waited = int(time.time() - self.started_at)
        names = ", ".join(pending[:3])
        if len(pending) > 3:
            names += f" and {len(pending) - 3} more"
        site_word = "site" if len(pending) == 1 else "sites"
        return (f"Still searching {len(pending)} {site_word} after {waited}s: "
                f"{names}.")

    def _result_count(self):
        count = len(self.results)
        return f"{count} result" if count == 1 else f"{count} results"

    def _tick(self, event):
        """Keep the status bar honest while slow sites are still working."""
        if self.done and not self._pending():
            self.timer.Stop()
            return
        if self.done:
            self.frame.announce(
                f"{self._result_count()} so far. {self._pending_phrase()}")

    def _search_done(self, token, items, engine, asked=()):
        if token is not self.token:
            return
        self.search_btn.Enable()
        # yt-dlp hands back everything at once; music results arrived per site.
        self._add_site(token, engine, "", items)
        self.asked = list(asked)
        self.done = True
        pending = self._pending()
        if pending:
            # Deezer and friends can run for minutes; never call that "found
            # nothing" when the sites are still going.
            self.frame.announce(
                f"{self._result_count()} so far. {self._pending_phrase()}")
            self.timer.Start(10000)
        else:
            self.frame.announce(f"{self._result_count()} found.")
        if self.results:
            self.results_list.SetFocus()
            self.results_list.Focus(0)
            self.results_list.Select(0)

    # -- download -----------------------------------------------------------

    def _selected_indices(self):
        indices = []
        index = self.results_list.GetFirstSelected()
        while index != -1:
            indices.append(index)
            index = self.results_list.GetNextSelected(index)
        return indices

    def _select_all(self, event):
        for index in range(self.results_list.GetItemCount()):
            self.results_list.Select(index)
        count = self.results_list.GetSelectedItemCount()
        noun = "result" if count == 1 else "results"
        self.frame.announce(f"Selected {count} {noun}.")

    def _clear_selection(self, event):
        for index in self._selected_indices():
            self.results_list.Select(index, False)
        self.frame.announce("Selection cleared.")

    def _target_context_item(self, event):
        """Make a right-clicked row the target while preserving a group click."""
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            if not self._selected_indices():
                focused = self.results_list.GetFocusedItem()
                if focused >= 0:
                    self.results_list.Select(focused)
            return
        index, _flags = self.results_list.HitTest(
            self.results_list.ScreenToClient(position))
        if index < 0 or self.results_list.IsSelected(index):
            return
        for selected in self._selected_indices():
            self.results_list.Select(selected, False)
        self.results_list.Focus(index)
        self.results_list.Select(index)

    def on_results_menu(self, event):
        self._target_context_item(event)
        menu = wx.Menu()
        download = menu.Append(wx.ID_ANY, "&Download selected")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear = menu.Append(wx.ID_ANY, "&Clear selection")
        has_selection = bool(self._selected_indices())
        download.Enable(has_selection)
        clear.Enable(has_selection)
        select_all.Enable(
            self.results_list.GetSelectedItemCount() <
            self.results_list.GetItemCount())
        menu.Bind(wx.EVT_MENU, self.on_download_selected, download)
        menu.Bind(wx.EVT_MENU, self._select_all, select_all)
        menu.Bind(wx.EVT_MENU, self._clear_selection, clear)
        self.results_list.PopupMenu(menu)
        menu.Destroy()

    def on_download_selected(self, event):
        indices = [i for i in self._selected_indices()
                   if i < len(self.results)]
        if not indices:
            self.frame.announce("Select a result first.")
            return
        engine = self.result_engine
        for index in indices:
            item = self.results[index]
            if engine == 0:
                if item.get("kind") == "sideb":
                    self.frame.queue.add_sideb(item["url"], item["title"])
                else:
                    self.frame.queue.add_musicdl(
                        item["song_info"], item["title"])
            else:
                self.frame.queue.add_ytdlp(item["url"], item["title"])
        if len(indices) == 1:
            self.frame.announce(f"Queued: {self.results[indices[0]]['title']}")
        else:
            self.frame.announce(f"Queued {len(indices)} downloads.")
