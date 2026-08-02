# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Pick which music and adult sites a search covers.

Every site musicdl registers is listed, all of them on by default. The
config stores the switched-off ones, so sites added by a later musicdl
update arrive switched on.
"""

import wx

from .. import adult_backend, musicdl_backend

NEEDS_ACCOUNT = " (account required)"
UNAVAILABLE = " (provider unavailable)"


class SourcesDialog(wx.Dialog):
    def __init__(self, parent, config):
        super().__init__(parent, title="Search sites")
        self.config = config
        self.sources = musicdl_backend.sources_by_label()
        unavailable = musicdl_backend.unavailable_sources()
        self.straight_adult_sources = adult_backend.sources_by_label(
            adult_backend.AUDIENCE_STRAIGHT)
        self.lgbtq_adult_sources = adult_backend.sources_by_label(
            adult_backend.AUDIENCE_LGBTQ)
        self.adult_sources = (
            self.straight_adult_sources + self.lgbtq_adult_sources)
        adult_unavailable = adult_backend.unavailable_sources()

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
        self.check_list.Bind(
            wx.EVT_CONTEXT_MENU,
            lambda event: self.on_context_menu(event, self.check_list),
        )
        if labels:
            self.check_list.SetSelection(0)

        disabled_adult = set(config["disabled_adult_sources"])
        self.straight_adult_check_list = self._create_adult_list(
            self.straight_adult_sources, adult_unavailable, disabled_adult,
            "Straight adult sites",
        )
        self.lgbtq_adult_check_list = self._create_adult_list(
            self.lgbtq_adult_sources, adult_unavailable, disabled_adult,
            "LGBTQ+ adult sites",
        )

        self.count_text = wx.StaticText(self, label="")
        self._update_count()

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(list_label, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.check_list, 1, wx.EXPAND | wx.ALL, 8)
        straight_adult_label = wx.StaticText(
            self, label="&Straight adult sites:")
        sizer.Add(straight_adult_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.straight_adult_check_list, 1, wx.EXPAND | wx.ALL, 8)
        lgbtq_adult_label = wx.StaticText(
            self, label="&LGBTQ+ adult sites:")
        sizer.Add(lgbtq_adult_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.lgbtq_adult_check_list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.count_text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.SetSize((560, 700))
        self.check_list.SetFocus()

    # -- helpers -------------------------------------------------------------

    def _create_adult_list(self, sources, unavailable, disabled, name):
        labels = [
            adult_backend.source_label(source)
            + (UNAVAILABLE if source in unavailable else "")
            for source in sources
        ]
        control = wx.CheckListBox(self, choices=labels)
        control.SetName(name)
        control.SetHelpText(
            "Space toggles a site. Unavailable providers indicate an "
            "incomplete installation.")
        control.Enable(bool(self.config["adult_sites_enabled"]))
        for index, source in enumerate(sources):
            control.Check(index, source not in disabled)
        control.Bind(wx.EVT_CHECKLISTBOX, self.on_toggle)
        control.Bind(
            wx.EVT_CONTEXT_MENU,
            lambda event: self.on_context_menu(event, control),
        )
        if labels:
            control.SetSelection(0)
        return control

    def _checked_count(self):
        return sum(1 for i in range(len(self.sources))
                   if self.check_list.IsChecked(i))

    @staticmethod
    def _list_checked_count(control):
        return sum(1 for index in range(control.GetCount())
                   if control.IsChecked(index))

    def _straight_adult_checked_count(self):
        return self._list_checked_count(self.straight_adult_check_list)

    def _lgbtq_adult_checked_count(self):
        return self._list_checked_count(self.lgbtq_adult_check_list)

    def _adult_checked_count(self):
        return (self._straight_adult_checked_count()
                + self._lgbtq_adult_checked_count())

    def _update_count(self):
        self.count_text.SetLabel(
            f"{self._checked_count()} of {len(self.sources)} music and "
            f"{self._straight_adult_checked_count()} of "
            f"{len(self.straight_adult_sources)} straight adult and "
            f"{self._lgbtq_adult_checked_count()} of "
            f"{len(self.lgbtq_adult_sources)} LGBTQ+ adult sites selected.")

    def _set_all(self, control, checked):
        for index in range(control.GetCount()):
            control.Check(index, checked)
        self._update_count()
        control.SetFocus()

    def on_toggle(self, event):
        self._update_count()
        event.Skip()

    def on_context_menu(self, event, control):
        menu = wx.Menu()
        all_item = menu.Append(wx.ID_SELECTALL, "Select &all")
        clear_item = menu.Append(wx.ID_ANY, "&Clear all")
        menu.Bind(wx.EVT_MENU, lambda e: self._set_all(control, True), all_item)
        menu.Bind(wx.EVT_MENU, lambda e: self._set_all(control, False), clear_item)
        control.PopupMenu(menu)
        menu.Destroy()

    # -- result --------------------------------------------------------------

    def apply(self):
        """Write the selection back into the config object."""
        self.config["disabled_music_sources"] = [
            source for index, source in enumerate(self.sources)
            if not self.check_list.IsChecked(index)]
        self.config["disabled_adult_sources"] = [
            source for sources, control in (
                (self.straight_adult_sources,
                 self.straight_adult_check_list),
                (self.lgbtq_adult_sources, self.lgbtq_adult_check_list),
            )
            for index, source in enumerate(sources)
            if not control.IsChecked(index)
        ]
        self.config.save()

    def summary(self):
        return (
            f"{self._checked_count()} music and "
            f"{self._straight_adult_checked_count()} straight adult and "
            f"{self._lgbtq_adult_checked_count()} LGBTQ+ adult sites selected."
        )
