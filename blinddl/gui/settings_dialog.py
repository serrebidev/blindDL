# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Settings dialog."""

import wx

AUDIO_FORMATS = ["mp3", "m4a", "flac", "wav", "opus"]


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, config):
        super().__init__(parent, title="Settings")
        self.config = config

        sizer = wx.BoxSizer(wx.VERTICAL)

        dir_label = wx.StaticText(self, label="&Download folder:")
        self.dir_picker = wx.DirPickerCtrl(
            self, path=config["download_dir"],
            message="Choose download folder")
        self.dir_picker.SetName("Download folder")

        self.audio_only_check = wx.CheckBox(
            self, label="Download &audio only")
        self.audio_only_check.SetValue(bool(config["audio_only"]))

        fmt_label = wx.StaticText(self, label="Audio f&ormat:")
        self.format_choice = wx.Choice(self, choices=AUDIO_FORMATS)
        self.format_choice.SetName("Audio format")
        if config["audio_format"] in AUDIO_FORMATS:
            self.format_choice.SetSelection(
                AUDIO_FORMATS.index(config["audio_format"]))
        else:
            self.format_choice.SetSelection(0)

        conc_label = wx.StaticText(self, label="&Concurrent downloads:")
        self.conc_spin = wx.SpinCtrl(self, min=1, max=32,
                                     initial=int(config["max_concurrent"]))
        self.conc_spin.SetName("Concurrent downloads")

        search_label = wx.StaticText(
            self, label="Search &timeout per site (seconds):")
        self.search_spin = wx.SpinCtrl(self, min=1, max=120,
                                       initial=int(config["search_timeout_s"]))
        self.search_spin.SetName("Search timeout per site in seconds")

        sub_label = wx.StaticText(self, label="Subscription &interval (hours):")
        self.sub_spin = wx.SpinCtrl(self, min=1, max=168,
                                    initial=int(config["sub_check_hours"]))
        self.sub_spin.SetName("Subscription interval in hours")

        self.update_check = wx.CheckBox(
            self, label="&Update download tools automatically")
        self.update_check.SetValue(bool(config["auto_update"]))

        self.lyrics_check = wx.CheckBox(
            self, label="Embed synced Deezer &lyrics")
        self.lyrics_check.SetValue(bool(config["sideb_lyrics"]))

        arl_label = wx.StaticText(
            self, label="Deezer A&RL cookie:")
        self.arl_text = wx.TextCtrl(self, value=config["deezer_arl"],
                                    style=wx.TE_PASSWORD)
        self.arl_text.SetName("Deezer ARL cookie")

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        def row(label_ctrl, ctrl):
            box = wx.BoxSizer(wx.HORIZONTAL)
            box.Add(label_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
            box.Add(ctrl, 1)
            return box

        sizer.Add(dir_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.dir_picker, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.audio_only_check, 0, wx.ALL, 8)
        sizer.Add(row(fmt_label, self.format_choice), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(row(conc_label, self.conc_spin), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(row(search_label, self.search_spin), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(row(sub_label, self.sub_spin), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.update_check, 0, wx.ALL, 8)
        sizer.Add(self.lyrics_check, 0, wx.ALL, 8)
        sizer.Add(row(arl_label, self.arl_text), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)

    def apply(self):
        """Write the dialog values back into the config object."""
        self.config["download_dir"] = self.dir_picker.GetPath()
        self.config["audio_only"] = self.audio_only_check.GetValue()
        self.config["audio_format"] = self.format_choice.GetStringSelection()
        self.config["max_concurrent"] = self.conc_spin.GetValue()
        self.config["search_timeout_s"] = self.search_spin.GetValue()
        self.config["sub_check_hours"] = self.sub_spin.GetValue()
        self.config["auto_update"] = self.update_check.GetValue()
        self.config["sideb_lyrics"] = self.lyrics_check.GetValue()
        self.config["deezer_arl"] = self.arl_text.GetValue().strip()
        self.config.save()
