# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Choose one exact stream from everything a link offers."""

import wx


class FormatDialog(wx.Dialog):
    """Pick one of a link's streams, named in words rather than columns.

    A stream table is the one part of a downloader that is pure layout: a
    grid of numbers, sorted by nothing anybody asked for, and the reason
    someone reaches for a mouse. Here every stream is one sentence on one
    line -- what it holds, how fast it runs and how big it is -- so it
    reads out as what it is, and the best is offered first because that is
    the one usually wanted.
    """

    def __init__(self, parent, title, choices):
        super().__init__(
            parent,
            title="Choose an exact stream",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.choices = list(choices)
        prompt = wx.StaticText(self, label=f"Streams in {title}.")
        self.stream_list = wx.ListBox(
            self, choices=[choice["label"] for choice in self.choices])
        self.stream_list.SetName("Streams to download")
        self.stream_list.SetHelpText(
            "Every stream this link offers, best first. Enter downloads the "
            "one you are on exactly as the site serves it, with nothing "
            "converted; Escape cancels."
        )
        if self.choices:
            self.stream_list.SetSelection(0)
        self.stream_list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_choose)

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, 8)
        sizer.Add(self.stream_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((700, 420))
        self.stream_list.SetFocus()

    def on_choose(self, event):
        if self.selected() is not None:
            self.EndModal(wx.ID_OK)

    def selected(self):
        """The stream being read, or None when the list is empty."""
        index = self.stream_list.GetSelection()
        if 0 <= index < len(self.choices):
            return self.choices[index]
        return None
