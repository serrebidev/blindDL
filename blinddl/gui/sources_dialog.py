# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Pick which music sites a search covers.

Every site musicdl registers is listed, all of them on by default. The
config stores the switched-off ones, so sites added by a later musicdl
update arrive switched on.
"""

import wx

from .. import musicdl_backend

NEEDS_ACCOUNT = " (account required)"


class SourcesDialog(wx.Dialog):
    def __init__(self, parent, config):
        super().__init__(parent, title="Search sites")
        self.config = config
        self.sources = musicdl_backend.sources_by_label()
        unavailable = musicdl_backend.unavailable_sources()

        labels = [musicdl_backend.source_label(s)
                  + (NEEDS_ACCOUNT if s in unavailable else "")
                  for s in self.sources]

        list_label = wx.StaticText(self, label="&Sites:")
        self.check_list = wx.CheckListBox(self, choices=labels)
        self.check_list.SetName("Music sites")
        self.check_list.SetHelpText(
            "Space toggles a site. Context Menu selects or clears all.")
        disabled = set(config["disabled_music_sources"])
        for index, source in enumerate(self.sources):
            self.check_list.Check(index, source not in disabled)
        self.check_list.Bind(wx.EVT_CHECKLISTBOX, self.on_toggle)
        self.check_list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
        if labels:
            self.check_list.SetSelection(0)

        self.count_text = wx.StaticText(self, label="")
        self._update_count()

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(list_label, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.check_list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.count_text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.SetSize((520, 560))
        self.check_list.SetFocus()

    # -- helpers -------------------------------------------------------------

    def _checked_count(self):
        return sum(1 for i in range(len(self.sources))
                   if self.check_list.IsChecked(i))

    def _update_count(self):
        self.count_text.SetLabel(
            f"{self._checked_count()} of {len(self.sources)} selected.")

    def _set_all(self, checked):
        for index in range(len(self.sources)):
            self.check_list.Check(index, checked)
        self._update_count()
        self.check_list.SetFocus()

    def on_toggle(self, event):
        self._update_count()
        event.Skip()

    def on_context_menu(self, event):
        menu = wx.Menu()
        all_item = menu.Append(wx.ID_SELECTALL, "Select &all")
        clear_item = menu.Append(wx.ID_ANY, "&Clear all")
        menu.Bind(wx.EVT_MENU, lambda e: self._set_all(True), all_item)
        menu.Bind(wx.EVT_MENU, lambda e: self._set_all(False), clear_item)
        self.check_list.PopupMenu(menu)
        menu.Destroy()

    # -- result --------------------------------------------------------------

    def apply(self):
        """Write the selection back into the config object."""
        self.config["disabled_music_sources"] = [
            source for index, source in enumerate(self.sources)
            if not self.check_list.IsChecked(index)]
        self.config.save()

    def summary(self):
        return f"{self._checked_count()} of {len(self.sources)} sites selected."
