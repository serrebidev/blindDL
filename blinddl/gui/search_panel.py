# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Search tab: music, books, audiobooks, Archive media, adult sites, yt-dlp."""

import threading
import time

import wx

from .. import (
    adult_backend, archive_backend, audiobook_backend, book_backend,
    musicdl_backend, preview, sideb_backend, ytdlp_backend,
)
from .item_picker_dialog import ItemPickerDialog
from .media_player import MediaPlayerPanel

ENGINE_MUSIC = 0
ENGINE_YOUTUBE = 1
ENGINE_BOOKS = 2
ENGINE_AUDIOBOOKS = 3
ENGINE_ARCHIVE_AUDIO = 4
ENGINE_ARCHIVE_VIDEO = 5
ENGINE_STRAIGHT = 6
ENGINE_GAY = 7
ENGINE_LESBIAN = 8
ENGINE_BISEXUAL = 9
ENGINE_TRANS = 10
# Kept as an import-compatible name for callers that treated adult search as
# the first adult choice before content categories were separated.
ENGINE_ADULT = ENGINE_STRAIGHT
ENGINE_LABELS = [
    "Music sites",
    "YouTube/web",
    "Books",
    "Audiobooks",
    "Old-time radio and music",
    "Movies and TV",
    "Straight porn",
    "Gay porn",
    "Lesbian porn",
    "Bisexual porn",
    "Trans porn",
]
# The engines shown before the adult categories, which stay hidden until the
# user switches them on in Settings.
GENERAL_ENGINE_COUNT = 6
ARCHIVE_ENGINE_CATEGORIES = {
    ENGINE_ARCHIVE_AUDIO: archive_backend.AUDIO_CATEGORIES,
    ENGINE_ARCHIVE_VIDEO: archive_backend.VIDEO_CATEGORIES,
}
ADULT_ENGINE_CATEGORIES = {
    ENGINE_STRAIGHT: adult_backend.CONTENT_STRAIGHT,
    ENGINE_GAY: adult_backend.CONTENT_GAY,
    ENGINE_LESBIAN: adult_backend.CONTENT_LESBIAN,
    ENGINE_BISEXUAL: adult_backend.CONTENT_BISEXUAL,
    ENGINE_TRANS: adult_backend.CONTENT_TRANS,
}
SORT_RELEVANCE = 0
SORT_NAME = 1
SORT_SITE = 2
SORT_ARTIST = 3
SORT_SHORTEST = 4
SORT_LONGEST = 5
SORT_LABELS = [
    "Relevance",
    "Name",
    "Site",
    "Artist / channel",
    "Shortest duration",
    "Longest duration",
]
# Same six sort slots, named for what they actually do to a list of books.
BOOK_SORT_LABELS = [
    "Relevance",
    "Title",
    "Library",
    "Author",
    "Oldest first",
    "Newest first",
]
AUDIOBOOK_SORT_LABELS = [
    "Relevance",
    "Title",
    "Site",
    "Author",
    "Shortest recording",
    "Longest recording",
]
ARCHIVE_SORT_LABELS = [
    "Relevance",
    "Title",
    "Collection",
    "Creator",
    "Oldest first",
    "Newest first",
]
COLUMN_HEADINGS = ("Title", "Artist / channel", "Source", "Duration", "Size")
BOOK_COLUMN_HEADINGS = ("Title", "Author", "Library", "Year", "Size")
AUDIOBOOK_COLUMN_HEADINGS = ("Title", "Author", "Site", "Duration", "Chapters")
ARCHIVE_COLUMN_HEADINGS = ("Title", "Creator", "Collection", "Year", "Size")


def _is_adult_engine(engine):
    return engine in ADULT_ENGINE_CATEGORIES


def _is_archive_engine(engine):
    return engine in ARCHIVE_ENGINE_CATEGORIES


def _sort_labels(engine):
    if engine == ENGINE_BOOKS:
        return BOOK_SORT_LABELS
    if engine == ENGINE_AUDIOBOOKS:
        return AUDIOBOOK_SORT_LABELS
    if _is_archive_engine(engine):
        return ARCHIVE_SORT_LABELS
    return SORT_LABELS


def _column_headings(engine):
    if engine == ENGINE_BOOKS:
        return BOOK_COLUMN_HEADINGS
    if engine == ENGINE_AUDIOBOOKS:
        return AUDIOBOOK_COLUMN_HEADINGS
    if _is_archive_engine(engine):
        return ARCHIVE_COLUMN_HEADINGS
    return COLUMN_HEADINGS


def _year(item):
    try:
        return int(str(item.get("year") or "").strip()[:4])
    except (TypeError, ValueError):
        return None


def _sorted_results(items, mode, engine=None):
    """Return results in a stable, deterministic display order."""
    indexed = list(enumerate(items))

    if ((engine == ENGINE_BOOKS or _is_archive_engine(engine)) and
            mode in (SORT_SHORTEST, SORT_LONGEST)):
        # These results carry a year rather than a duration, so the two
        # duration slots sort by when the work was published.
        newest = mode == SORT_LONGEST

        def key(pair):
            year = _year(pair[1])
            return (
                year is None,
                -(year or 0) if newest else (year or 0),
                str(pair[1].get("title", "")).casefold(),
                pair[0],
            )

        return [item for _index, item in sorted(indexed, key=key)]

    def text(item, *names):
        for name in names:
            value = item.get(name)
            if value:
                return str(value).casefold()
        return ""

    def duration(item):
        value = item.get("duration_s")
        if value is None:
            value = item.get("duration")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    if mode == SORT_RELEVANCE:
        return [item for index, item in sorted(
            indexed,
            key=lambda pair: pair[1].get("_search_order", pair[0]),
        )]
    if mode == SORT_NAME:
        def key(pair):
            return text(pair[1], "title"), pair[0]
    elif mode == SORT_SITE:
        def key(pair):
            return (
                text(pair[1], "source") or "youtube",
                text(pair[1], "title"), pair[0],
            )
    elif mode == SORT_ARTIST:
        def key(pair):
            return (
                text(pair[1], "artist", "uploader"),
                text(pair[1], "title"), pair[0],
            )
    elif mode == SORT_SHORTEST:
        def key(pair):
            return (
                duration(pair[1]) is None,
                duration(pair[1]) or 0,
                text(pair[1], "title"), pair[0],
            )
    elif mode == SORT_LONGEST:
        def key(pair):
            return (
                duration(pair[1]) is None,
                -(duration(pair[1]) or 0),
                text(pair[1], "title"), pair[0],
            )
    else:
        return list(items)
    return [item for _index, item in sorted(indexed, key=key)]


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
        self.closing = False
        self.next_result_order = 0
        self.preview_token = None
        self.archive_token = None
        # Refreshes the status bar while slow sites are still working.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        query_label = wx.StaticText(self, label="&Search:")
        self.query_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.query_text.SetName("Search query")
        self.query_text.Bind(wx.EVT_TEXT_ENTER, self.on_search)

        engine_label = wx.StaticText(self, label="S&ource:")
        self.engine_choice = wx.Choice(
            self, choices=self._visible_engine_labels())
        self.engine_choice.SetName("Search source")
        self.engine_choice.SetSelection(0)
        self.engine_choice.Bind(wx.EVT_CHOICE, self.on_engine_changed)

        sort_label = wx.StaticText(self, label="Sort &by:")
        self.sort_choice = wx.Choice(self, choices=SORT_LABELS)
        self.sort_choice.SetName("Sort search results")
        self.sort_choice.SetSelection(SORT_RELEVANCE)
        self.sort_choice.Bind(wx.EVT_CHOICE, self.on_sort_changed)

        self.search_btn = wx.Button(self, label="&Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self.on_search)

        self.results_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.results_list.SetName("Search results")
        self.results_list.SetHelpText(
            "Select results. Enter downloads; Control C copies the URL; "
            "Context Menu opens actions.")
        for i, heading in enumerate(COLUMN_HEADINGS):
            self.results_list.InsertColumn(i, heading)
        self.results_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_download_selected)
        self.results_list.Bind(wx.EVT_CONTEXT_MENU, self.on_results_menu)
        self.results_list.Bind(wx.EVT_CHAR, self.on_results_char)

        self.preview_btn = wx.Button(self, label="&Preview selected")
        self.preview_btn.SetHelpText(
            "Plays music as audio and video results with picture and sound.")
        self.preview_btn.Bind(wx.EVT_BUTTON, self.on_preview_selected)
        self.player = MediaPlayerPanel(self, frame, video_height=150)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(engine_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.engine_choice, 0, wx.RIGHT, 12)
        top.Add(sort_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.sort_choice, 0, wx.RIGHT, 12)
        top.Add(self.search_btn, 0)

        sizer.Add(query_label, 0, wx.ALL, 8)
        sizer.Add(self.query_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(top, 0, wx.ALL, 8)
        sizer.Add(self.results_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT |
                  wx.BOTTOM, 8)
        sizer.Add(self.preview_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)

    def focus_input(self):
        if self.closing:
            return
        self.query_text.SetFocus()

    def _visible_engine_labels(self):
        if self.frame.config["adult_sites_enabled"]:
            return ENGINE_LABELS
        return ENGINE_LABELS[:GENERAL_ENGINE_COUNT]

    def refresh_engine_choices(self):
        """Show or hide adult categories after Settings changes."""
        selection = self.engine_choice.GetSelection()
        labels = self._visible_engine_labels()
        self.engine_choice.Clear()
        self.engine_choice.AppendItems(labels)
        if selection < 0 or selection >= len(labels):
            selection = ENGINE_MUSIC
        self.engine_choice.SetSelection(selection)
        self._apply_engine_controls(selection)

    def _apply_engine_controls(self, engine):
        """Name the sort choices and the preview button for one engine.

        Books have no duration to sort by and nothing to play, so the same
        controls are renamed rather than duplicated -- a screen reader then
        reads "Author" and "Newest first" instead of options that mean
        nothing for a book.
        """
        labels = _sort_labels(engine)
        if [self.sort_choice.GetString(index)
                for index in range(self.sort_choice.GetCount())] != labels:
            selection = self.sort_choice.GetSelection()
            self.sort_choice.Clear()
            self.sort_choice.AppendItems(labels)
            self.sort_choice.SetSelection(
                selection if 0 <= selection < len(labels) else SORT_RELEVANCE)
        self.preview_btn.Enable(engine != ENGINE_BOOKS)

    def _apply_engine_columns(self, engine):
        for index, heading in enumerate(_column_headings(engine)):
            column = self.results_list.GetColumn(index)
            if column.GetText() != heading:
                column.SetText(heading)
                self.results_list.SetColumn(index, column)

    def on_engine_changed(self, event):
        self._apply_engine_controls(self.engine_choice.GetSelection())
        event.Skip()

    def shutdown(self):
        """Stop timers and silence worker callbacks before widgets are freed."""
        if self.closing:
            return
        self.closing = True
        if self.stop is not None:
            self.stop.set()
        self.timer.Stop()
        self.player.shutdown()

    # -- search -----------------------------------------------------------

    def on_search(self, event):
        if self.closing:
            return
        query = self.query_text.GetValue().strip()
        if not query:
            self.frame.announce("Type a search first.")
            return
        engine = self.engine_choice.GetSelection()
        if engine == ENGINE_MUSIC:
            sources = musicdl_backend.enabled_sources(
                self.frame.config["disabled_music_sources"])
            if not sources:
                self.frame.announce(
                    "No music sites selected. Use Tools, Search sites.")
                return
        elif engine == ENGINE_BOOKS:
            sources = book_backend.enabled_sources(
                self.frame.config["disabled_book_sources"])
            if not sources:
                self.frame.announce(
                    "No book libraries selected. Use Tools, Search sites.")
                return
        elif engine == ENGINE_AUDIOBOOKS:
            sources = audiobook_backend.enabled_sources(
                self.frame.config["disabled_audiobook_sources"])
            if not sources:
                self.frame.announce(
                    "No audiobook sites selected. Use Tools, Search sites.")
                return
        elif _is_archive_engine(engine):
            sources = archive_backend.enabled_sources(
                self.frame.config["disabled_archive_sources"],
                ARCHIVE_ENGINE_CATEGORIES[engine])
            if not sources:
                self.frame.announce(
                    "No Internet Archive collections selected. Use Tools, "
                    "Search sites.")
                return
        elif _is_adult_engine(engine):
            if not self.frame.config["adult_sites_enabled"]:
                self.frame.announce(
                    "Adult sites are disabled. Enable them in Settings.")
                return
            sources = adult_backend.enabled_sources(
                self.frame.config["disabled_adult_sources"])
            unavailable = adult_backend.unavailable_sources()
            sources = [source for source in sources if source not in unavailable]
            if not sources:
                self.frame.announce(
                    "Adult API packages are unavailable. Reinstall blindDL "
                    "to restore them.")
                return
        else:
            sources = []
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
        self.next_result_order = 0
        self.timer.Stop()
        self.results_list.DeleteAllItems()
        self._apply_engine_columns(engine)

        if engine == ENGINE_MUSIC:
            # Side B's Deezer catalog search goes out next to the musicdl
            # sites and reports through the same per-site callback.
            count = len(sources) + 1
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} music {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        elif engine == ENGINE_BOOKS:
            count = len(sources)
            library_word = "library" if count == 1 else "libraries"
            self.frame.announce(
                f"Searching {count} book {library_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        elif engine == ENGINE_AUDIOBOOKS:
            count = len(sources)
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} audiobook {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        elif _is_archive_engine(engine):
            count = len(sources)
            word = "collection" if count == 1 else "collections"
            self.frame.announce(
                f"Searching {count} Internet Archive {word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        elif _is_adult_engine(engine):
            count = len(sources)
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} {ENGINE_LABELS[engine]} {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)...")
        else:
            self.frame.announce("Searching YouTube...")
        threading.Thread(target=self._search, args=(query, engine, self.token,
                                                    self.stop, sources),
                         daemon=True).start()

    def _search(self, query, engine, token, stop, sources):
        asked = []
        try:
            if engine == ENGINE_MUSIC:
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
            elif engine == ENGINE_BOOKS:
                def on_library(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = book_backend.search(
                    query, self.frame.config["search_timeout_s"],
                    on_site=on_library, stop=stop, sources=sources)
                # on_library already delivered these.
                items = []
            elif engine == ENGINE_AUDIOBOOKS:
                def on_audiobook_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = audiobook_backend.search(
                    query, self.frame.config["search_timeout_s"],
                    on_site=on_audiobook_site, stop=stop, sources=sources)
                # on_audiobook_site already delivered these.
                items = []
            elif _is_archive_engine(engine):
                def on_collection(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = archive_backend.search(
                    query, self.frame.config["search_timeout_s"],
                    on_site=on_collection, stop=stop, sources=sources)
                # on_collection already delivered these.
                items = []
            elif _is_adult_engine(engine):
                def on_adult_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = adult_backend.search(
                    query, self.frame.config["search_timeout_s"],
                    on_site=on_adult_site, stop=stop, sources=sources,
                    category=ADULT_ENGINE_CATEGORIES[engine])
                # on_adult_site already delivered these.
                items = []
            else:
                items = ytdlp_backend.search(query)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._search_failed, token, str(exc))
            return
        if not stop.is_set():
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
        if self.closing or token is not self.token:
            return
        self.search_btn.Enable()
        self.frame.announce("Search failed.")
        wx.MessageBox(f"Search failed:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _add_site(self, token, engine, source, items):
        """Append one site's results. Runs on the GUI thread."""
        if (self.closing or token is not self.token or
                source in self.shown_sources):
            return
        self.shown_sources.add(source)
        if not items:
            return
        selected = self._selected_result_objects()
        focused = self._focused_result_object()
        for item in items:
            item["_search_order"] = self.next_result_order
            self.next_result_order += 1
            self.results.append(item)
        self.results = _sorted_results(
            self.results, self.sort_choice.GetSelection(), engine)
        self._render_results(engine, selected=selected, focused=focused)
        if self.done:
            # A late site: say so on the status bar, but leave focus alone.
            self.frame.announce(
                f"{self._result_count()}, latest from {source}. "
                f"{self._pending_phrase()}")
            if not self._pending():
                self.timer.Stop()

    def _insert_result_row(self, row, item, engine):
        self.results_list.InsertItem(row, item["title"])
        if engine == ENGINE_BOOKS:
            self.results_list.SetItem(row, 1, item.get("author", ""))
            self.results_list.SetItem(row, 2, item.get("source", ""))
            self.results_list.SetItem(row, 3, str(item.get("year") or ""))
            size = item.get("file_size") or ""
            book_format = item.get("format") or ""
            # The format matters more than the byte count when choosing a
            # book, so it rides along in the column that has room for it.
            self.results_list.SetItem(
                row, 4, f"{book_format} {size}".strip())
        elif engine == ENGINE_AUDIOBOOKS:
            author = item.get("author", "")
            narrator = item.get("narrator", "")
            if narrator and narrator != author:
                author = f"{author}, read by {narrator}" if author else \
                    f"read by {narrator}"
            self.results_list.SetItem(row, 1, author)
            self.results_list.SetItem(row, 2, item.get("source", ""))
            self.results_list.SetItem(
                row, 3, ytdlp_backend.format_duration(item.get("duration_s")))
            chapters = item.get("chapters") or 0
            self.results_list.SetItem(
                row, 4, str(chapters) if chapters else "")
        elif _is_archive_engine(engine):
            self.results_list.SetItem(row, 1, item.get("creator", ""))
            self.results_list.SetItem(row, 2, item.get("source", ""))
            self.results_list.SetItem(row, 3, str(item.get("year") or ""))
            self.results_list.SetItem(row, 4, item.get("file_size", ""))
        elif engine != ENGINE_YOUTUBE:
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

    def _selected_result_objects(self):
        return [
            self.results[index] for index in self._selected_indices()
            if index < len(self.results)
        ]

    def _focused_result_object(self):
        index = self.results_list.GetFocusedItem()
        return self.results[index] if 0 <= index < len(self.results) else None

    def _render_results(self, engine, selected=(), focused=None):
        selected_ids = {id(item) for item in selected}
        self._apply_engine_columns(engine)
        self.results_list.DeleteAllItems()
        for row, item in enumerate(self.results):
            self._insert_result_row(row, item, engine)
            if id(item) in selected_ids:
                self.results_list.Select(row)
            if item is focused:
                self.results_list.Focus(row)

    def on_sort_changed(self, event):
        selected = self._selected_result_objects()
        focused = self._focused_result_object()
        mode = self.sort_choice.GetSelection()
        self.results = _sorted_results(self.results, mode, self.result_engine)
        self._render_results(
            self.result_engine, selected=selected, focused=focused)
        labels = _sort_labels(self.result_engine)
        label = labels[mode] if 0 <= mode < len(labels) else "selected order"
        if self.results:
            self.frame.announce(f"Sorted {self._result_count()} by {label}.")
        else:
            self.frame.announce(f"Sort set to {label}.")

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
        if self.closing:
            return
        if self.done and not self._pending():
            self.timer.Stop()
            return
        if self.done:
            self.frame.announce(
                f"{self._result_count()} so far. {self._pending_phrase()}")

    def _search_done(self, token, items, engine, asked=()):
        if self.closing or token is not self.token:
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

    # -- Internet Archive items ---------------------------------------------

    def _queue_archive_item(self, item):
        """Resolve one Archive item's files, then queue or offer a choice."""
        token = self.archive_token = object()
        self.frame.announce(f"Reading file list: {item['title']}")
        threading.Thread(
            target=self._resolve_archive_files,
            args=(token, item),
            daemon=True,
            name="blinddl-archive-files",
        ).start()

    def _resolve_archive_files(self, token, item):
        try:
            files = archive_backend.item_files(
                item["identifier"], video=bool(item.get("video")))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._archive_files_failed, token, str(exc))
            return
        wx.CallAfter(self._archive_files_ready, token, item, files)

    def _archive_files_failed(self, token, error):
        if self.closing or token is not self.archive_token:
            return
        self.frame.announce("Could not read that item's file list.")
        wx.MessageBox(f"Could not read that item:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _archive_files_ready(self, token, item, files):
        if self.closing or token is not self.archive_token:
            return
        if len(files) == 1:
            chosen = files
        else:
            dialog = ItemPickerDialog(self, files, item["title"])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    self.frame.announce("Download cancelled.")
                    return
                chosen = dialog.selected_items()
            finally:
                dialog.Destroy()
            self.results_list.SetFocus()
        if not chosen:
            self.frame.announce("Nothing selected.")
            return
        for entry in chosen:
            payload = dict(entry)
            payload["collection_title"] = item["title"]
            self.frame.queue.add_archive(payload, entry["title"])
        if len(chosen) == 1:
            self.frame.announce(f"Queued: {chosen[0]['title']}")
        else:
            self.frame.announce(f"Queued {len(chosen)} downloads.")

    def on_results_char(self, event):
        if event.GetKeyCode() == 3 and event.ControlDown():  # Ctrl+C
            self.on_copy_url(event)
            return
        event.Skip()

    def on_copy_url(self, event):
        indices = [index for index in self._selected_indices()
                   if index < len(self.results)]
        if not indices:
            self.frame.announce("Select a result first.")
            return
        urls = []
        missing = 0
        for index in indices:
            url = preview.result_url(self.results[index])
            if not url:
                missing += 1
            elif url not in urls:
                urls.append(url)
        if not urls:
            self.frame.announce("No URL for that result.")
            return
        if not wx.TheClipboard.Open():
            self.frame.announce("Could not open the clipboard.")
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject("\n".join(urls)))
            # Keep the URL on the clipboard after blindDL exits.
            wx.TheClipboard.Flush()
        finally:
            wx.TheClipboard.Close()
        noun = "URL" if len(urls) == 1 else "URLs"
        message = f"Copied {len(urls)} {noun}."
        if missing:
            message += f" {missing} had no URL."
        self.frame.announce(message)

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
        preview_item = menu.Append(wx.ID_ANY, "&Preview selected")
        download = menu.Append(wx.ID_ANY, "&Download selected")
        copy_url = menu.Append(wx.ID_ANY, "Copy &URL\tCtrl+C")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear = menu.Append(wx.ID_ANY, "&Clear selection")
        has_selection = bool(self._selected_indices())
        preview_item.Enable(has_selection and self.result_engine != ENGINE_BOOKS)
        download.Enable(has_selection)
        copy_url.Enable(has_selection)
        clear.Enable(has_selection)
        select_all.Enable(
            self.results_list.GetSelectedItemCount() <
            self.results_list.GetItemCount())
        menu.Bind(wx.EVT_MENU, self.on_preview_selected, preview_item)
        menu.Bind(wx.EVT_MENU, self.on_download_selected, download)
        menu.Bind(wx.EVT_MENU, self.on_copy_url, copy_url)
        menu.Bind(wx.EVT_MENU, self._select_all, select_all)
        menu.Bind(wx.EVT_MENU, self._clear_selection, clear)
        self.results_list.PopupMenu(menu)
        menu.Destroy()

    def on_preview_selected(self, event):
        if self.result_engine == ENGINE_BOOKS:
            self.frame.announce(
                "Books cannot be previewed. Press Enter to download, then "
                "open it from the Library tab.")
            return
        indices = [index for index in self._selected_indices()
                   if index < len(self.results)]
        if not indices:
            self.frame.announce("Select a result to preview first.")
            return
        index = self.results_list.GetFocusedItem()
        if index not in indices:
            index = indices[0]
        item = self.results[index]
        audio_only = self.result_engine in (ENGINE_MUSIC, ENGINE_AUDIOBOOKS)
        token = self.preview_token = object()
        self.preview_btn.Disable()
        self.frame.announce(f"Preparing preview: {item['title']}")
        threading.Thread(
            target=self._resolve_preview,
            args=(token, item, audio_only),
            daemon=True,
            name="blinddl-search-preview",
        ).start()

    def _resolve_preview(self, token, item, audio_only):
        try:
            location, title = preview.resolve_search_result(
                item, audio_only, self.frame.config)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._preview_failed, token, str(exc))
            return
        wx.CallAfter(self._preview_ready, token, location, title)

    def _preview_ready(self, token, location, title):
        if self.closing or token is not self.preview_token:
            return
        self.preview_btn.Enable()
        self.frame.play_media(self.player, location, title)

    def _preview_failed(self, token, error):
        if self.closing or token is not self.preview_token:
            return
        self.preview_btn.Enable()
        self.frame.announce("Could not play that preview.")
        wx.MessageBox(
            f"Could not play that preview:\n{error}", "blindDL",
            wx.OK | wx.ICON_ERROR, self,
        )

    def on_download_selected(self, event):
        indices = [i for i in self._selected_indices()
                   if i < len(self.results)]
        if not indices:
            self.frame.announce("Select a result first.")
            return
        engine = self.result_engine
        if _is_archive_engine(engine) and len(indices) == 1:
            # One Archive item can be a whole radio series. Ask which
            # episodes to take before filling the queue with hundreds.
            self._queue_archive_item(self.results[indices[0]])
            return
        for index in indices:
            item = self.results[index]
            if engine == ENGINE_MUSIC:
                if item.get("kind") == "sideb":
                    self.frame.queue.add_sideb(item["url"], item["title"])
                else:
                    self.frame.queue.add_musicdl(
                        item["song_info"], item["title"])
            elif engine == ENGINE_BOOKS:
                self.frame.queue.add_book(item, item["title"])
            elif engine == ENGINE_AUDIOBOOKS:
                self.frame.queue.add_audiobook(item, item["title"])
            elif _is_archive_engine(engine):
                self.frame.queue.add_archive(item, item["title"])
            elif _is_adult_engine(engine):
                self.frame.queue.add_adult(item, item["title"])
            else:
                self.frame.queue.add_ytdlp(item["url"], item["title"])
        if len(indices) == 1:
            self.frame.announce(f"Queued: {self.results[indices[0]]['title']}")
        else:
            self.frame.announce(f"Queued {len(indices)} downloads.")
