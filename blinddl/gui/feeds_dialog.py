# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Add, edit and remove the user's own torrent indexer feeds.

One feed is one Torznab or Newznab endpoint. It can be a whole Prowlarr or
Jackett instance, which is how private trackers are searched: that tool holds
the login, the cookie and the passkey, and answers for every tracker set up
in it. blindDL only ever keeps the endpoint and its API key.
"""

import wx

from .. import torrent_backend

PROWLARR_HINT = "http://localhost:9696/api/v1/search"
JACKETT_HINT = ("http://localhost:9117/api/v2.0/indexers/all/results/"
                "torznab/api")


class FeedDialog(wx.Dialog):
    """The name, URL and key of one feed."""

    def __init__(self, parent, feed=None):
        super().__init__(parent,
                         title="Edit indexer" if feed else "Add indexer")
        feed = feed or {}

        sizer = wx.BoxSizer(wx.VERTICAL)

        name_label = wx.StaticText(self, label="&Name:")
        self.name_text = wx.TextCtrl(self, value=feed.get("name", ""))
        self.name_text.SetName("Indexer name")
        self.name_text.SetHelpText(
            "What this indexer is called in the results list and in Search "
            "sites.")

        url_label = wx.StaticText(self, label="&URL:")
        self.url_text = wx.TextCtrl(self, value=feed.get("url", ""))
        self.url_text.SetName("Indexer URL")
        self.url_text.SetHelpText(
            f"A Torznab or Newznab search endpoint. Prowlarr: "
            f"{PROWLARR_HINT}. Jackett, all trackers at once: {JACKETT_HINT}")

        key_label = wx.StaticText(self, label="API &key:")
        self.key_text = wx.TextCtrl(self, value=feed.get("api_key", ""),
                                    style=wx.TE_PASSWORD)
        self.key_text.SetName("API key")
        self.key_text.SetHelpText(
            "Copy it from Prowlarr or Jackett's own settings. Leave empty if "
            "the endpoint needs no key.")

        hint = wx.StaticText(
            self,
            label="Private trackers work through Prowlarr or Jackett, which\n"
                  "keep your tracker logins. blindDL stores only this URL\n"
                  "and key.")

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        for label, control in ((name_label, self.name_text),
                               (url_label, self.url_text),
                               (key_label, self.key_text)):
            sizer.Add(label, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
            sizer.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(hint, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.SetSize((520, self.GetSize().GetHeight()))
        self.name_text.SetFocus()

    def feed(self):
        return {
            "name": self.name_text.GetValue().strip(),
            "url": self.url_text.GetValue().strip(),
            "api_key": self.key_text.GetValue().strip(),
        }


class FeedsDialog(wx.Dialog):
    """The list of feeds, with Add, Edit and Remove."""

    def __init__(self, parent, config):
        super().__init__(parent, title="My torrent indexers")
        self.config = config
        self.feeds = [dict(feed) for feed in torrent_backend.feeds(config)]

        sizer = wx.BoxSizer(wx.VERTICAL)

        list_label = wx.StaticText(self, label="&Indexers:")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("My torrent indexers")
        self.list.SetHelpText(
            "Enter edits the selected indexer. Delete removes it. "
            "Context Menu opens actions.")
        for index, heading in enumerate(("Name", "URL", "API key")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 150)
        self.list.SetColumnWidth(1, 330)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_edit)
        self.list.Bind(wx.EVT_CHAR, self.on_char)

        self.add_btn = wx.Button(self, label="&Add...")
        self.add_btn.SetName("Add indexer")
        self.edit_btn = wx.Button(self, label="&Edit...")
        self.edit_btn.SetName("Edit indexer")
        self.remove_btn = wx.Button(self, label="&Remove")
        self.remove_btn.SetName("Remove indexer")
        self.add_btn.Bind(wx.EVT_BUTTON, self.on_add)
        self.edit_btn.Bind(wx.EVT_BUTTON, self.on_edit)
        self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(self.add_btn, 0, wx.RIGHT, 8)
        actions.Add(self.edit_btn, 0, wx.RIGHT, 8)
        actions.Add(self.remove_btn, 0)

        sizer.Add(list_label, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.SetSize((620, 420))
        self._refresh()
        self.list.SetFocus()

    # -- helpers -------------------------------------------------------------

    def _refresh(self, select=-1):
        self.list.DeleteAllItems()
        for row, feed in enumerate(self.feeds):
            self.list.InsertItem(row, feed["name"])
            self.list.SetItem(row, 1, feed["url"])
            # Never redisplay the key itself; saying whether one is set is
            # what the user actually needs to check.
            self.list.SetItem(row, 2, "Set" if feed["api_key"] else "None")
        if self.feeds:
            row = min(max(select, 0), len(self.feeds) - 1)
            self.list.Select(row)
            self.list.Focus(row)
        has = bool(self.feeds)
        self.edit_btn.Enable(has)
        self.remove_btn.Enable(has)

    def _selected(self):
        index = self.list.GetFirstSelected()
        return index if 0 <= index < len(self.feeds) else -1

    def _name_taken(self, name, skip=-1):
        lowered = name.casefold()
        if lowered in {source.casefold()
                       for source in torrent_backend.ALL_SOURCES}:
            return True
        return any(feed["name"].casefold() == lowered
                   for index, feed in enumerate(self.feeds) if index != skip)

    def _ask(self, feed=None, index=-1):
        dialog = FeedDialog(self, feed)
        try:
            while dialog.ShowModal() == wx.ID_OK:
                entry = dialog.feed()
                if not entry["name"] or not entry["url"]:
                    wx.MessageBox(
                        "An indexer needs both a name and a URL.", "blindDL",
                        wx.OK | wx.ICON_ERROR, self)
                    continue
                if self._name_taken(entry["name"], skip=index):
                    wx.MessageBox(
                        f"Another indexer is already called "
                        f"{entry['name']}. Choose a different name.",
                        "blindDL", wx.OK | wx.ICON_ERROR, self)
                    continue
                return entry
            return None
        finally:
            dialog.Destroy()

    # -- actions -------------------------------------------------------------

    def on_add(self, event=None):
        entry = self._ask()
        if entry is None:
            return
        self.feeds.append(entry)
        self._refresh(len(self.feeds) - 1)
        self.list.SetFocus()

    def on_edit(self, event=None):
        index = self._selected()
        if index < 0:
            return
        entry = self._ask(self.feeds[index], index)
        if entry is None:
            return
        self.feeds[index] = entry
        self._refresh(index)
        self.list.SetFocus()

    def on_remove(self, event=None):
        index = self._selected()
        if index < 0:
            return
        removed = self.feeds.pop(index)
        self._refresh(index)
        self.list.SetFocus()
        wx.MessageBox(f"Removed {removed['name']}.", "blindDL",
                      wx.OK | wx.ICON_INFORMATION, self)

    def on_char(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_DELETE:
            self.on_remove()
            return
        event.Skip()

    # -- result --------------------------------------------------------------

    def apply(self):
        """Write the feeds back into the config object.

        A renamed or removed indexer leaves its name behind in the
        switched-off list, where it would silently switch off a later
        indexer that happened to reuse the name.
        """
        self.config["torznab_feeds"] = [dict(feed) for feed in self.feeds]
        live = {source.casefold()
                for source in torrent_backend.all_sources(self.config)}
        self.config["disabled_torrent_sources"] = [
            source for source in self.config["disabled_torrent_sources"]
            if str(source).casefold() in live]
        self.config.save()

    def summary(self):
        count = len(self.feeds)
        noun = "indexer" if count == 1 else "indexers"
        return f"{count} of your own {noun} configured."
