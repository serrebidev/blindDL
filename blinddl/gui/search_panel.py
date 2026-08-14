# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Search tab: music, books, audiobooks, Archive media, adult sites, yt-dlp."""

import ntpath
import re
import sys
import threading
import time

import wx

from .. import (
    adult_backend,
    archive_backend,
    applemusic_backend,
    audiobook_backend,
    bandcamp_backend,
    book_backend,
    deezer_backend,
    musicdl_backend,
    preview,
    search_order,
    sideb_backend,
    soulseek_backend,
    torrent_backend,
    ytdlp_backend,
)
from .. import search_kind
from ..search_kind import KIND_ALBUM, KIND_BEST
from ..search_order import ORDER_RECENT, ORDER_RELEVANCE
from ..downloader import addition_summary
from .item_picker_dialog import ItemPickerDialog
from .media_player import MediaPlayerPanel

ENGINE_MUSIC = 0
ENGINE_YOUTUBE = 1
ENGINE_SOUNDCLOUD = 2
ENGINE_BANDCAMP = 3
ENGINE_APPLE_MUSIC = 4
ENGINE_BOOKS = 5
ENGINE_AUDIOBOOKS = 6
ENGINE_ARCHIVE_AUDIO = 7
ENGINE_ARCHIVE_VIDEO = 8
ENGINE_TORRENTS = 9
ENGINE_STRAIGHT = 10
ENGINE_GAY = 11
ENGINE_LESBIAN = 12
ENGINE_BISEXUAL = 13
ENGINE_TRANS = 14
ENGINE_SOULSEEK_AUDIO = 15
ENGINE_SOULSEEK_VIDEO = 16
ENGINE_SOULSEEK_BOOKS = 17
ENGINE_SOULSEEK_TORRENTS = 18
ENGINE_DEEZER = 19
# Kept as an import-compatible name for callers that treated adult search as
# the first adult choice before content categories were separated.
ENGINE_ADULT = ENGINE_STRAIGHT
ENGINE_LABELS = [
    "Music sites",
    "YouTube",
    "SoundCloud",
    "Bandcamp",
    "Apple Music",
    "Books",
    "Audiobooks",
    "Internet Archive audio and old-time radio",
    "Internet Archive movies and TV",
    "Torrents",
    "Straight porn",
    "Gay porn",
    "Lesbian porn",
    "Bisexual porn",
    "Trans porn",
    "Soulseek music and audio",
    "Soulseek movies and video",
    "Soulseek books and documents",
    "Soulseek torrent files",
    "Deezer",
]
# The engines shown before the adult categories (and the Soulseek file-type
# sections), in display order. Deezer sits straight after "Music sites" as
# the single-service choice that mirrors it; its search needs no sign-in, so
# it is always shown.
GENERAL_ENGINES = (
    ENGINE_MUSIC,
    ENGINE_DEEZER,
    ENGINE_YOUTUBE,
    ENGINE_SOUNDCLOUD,
    ENGINE_BANDCAMP,
    ENGINE_APPLE_MUSIC,
    ENGINE_BOOKS,
    ENGINE_AUDIOBOOKS,
    ENGINE_ARCHIVE_AUDIO,
    ENGINE_ARCHIVE_VIDEO,
    ENGINE_TORRENTS,
)
# How many engines are always shown. The ids are not contiguous because the
# adult and Soulseek choices keep their historical numbers, so callers must
# count the tuple rather than assume ENGINE_0..N.
GENERAL_ENGINE_COUNT = len(GENERAL_ENGINES)
# The engines whose results are music, and where "album" or "artist" is
# therefore something to search for. A book library or a torrent indexer has
# no such fields, so the Search type control is switched off for them rather
# than offering choices that could not change the answer.
KIND_ENGINES = (
    ENGINE_MUSIC,
    ENGINE_DEEZER,
    ENGINE_APPLE_MUSIC,
)
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
SOULSEEK_ENGINE_KINDS = {
    ENGINE_SOULSEEK_AUDIO: "audio",
    ENGINE_SOULSEEK_VIDEO: "video",
    ENGINE_SOULSEEK_BOOKS: "book",
    ENGINE_SOULSEEK_TORRENTS: "torrent",
}
# The Sort by control rearranges rows that have already arrived. The Order
# control above it goes out with the query and decides which rows arrive at
# all; the two are separate because a site cannot be re-asked for nothing,
# and re-sorting a page of results is instant while re-searching is not.
SORT_RELEVANCE = 0
SORT_NAME = 1
SORT_SITE = 2
SORT_ARTIST = 3
SORT_SHORTEST = 4
SORT_LONGEST = 5
# Two further slots that only some engines have anything to fill. A list
# shorter than this simply does not offer them.
SORT_OLDEST = 6
SORT_NEWEST = 7
SORT_LABELS = [
    # Relevance is the order the sites answered in, so it is also what
    # "most recent" and "most popular" look like once they have been asked
    # for -- which is why choosing an Order returns this control to it.
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
# A torrent has no artist and no duration; what decides between two of them
# is how many people are sharing it, so the two duration slots sort on that.
# It does have a posting date, and unlike a book's year that date is exact,
# so torrents are the one engine that offers both pairs.
TORRENT_SORT_LABELS = [
    "Relevance",
    "Name",
    "Indexer",
    "Uploader",
    "Fewest seeders",
    "Most seeders",
    "Oldest first",
    "Newest first",
]
SOULSEEK_SORT_LABELS = [
    "Relevance",
    "Name",
    "Availability",
    "Peer",
    "Smallest file",
    "Largest file",
]
# File type sits second everywhere: a screen reader reads a row in column
# order, so the answer to "what will I actually get?" arrives right after the
# title instead of at the end of the row.
COLUMN_HEADINGS = ("Title", "Type", "Artist / channel", "Source", "Duration", "Size")
BOOK_COLUMN_HEADINGS = ("Title", "Type", "Author", "Library", "Year", "Size")
AUDIOBOOK_COLUMN_HEADINGS = ("Title", "Type", "Author", "Site", "Duration", "Chapters")
ARCHIVE_COLUMN_HEADINGS = ("Title", "Type", "Creator", "Collection", "Year", "Size")
TORRENT_COLUMN_HEADINGS = ("Title", "Type", "Seeders", "Indexer", "Age", "Size")
SOULSEEK_COLUMN_HEADINGS = (
    "Title",
    "Type",
    "Peer",
    "Folder",
    "Availability",
    "Size",
)


def _is_adult_engine(engine):
    return engine in ADULT_ENGINE_CATEGORIES


def _is_soulseek_engine(engine):
    return engine in SOULSEEK_ENGINE_KINDS


def _is_book_engine(engine):
    return engine in (ENGINE_BOOKS, ENGINE_SOULSEEK_BOOKS)


def _is_torrent_engine(engine):
    return engine in (ENGINE_TORRENTS, ENGINE_SOULSEEK_TORRENTS)


def _plays(engine):
    """Whether this engine's results are something blindDL can play.

    A book is a file for a reader, and a torrent is a link for a BitTorrent
    client -- neither has a stream to preview.
    """
    return not (
        _is_book_engine(engine)
        or _is_torrent_engine(engine)
        or _is_soulseek_engine(engine)
    )


def _is_archive_engine(engine):
    return engine in ARCHIVE_ENGINE_CATEGORIES


def _soulseek_media_kind(engine):
    """Return the extension group for an explicit Soulseek-only source."""
    return SOULSEEK_ENGINE_KINDS.get(engine)


def _sort_labels(engine):
    if _is_soulseek_engine(engine):
        return SOULSEEK_SORT_LABELS
    if _is_book_engine(engine):
        return BOOK_SORT_LABELS
    if engine == ENGINE_AUDIOBOOKS:
        return AUDIOBOOK_SORT_LABELS
    if _is_torrent_engine(engine):
        return TORRENT_SORT_LABELS
    if _is_archive_engine(engine):
        return ARCHIVE_SORT_LABELS
    return SORT_LABELS


def _sort_for_order(engine, order):
    """The Sort by slot that shows *order* the way the user just asked for it.

    Choosing Most recent and then reading the list in relevance order looks
    like nothing happened, so the display sort follows the search order.
    Where an engine has a real date or swarm column the rows are put in that
    order outright; where it has neither, Relevance is already the answer,
    because that slot keeps the sequence the sites replied in.
    """
    order = search_order.normalize(order)
    if order == ORDER_RELEVANCE:
        return SORT_RELEVANCE
    if engine == ENGINE_TORRENTS:
        return SORT_NEWEST if order == ORDER_RECENT else SORT_LONGEST
    # A book or Archive row's year is when the work was originally made, while
    # providers interpret "recent" as when the edition or upload appeared.
    # Re-sorting by year would therefore undo the order just requested.
    return SORT_RELEVANCE


def _order_capable_sources(engine, sources, order, config, kind=KIND_BEST):
    """Split *sources* into the ones that can answer *order* and the rest.

    Every engine keeps its own map of what its sites can sort by, so this
    asks each backend rather than second-guessing it here. An engine whose
    sites cannot sort at all -- the music sites, SoundCloud, Bandcamp --
    reports everything as unable, which is what the user is then told.
    """
    order = search_order.normalize(order)
    if order == ORDER_RELEVANCE:
        return list(sources), []

    def can(source):
        if engine == ENGINE_BOOKS:
            return book_backend.supports_order(source, order)
        if engine == ENGINE_AUDIOBOOKS:
            return audiobook_backend.supports_order(source, order)
        if engine == ENGINE_TORRENTS:
            return torrent_backend.supports_order(source, order, config)
        if _is_archive_engine(engine):
            return archive_backend.supports_order(source, order)
        if _is_adult_engine(engine):
            return adult_backend.supports_order(source, order)
        if engine == ENGINE_YOUTUBE:
            return ytdlp_backend.supports_order(order)
        if engine in (ENGINE_MUSIC, ENGINE_DEEZER):
            # musicdl drives four dozen site search forms and not one of
            # them exposes a sort. Deezer is the exception, and only for
            # popularity, which it publishes as a rank per track.
            return (
                source == deezer_backend._SEARCH_SOURCE
                and deezer_backend.supports_order(order, kind)
            )
        # SoundCloud, Bandcamp and Apple Music each offer one search and no
        # way to order it.
        return False

    able = [source for source in sources if can(source)]
    unable = [source for source in sources if not can(source)]
    return able, unable


def _order_phrase(order, unable, total):
    """One sentence on how far the chosen order actually reached.

    A screen reader reads this straight after the result count, so it names
    at most a few sites and then counts the rest.
    """
    if search_order.normalize(order) == ORDER_RELEVANCE or not unable:
        return ""
    label = search_order.label(order).lower()
    if len(unable) >= total:
        return f"No site here can sort by {label}; showing best match."
    names = ", ".join(unable[:3])
    if len(unable) > 3:
        names += f" and {len(unable) - 3} more"
    site_word = "site" if len(unable) == 1 else "sites"
    pronoun = "it" if len(unable) == 1 else "they"
    return (
        f"{len(unable)} {site_word} cannot sort by {label}, so {pronoun} "
        f"answered by best match: {names}."
    )


def _music_source_label(source):
    """A musicdl source read out the way its own site is named."""
    if source in musicdl_backend.ALL_SOURCES:
        return musicdl_backend.source_label(source)
    return source


def _dropdown_is_open(control):
    """Whether this combo box is showing its list.

    Enter means two things in a combo box. With the list open it picks the
    item being read, and that is all it means: searching then would carry
    the focus off into the results while the choice was still being walked,
    which is the whole complaint about these controls. With the list closed
    there is nothing left to pick, so Enter is free to run the search.

    Windows can be asked outright, through the native combo box behind the
    control. Everywhere else the dropdown and close-up events recorded on
    the control answer instead, and a platform that reports neither simply
    behaves as it did before.
    """
    if getattr(control, "_blinddl_popup_open", False):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes  # noqa: PLC0415 - Windows-only, and only for this ask

        handle = control.GetHandle()
        if not handle:
            return False
        # CB_GETDROPPEDSTATE: non-zero while the list part is dropped down.
        return bool(ctypes.windll.user32.SendMessageW(int(handle), 0x0157, 0, 0))
    except Exception:  # noqa: BLE001 - a key press must never fail on this
        return False


def _album_folder(album):
    """The folder a whole album downloads into: "Artist - Album".

    Two artists can release an album under the same name, and a folder
    called Greatest Hits with both of them in it is no use to anyone. The
    artist is dropped when the row does not name one.
    """
    title = str(album.get("title") or album.get("album") or "").strip()
    artist = str(album.get("artist") or "").strip()
    if not title:
        return ""
    return f"{artist} - {title}" if artist else title


def _is_album_item(item):
    """Whether this row is a whole album rather than one track.

    Album rows are resolved to their tracks before anything is queued, so
    the queue never sees one; the download and preview paths ask this to
    know that resolving is needed.
    """
    return str(item.get("kind") or "").endswith("_album")


def _kind_capable_sources(engine, sources, kind):
    """Split *sources* into the ones that can search by *kind* and the rest.

    Only the two services with a real catalogue API behind them can match a
    single named field. The music sites musicdl drives all have one search
    box and nothing else, which is what they are then said to have answered.
    """
    kind = search_kind.normalize(kind)
    if kind == KIND_BEST:
        return list(sources), []

    def can(source):
        if engine in (ENGINE_MUSIC, ENGINE_DEEZER):
            return (
                source == deezer_backend._SEARCH_SOURCE
                and deezer_backend.supports_kind(kind)
            )
        if engine == ENGINE_APPLE_MUSIC:
            return applemusic_backend.supports_kind(kind)
        return False

    able = [source for source in sources if can(source)]
    unable = [source for source in sources if not can(source)]
    return able, unable


def _kind_phrase(kind, able, unable):
    """One sentence on which sites could search for what was asked.

    Album is the type that changes what a result *is*, so the sites that
    cannot answer it are left out of the search rather than filling the list
    with tracks. The other types only change the matching, so every site is
    still asked and the ones that could not narrow it are named.
    """
    kind = search_kind.normalize(kind)
    if kind == KIND_BEST or not unable:
        return ""
    label = search_kind.label(kind).lower()
    if not able:
        return f"No site here can search by {label}; showing best match."
    site_word = "site" if len(unable) == 1 else "sites"
    names = ", ".join(able[:3])
    if kind == KIND_ALBUM:
        return (
            f"Only {names} can search by {label}, so the other "
            f"{len(unable)} {site_word} were not asked."
        )
    pronoun = "it" if len(unable) == 1 else "they"
    return (
        f"{len(unable)} {site_word} cannot search by {label}, so {pronoun} "
        f"answered by best match."
    )


def _column_headings(engine):
    if _is_soulseek_engine(engine):
        return SOULSEEK_COLUMN_HEADINGS
    if _is_book_engine(engine):
        return BOOK_COLUMN_HEADINGS
    if engine == ENGINE_AUDIOBOOKS:
        return AUDIOBOOK_COLUMN_HEADINGS
    if _is_torrent_engine(engine):
        return TORRENT_COLUMN_HEADINGS
    if _is_archive_engine(engine):
        return ARCHIVE_COLUMN_HEADINGS
    return COLUMN_HEADINGS


# Extensions worth reading out of a media URL. Anything else in a path is
# far more likely to be a tracking segment than the file that arrives.
_URL_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".ogg",
    ".opus",
    ".flac",
    ".wav",
    ".aac",
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".mpeg",
    ".mpg",
    ".ts",
    ".epub",
    ".pdf",
    ".txt",
    ".mobi",
    ".azw3",
    ".djvu",
    ".fb2",
    ".cbz",
)


def _result_type(item):
    """The file type of one result, said the way a reader would say it.

    Backends that know their file type publish it as "format"; the rest are
    read off whatever URL the result already carries. An empty string means
    the site genuinely has not said yet, which is better than guessing.
    """
    known = str(item.get("format") or "").strip()
    if known:
        return known if known.isupper() or " " in known else known.upper()
    for field in ("direct_url", "download_url", "file_name", "url"):
        value = str(item.get(field) or "").split("?")[0].split("#")[0].lower()
        for extension in _URL_EXTENSIONS:
            if value.endswith(extension):
                return extension.lstrip(".").upper()
    if item.get("kind") == "adult" or item.get("hls"):
        # Every adult provider hands back one MP4, whether it came down as a
        # progressive file or as an HLS playlist yt-dlp muxed.
        return "MP4"
    return ""


def _pick(column, item, *fields):
    """The plain item field one of columns 2 to 5 shows, or "" for none.

    Each engine lays its four trailing columns out differently, and most of
    them are a straight field lookup. Naming those four in order keeps the
    per-engine layouts readable next to each other; a None means that column
    is not a plain lookup and the caller handled it already.
    """
    index = column - 2
    if not (0 <= index < len(fields)):
        return ""
    field = fields[index]
    return str(item.get(field) or "") if field else ""


def _year(item):
    try:
        return int(str(item.get("year") or "").strip()[:4])
    except (TypeError, ValueError):
        return None


def _sorted_results(items, mode, engine=None):
    """Return results in a stable, deterministic display order."""
    indexed = list(enumerate(items))

    if _is_soulseek_engine(engine) and mode in (SORT_SHORTEST, SORT_LONGEST):
        largest = mode == SORT_LONGEST

        def soulseek_size_key(pair):
            size = int(pair[1].get("size_bytes") or 0)
            return (
                size == 0,
                -size if largest else size,
                str(pair[1].get("title") or "").casefold(),
                pair[0],
            )

        return [item for _index, item in sorted(indexed, key=soulseek_size_key)]

    if _is_torrent_engine(engine) and mode in (SORT_SHORTEST, SORT_LONGEST):
        # Nothing here has a duration; the swarm is what ranks two torrents.
        most = mode == SORT_LONGEST

        def torrent_seed_key(pair):
            seeders = int(pair[1].get("seeders") or 0)
            return (
                -seeders if most else seeders,
                pair[1].get("_search_order", pair[0]),
                pair[0],
            )

        return [item for _index, item in sorted(indexed, key=torrent_seed_key)]

    if _is_torrent_engine(engine) and mode in (SORT_OLDEST, SORT_NEWEST):
        # Every indexer states a posting date except the two that scrape a
        # page and read the age out as words. Those sort last either way: an
        # unknown date is neither the newest nor the oldest.
        newest = mode == SORT_NEWEST

        def torrent_date_key(pair):
            posted = int(pair[1].get("posted") or 0)
            return (
                posted == 0,
                -posted if newest else posted,
                pair[1].get("_search_order", pair[0]),
                pair[0],
            )

        return [item for _index, item in sorted(indexed, key=torrent_date_key)]

    if (_is_book_engine(engine) or _is_archive_engine(engine)) and mode in (
        SORT_SHORTEST,
        SORT_LONGEST,
    ):
        # These results carry a year rather than a duration, so the two
        # duration slots sort by when the work was published.
        newest = mode == SORT_LONGEST

        def publication_year_key(pair):
            year = _year(pair[1])
            return (
                year is None,
                -(year or 0) if newest else (year or 0),
                str(pair[1].get("title", "")).casefold(),
                pair[0],
            )

        return [item for _index, item in sorted(indexed, key=publication_year_key)]

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
        return [
            item
            for index, item in sorted(
                indexed,
                key=lambda pair: pair[1].get("_search_order", pair[0]),
            )
        ]
    if mode == SORT_NAME:

        def name_sort_key(pair):
            return text(pair[1], "title"), pair[0]

        sort_key = name_sort_key
    elif mode == SORT_SITE:

        def site_sort_key(pair):
            return (
                text(pair[1], "source") or "youtube",
                text(pair[1], "title"),
                pair[0],
            )

        sort_key = site_sort_key
    elif mode == SORT_ARTIST:

        def artist_sort_key(pair):
            return (
                text(pair[1], "artist", "uploader"),
                text(pair[1], "title"),
                pair[0],
            )

        sort_key = artist_sort_key
    elif mode == SORT_SHORTEST:

        def shortest_sort_key(pair):
            return (
                duration(pair[1]) is None,
                duration(pair[1]) or 0,
                text(pair[1], "title"),
                pair[0],
            )

        sort_key = shortest_sort_key
    elif mode == SORT_LONGEST:

        def longest_sort_key(pair):
            return (
                duration(pair[1]) is None,
                -(duration(pair[1]) or 0),
                text(pair[1], "title"),
                pair[0],
            )

        sort_key = longest_sort_key
    else:
        return list(items)
    return [item for _index, item in sorted(indexed, key=sort_key)]


class _ResultsList(wx.ListCtrl):
    """The results list, drawn on demand instead of built row by row.

    An all-sites music search asks 57 sources for a page each, so the list
    routinely holds thousands of rows. Filling those rows one SetItem call at
    a time cost seconds per redraw, and because every site that answers
    triggers another redraw of the whole list, a single search spent minutes
    of GUI-thread time rebuilding rows nobody was looking at. The app was
    unresponsive throughout, which for a screen reader means the results are
    unreadable while they arrive.

    A virtual list keeps the rows in ``self.results`` and asks for text only
    for the handful of rows actually on screen, so a redraw costs the same
    whether the search found ten results or ten thousand.
    """

    def __init__(self, *args, **kwargs):
        kwargs["style"] = kwargs.get("style", 0) | wx.LC_REPORT | wx.LC_VIRTUAL
        super().__init__(*args, **kwargs)
        # Set by the panel once it can answer for a cell.
        self.cell_provider = None

    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        try:
            return self.cell_provider(item, column)
        except Exception:  # noqa: BLE001 - a redraw must never raise at the user
            return ""


class SearchPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.results = []
        self._result_index = {}
        self.result_engine = 0
        self.token = None  # identifies the current search
        self.stop = None  # set to silence a superseded search's late sites
        self.shown_sources = set()
        self.asked = []  # sites this search went out to
        self.done = False  # True once the current search hit its deadline
        self._soulseek_streaming = False  # a Soulseek search is running now
        self.started_at = 0.0
        self.closing = False
        self.next_result_order = 0
        self.current_order = search_order.normalize(
            self.frame.config.get("search_order", ORDER_RELEVANCE)
        )
        self.current_kind = search_kind.normalize(
            self.frame.config.get("search_kind", KIND_BEST)
        )
        self.order_unable = []
        self.order_source_count = 0
        self.kind_used = KIND_BEST
        self.kind_able = []
        self.kind_unable = []
        self.preview_token = None
        self.archive_token = None
        self.album_token = None
        # Refreshes the status bar while slow sites are still working.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        # Many providers finish together. Coalesce their GUI work so the
        # native list and accessibility tree are rebuilt at most ten times a
        # second instead of once per provider.
        self.render_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._flush_results, self.render_timer)
        # Both timers are owned by this panel, and wxGTK deletes a window some
        # time after Destroy() rather than at once. A tick that lands in that
        # gap runs against a window that is already gone, which segfaults
        # instead of raising -- so stop them the moment the panel goes away.
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        sizer = wx.BoxSizer(wx.VERTICAL)

        query_label = wx.StaticText(self, label="&Search:")
        self.query_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.query_text.SetName("Search query")
        self.query_text.Bind(wx.EVT_TEXT_ENTER, self.on_search)

        engine_label = wx.StaticText(self, label="S&ource:")
        self.engine_choice = wx.Choice(self, choices=self._visible_engine_labels())
        self.engine_choice.SetName("Search source")
        self.engine_choice.SetHelpText(
            "Each choice searches only the named service or provider group. "
            "Soulseek file types have their own choices when enabled."
        )
        self.engine_choice.SetSelection(0)
        self.engine_choice.Bind(wx.EVT_CHOICE, self.on_engine_changed)

        kind_label = wx.StaticText(self, label="Search t&ype:")
        self.kind_choice = wx.Choice(self, choices=search_kind.KIND_LABEL_LIST)
        self.kind_choice.SetName("Search type")
        self.kind_choice.SetHelpText(
            "Best match searches everything. Track title and Artist match "
            "only that field. Album lists whole albums, and Enter on an "
            "album row downloads every track it contains. Choosing a type "
            "here takes effect on the next search."
        )
        self.kind_choice.SetSelection(search_kind.KINDS.index(self.current_kind))
        self.kind_choice.Bind(wx.EVT_CHOICE, self.on_kind_changed)

        order_label = wx.StaticText(self, label="&Order:")
        self.order_choice = wx.Choice(self, choices=search_order.ORDER_LABEL_LIST)
        self.order_choice.SetName("Search result order")
        self.order_choice.SetHelpText(
            "Chooses which results each site returns, so it takes effect on "
            "the next search. Sites that cannot honour the order are named "
            "afterwards."
        )
        self.order_choice.SetSelection(search_order.ORDERS.index(self.current_order))
        self.order_choice.Bind(wx.EVT_CHOICE, self.on_order_changed)

        sort_label = wx.StaticText(self, label="Sort &by:")
        self.sort_choice = wx.Choice(self, choices=SORT_LABELS)
        self.sort_choice.SetName("Sort search results")
        self.sort_choice.SetHelpText(
            "Rearranges the results already in the list, which happens as "
            "soon as it is chosen. It never re-runs the search."
        )
        self.sort_choice.SetSelection(SORT_RELEVANCE)
        self.sort_choice.Bind(wx.EVT_CHOICE, self.on_sort_changed)

        self.search_btn = wx.Button(self, label="&Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self.on_search)
        self.stop_btn = wx.Button(self, label="&Stop search")
        self.stop_btn.SetName("Stop search")
        self.stop_btn.SetHelpText(
            "Stops the current search. Results already found stay in the list."
        )
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop_search)
        self.stop_btn.Hide()

        # Every control on this row is a way of describing the search, so
        # Enter runs it from any of them, exactly as it does from the query
        # box. Nothing here searches merely because it was arrowed past.
        # The hook is needed because a native combo box swallows Return
        # before EVT_KEY_DOWN ever sees it.
        for control in (
            self.engine_choice,
            self.kind_choice,
            self.order_choice,
            self.sort_choice,
        ):
            control.Bind(wx.EVT_CHAR_HOOK, self.on_row_key)
            # An open list is being walked, and the Enter that ends the walk
            # belongs to the list, not to the search. See _dropdown_is_open.
            control.Bind(wx.EVT_COMBOBOX_DROPDOWN, self.on_dropdown_opened)
            control.Bind(wx.EVT_COMBOBOX_CLOSEUP, self.on_dropdown_closed)

        self.results_list = _ResultsList(self)
        self.results_list.cell_provider = self._result_cell
        self.results_list.SetName("Search results")
        self.results_list.SetHelpText(
            "Select one or more results. Enter downloads every selection; "
            "Control C copies URLs; "
            "Context Menu opens actions."
        )
        for i, heading in enumerate(COLUMN_HEADINGS):
            self.results_list.InsertColumn(i, heading)
        self.results_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_download_selected)
        self.results_list.Bind(wx.EVT_CONTEXT_MENU, self.on_results_menu)
        self.results_list.Bind(wx.EVT_CHAR, self.on_results_char)

        self.preview_btn = wx.Button(self, label="&Preview selected")
        self.preview_btn.SetHelpText(
            "Plays music as audio and video results with picture and sound."
        )
        self.preview_btn.Bind(wx.EVT_BUTTON, self.on_preview_selected)
        self.player = MediaPlayerPanel(self, frame, video_height=150)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(engine_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.engine_choice, 0, wx.RIGHT, 12)
        top.Add(kind_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.kind_choice, 0, wx.RIGHT, 12)
        top.Add(order_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.order_choice, 0, wx.RIGHT, 12)
        top.Add(sort_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        top.Add(self.sort_choice, 0, wx.RIGHT, 12)
        top.Add(self.search_btn, 0)
        top.Add(self.stop_btn, 0, wx.LEFT, 8)

        sizer.Add(query_label, 0, wx.ALL, 8)
        sizer.Add(self.query_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(top, 0, wx.ALL, 8)
        sizer.Add(self.results_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.preview_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)

    def focus_input(self):
        if self.closing:
            return
        self.query_text.SetFocus()

    def _visible_engine_labels(self):
        engines = list(GENERAL_ENGINES)
        if self.frame.config.get("soulseek_enabled"):
            engines.extend(SOULSEEK_ENGINE_KINDS)
        if self.frame.config["adult_sites_enabled"]:
            engines.extend(ADULT_ENGINE_CATEGORIES)
        self.visible_engines = engines
        return [ENGINE_LABELS[engine] for engine in engines]

    def _selected_engine(self):
        selection = self.engine_choice.GetSelection()
        if 0 <= selection < len(self.visible_engines):
            return self.visible_engines[selection]
        return ENGINE_MUSIC

    def refresh_engine_choices(self):
        """Show or hide adult categories after Settings changes."""
        engine = self._selected_engine()
        labels = self._visible_engine_labels()
        self.engine_choice.Clear()
        self.engine_choice.AppendItems(labels)
        if engine not in self.visible_engines:
            engine = ENGINE_MUSIC
        self.engine_choice.SetSelection(self.visible_engines.index(engine))
        self._apply_engine_controls(engine)

    def _selected_kind(self):
        """The search type this engine will actually be searched with.

        An engine with no album or artist to speak of is searched by best
        match whatever the control was last left on, which is also what the
        control itself shows once it has been switched off.
        """
        if self._selected_engine() not in KIND_ENGINES:
            return KIND_BEST
        selection = self.kind_choice.GetSelection()
        if 0 <= selection < len(search_kind.KINDS):
            return search_kind.KINDS[selection]
        return KIND_BEST

    def _apply_engine_controls(self, engine):
        """Name the sort choices and the preview button for one engine.

        Books have no duration to sort by and nothing to play, so the same
        controls are renamed rather than duplicated -- a screen reader then
        reads "Author" and "Newest first" instead of options that mean
        nothing for a book.
        """
        # The search type only means something for music. Switching it off
        # elsewhere keeps the page honest: a choice that cannot change the
        # answer should not be sitting there offering to.
        searchable = engine in KIND_ENGINES
        self.kind_choice.SetSelection(
            search_kind.KINDS.index(self.current_kind if searchable else KIND_BEST)
        )
        self.kind_choice.Enable(searchable)
        labels = _sort_labels(engine)
        if [
            self.sort_choice.GetString(index)
            for index in range(self.sort_choice.GetCount())
        ] != labels:
            selection = self.sort_choice.GetSelection()
            self.sort_choice.Clear()
            self.sort_choice.AppendItems(labels)
            self.sort_choice.SetSelection(
                selection if 0 <= selection < len(labels) else SORT_RELEVANCE
            )
        self.preview_btn.Enable(_plays(engine))

    def _apply_engine_columns(self, engine):
        for index, heading in enumerate(_column_headings(engine)):
            column = self.results_list.GetColumn(index)
            if column.GetText() != heading:
                column.SetText(heading)
                self.results_list.SetColumn(index, column)

    def on_engine_changed(self, event):
        engine = self._selected_engine()
        self._apply_engine_controls(engine)
        mode = _sort_for_order(engine, self.current_order)
        if mode < self.sort_choice.GetCount():
            self.sort_choice.SetSelection(mode)
        event.Skip()

    def on_dropdown_opened(self, event):
        setattr(event.GetEventObject(), "_blinddl_popup_open", True)
        event.Skip()

    def on_dropdown_closed(self, event):
        setattr(event.GetEventObject(), "_blinddl_popup_open", False)
        event.Skip()

    def on_row_key(self, event):
        """Run the search when Enter is pressed on one of the row's controls.

        Not while the list is open, though: that Enter is how the item being
        read is chosen, and answering it with a search sends the focus into
        the results before the choice has even been made. Walking a combo
        box open is exactly what a screen reader user does with one.
        """
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            # Skipped either way, so an open dropdown still closes and
            # commits the choice the Enter was meant to pick.
            event.Skip()
            if not _dropdown_is_open(event.GetEventObject()):
                self.on_search(None)
            return
        event.Skip()

    def _setting_changed(self, message):
        """Say what a search setting is now, and how to act on it.

        Choosing one of these used to search straight away. Arrowing through
        the choices then fired a search per step and threw the focus into
        the results at the end of each one, so the list could not be walked
        to the option wanted -- the whole point of a combo box. They now
        only describe the next search; Enter or the Search button runs it.
        """
        if self.query_text.GetValue().strip():
            message += " Press Enter to search."
        self.frame.announce(message)

    def on_kind_changed(self, event):
        """Save the search type. The next search is the one that uses it."""
        selection = self.kind_choice.GetSelection()
        if not 0 <= selection < len(search_kind.KINDS):
            selection = 0
            self.kind_choice.SetSelection(selection)
        self.current_kind = search_kind.KINDS[selection]
        self.frame.config["search_kind"] = self.current_kind
        save = getattr(self.frame.config, "save", None)
        if save is not None:
            save()
        self._setting_changed(
            f"Search type set to {search_kind.label(self.current_kind)}."
        )
        if event is not None:
            event.Skip()

    def on_order_changed(self, event):
        """Save the query order. The next search is the one that uses it."""
        selection = self.order_choice.GetSelection()
        if not 0 <= selection < len(search_order.ORDERS):
            selection = 0
            self.order_choice.SetSelection(selection)
        self.current_order = search_order.ORDERS[selection]
        self.frame.config["search_order"] = self.current_order
        save = getattr(self.frame.config, "save", None)
        if save is not None:
            save()

        engine = self._selected_engine()
        sort_mode = _sort_for_order(engine, self.current_order)
        if sort_mode < self.sort_choice.GetCount():
            self.sort_choice.SetSelection(sort_mode)
        self._setting_changed(
            f"Search order set to {search_order.label(self.current_order)}."
        )
        if event is not None:
            event.Skip()

    def shutdown(self):
        """Stop timers and silence worker callbacks before widgets are freed."""
        if self.closing:
            return
        self.closing = True
        if self.stop is not None:
            self.stop.set()
        self.timer.Stop()
        self.render_timer.Stop()
        self.player.shutdown()

    # -- search -----------------------------------------------------------

    def on_search(self, event):
        if self.closing:
            return
        query = self.query_text.GetValue().strip()
        if not query:
            self.frame.announce("Type a search first.")
            return
        engine = self._selected_engine()
        kind = self._selected_kind()
        # An album search asks for a different thing, not a differently
        # matched one, so only the sites that can return albums go out. The
        # rest would answer with tracks and bury the albums under them.
        albums_only = search_kind.is_album(kind)
        if engine == ENGINE_MUSIC:
            sources = musicdl_backend.enabled_sources(
                self.frame.config["disabled_music_sources"]
            )
            if not sources and not albums_only:
                self.frame.announce("No music sites selected. Use Tools, Search sites.")
                return
        elif engine == ENGINE_BOOKS:
            sources = book_backend.enabled_sources(
                self.frame.config["disabled_book_sources"]
            )
            if not sources:
                self.frame.announce(
                    "No book libraries selected. Use Tools, Search sites."
                )
                return
        elif engine == ENGINE_AUDIOBOOKS:
            sources = audiobook_backend.enabled_sources(
                self.frame.config["disabled_audiobook_sources"]
            )
            if not sources:
                self.frame.announce(
                    "No audiobook sites selected. Use Tools, Search sites."
                )
                return
        elif engine == ENGINE_TORRENTS:
            sources = torrent_backend.enabled_sources(
                self.frame.config["disabled_torrent_sources"], self.frame.config
            )
            if not sources:
                self.frame.announce(
                    "No torrent indexers selected. Use Tools, Search sites."
                )
                return
        elif _is_archive_engine(engine):
            sources = archive_backend.enabled_sources(
                self.frame.config["disabled_archive_sources"],
                ARCHIVE_ENGINE_CATEGORIES[engine],
            )
            if not sources:
                self.frame.announce(
                    "No Internet Archive collections selected. Use Tools, Search sites."
                )
                return
        elif _is_adult_engine(engine):
            if not self.frame.config["adult_sites_enabled"]:
                self.frame.announce(
                    "Adult sites are disabled. Enable them in Settings."
                )
                return
            sources = adult_backend.enabled_sources(
                self.frame.config["disabled_adult_sources"]
            )
            unavailable = adult_backend.unavailable_sources()
            sources = [source for source in sources if source not in unavailable]
            if not sources:
                self.frame.announce(
                    "Adult API packages are unavailable. Reinstall blindDL "
                    "to restore them."
                )
                return
        elif _is_soulseek_engine(engine):
            if not self.frame.config.get("soulseek_enabled"):
                self.frame.announce(
                    "Soulseek is disabled. Enable it in Settings, Soulseek."
                )
                return
            sources = []
        else:
            sources = []
        selection = self.order_choice.GetSelection()
        order = (
            search_order.ORDERS[selection]
            if 0 <= selection < len(search_order.ORDERS)
            else ORDER_RELEVANCE
        )
        self.current_order = order

        if engine == ENGINE_MUSIC:
            order_sources = list(sources) + [
                sideb_backend.SIDEB_SOURCE,
                deezer_backend._SEARCH_SOURCE,
            ]
        elif engine == ENGINE_YOUTUBE:
            order_sources = ["YouTube"]
        elif engine == ENGINE_SOUNDCLOUD:
            order_sources = ["SoundCloud"]
        elif engine == ENGINE_BANDCAMP:
            order_sources = ["Bandcamp"]
        elif engine == ENGINE_APPLE_MUSIC:
            order_sources = ["Apple Music"]
        elif engine == ENGINE_DEEZER:
            order_sources = [deezer_backend._SEARCH_SOURCE]
        elif _is_soulseek_engine(engine):
            order_sources = [soulseek_backend.SOURCE]
        else:
            order_sources = list(sources)
        _able, unable = _order_capable_sources(
            engine, order_sources, order, self.frame.config, kind
        )
        kind_able, kind_unable = _kind_capable_sources(engine, order_sources, kind)
        if engine == ENGINE_MUSIC:
            unable = [_music_source_label(source) for source in unable]
            kind_able = [_music_source_label(source) for source in kind_able]
            kind_unable = [_music_source_label(source) for source in kind_unable]
        self.order_unable = unable
        self.order_source_count = len(order_sources)
        self.kind_used = kind
        self.kind_able = kind_able
        self.kind_unable = kind_unable
        self.search_btn.Disable()

        # Everything below is tagged with this token, so results still
        # trickling in from a previous search are ignored.
        self.token = object()
        if self.stop is not None:
            self.stop.set()
        self.stop = threading.Event()
        self.results = []
        self._result_index = {}
        self.result_engine = engine
        self.shown_sources = set()
        self.asked = []
        self.done = False
        self.started_at = time.time()
        self.next_result_order = 0
        self.timer.Stop()
        self.render_timer.Stop()
        # DeleteAllItems on a virtual list clears the rows without telling it
        # the count changed, which leaves the old count behind.
        self.results_list.SetItemCount(0)
        self._apply_engine_columns(engine)

        # A Soulseek search never times out: it keeps finding peers until the
        # user starts another search or presses Stop. Every search shows the
        # Stop button, so a slow source can be cut off whatever the category.
        self._soulseek_streaming = _is_soulseek_engine(engine)
        if self._soulseek_streaming:
            self.asked = [soulseek_backend.SOURCE]
        self.stop_btn.Show()

        if _is_soulseek_engine(engine):
            self.frame.announce(
                f"Searching {ENGINE_LABELS[engine]}. Results arrive as they come."
            )
        elif engine == ENGINE_MUSIC and albums_only:
            self.frame.announce(
                f"Searching {deezer_backend._SEARCH_SOURCE} for albums "
                f"({self.frame.config['search_timeout_s']:g} seconds)..."
            )
        elif engine == ENGINE_MUSIC:
            # Side B's Deezer catalog search goes out next to the musicdl
            # sites and reports through the same per-site callback.
            count = len(sources) + 2
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} music {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        elif engine == ENGINE_BOOKS:
            count = len(sources)
            library_word = "library" if count == 1 else "libraries"
            self.frame.announce(
                f"Searching {count} book {library_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        elif engine == ENGINE_AUDIOBOOKS:
            count = len(sources)
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} audiobook {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        elif engine == ENGINE_TORRENTS:
            count = len(sources)
            word = "indexer" if count == 1 else "indexers"
            self.frame.announce(
                f"Searching {count} torrent {word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        elif _is_archive_engine(engine):
            count = len(sources)
            word = "collection" if count == 1 else "collections"
            self.frame.announce(
                f"Searching {count} Internet Archive {word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        elif _is_adult_engine(engine):
            count = len(sources)
            site_word = "site" if count == 1 else "sites"
            self.frame.announce(
                f"Searching {count} {ENGINE_LABELS[engine]} {site_word} "
                f"({self.frame.config['search_timeout_s']:g} seconds each)..."
            )
        else:
            self.frame.announce(f"Searching {ENGINE_LABELS[engine]}...")
        threading.Thread(
            target=self._search,
            args=(query, engine, self.token, self.stop, sources, order, kind),
            daemon=True,
        ).start()

    def _search(self, query, engine, token, stop, sources, order=None, kind=KIND_BEST):
        order = search_order.normalize(order or self.current_order)
        kind = search_kind.normalize(kind)
        asked = []
        try:
            if _is_soulseek_engine(engine):
                def on_soulseek_batch(batch):
                    wx.CallAfter(self._add_soulseek_batch, token, batch)

                items = soulseek_backend.search(
                    query,
                    self.frame.config,
                    _soulseek_media_kind(engine),
                    self.frame.config["search_timeout_s"],
                    stop_event=stop,
                    on_batch=on_soulseek_batch,
                )
                asked = [soulseek_backend.SOURCE]
            elif engine == ENGINE_MUSIC and search_kind.is_album(kind):
                # Deezer is the only one of the music sources with an album
                # catalogue to search. The musicdl sites and Side B match
                # song titles, so asking them here would answer an album
                # search with several hundred tracks.
                threading.Thread(
                    target=self._deezer_search,
                    args=(query, token, engine, stop, order, kind),
                    daemon=True,
                    name="search-deezer",
                ).start()
                asked = [deezer_backend._SEARCH_SOURCE]
                items = []
            elif engine == ENGINE_MUSIC:

                def on_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                threading.Thread(
                    target=self._sideb_search,
                    args=(query, token, engine, stop, order),
                    daemon=True,
                    name="search-sideb",
                ).start()
                threading.Thread(
                    target=self._deezer_search,
                    args=(query, token, engine, stop, order, kind),
                    daemon=True,
                    name="search-deezer",
                ).start()
                items, _answered, asked = musicdl_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_site,
                    stop=stop,
                    sources=sources,
                    order=order,
                )
                asked.append(sideb_backend.SIDEB_SOURCE)
                asked.append(deezer_backend._SEARCH_SOURCE)
                # on_site already delivered these; nothing left to hand over.
                items = []
            elif engine == ENGINE_BOOKS:

                def on_library(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = book_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_library,
                    stop=stop,
                    sources=sources,
                    order=order,
                )
                # on_library already delivered these.
                items = []
            elif engine == ENGINE_AUDIOBOOKS:

                def on_audiobook_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = audiobook_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_audiobook_site,
                    stop=stop,
                    sources=sources,
                    order=order,
                )
                # on_audiobook_site already delivered these.
                items = []
            elif engine == ENGINE_TORRENTS:

                def on_indexer(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = torrent_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_indexer,
                    stop=stop,
                    sources=sources,
                    config=self.frame.config,
                    order=order,
                )
                # on_indexer already delivered these.
                items = []
            elif _is_archive_engine(engine):

                def on_collection(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = archive_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_collection,
                    stop=stop,
                    sources=sources,
                    order=order,
                )
                # on_collection already delivered these.
                items = []
            elif engine == ENGINE_SOUNDCLOUD:
                items, _title = ytdlp_backend.extract_flat(
                    f"scsearch200:{query}", order=order
                )
            elif engine == ENGINE_BANDCAMP:
                items = bandcamp_backend.search(query, self.frame.config, order=order)
            elif engine == ENGINE_APPLE_MUSIC:
                items = applemusic_backend.search(
                    query, self.frame.config, order=order, kind=kind
                )
            elif engine == ENGINE_DEEZER:
                items = deezer_backend.search(
                    query, self.frame.config, order=order, kind=kind
                )
            elif _is_adult_engine(engine):

                def on_adult_site(source, items):
                    wx.CallAfter(self._add_site, token, engine, source, items)

                items, _answered, asked = adult_backend.search(
                    query,
                    self.frame.config["search_timeout_s"],
                    on_site=on_adult_site,
                    stop=stop,
                    sources=sources,
                    category=ADULT_ENGINE_CATEGORIES[engine],
                    order=order,
                )
                # on_adult_site already delivered these.
                items = []
            else:
                items = ytdlp_backend.search(query, order=order)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            if _is_soulseek_engine(engine):
                wx.CallAfter(self._soulseek_failed, token, str(exc))
            else:
                wx.CallAfter(self._search_failed, token, str(exc))
            return
        if not stop.is_set():
            wx.CallAfter(self._search_done, token, items, engine, asked)

    def _sideb_search(self, query, token, engine, stop, order=ORDER_RELEVANCE):
        try:
            items = sideb_backend.search(query, self.frame.config, order=order)
        except Exception:  # noqa: BLE001 - one failing site must not kill the rest
            items = []
        if stop.is_set():
            return
        wx.CallAfter(self._add_site, token, engine, sideb_backend.SIDEB_SOURCE, items)

    def _deezer_search(self, query, token, engine, stop, order=ORDER_RELEVANCE,
                       kind=KIND_BEST):
        try:
            items = deezer_backend.search(
                query, self.frame.config, order=order, kind=kind
            )
        except Exception:  # noqa: BLE001 - one failing site must not kill the rest
            items = []
        if stop.is_set():
            return
        wx.CallAfter(
            self._add_site, token, engine, deezer_backend._SEARCH_SOURCE, items
        )

    def _soulseek_failed(self, token, error):
        if self.closing or token is not self.token:
            return
        self.search_btn.Enable()
        self.done = True
        self._soulseek_streaming = False
        self.stop_btn.Hide()
        self.frame.announce(f"Soulseek unavailable: {error}")

    def _search_failed(self, token, error):
        if self.closing or token is not self.token:
            return
        self.search_btn.Enable()
        self.stop_btn.Hide()
        self.frame.announce("Search failed.")
        wx.MessageBox(
            f"Search failed:\n{error}", "blindDL", wx.OK | wx.ICON_ERROR, self
        )

    def _add_site(self, token, engine, source, items):
        """Append one site's results. Runs on the GUI thread."""
        if self.closing or token is not self.token or source in self.shown_sources:
            return
        self.shown_sources.add(source)
        if not items:
            return
        changed = False
        for item in items:
            changed = self._insert_deduped(item) or changed
        if not changed:
            return
        self.render_timer.StartOnce(100)
        if self.done:
            # A late site reports once, not once for every result it returned.
            self.frame.announce(
                f"{self._result_count()}, latest from {source}. "
                f"{self._pending_phrase()}"
            )
            if not self._pending():
                self.timer.Stop()

    def _add_soulseek_batch(self, token, batch):
        """Fold one streaming Soulseek batch into the list. Runs on the GUI thread.

        Rows are inserted silently: the render timer coalesces the work, and
        announcing each batch would flood the screen reader while the search
        is meant to keep running.
        """
        if self.closing or token is not self.token:
            return
        self.shown_sources.add(soulseek_backend.SOURCE)
        changed = False
        for item in batch:
            changed = self._insert_deduped(item) or changed
        if changed:
            self.render_timer.StartOnce(100)

    def on_stop_search(self, event=None):
        """Stop the current search without starting a new one."""
        if self.stop is not None:
            self.stop.set()
        # Ignore results still in flight from the stopped search.
        self.token = None
        self.done = True
        self._soulseek_streaming = False
        self.stop_btn.Hide()
        self.search_btn.Enable()
        self.timer.Stop()
        self.frame.announce("Search stopped.")

    def _on_destroy(self, event):
        """Silence anything still due to run against this panel.

        wxGTK and wxOSX delete a window some time after Destroy() returns, so
        a timer tick or a queued CallAfter can land on a half-deleted control.
        On those platforms that is a segfault rather than an exception, so
        nothing may be allowed to run past this point.
        """
        if event.GetEventObject() is self:
            self.closing = True
            self.render_timer.Stop()
            self.timer.Stop()
        event.Skip()

    def _flush_results(self, event=None):
        if self.closing:
            return
        self.render_timer.Stop()
        selected = self._selected_result_objects()
        focused = self._focused_result_object()
        self.results = _sorted_results(
            self.results, self.sort_choice.GetSelection(), self.result_engine
        )
        self._result_index = {
            self._dedup_key(item): index for index, item in enumerate(self.results)
        }
        self._render_results(
            self.result_engine, selected=selected, focused=focused
        )

    @staticmethod
    def _dedup_key(item):
        """Normalised artist + title for deduplication."""
        if item.get("kind") == "soulseek":
            # Peer availability is essential on Soulseek: two copies of the
            # same song are distinct choices with different queues and speeds.
            return "soulseek\x00{}\x00{}".format(
                str(item.get("username") or "").casefold(),
                str(item.get("remote_path") or "").casefold(),
            )
        title = str(item.get("title") or "").strip().lower()
        artist = str(item.get("artist") or "").strip().lower()
        # Remove punctuation and extra whitespace for fuzzy matching.
        title = re.sub(r"[^\w\s]", "", title)
        artist = re.sub(r"[^\w\s]", "", artist)
        title = " ".join(title.split())
        artist = " ".join(artist.split())
        return f"{artist}\x00{title}"

    @staticmethod
    def _item_quality(item):
        """Heuristic quality score for dedup: higher is better."""
        fmt = str(item.get("format", "") or "").upper()
        if fmt in ("FLAC", "WAV", "AIFF", "ALAC"):
            score = 100
        elif fmt in ("MP3", "M4A", "AAC", "OGG", "OPUS"):
            # Higher bitrate hints come from file_size.
            score = 50
        else:
            score = 20
        # Large files suggest higher quality.
        size_str = str(item.get("file_size", "") or "")
        if "MB" in size_str.upper():
            try:
                score += int(float(size_str.upper().replace("MB", "").strip()))
            except ValueError:
                pass
        # Known high-quality sources get a bonus.
        source = str(item.get("source", "") or "")
        if source in ("Deezer", "Qobuz", "TIDAL", "Apple Music"):
            score += 30
        elif source == "Deezer (Side B)":
            score += 20
        return score

    def _insert_deduped(self, item):
        """Insert *item*, replacing a duplicate if this one is higher quality."""
        key = self._dedup_key(item)
        if not key:
            return False
        # Tests and integrations occasionally seed ``results`` directly.
        # Rebuild once when that happens; ordinary provider inserts keep this
        # index current in constant time.
        if len(self._result_index) != len(self.results):
            self._result_index = {
                self._dedup_key(existing): index
                for index, existing in enumerate(self.results)
            }
        existing_index = self._result_index.get(key)
        if existing_index is not None:
            existing = self.results[existing_index]
            if self._item_quality(item) > self._item_quality(existing):
                # Replace the lower-quality entry without changing its
                # provider arrival order.
                item["_search_order"] = existing.get("_search_order", 0)
                self.results[existing_index] = item
                return True
            return False
        item["_search_order"] = self.next_result_order
        self.next_result_order += 1
        self._result_index[key] = len(self.results)
        self.results.append(item)
        return True

    def _result_cell(self, row, column):
        """One cell of the results list, asked for as it is drawn.

        wx calls this for visible rows only, so it has to stay cheap and it
        has to tolerate being called while a search is still replacing the
        rows underneath it.
        """
        if not (0 <= row < len(self.results)):
            return ""
        item = self.results[row]
        engine = self.result_engine
        if column == 0:
            return str(item.get("title") or "")
        if column == 1:
            return _result_type(item)
        if _is_soulseek_engine(engine):
            return _pick(column, item, "username", "folder", "availability", "file_size")
        if _is_book_engine(engine):
            if column == 4:
                return str(item.get("year") or "")
            return _pick(column, item, "author", "source", None, "file_size")
        if engine == ENGINE_AUDIOBOOKS:
            if column == 2:
                author = item.get("author", "")
                narrator = item.get("narrator", "")
                if narrator and narrator != author:
                    return (
                        f"{author}, read by {narrator}"
                        if author
                        else f"read by {narrator}"
                    )
                return author
            if column == 4:
                return ytdlp_backend.format_duration(item.get("duration_s"))
            if column == 5:
                chapters = item.get("chapters") or 0
                return str(chapters) if chapters else ""
            return _pick(column, item, None, "source", None, None)
        if _is_torrent_engine(engine):
            if column == 2:
                # Both halves of the swarm in one column: seeders alone say how
                # fast it will go, leechers say whether anyone still wants it.
                seeders = item.get("seeders") or 0
                leechers = item.get("leechers") or 0
                return f"{seeders} seeding, {leechers} leeching"
            return _pick(column, item, None, "source", "age", "file_size")
        if _is_archive_engine(engine):
            if column == 4:
                return str(item.get("year") or "")
            return _pick(column, item, "creator", "source", None, "file_size")
        if engine != ENGINE_YOUTUBE:
            if column == 4:
                return ytdlp_backend.format_duration(item.get("duration_s"))
            return _pick(column, item, "artist", "source", None, "file_size")
        if column == 3:
            return "YouTube"
        if column == 4:
            return ytdlp_backend.format_duration(item.get("duration"))
        return _pick(column, item, "uploader", None, None, None)

    def _selected_result_objects(self):
        return [
            self.results[index]
            for index in self._selected_indices()
            if index < len(self.results)
        ]

    def _focused_result_object(self):
        index = self.results_list.GetFocusedItem()
        return self.results[index] if 0 <= index < len(self.results) else None

    def _render_results(self, engine, selected=(), focused=None):
        """Show the current results, keeping the user where they were.

        The rows themselves cost nothing to publish -- the list asks for the
        text of the few it draws -- so the work here is restoring the
        selection and the focused row, which are the things a search arriving
        underneath the user would otherwise take away.
        """
        selected_ids = {id(item) for item in selected}
        self._apply_engine_columns(engine)
        self.results_list.Freeze()
        try:
            # A virtual list keeps selection by row number, so rows that moved
            # under a re-sort would stay selected at their old positions.
            for index in self._selected_indices():
                self.results_list.Select(index, False)
            self.results_list.SetItemCount(len(self.results))
            # Walking every row costs nothing to look at but is still a walk,
            # and during a live search there is usually nothing to put back.
            if selected_ids or focused is not None:
                for row, item in enumerate(self.results):
                    if id(item) in selected_ids:
                        self.results_list.Select(row)
                    if item is focused:
                        self.results_list.Focus(row)
            self.results_list.Refresh()
        finally:
            self.results_list.Thaw()

    def on_sort_changed(self, event):
        self.render_timer.Stop()
        selected = self._selected_result_objects()
        focused = self._focused_result_object()
        mode = self.sort_choice.GetSelection()
        self.results = _sorted_results(self.results, mode, self.result_engine)
        self._result_index = {
            self._dedup_key(item): index for index, item in enumerate(self.results)
        }
        self._render_results(self.result_engine, selected=selected, focused=focused)
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
        return f"Still searching {len(pending)} {site_word} after {waited}s: {names}."

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
                f"{self._result_count()} so far. {self._pending_phrase()}"
            )

    def _search_done(self, token, items, engine, asked=()):
        if self.closing or token is not self.token:
            return
        self.search_btn.Enable()
        self.stop_btn.Hide()
        # yt-dlp hands back everything at once; music results arrived per site.
        source = soulseek_backend.SOURCE if _is_soulseek_engine(engine) else ""
        self._add_site(token, engine, source, items)
        self.asked = list(asked)
        self.done = True
        self._flush_results()
        pending = self._pending()
        order_phrase = _order_phrase(
            self.current_order, self.order_unable, self.order_source_count
        )
        kind_phrase = _kind_phrase(
            self.kind_used, self.kind_able, self.kind_unable
        )
        if pending:
            # Deezer and friends can run for minutes; never call that "found
            # nothing" when the sites are still going.
            message = f"{self._result_count()} so far. {self._pending_phrase()}"
        else:
            message = f"{self._result_count()} found."
        for phrase in (kind_phrase, order_phrase):
            if phrase:
                message += f" {phrase}"
        self.frame.announce(message)
        if pending:
            self.timer.Start(10000)
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
                item["identifier"], video=bool(item.get("video"))
            )
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._archive_files_failed, token, str(exc))
            return
        wx.CallAfter(self._archive_files_ready, token, item, files)

    def _archive_files_failed(self, token, error):
        if self.closing or token is not self.archive_token:
            return
        self.frame.announce("Could not read that item's file list.")
        wx.MessageBox(
            f"Could not read that item:\n{error}",
            "blindDL",
            wx.OK | wx.ICON_ERROR,
            self,
        )

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
        with self.frame.queue.batch_additions():
            added = []
            for entry in chosen:
                payload = dict(entry)
                payload["collection_title"] = item["title"]
                added.append(
                    self.frame.queue.add_archive(payload, entry["title"])
                )
        self.frame.announce(
            addition_summary(added, [entry["title"] for entry in chosen])
        )

    # -- whole albums --------------------------------------------------------

    def _queue_album_items(self, albums):
        """Read each album's track list, then queue or offer a choice."""
        token = self.album_token = object()
        if len(albums) == 1:
            self.frame.announce(f"Reading track list: {albums[0]['title']}")
        else:
            self.frame.announce(f"Reading {len(albums)} album track lists...")
        threading.Thread(
            target=self._resolve_album_tracks,
            args=(token, list(albums)),
            daemon=True,
            name="blinddl-album-tracks",
        ).start()

    def _resolve_album_tracks(self, token, albums):
        resolved = []
        errors = []
        for album in albums:
            try:
                backend = (
                    applemusic_backend
                    if album.get("kind") == "applemusic_album"
                    else deezer_backend
                )
                tracks, _title = backend.extract_flat(
                    album["url"], self.frame.config
                )
            except Exception as exc:  # noqa: BLE001 - reported to the user
                errors.append(f"{album['title']}: {exc}")
                continue
            if tracks:
                resolved.append((album, tracks))
            else:
                errors.append(f"{album['title']}: no tracks listed")
        wx.CallAfter(self._album_tracks_ready, token, resolved, errors)

    def _album_tracks_ready(self, token, resolved, errors):
        if self.closing or token is not self.album_token:
            return
        if not resolved:
            self.frame.announce("Could not read that album.")
            if errors:
                wx.MessageBox(
                    "Could not read that album:\n" + "\n".join(errors),
                    "blindDL",
                    wx.OK | wx.ICON_ERROR,
                    self,
                )
            return
        if len(resolved) == 1 and len(resolved[0][1]) > 1:
            # One album is a list worth reading before it fills the queue,
            # the same way one Archive item is.
            album, tracks = resolved[0]
            dialog = ItemPickerDialog(self, tracks, album["title"])
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
            resolved = [(album, chosen)]
        added = []
        titles = []
        with self.frame.queue.batch_additions():
            for album, tracks in resolved:
                apple = album.get("kind") == "applemusic_album"
                # An album is a release, so its tracks go in a folder of its
                # own -- named for the artist as well when the row knows one,
                # since two artists can put out the same album title.
                folder = _album_folder(album)
                for track in tracks:
                    title = track.get("title") or album["title"]
                    titles.append(title)
                    if apple:
                        added.append(
                            self.frame.queue.add_applemusic(
                                track["url"], title, folder=folder)
                        )
                    else:
                        added.append(
                            self.frame.queue.add_sideb(
                                track["url"], title, folder=folder)
                        )
        message = addition_summary(added, titles)
        if errors:
            failed = "album" if len(errors) == 1 else "albums"
            message += f" {len(errors)} {failed} could not be read."
        self.frame.announce(message)

    def on_results_char(self, event):
        if event.GetKeyCode() == 3 and event.ControlDown():  # Ctrl+C
            self.on_copy_url(event)
            return
        if event.GetKeyCode() == ord("O") and event.ControlDown():  # Ctrl+O
            self.on_open_browser(event)
            return
        event.Skip()

    def on_copy_url(self, event):
        indices = [
            index for index in self._selected_indices() if index < len(self.results)
        ]
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
        self._try_copy_urls(urls, missing)

    def _try_copy_urls(self, urls, missing, attempt=0):
        """Retry a busy Windows clipboard without blocking the GUI thread."""
        if self.closing:
            return
        silence = wx.LogNull()
        try:
            opened = wx.TheClipboard.Open()
        finally:
            del silence
        copied = False
        if opened:
            try:
                set_ok = bool(
                    wx.TheClipboard.SetData(wx.TextDataObject("\n".join(urls)))
                )
                if set_ok:
                    # Keep the URL on the clipboard after blindDL exits.
                    copied = bool(wx.TheClipboard.Flush())
            finally:
                wx.TheClipboard.Close()
        if copied:
            noun = "URL" if len(urls) == 1 else "URLs"
            message = f"Copied {len(urls)} {noun}."
            if missing:
                message += f" {missing} had no URL."
            self.frame.announce(message)
            return
        if attempt < 19:
            wx.CallLater(25, self._try_copy_urls, urls, missing, attempt + 1)
        else:
            self.frame.announce(
                "The clipboard is busy. Wait a moment and press Control+C again."
            )

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
            self.results_list.ScreenToClient(position)
        )
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
        focused = self._focused_result_object()
        soulseek_item = (
            focused if focused and focused.get("kind") == "soulseek" else None
        )
        download_folder = menu.Append(wx.ID_ANY, "Download containing &folder")
        browse_user = menu.Append(wx.ID_ANY, "&Browse user's files")
        send_message = menu.Append(wx.ID_ANY, "Send user a &message")
        add_friend = menu.Append(wx.ID_ANY, "Add user to &friends")
        free_slot = menu.Append(wx.ID_ANY, "Give user a free &slot")
        view_profile = menu.Append(wx.ID_ANY, "View user &profile")
        for action in (
            download_folder,
            browse_user,
            send_message,
            add_friend,
            free_slot,
            view_profile,
        ):
            action.Enable(soulseek_item is not None)
        copy_url = menu.Append(wx.ID_ANY, "Copy &URL\tCtrl+C")
        open_browser = menu.Append(wx.ID_ANY, "&Open in browser\tCtrl+O")
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_ANY, "Select &all")
        clear = menu.Append(wx.ID_ANY, "&Clear selection")
        has_selection = bool(self._selected_indices())
        preview_item.Enable(has_selection and _plays(self.result_engine))
        download.Enable(has_selection)
        copy_url.Enable(has_selection and soulseek_item is None)
        open_browser.Enable(
            has_selection
            and self.result_engine
            in (
                ENGINE_MUSIC,
                ENGINE_DEEZER,
                ENGINE_YOUTUBE,
                ENGINE_SOUNDCLOUD,
                ENGINE_TORRENTS,
            )
        )
        clear.Enable(has_selection)
        select_all.Enable(
            self.results_list.GetSelectedItemCount() < self.results_list.GetItemCount()
        )
        menu.Bind(wx.EVT_MENU, self.on_preview_selected, preview_item)
        menu.Bind(wx.EVT_MENU, self.on_download_selected, download)
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self._download_soulseek_folder(soulseek_item),
            download_folder,
        )
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self.frame.open_soulseek_user(
                soulseek_item.get("username", "") if soulseek_item else ""
            ),
            browse_user,
        )
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self.frame.message_soulseek_user(
                soulseek_item.get("username", "") if soulseek_item else ""
            ),
            send_message,
        )
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self.frame.add_soulseek_friend(
                soulseek_item.get("username", "") if soulseek_item else ""
            ),
            add_friend,
        )
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self.frame.give_soulseek_free_slot(
                soulseek_item.get("username", "") if soulseek_item else ""
            ),
            free_slot,
        )
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self.frame.view_soulseek_profile(
                soulseek_item.get("username", "") if soulseek_item else ""
            ),
            view_profile,
        )
        menu.Bind(wx.EVT_MENU, self.on_copy_url, copy_url)
        menu.Bind(wx.EVT_MENU, self.on_open_browser, open_browser)
        menu.Bind(wx.EVT_MENU, self._select_all, select_all)
        menu.Bind(wx.EVT_MENU, self._clear_selection, clear)
        self.results_list.PopupMenu(menu)
        menu.Destroy()

    def _download_soulseek_folder(self, item):
        if not item:
            return
        username = item.get("username", "")
        folder = item.get("folder", "")
        if not username or not folder:
            self.frame.announce("That Soulseek result has no containing folder.")
            return
        self.frame.announce(f"Loading folder {folder} from {username}...")

        def worker():
            try:
                directories = soulseek_backend.browse_user(
                    username, self.frame.config
                )
                prefix = folder.rstrip("\\") + "\\"
                files = [
                    file_item
                    for directory in directories
                    if directory["name"].casefold() == folder.casefold()
                    or directory["name"].casefold().startswith(prefix.casefold())
                    for file_item in directory["files"]
                    if not file_item.get("locked")
                ]
            except Exception as exc:  # noqa: BLE001 - shown in the GUI
                wx.CallAfter(
                    self.frame.announce,
                    f"Could not load Soulseek folder: {exc}",
                )
                return
            wx.CallAfter(self._queue_soulseek_folder, folder, files)

        threading.Thread(
            target=worker,
            daemon=True,
            name="blinddl-soulseek-folder-download",
        ).start()

    def _queue_soulseek_folder(self, folder, files):
        with self.frame.queue.batch_additions():
            for original in files:
                item = dict(original)
                relative = ntpath.relpath(item["remote_path"], folder)
                item["target_relative_path"] = ntpath.join(
                    ntpath.basename(folder.rstrip("\\")), relative
                )
                self.frame.queue.add_soulseek(item, item["title"])
        if files:
            self.frame.announce(f"Queued {len(files)} files from {folder}.")
        else:
            self.frame.announce("That Soulseek folder contains no downloadable files.")

    def on_open_browser(self, event):
        import webbrowser

        for index in self._selected_indices():
            if index >= len(self.results):
                continue
            url = preview.result_url(self.results[index])
            if url:
                webbrowser.open(url)
        count = len(self._selected_indices())
        noun = "link" if count == 1 else "links"
        self.frame.announce(f"Opened {count} {noun} in browser.")

    def on_preview_selected(self, event):
        if _is_book_engine(self.result_engine):
            self.frame.announce(
                "Books cannot be previewed. Press Enter to download, then "
                "open it from the Library tab."
            )
            return
        if _is_torrent_engine(self.result_engine):
            self.frame.announce(
                "Torrents cannot be previewed. Press Enter to open the "
                "magnet link in your torrent client."
            )
            return
        indices = [
            index for index in self._selected_indices() if index < len(self.results)
        ]
        if not indices:
            self.frame.announce("Select a result to preview first.")
            return
        index = self.results_list.GetFocusedItem()
        if index not in indices:
            index = indices[0]
        item = self.results[index]
        if item.get("kind") == "soulseek":
            self.frame.announce(
                "Soulseek files cannot be previewed before downloading. "
                "Press Enter to download this file."
            )
            return
        if _is_album_item(item):
            self.frame.announce(
                "An album has no single track to play. Press Enter to "
                "choose which of its tracks to download."
            )
            return
        audio_only = self.result_engine in (
            ENGINE_MUSIC,
            ENGINE_DEEZER,
            ENGINE_APPLE_MUSIC,
            ENGINE_SOUNDCLOUD,
            ENGINE_BANDCAMP,
            ENGINE_AUDIOBOOKS,
        )
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
                item, audio_only, self.frame.config
            )
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
            f"Could not play that preview:\n{error}",
            "blindDL",
            wx.OK | wx.ICON_ERROR,
            self,
        )

    def _artist_folder(self, item):
        """Folder for a row from an artist search, or "" for any other row.

        Searching by Artist is a request for that artist's work, so what
        comes out of it is filed under their name instead of scattered
        through the download folder a track at a time. A best-match or
        title search is not about anyone in particular, and keeps landing
        where it always did.
        """
        if not search_kind.is_artist(self.kind_used):
            return ""
        artist = item.get("artist") or item.get("singers") or ""
        if isinstance(artist, (list, tuple)):
            artist = ", ".join(str(name) for name in artist if name)
        return str(artist).strip()

    def on_download_selected(self, event):
        indices = [i for i in self._selected_indices() if i < len(self.results)]
        if not indices:
            self.frame.announce("Select a result first.")
            return
        engine = self.result_engine
        if (
            _is_archive_engine(engine)
            and len(indices) == 1
            and self.results[indices[0]].get("kind") != "soulseek"
        ):
            # One Archive item can be a whole radio series. Ask which
            # episodes to take before filling the queue with hundreds.
            self._queue_archive_item(self.results[indices[0]])
            return
        # An album row is a whole release, so its track list has to be
        # fetched before anything can be queued. That is a network call, so
        # it happens off the GUI thread and the rest of the selection is
        # queued straight away rather than waiting behind it.
        albums = [
            self.results[index] for index in indices
            if _is_album_item(self.results[index])
        ]
        indices = [
            index for index in indices
            if not _is_album_item(self.results[index])
        ]
        if albums:
            self._queue_album_items(albums)
        if not indices:
            return
        with self.frame.queue.batch_additions():
            added = []
            for index in indices:
                item = self.results[index]
                folder = self._artist_folder(item)
                if item.get("kind") == "soulseek":
                    added.append(
                        self.frame.queue.add_soulseek(item, item["title"])
                    )
                elif engine == ENGINE_MUSIC:
                    if item.get("kind") in ("sideb", "deezer"):
                        added.append(
                            self.frame.queue.add_sideb(
                                item["url"], item["title"], folder=folder)
                        )
                    else:
                        added.append(self.frame.queue.add_musicdl(
                            item["song_info"], item["title"], folder=folder
                        ))
                elif engine == ENGINE_DEEZER:
                    added.append(
                        self.frame.queue.add_sideb(
                            item["url"], item["title"], folder=folder)
                    )
                elif engine == ENGINE_BOOKS:
                    added.append(self.frame.queue.add_book(item, item["title"]))
                elif engine == ENGINE_AUDIOBOOKS:
                    added.append(
                        self.frame.queue.add_audiobook(item, item["title"])
                    )
                elif engine == ENGINE_TORRENTS:
                    added.append(
                        self.frame.queue.add_torrent(item, item["title"])
                    )
                elif engine == ENGINE_SOUNDCLOUD:
                    added.append(self.frame.queue.add_ytdlp(
                        item["url"], item["title"], audio_only=True
                    ))
                elif engine == ENGINE_BANDCAMP:
                    added.append(self.frame.queue.add_ytdlp(
                        item["url"], item["title"], audio_only=True
                    ))
                elif _is_archive_engine(engine):
                    added.append(
                        self.frame.queue.add_archive(item, item["title"])
                    )
                elif _is_adult_engine(engine):
                    added.append(
                        self.frame.queue.add_adult(item, item["title"])
                    )
                elif engine == ENGINE_APPLE_MUSIC:
                    added.append(
                        self.frame.queue.add_applemusic(
                            item["url"], item["title"], folder=folder)
                    )
                else:
                    added.append(
                        self.frame.queue.add_ytdlp(item["url"], item["title"])
                    )
        self.frame.announce(addition_summary(
            added, [self.results[index]["title"] for index in indices]
        ))
