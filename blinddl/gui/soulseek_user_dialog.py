# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible Soulseek user profile and shared-file browser dialogs."""

from __future__ import annotations

import io
import ntpath
import threading

import wx

from ..downloader import addition_summary

from .. import soulseek_backend


def _format_size(value):
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


class UserProfileDialog(wx.Dialog):
    def __init__(self, parent, profile):
        username = profile.get("username", "")
        super().__init__(parent, title=f"Soulseek profile: {username}", size=(620, 520))
        sizer = wx.BoxSizer(wx.VERTICAL)

        picture = profile.get("picture")
        if picture:
            try:
                image = wx.Image(io.BytesIO(picture))
                if image.IsOk():
                    image.Rescale(160, 160, wx.IMAGE_QUALITY_HIGH)
                    bitmap = wx.StaticBitmap(self, bitmap=wx.Bitmap(image))
                    bitmap.SetName(f"Profile picture for {username}")
                    sizer.Add(bitmap, 0, wx.ALIGN_CENTER | wx.ALL, 8)
            except Exception:  # noqa: BLE001 - malformed peer images are optional
                pass

        slots = "yes" if profile.get("has_slots_free") else "no"
        details = (
            f"Username: {username}\n"
            f"Status: {profile.get('status', 'Unknown')}\n"
            f"Free upload slot: {slots}\n"
            f"Upload slots: {profile.get('upload_slots', 0)}\n"
            f"Upload queue: {profile.get('queue_length', 0)}\n"
            f"Who may download: {profile.get('upload_permissions', 'Unknown')}\n"
            f"Average speed: {_format_size(profile.get('average_speed'))}/s\n"
            f"Completed uploads: {profile.get('uploads', 0)}\n"
            f"Shared folders: {profile.get('shared_folders', 0)}\n"
            f"Shared files: {profile.get('shared_files', 0)}"
        )
        info = wx.TextCtrl(
            self, value=details, style=wx.TE_MULTILINE | wx.TE_READONLY
        )
        info.SetName("Soulseek profile details")
        sizer.Add(info, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        description_label = wx.StaticText(self, label="Profile &description:")
        description = wx.TextCtrl(
            self,
            value=profile.get("description", "") or "No profile description.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        description.SetName("Soulseek profile description")
        sizer.Add(description_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(description, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)


class UserBrowserDialog(wx.Dialog):
    """Browse one peer's folders in a tree and its contents in a list."""

    def __init__(self, parent, frame, username=""):
        super().__init__(parent, title="Browse Soulseek user", size=(960, 680))
        self.frame = frame
        self.directories = []
        self._by_name = {}
        self._tree_items = {}
        self._visible = []
        self._alive = True

        outer = wx.BoxSizer(wx.VERTICAL)
        user_row = wx.BoxSizer(wx.HORIZONTAL)
        user_label = wx.StaticText(self, label="&Username:")
        self.username_text = wx.TextCtrl(self, value=username, style=wx.TE_PROCESS_ENTER)
        self.username_text.SetName("Soulseek user to browse")
        self.browse_button = wx.Button(self, label="&Browse files")
        self.browse_button.SetName("Browse this user's files")
        self.message_button = wx.Button(self, label="&Message")
        self.message_button.SetName("Message this Soulseek user")
        self.friend_button = wx.Button(self, label="Add &friend")
        self.friend_button.SetName("Add this user as a friend")
        self.slot_button = wx.Button(self, label="Give free &slot")
        self.slot_button.SetName("Give this user a free slot")
        self.profile_button = wx.Button(self, label="View &profile")
        self.profile_button.SetName("View this user's profile")
        self.follow_button = wx.Button(self, label="F&ollow user")
        self.follow_button.SetName("Follow this user's shared files")
        self.follow_button.SetHelpText(
            "Adds this user to Subscriptions. Anything they share from now "
            "on is downloaded automatically.")
        user_row.Add(user_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        user_row.Add(self.username_text, 1, wx.RIGHT, 6)
        for button in (
            self.browse_button,
            self.message_button,
            self.friend_button,
            self.slot_button,
            self.profile_button,
            self.follow_button,
        ):
            user_row.Add(button, 0, wx.RIGHT, 6)

        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_label = wx.StaticText(self, label="&Filter folders and files:")
        self.filter_text = wx.TextCtrl(self)
        self.filter_text.SetName("Soulseek share filter")
        self.filter_text.SetHelpText(
            "Type part of a folder or file name. Clear it to show the selected folder."
        )
        filter_row.Add(filter_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        filter_row.Add(self.filter_text, 1)

        views = wx.BoxSizer(wx.HORIZONTAL)
        tree_box = wx.BoxSizer(wx.VERTICAL)
        tree_box.Add(wx.StaticText(self, label="Folder &tree:"), 0, wx.BOTTOM, 4)
        self.tree = wx.TreeCtrl(
            self, style=wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE
        )
        self.tree.SetName("Soulseek shared folder tree")
        self.tree.SetHelpText("Choose a folder to show its folders and files in the list.")
        tree_box.Add(self.tree, 1, wx.EXPAND)

        list_box = wx.BoxSizer(wx.VERTICAL)
        self.path_label = wx.StaticText(self, label="Folder contents:")
        list_box.Add(self.path_label, 0, wx.BOTTOM, 4)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Soulseek shared folder list")
        self.list.SetHelpText(
            "Enter opens a folder. Context Menu downloads selected files or folders."
        )
        for index, heading in enumerate(("Name", "Type", "Size", "Folder")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 310)
        self.list.SetColumnWidth(1, 100)
        self.list.SetColumnWidth(2, 100)
        self.list.SetColumnWidth(3, 310)
        list_box.Add(self.list, 1, wx.EXPAND)
        views.Add(tree_box, 2, wx.EXPAND | wx.RIGHT, 8)
        views.Add(list_box, 3, wx.EXPAND)

        outer.Add(user_row, 0, wx.EXPAND | wx.ALL, 8)
        outer.Add(filter_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        outer.Add(views, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        outer.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(outer)

        self.browse_button.Bind(wx.EVT_BUTTON, self.on_browse)
        self.username_text.Bind(wx.EVT_TEXT_ENTER, self.on_browse)
        self.message_button.Bind(wx.EVT_BUTTON, self.on_message)
        self.friend_button.Bind(wx.EVT_BUTTON, self.on_friend)
        self.slot_button.Bind(wx.EVT_BUTTON, self.on_slot)
        self.profile_button.Bind(wx.EVT_BUTTON, self.on_profile)
        self.follow_button.Bind(wx.EVT_BUTTON, self.on_follow)
        self.filter_text.Bind(wx.EVT_TEXT, self.on_filter)
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_tree_selected)
        self.tree.Bind(wx.EVT_CONTEXT_MENU, self.on_tree_menu)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_list_activated)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_list_menu)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_BUTTON, self.on_close, id=wx.ID_CLOSE)
        if username:
            wx.CallAfter(self.on_browse)

    def _username(self):
        return self.username_text.GetValue().strip()

    def _run(self, action, success, *args):
        try:
            result = action(*args)
        except Exception as exc:  # noqa: BLE001 - peer errors belong in the UI
            wx.CallAfter(self._failed, str(exc))
            return
        wx.CallAfter(success, result)

    def _failed(self, error):
        if self._alive:
            self.browse_button.Enable()
            self.frame.announce(f"Could not browse Soulseek user: {error}")

    def on_browse(self, event=None):
        username = self._username()
        if not username:
            self.frame.announce("Enter a Soulseek username to browse.")
            return
        self.browse_button.Disable()
        self.frame.announce(f"Loading shared folders from {username}...")
        threading.Thread(
            target=self._run,
            args=(soulseek_backend.browse_user, self._loaded, username, self.frame.config),
            daemon=True,
            name="blinddl-soulseek-browse-user",
        ).start()

    def _loaded(self, directories):
        if not self._alive:
            return
        self.browse_button.Enable()
        self.directories = list(directories)
        self._by_name = {row["name"].casefold(): row for row in self.directories}
        self._build_tree()
        file_count = sum(len(row["files"]) for row in self.directories)
        self.frame.announce(
            f"Loaded {len(self.directories)} folders and {file_count} files from {self._username()}."
        )

    @staticmethod
    def _parts(path):
        return [part for part in str(path).replace("/", "\\").split("\\") if part]

    def _build_tree(self):
        self.tree.DeleteAllItems()
        self._tree_items = {}
        root = self.tree.AddRoot(self._username() or "Shared files")
        self.tree.SetItemData(root, "")
        nodes = {"": root}
        for directory in self.directories:
            cumulative = ""
            parent = root
            for part in self._parts(directory["name"]):
                cumulative = ntpath.join(cumulative, part) if cumulative else part
                key = cumulative.casefold()
                child = nodes.get(key)
                if child is None:
                    child = self.tree.AppendItem(parent, part)
                    self.tree.SetItemData(child, cumulative)
                    nodes[key] = child
                self._tree_items[key] = child
                parent = child
            self._tree_items[directory["name"].casefold()] = parent
        self.tree.Expand(root)
        self.tree.SelectItem(root)

    def _selected_folder(self):
        item = self.tree.GetSelection()
        if not item.IsOk():
            return ""
        return str(self.tree.GetItemData(item) or "")

    def _direct_child_folders(self, folder):
        prefix = folder.rstrip("\\") + "\\" if folder else ""
        children = {}
        for directory in self.directories:
            name = directory["name"]
            if folder and not name.casefold().startswith(prefix.casefold()):
                continue
            remainder = name[len(prefix):] if prefix else name
            first = self._parts(remainder)
            if not first:
                continue
            child = ntpath.join(folder, first[0]) if folder else first[0]
            if child.casefold() != folder.casefold():
                children[child.casefold()] = child
        return sorted(children.values(), key=str.casefold)

    def _render(self):
        query = self.filter_text.GetValue().strip().casefold()
        folder = self._selected_folder()
        visible = []
        if query:
            for directory in self.directories:
                if query in directory["name"].casefold():
                    visible.append({"type": "folder", "path": directory["name"]})
                for item in directory["files"]:
                    if query in item["title"].casefold() or query in item["remote_path"].casefold():
                        visible.append({"type": "file", "item": item})
            self.path_label.SetLabel(f"Filter results for {self.filter_text.GetValue().strip()}:")
        else:
            for child in self._direct_child_folders(folder):
                visible.append({"type": "folder", "path": child})
            directory = self._by_name.get(folder.casefold())
            if directory:
                visible.extend({"type": "file", "item": item} for item in directory["files"])
            self.path_label.SetLabel(f"Folder contents: {folder or 'shared files'}")

        # A matching folder may appear both as a real directory and as a
        # parent synthesized from deeper paths. Present it only once.
        deduped = []
        seen = set()
        for entry in visible:
            key = (
                "folder",
                entry["path"].casefold(),
            ) if entry["type"] == "folder" else (
                "file",
                entry["item"]["remote_path"].casefold(),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(entry)
        self._visible = deduped
        self.list.DeleteAllItems()
        for entry in self._visible:
            if entry["type"] == "folder":
                path = entry["path"]
                row = self.list.InsertItem(self.list.GetItemCount(), ntpath.basename(path) or path)
                self.list.SetItem(row, 1, "Folder")
                self.list.SetItem(row, 3, ntpath.dirname(path))
            else:
                item = entry["item"]
                row = self.list.InsertItem(self.list.GetItemCount(), item["title"])
                self.list.SetItem(row, 1, item.get("format") or "File")
                self.list.SetItem(row, 2, item.get("file_size", ""))
                self.list.SetItem(row, 3, item.get("folder", ""))

    def on_tree_selected(self, event):
        if not self.filter_text.GetValue():
            self._render()
        event.Skip()

    def on_filter(self, event):
        self._render()
        event.Skip()

    def _selected_entries(self):
        entries = []
        row = self.list.GetFirstSelected()
        while row != -1:
            if row < len(self._visible):
                entries.append(self._visible[row])
            row = self.list.GetNextSelected(row)
        return entries

    def _select_tree_path(self, path):
        item = self._tree_items.get(path.casefold())
        if item is not None:
            self.filter_text.Clear()
            self.tree.SelectItem(item)
            self.tree.EnsureVisible(item)
            self.list.SetFocus()

    def on_list_activated(self, event):
        if event.GetIndex() >= len(self._visible):
            return
        entry = self._visible[event.GetIndex()]
        if entry["type"] == "folder":
            self._select_tree_path(entry["path"])
        else:
            self._queue_files([entry["item"]])

    def _files_in_folder(self, folder):
        prefix = folder.rstrip("\\") + "\\"
        files = []
        for directory in self.directories:
            if directory["name"].casefold() == folder.casefold() or directory["name"].casefold().startswith(prefix.casefold()):
                files.extend(directory["files"])
        return files

    def _queue_files(self, files, folder=""):
        added = []
        titles = []
        with self.frame.queue.batch_additions():
            for original in files:
                if original.get("locked"):
                    continue
                item = dict(original)
                if folder:
                    relative = ntpath.relpath(item["remote_path"], folder)
                    item["target_relative_path"] = ntpath.join(
                        ntpath.basename(folder.rstrip("\\")), relative
                    )
                added.append(
                    self.frame.queue.add_soulseek(item, item["title"])
                )
                titles.append(item["title"])
        if added:
            self.frame.announce(addition_summary(added, titles))
        else:
            self.frame.announce("That selection contains no downloadable files.")

    def on_list_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            row, _flags = self.list.HitTest(self.list.ScreenToClient(position))
            if row >= 0 and not self.list.IsSelected(row):
                for selected in range(self.list.GetItemCount()):
                    if self.list.IsSelected(selected):
                        self.list.Select(selected, False)
                self.list.Focus(row)
                self.list.Select(row)
        entries = self._selected_entries()
        menu = wx.Menu()
        download_files = menu.Append(wx.ID_ANY, "&Download selected files")
        download_folders = menu.Append(wx.ID_ANY, "Download selected &folders")
        files = [entry["item"] for entry in entries if entry["type"] == "file"]
        folders = [entry["path"] for entry in entries if entry["type"] == "folder"]
        download_files.Enable(bool(files))
        download_folders.Enable(bool(folders))
        menu.Bind(wx.EVT_MENU, lambda event: self._queue_files(files), download_files)
        menu.Bind(wx.EVT_MENU, lambda event: [self._queue_files(self._files_in_folder(folder), folder) for folder in folders], download_folders)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def on_tree_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            item, _flags = self.tree.HitTest(self.tree.ScreenToClient(position))
            if item.IsOk():
                self.tree.SelectItem(item)
        folder = self._selected_folder()
        menu = wx.Menu()
        download = menu.Append(wx.ID_ANY, "&Download folder")
        download.Enable(bool(folder))
        menu.Bind(
            wx.EVT_MENU,
            lambda selected: self._queue_files(self._files_in_folder(folder), folder),
            download,
        )
        self.tree.PopupMenu(menu)
        menu.Destroy()

    def on_message(self, event=None):
        self.frame.message_soulseek_user(self._username())

    def on_friend(self, event=None):
        self.frame.add_soulseek_friend(self._username())

    def on_slot(self, event=None):
        self.frame.give_soulseek_free_slot(self._username())

    def on_follow(self, event=None):
        self.frame.follow_soulseek_user(self._username())

    def on_profile(self, event=None):
        self.frame.view_soulseek_profile(self._username())

    def on_close(self, event):
        self._alive = False
        if self.IsModal():
            self.EndModal(wx.ID_CLOSE)
        else:
            self.Destroy()
