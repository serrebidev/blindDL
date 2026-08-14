# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Library tab: a Windows-Explorer-like browser of every shared folder.

The left pane is the folder tree; the right pane lists the folders and files
inside the selected folder. Every folder the user shares -- the download
folder and each additional Soulseek shared folder -- appears as a top-level
node with its whole subfolder tree, and every file is shown, not only media.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import wx

from ..book_backend import BOOK_SUBFOLDER
from ..runtime import open_file, open_folder
from .media_player import MediaPlayerPanel

AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogv",
    ".ts",
    ".webm",
    ".wmv",
}
BOOK_EXTENSIONS = {
    ".azw3",
    ".djvu",
    ".epub",
    ".fb2",
    ".mobi",
    ".pdf",
    ".txt",
}
TORRENT_EXTENSIONS = {".torrent"}
# A .txt is a book only inside the folder books are downloaded to; anywhere
# else it is far more likely to be the user's own notes.
BOOK_FOLDER_ONLY_EXTENSIONS = {".txt"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | BOOK_EXTENSIONS
KIND_AUDIO = "Audio"
KIND_VIDEO = "Video"
KIND_BOOK = "Book"
KIND_TORRENT = "Torrent"
KIND_FILE = "File"
KIND_FOLDER = "Folder"


def _kind_for(extension):
    if extension in AUDIO_EXTENSIONS:
        return KIND_AUDIO
    if extension in VIDEO_EXTENSIONS:
        return KIND_VIDEO
    return KIND_BOOK


def _file_kind(extension, in_book_folder=False):
    """Kind of any file, including those outside the media extension sets."""
    if extension in AUDIO_EXTENSIONS:
        return KIND_AUDIO
    if extension in VIDEO_EXTENSIONS:
        return KIND_VIDEO
    if extension in TORRENT_EXTENSIONS:
        return KIND_TORRENT
    if extension in BOOK_EXTENSIONS and (
        extension not in BOOK_FOLDER_ONLY_EXTENSIONS or in_book_folder
    ):
        return KIND_BOOK
    return KIND_FILE


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _display_name(path):
    path = os.path.normpath(os.path.abspath(path))
    name = os.path.basename(path)
    return name if name else path


def library_roots(config):
    """Return the top-level folders the Library shows, as name/path pairs.

    The download folder always comes first; every additional shared folder
    follows. Duplicate paths are shown once, and duplicate display names get a
    numeric suffix so a screen reader can tell them apart.
    """
    candidates = []
    download_dir = str(config.get("download_dir", "") or "").strip()
    if download_dir:
        candidates.append(download_dir)
    for value in config.get("soulseek_shared_folders", []) or []:
        path = str(value or "").strip()
        if path:
            candidates.append(path)

    roots = []
    seen = set()
    used_names = {}
    for path in candidates:
        absolute = os.path.abspath(path)
        key = _norm(absolute)
        if key in seen:
            continue
        seen.add(key)
        name = _display_name(absolute)
        count = used_names.get(name.casefold(), 0)
        used_names[name.casefold()] = count + 1
        if count:
            name = f"{name} ({count + 1})"
        roots.append({"name": name, "path": absolute})
    return roots


def discover_media(root):
    """Return media and book file records below *root*, by relative path."""
    base = Path(root)
    if not base.is_dir():
        return []
    records = []
    for folder, _directories, filenames in os.walk(base):
        for filename in filenames:
            path = Path(folder) / filename
            extension = path.suffix.lower()
            if extension not in MEDIA_EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
                relative_parent = path.parent.relative_to(base)
            except OSError:
                continue
            if (extension in BOOK_FOLDER_ONLY_EXTENSIONS and
                    relative_parent.parts[:1] != (BOOK_SUBFOLDER,)):
                continue
            records.append(
                {
                    "path": str(path),
                    "title": path.stem,
                    "kind": _kind_for(extension),
                    "folder": ""
                    if str(relative_parent) == "."
                    else str(relative_parent),
                    "size": size,
                }
            )
    return sorted(
        records,
        key=lambda item: os.path.relpath(item["path"], base).casefold(),
    )


def discover_library(roots):
    """Walk every shared root once and index its whole folder tree.

    ``roots`` is the list returned by :func:`library_roots`. The result holds:

    * ``roots`` -- ordered normalized paths of the shared folders;
    * ``names`` -- normalized path to display name for every folder;
    * ``dirs`` -- normalized folder to its child folders (normalized paths);
    * ``files`` -- normalized folder to its direct file records.

    Overlapping shared folders are only walked once, so a file appears once no
    matter how many shared folders contain it. Walking is recursive, so every
    subfolder of a shared folder is indexed and shown.
    """
    dirs = {}
    files = {}
    names = {}
    root_norms = []
    visited = set()

    for root in roots:
        base = os.path.abspath(str(root["path"]))
        norm_base = _norm(base)
        if norm_base in root_norms:
            continue
        root_norms.append(norm_base)
        names[norm_base] = root.get("name") or _display_name(base)

        for dirpath, dirnames, filenames in os.walk(base):
            norm_dir = _norm(dirpath)
            names.setdefault(norm_dir, os.path.basename(dirpath) or dirpath)
            if norm_dir in visited:
                # Reached through an earlier, overlapping root; the subtree is
                # already indexed. Stop the walk from descending into it again.
                dirnames[:] = []
                continue
            visited.add(norm_dir)

            children = []
            for name in dirnames:
                child_norm = _norm(os.path.join(dirpath, name))
                names.setdefault(child_norm, name)
                children.append(child_norm)
            children.sort(key=lambda norm: names.get(norm, "").casefold())
            dirs[norm_dir] = children

            relative_parent = os.path.relpath(dirpath, base)
            in_book_folder = (
                relative_parent == BOOK_SUBFOLDER
                or relative_parent.startswith(BOOK_SUBFOLDER + os.sep)
            )
            records = []
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                extension = os.path.splitext(name)[1].lower()
                records.append(
                    {
                        "name": name,
                        "path": path,
                        "kind": _file_kind(extension, in_book_folder),
                        "size": size,
                    }
                )
            files[norm_dir] = records

    return {
        "roots": root_norms,
        "names": names,
        "dirs": dirs,
        "files": files,
    }


def _format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


class LibraryPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.roots = []
        self.dirs = {}
        self.files = {}
        self.names = {}
        self._root_norms = set()
        self._tree_items = {}
        self._loaded = set()
        self._visible = []
        self._alive = True
        self._refreshing = False
        self._pending_refresh = None
        self._announce_refresh = False

        outer = wx.BoxSizer(wx.VERTICAL)
        views = wx.BoxSizer(wx.HORIZONTAL)

        tree_box = wx.BoxSizer(wx.VERTICAL)
        tree_box.Add(wx.StaticText(self, label="Folder &tree:"), 0, wx.BOTTOM, 4)
        self.tree = wx.TreeCtrl(
            self,
            style=wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE,
        )
        self.tree.SetName("Library folder tree")
        self.tree.SetHelpText(
            "Choose a folder to show its folders and files in the list."
        )
        tree_box.Add(self.tree, 1, wx.EXPAND)

        list_box = wx.BoxSizer(wx.VERTICAL)
        self.path_label = wx.StaticText(self, label="Folder contents:")
        list_box.Add(self.path_label, 0, wx.BOTTOM, 4)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.SetName("Library folder contents")
        self.list.SetHelpText(
            "Enter opens a folder or plays the selected file. Context Menu "
            "opens actions."
        )
        for index, heading in enumerate(("Name", "Type", "Size")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 380)
        self.list.SetColumnWidth(1, 90)
        self.list.SetColumnWidth(2, 100)
        list_box.Add(self.list, 1, wx.EXPAND)
        views.Add(tree_box, 2, wx.EXPAND | wx.RIGHT, 8)
        views.Add(list_box, 3, wx.EXPAND)

        self.play_btn = wx.Button(self, label="&Play or open selected")
        self.refresh_btn = wx.Button(self, label="&Refresh library")
        self.play_btn.Bind(wx.EVT_BUTTON, self.on_play_selected)
        self.refresh_btn.Bind(wx.EVT_BUTTON, self.on_refresh)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.refresh_btn, 0)

        self.player = MediaPlayerPanel(self, frame, video_height=220)

        outer.Add(views, 3, wx.EXPAND | wx.ALL, 8)
        outer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        outer.Add(self.player, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(outer)

        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_tree_selected)
        self.tree.Bind(wx.EVT_TREE_ITEM_EXPANDING, self.on_tree_expanding)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_list_activated)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
        self.refresh(announce=False)

    # -- refresh -------------------------------------------------------------

    def refresh(self, announce=True):
        if not self._alive:
            return
        folder = self._selected_norm()
        file_path = None
        entry = self._selected_entry()
        if entry is not None and entry["type"] == "file":
            file_path = entry["path"]
        self._announce_refresh = self._announce_refresh or announce
        # Keep only the newest request while a walk is running. A burst of
        # completed downloads therefore causes one follow-up scan, not one
        # recursive walk per file.
        self._pending_refresh = (
            library_roots(self.frame.config),
            folder,
            file_path,
        )
        self._start_refresh()

    def _start_refresh(self):
        if not self._alive or self._refreshing or self._pending_refresh is None:
            return
        roots, folder, file_path = self._pending_refresh
        self._pending_refresh = None
        self._refreshing = True
        threading.Thread(
            target=self._discover,
            args=(roots, folder, file_path),
            daemon=True,
            name="blinddl-library-scan",
        ).start()

    def _discover(self, roots, folder, file_path):
        result = discover_library(roots)
        wx.CallAfter(self._refresh_finished, result, folder, file_path)

    def _refresh_finished(self, result, folder, file_path):
        if not self._alive:
            return
        self._refreshing = False
        if self._pending_refresh is not None:
            self._start_refresh()
            return
        announce = self._announce_refresh
        self._announce_refresh = False
        self.roots = result["roots"]
        self.names = result["names"]
        self.dirs = result["dirs"]
        self.files = result["files"]
        self._root_norms = set(self.roots)
        self._rebuild_tree()
        self._select_folder(folder or "")
        if file_path:
            self._select_file(file_path)
        if announce:
            folder_count = len(self.dirs)
            file_count = sum(len(records) for records in self.files.values())
            folder_noun = "folder" if folder_count == 1 else "folders"
            file_noun = "file" if file_count == 1 else "files"
            self.frame.announce(
                f"Library refreshed: {folder_count} {folder_noun}, "
                f"{file_count} {file_noun}."
            )

    def on_refresh(self, event):
        self.refresh()

    # -- tree ----------------------------------------------------------------

    def _selected_norm(self):
        item = self.tree.GetSelection()
        if not item.IsOk():
            return ""
        return str(self.tree.GetItemData(item) or "")

    def _rebuild_tree(self):
        self.tree.DeleteAllItems()
        self._tree_items = {}
        self._loaded = {""}
        root = self.tree.AddRoot("Library")
        self.tree.SetItemData(root, "")
        for norm in self.roots:
            self._append_folder(root, norm)
        self.tree.Expand(root)

    def _append_folder(self, parent_item, norm):
        name = self.names.get(norm, os.path.basename(os.path.normpath(norm)) or norm)
        item = self.tree.AppendItem(parent_item, name)
        self.tree.SetItemData(item, norm)
        self._tree_items[norm] = item
        if self.dirs.get(norm):
            # A placeholder keeps the expander visible; real children are
            # added the first time the node is opened.
            self.tree.AppendItem(item, "")
        return item

    def _ensure_children_loaded(self, item):
        norm = self._selected_norm_for(item)
        if norm in self._loaded:
            return
        self._loaded.add(norm)
        self.tree.DeleteChildren(item)
        for child in self.dirs.get(norm, []):
            self._append_folder(item, child)

    @staticmethod
    def _selected_norm_for(item):
        return str(item.GetItemData() or "")

    def on_tree_expanding(self, event):
        self._ensure_children_loaded(event.GetItem())
        event.Skip()

    def on_tree_selected(self, event):
        self._render()
        event.Skip()

    def _find_child(self, parent_item, norm):
        child, cookie = self.tree.GetFirstChild(parent_item)
        while child.IsOk():
            if str(self.tree.GetItemData(child) or "") == norm:
                return child
            child, cookie = self.tree.GetNextChild(parent_item, cookie)
        return None

    def _chain_to(self, norm):
        """Ordered normalized paths from the containing shared folder to norm."""
        chain = []
        current = norm
        while True:
            chain.append(current)
            if current in self._root_norms:
                break
            parent = _norm(os.path.dirname(os.path.normpath(current)))
            if parent == current:
                break
            current = parent
        chain.reverse()
        return chain

    def _select_folder(self, norm):
        item = self.tree.GetRootItem()
        self._ensure_children_loaded(item)
        if norm:
            for ancestor in self._chain_to(norm):
                child = self._find_child(item, ancestor)
                if child is None:
                    break
                self.tree.Expand(item)
                item = child
                self._ensure_children_loaded(item)
        self.tree.SelectItem(item)
        self.tree.EnsureVisible(item)
        self._render()

    # -- file list -----------------------------------------------------------

    def _folder_entry(self, norm):
        return {
            "type": "folder",
            "name": self.names.get(norm, os.path.basename(os.path.normpath(norm)) or norm),
            "norm": norm,
            "path": os.path.normpath(norm),
        }

    def _render(self):
        folder = self._selected_norm()
        entries = []
        if folder == "":
            for norm in self.roots:
                entries.append(self._folder_entry(norm))
            self.path_label.SetLabel("Folder contents: all shared folders")
        else:
            for child in sorted(
                self.dirs.get(folder, []),
                key=lambda norm: self.names.get(norm, "").casefold(),
            ):
                entries.append(self._folder_entry(child))
            for record in sorted(
                self.files.get(folder, []),
                key=lambda record: record["name"].casefold(),
            ):
                entries.append({"type": "file", **record})
            self.path_label.SetLabel(f"Folder contents: {self.names.get(folder, '')}")
        self._visible = entries
        self.list.DeleteAllItems()
        for row, entry in enumerate(self._visible):
            if entry["type"] == "folder":
                self.list.InsertItem(row, entry["name"])
                self.list.SetItem(row, 1, KIND_FOLDER)
                self.list.SetItem(row, 2, "")
            else:
                self.list.InsertItem(row, entry["name"])
                self.list.SetItem(row, 1, entry["kind"])
                self.list.SetItem(row, 2, _format_size(entry["size"]))

    def _select_file(self, path):
        for row, entry in enumerate(self._visible):
            if entry.get("path") == path:
                self.list.Select(row)
                self.list.Focus(row)
                return

    def _selected_entry(self):
        row = self.list.GetFirstSelected()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    # -- actions -------------------------------------------------------------

    def on_list_activated(self, event):
        index = event.GetIndex()
        if not 0 <= index < len(self._visible):
            return
        entry = self._visible[index]
        if entry["type"] == "folder":
            self._select_folder(entry["norm"])
        else:
            self._open_file(entry)

    def on_play_selected(self, event):
        entry = self._selected_entry()
        if entry is None:
            self.frame.announce("Select a library folder or file first.")
            return
        if entry["type"] == "folder":
            self._select_folder(entry["norm"])
        else:
            self._open_file(entry)

    def _open_file(self, entry):
        path = entry["path"]
        if not os.path.isfile(path):
            self.frame.announce("That file no longer exists. Refreshing library.")
            self.refresh(announce=False)
            return
        kind = entry["kind"]
        title = os.path.splitext(entry["name"])[0]
        if kind == KIND_BOOK:
            # blindDL has no reader of its own; the book goes to whichever
            # application the user already reads books in.
            self.frame.announce(f"Opening {title} in your reader.")
            open_file(path)
            return
        if kind in (KIND_AUDIO, KIND_VIDEO):
            self.frame.play_media(self.player, path, title)
            return
        # Anything else opens in the application the user has set for its type.
        self.frame.announce(f"Opening {entry['name']}.")
        open_file(path)

    def on_context_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            row, _flags = self.list.HitTest(self.list.ScreenToClient(position))
            if row >= 0:
                self.list.Select(row)
                self.list.Focus(row)
        entry = self._selected_entry()
        menu = wx.Menu()
        if entry is not None and entry["type"] == "folder":
            open_item = menu.Append(wx.ID_ANY, "&Open folder")
            menu.Bind(
                wx.EVT_MENU,
                lambda event, norm=entry["norm"]: self._select_folder(norm),
                open_item,
            )
        else:
            play = menu.Append(
                wx.ID_ANY,
                "&Open"
                if (entry is not None and entry["kind"] == KIND_BOOK)
                else "&Play",
            )
            open_location = menu.Append(wx.ID_ANY, "Open file &location")
            play.Enable(entry is not None)
            open_location.Enable(entry is not None)
            menu.Bind(wx.EVT_MENU, self.on_play_selected, play)
            menu.Bind(wx.EVT_MENU, self._on_open_location, open_location)
        refresh_item = menu.Append(wx.ID_ANY, "&Refresh library")
        menu.Bind(wx.EVT_MENU, self.on_refresh, refresh_item)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def _on_open_location(self, event):
        entry = self._selected_entry()
        if entry is not None and entry["type"] == "file":
            open_folder(os.path.dirname(entry["path"]))

    def shutdown(self):
        self._alive = False
        self._pending_refresh = None
        self.player.shutdown()
