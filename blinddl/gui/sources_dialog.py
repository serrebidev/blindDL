# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Pick which music, book, audiobook and adult sites a search covers.

Every site musicdl registers is listed, all of them on by default. The
config stores the switched-off ones, so sites added by a later musicdl
update arrive switched on.
"""

import wx

from .. import (
    adult_backend, archive_backend, audiobook_backend, book_backend,
    musicdl_backend,
)

NEEDS_ACCOUNT = " (account required)"
UNAVAILABLE = " (provider unavailable)"


class SourcesDialog(wx.Dialog):
    def __init__(self, parent, config):
        super().__init__(parent, title="Search sites")
        self.config = config
        self.sources = musicdl_backend.sources_by_label()
        unavailable = musicdl_backend.unavailable_sources()
        self.adult_sources = adult_backend.sources_by_label()
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

        self.book_sources = book_backend.sources_by_label()
        self.book_check_list = wx.CheckListBox(
            self,
            choices=[book_backend.source_label(source)
                     for source in self.book_sources],
        )
        self.book_check_list.SetName("Book libraries")
        self.book_check_list.SetHelpText(
            "Space toggles a library. Context Menu selects or clears all.")
        disabled_books = set(config["disabled_book_sources"])
        for index, source in enumerate(self.book_sources):
            self.book_check_list.Check(index, source not in disabled_books)
        self.book_check_list.Bind(wx.EVT_CHECKLISTBOX, self.on_toggle)
        self.book_check_list.Bind(
            wx.EVT_CONTEXT_MENU,
            lambda event: self.on_context_menu(event, self.book_check_list),
        )
        if self.book_sources:
            self.book_check_list.SetSelection(0)

        # Sites whose package is not installed are not listed at all, so
        # nothing here can be checked and then quietly do nothing.
        self.audiobook_sources = audiobook_backend.sources_by_label()
        self.audiobook_check_list = wx.CheckListBox(
            self,
            choices=[audiobook_backend.source_label(source)
                     for source in self.audiobook_sources],
        )
        self.audiobook_check_list.SetName("Audiobook sites")
        self.audiobook_check_list.SetHelpText(
            "Space toggles a site. Context Menu selects or clears all.")
        disabled_audiobooks = set(config["disabled_audiobook_sources"])
        for index, source in enumerate(self.audiobook_sources):
            self.audiobook_check_list.Check(
                index, source not in disabled_audiobooks)
        self.audiobook_check_list.Bind(wx.EVT_CHECKLISTBOX, self.on_toggle)
        self.audiobook_check_list.Bind(
            wx.EVT_CONTEXT_MENU,
            lambda event: self.on_context_menu(event,
                                               self.audiobook_check_list),
        )
        if self.audiobook_sources:
            self.audiobook_check_list.SetSelection(0)

        self.archive_sources = archive_backend.sources_by_label()
        self.archive_check_list = wx.CheckListBox(
            self,
            choices=[archive_backend.source_label(source)
                     for source in self.archive_sources],
        )
        self.archive_check_list.SetName("Internet Archive collections")
        self.archive_check_list.SetHelpText(
            "Space toggles a collection. Context Menu selects or clears all.")
        disabled_archive = set(config["disabled_archive_sources"])
        for index, source in enumerate(self.archive_sources):
            self.archive_check_list.Check(index, source not in disabled_archive)
        self.archive_check_list.Bind(wx.EVT_CHECKLISTBOX, self.on_toggle)
        self.archive_check_list.Bind(
            wx.EVT_CONTEXT_MENU,
            lambda event: self.on_context_menu(event, self.archive_check_list),
        )
        if self.archive_sources:
            self.archive_check_list.SetSelection(0)

        disabled_adult = set(config["disabled_adult_sources"])
        self.adult_check_list = self._create_adult_list(
            self.adult_sources, adult_unavailable, disabled_adult,
            "Adult sites")

        self.count_text = wx.StaticText(self, label="")
        self._update_count()

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(list_label, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.check_list, 1, wx.EXPAND | wx.ALL, 8)
        book_label = wx.StaticText(self, label="&Book libraries:")
        sizer.Add(book_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.book_check_list, 1, wx.EXPAND | wx.ALL, 8)
        audiobook_label = wx.StaticText(self, label="A&udiobook sites:")
        sizer.Add(audiobook_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.audiobook_check_list, 1, wx.EXPAND | wx.ALL, 8)
        archive_label = wx.StaticText(
            self, label="&Internet Archive collections:")
        sizer.Add(archive_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.archive_check_list, 1, wx.EXPAND | wx.ALL, 8)
        adult_label = wx.StaticText(self, label="&Adult sites:")
        sizer.Add(adult_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.adult_check_list, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.count_text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.SetSize((600, 760))
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

    def _adult_checked_count(self):
        return sum(1 for index in range(len(self.adult_sources))
                   if self.adult_check_list.IsChecked(index))

    def _book_checked_count(self):
        return sum(1 for index in range(len(self.book_sources))
                   if self.book_check_list.IsChecked(index))

    def _audiobook_checked_count(self):
        return sum(1 for index in range(len(self.audiobook_sources))
                   if self.audiobook_check_list.IsChecked(index))

    def _archive_checked_count(self):
        return sum(1 for index in range(len(self.archive_sources))
                   if self.archive_check_list.IsChecked(index))

    def _update_count(self):
        self.count_text.SetLabel(
            f"{self._checked_count()} of {len(self.sources)} music sites, "
            f"{self._book_checked_count()} of {len(self.book_sources)} book "
            f"libraries, {self._audiobook_checked_count()} of "
            f"{len(self.audiobook_sources)} audiobook sites, "
            f"{self._archive_checked_count()} of {len(self.archive_sources)} "
            f"Archive collections and {self._adult_checked_count()} of "
            f"{len(self.adult_sources)} adult sites selected.")

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
            source for index, source in enumerate(self.adult_sources)
            if not self.adult_check_list.IsChecked(index)]
        self.config["disabled_book_sources"] = [
            source for index, source in enumerate(self.book_sources)
            if not self.book_check_list.IsChecked(index)]
        self.config["disabled_audiobook_sources"] = [
            source for index, source in enumerate(self.audiobook_sources)
            if not self.audiobook_check_list.IsChecked(index)]
        self.config["disabled_archive_sources"] = [
            source for index, source in enumerate(self.archive_sources)
            if not self.archive_check_list.IsChecked(index)]
        self.config.save()

    def summary(self):
        return (
            f"{self._checked_count()} music sites, "
            f"{self._book_checked_count()} book libraries, "
            f"{self._audiobook_checked_count()} audiobook sites, "
            f"{self._archive_checked_count()} Archive collections and "
            f"{self._adult_checked_count()} adult sites selected."
        )
