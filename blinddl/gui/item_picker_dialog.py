# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible item selection for URLs that contain multiple downloads."""

import wx

from .. import ytdlp_backend


class ItemPickerDialog(wx.Dialog):
    """Let the user choose which items from an expanded URL to queue."""

    def __init__(self, parent, items, title):
        super().__init__(
            parent,
            title="Choose downloads",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.items = list(items)
        self._changing_selection = False

        prompt = wx.StaticText(self, label=f"Choose from {title}.")
        self.item_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.item_list.SetName("Items to download")
        self.item_list.EnableCheckBoxes()
        for column, heading in enumerate(
                ("Title", "Artist or channel", "Duration")):
            self.item_list.InsertColumn(column, heading)

        for row, item in enumerate(self.items):
            self.item_list.InsertItem(row, item.get("title") or "Unknown title")
            self.item_list.SetItem(
                row, 1, item.get("artist") or item.get("uploader") or "")
            duration = item.get("duration_s", item.get("duration"))
            self.item_list.SetItem(
                row, 2, ytdlp_backend.format_duration(duration))
            self.item_list.CheckItem(row)

        self.item_list.SetColumnWidth(0, 380)
        self.item_list.SetColumnWidth(1, 200)
        self.item_list.SetColumnWidth(2, 90)
        self.item_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_check_changed)
        self.item_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_check_changed)
        self.item_list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)

        self.count_text = wx.StaticText(self)

        self.select_all_btn = wx.Button(self, label="Select &all")
        self.select_all_btn.Bind(wx.EVT_BUTTON, self.on_select_all)
        self.clear_btn = wx.Button(self, label="&Clear selection")
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_selection)

        self.download_btn = wx.Button(self, wx.ID_OK, "&Download selected")
        self.download_btn.SetDefault()
        cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")

        selection_buttons = wx.BoxSizer(wx.HORIZONTAL)
        selection_buttons.Add(self.select_all_btn, 0, wx.RIGHT, 8)
        selection_buttons.Add(self.clear_btn, 0)

        action_buttons = wx.BoxSizer(wx.HORIZONTAL)
        action_buttons.AddStretchSpacer()
        action_buttons.Add(self.download_btn, 0, wx.RIGHT, 8)
        action_buttons.Add(cancel_btn, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, 8)
        sizer.Add(self.item_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.count_text, 0, wx.ALL, 8)
        sizer.Add(selection_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(action_buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((720, 480))
        self.SetMinSize((520, 320))

        self._update_selection_state()
        if self.items:
            self.item_list.Focus(0)
            self.item_list.Select(0)
        self.item_list.SetFocus()

    def selected_items(self):
        """Return selected item dictionaries in their original order."""
        return [item for row, item in enumerate(self.items)
                if self.item_list.IsItemChecked(row)]

    def on_select_all(self, event):
        self._set_all_selected(True)

    def on_clear_selection(self, event):
        self._set_all_selected(False)

    def _set_all_selected(self, selected):
        self._changing_selection = True
        self.item_list.Freeze()
        try:
            for row in range(len(self.items)):
                self.item_list.CheckItem(row, selected)
        finally:
            self.item_list.Thaw()
            self._changing_selection = False
        self._update_selection_state()
        self._announce_count()
        self.item_list.SetFocus()

    def _on_check_changed(self, event):
        if not self._changing_selection:
            self._update_selection_state()
            self._announce_count()
        event.Skip()

    def _update_selection_state(self):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        total = len(self.items)
        self.count_text.SetLabel(f"{selected} of {total} selected")
        self.download_btn.Enable(selected > 0)
        self.select_all_btn.Enable(selected < total)
        self.clear_btn.Enable(selected > 0)

    def _announce_count(self):
        parent = self.GetParent()
        frame = getattr(parent, "frame", None)
        if frame is not None:
            frame.announce(self.count_text.GetLabel() + ".")

    def _on_context_menu(self, event):
        selected = sum(self.item_list.IsItemChecked(row)
                       for row in range(len(self.items)))
        menu = wx.Menu()
        download_item = menu.Append(wx.ID_OK, "Download selected")
        menu.Enable(download_item.GetId(), selected > 0)
        menu.AppendSeparator()
        select_all_item = menu.Append(wx.ID_ANY, "Select all")
        menu.Enable(select_all_item.GetId(), selected < len(self.items))
        clear_item = menu.Append(wx.ID_ANY, "Clear selection")
        menu.Enable(clear_item.GetId(), selected > 0)
        menu.Bind(wx.EVT_MENU, lambda _event: self.EndModal(wx.ID_OK),
                  download_item)
        menu.Bind(wx.EVT_MENU, self.on_select_all, select_all_item)
        menu.Bind(wx.EVT_MENU, self.on_clear_selection, clear_item)
        self.item_list.PopupMenu(menu)
        menu.Destroy()
