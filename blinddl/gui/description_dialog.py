# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Audio description for a download: find the one that exists, or make one."""

import wx

# What the dialog decided to do with the download it was opened for.
ACTION_DOWNLOAD = "download"
ACTION_PLAY = "play"
ACTION_MAKE = "make"


class DescriptionDialog(wx.Dialog):
    """Offer what was found for a download, and the last resort.

    Described versions are published as their own videos rather than as
    extra tracks, so a search is what finds them -- and a search for a
    title turns up plenty that is not one, which is why the candidates are
    listed for a person to choose from rather than picked automatically.
    Making one is offered here too, named as what it is: a reading of the
    picture by a machine, unchecked, and only worth having when nobody has
    described the video already.
    """

    def __init__(self, parent, title, candidates, can_make=True):
        super().__init__(
            parent,
            title="Audio description",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.candidates = list(candidates)
        self.action = None
        found = bool(self.candidates)
        prompt = wx.StaticText(self, label=(
            f"Published descriptions of {title}." if found else
            f"Nothing published describes {title}."))

        self.version_list = wx.ListBox(
            self, choices=[self._label(candidate)
                           for candidate in self.candidates])
        self.version_list.SetName("Published described versions")
        self.version_list.SetHelpText(
            "Every described version the search turned up that shares a "
            "word with the title. Enter downloads the one you are on."
        )
        if found:
            self.version_list.SetSelection(0)

        self.download_btn = wx.Button(
            self, label="&Download the described version")
        self.download_btn.SetHelpText(
            "Adds the described version to the download queue, exactly as "
            "the ordinary download would be.")
        self.download_btn.Bind(wx.EVT_BUTTON, lambda event: self._choose(
            ACTION_DOWNLOAD))
        self.play_btn = wx.Button(self, label="&Play it")
        self.play_btn.SetHelpText(
            "Plays the described version in the URL tab rather than "
            "downloading it, to hear what it sounds like first.")
        self.play_btn.Bind(wx.EVT_BUTTON, lambda event: self._choose(
            ACTION_PLAY))
        self.make_btn = wx.Button(self, label="&Make one with Claude")
        self.make_btn.SetHelpText(
            "Reads the picture at each cut and writes down what it sees. It "
            "is machine-made, it is not checked by anyone, and it is the "
            "only option for a video nobody has described.")
        self.make_btn.Bind(wx.EVT_BUTTON, lambda event: self._choose(
            ACTION_MAKE))
        close_btn = wx.Button(self, wx.ID_CANCEL, "&Close")

        for button in (self.download_btn, self.play_btn):
            button.Enable(found)
        self.make_btn.Enable(bool(can_make))

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.download_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.make_btn, 0, wx.RIGHT, 8)
        buttons.Add(close_btn, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, 8)
        sizer.Add(self.version_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(buttons, 0, wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetSize((700, 380))
        if found:
            self.download_btn.SetDefault()
            self.version_list.SetFocus()
        else:
            close_btn.SetDefault()
            self.make_btn.SetFocus()

    @staticmethod
    def _label(candidate):
        parts = [str(candidate.get("title") or "Unknown title")]
        uploader = str(candidate.get("uploader") or "").strip()
        if uploader:
            parts.append(uploader)
        duration = candidate.get("duration")
        if duration:
            minutes, seconds = divmod(int(duration), 60)
            parts.append(f"{minutes}:{seconds:02d}")
        return ", ".join(parts)

    def selected(self):
        """The candidate being read, or None."""
        index = self.version_list.GetSelection()
        if 0 <= index < len(self.candidates):
            return self.candidates[index]
        return None

    def _choose(self, action):
        self.action = action
        self.EndModal(wx.ID_OK)
