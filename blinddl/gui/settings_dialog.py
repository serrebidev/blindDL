# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Settings dialog."""

import wx

AUDIO_FORMATS = ["mp3", "m4a", "flac", "wav", "opus"]
BROWSER_COOKIE_CHOICES = [
    ("None", ""),
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Firefox", "firefox"),
    ("Brave", "brave"),
    ("Opera", "opera"),
    ("Vivaldi", "vivaldi"),
]


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

        self.adult_sites_check = wx.CheckBox(
            self, label="Enable &adult sites")
        self.adult_sites_check.SetValue(bool(config["adult_sites_enabled"]))
        self.adult_sites_check.SetHelpText(
            "Enables adult-site search results and adult URL downloads.")

        cookies_label = wx.StaticText(
            self, label="Use cookies from &browser:")
        self.cookies_choice = wx.Choice(
            self, choices=[label for label, _value in BROWSER_COOKIE_CHOICES])
        self.cookies_choice.SetName("Browser cookies")
        browser_value = config["cookies_from_browser"]
        browser_values = [value for _label, value in BROWSER_COOKIE_CHOICES]
        self.cookies_choice.SetSelection(
            browser_values.index(browser_value)
            if browser_value in browser_values else 0)
        self.cookies_choice.SetHelpText(
            "Lets yt-dlp read an existing signed-in browser profile when a "
            "site requires login.")

        onlyfans_label = wx.StaticText(
            self, label="OnlyFans auth &JSON file:")
        self.onlyfans_auth_picker = wx.FilePickerCtrl(
            self, path=config["onlyfans_auth_file"],
            message="Choose an ofd-compatible OnlyFans auth JSON file",
            wildcard="JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_USE_TEXTCTRL,
        )
        self.onlyfans_auth_picker.SetName("OnlyFans auth JSON file")

        justforfans_label = wx.StaticText(
            self, label="JustForFans auth &JSON file:")
        self.justforfans_auth_picker = wx.FilePickerCtrl(
            self, path=config["justforfans_auth_file"],
            message="Choose a JustForFans auth JSON file",
            wildcard="JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_USE_TEXTCTRL,
        )
        self.justforfans_auth_picker.SetName("JustForFans auth JSON file")

        def enable_adult_auth(_event=None):
            enabled = self.adult_sites_check.GetValue()
            self.onlyfans_auth_picker.Enable(enabled)
            self.justforfans_auth_picker.Enable(enabled)

        self.adult_sites_check.Bind(wx.EVT_CHECKBOX, enable_adult_auth)
        enable_adult_auth()

        arl_label = wx.StaticText(
            self, label="Deezer A&RL cookie:")
        self.arl_text = wx.TextCtrl(self, value=config["deezer_arl"],
                                    style=wx.TE_PASSWORD)
        self.arl_text.SetName("Deezer ARL cookie")

        annas_label = wx.StaticText(
            self, label="Anna's Archive &membership key:")
        self.annas_text = wx.TextCtrl(self, value=config["annas_archive_key"],
                                      style=wx.TE_PASSWORD)
        self.annas_text.SetName("Anna's Archive membership key")
        self.annas_text.SetHelpText(
            "Optional. With a key, book downloads use the fast partner "
            "servers; without one they come from the public LibGen mirrors.")

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
        sizer.Add(row(cookies_label, self.cookies_choice),
                  0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.adult_sites_check, 0, wx.ALL, 8)
        sizer.Add(onlyfans_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.onlyfans_auth_picker, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(justforfans_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.justforfans_auth_picker, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(row(arl_label, self.arl_text), 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(row(annas_label, self.annas_text), 0, wx.EXPAND | wx.ALL, 8)
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
        self.config["adult_sites_enabled"] = self.adult_sites_check.GetValue()
        self.config["cookies_from_browser"] = BROWSER_COOKIE_CHOICES[
            self.cookies_choice.GetSelection()][1]
        self.config["onlyfans_auth_file"] = (
            self.onlyfans_auth_picker.GetPath().strip())
        self.config["justforfans_auth_file"] = (
            self.justforfans_auth_picker.GetPath().strip())
        self.config["deezer_arl"] = self.arl_text.GetValue().strip()
        self.config["annas_archive_key"] = self.annas_text.GetValue().strip()
        self.config.save()
