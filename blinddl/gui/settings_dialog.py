# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Settings dialog.

The settings are grouped onto notebook pages rather than run down one long
column: Ctrl+Tab moves between them, and each page is short enough to read
end to end without losing your place.
"""

import sys
import threading

import wx

from .. import soulseek_backend, torrent_engine

# Label, stored value. "Original" means the file is kept exactly as the site
# serves it: no ffmpeg pass, no quality lost, whatever container comes down.
AUDIO_FORMAT_CHOICES = [
    ("Original (no conversion)", "original"),
    ("MP3", "mp3"),
    ("M4A", "m4a"),
    ("FLAC", "flac"),
    ("WAV", "wav"),
    ("Opus", "opus"),
]
# MP4 and MKV are remuxes, so they keep the site's own picture and sound.
# The last two re-encode: AVI because it cannot hold modern codecs at all,
# x265 because shrinking the file is the whole point of it.
VIDEO_FORMAT_CHOICES = [
    ("Original (no conversion)", "original"),
    ("MP4", "mp4"),
    ("MKV", "mkv"),
    ("AVI", "avi"),
    ("Small, x265 for long-term storage", "x265"),
]
# Kept for callers that only need the stored values.
AUDIO_FORMATS = [value for _label, value in AUDIO_FORMAT_CHOICES]
VIDEO_FORMATS = [value for _label, value in VIDEO_FORMAT_CHOICES]
# Apple Music output formats. M4A is Apple's own AAC, which the download
# produces directly; the MP3 options re-encode it afterwards with blindDL's
# ffmpeg. V0 and V2 are LAME's VBR quality settings, 320k a constant bitrate.
APPLE_FORMAT_CHOICES = [
    ("M4A (AAC, original)", "m4a"),
    ("MP3 V0 (~245 kbps)", "mp3_v0"),
    ("MP3 V2 (~190 kbps)", "mp3_v2"),
    ("MP3 320 kbps", "mp3_320"),
]
# Deezer (ARL-backed) output formats. FLAC is Deezer's own lossless master;
# MP3 320 is what free accounts and most tracks top out at.
DEEZER_FORMAT_CHOICES = [
    ("FLAC (lossless, best quality)", "flac"),
    ("MP3 320 kbps", "mp3_320"),
]
BROWSER_COOKIE_CHOICES = [
    ("None", ""),
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Firefox", "firefox"),
    ("Brave", "brave"),
    ("Opera", "opera"),
    ("Vivaldi", "vivaldi"),
]
ENCRYPTION_CHOICES = torrent_engine.ENCRYPTION_CHOICES


def _name_picker(picker, name):
    """Name a folder picker and the edit box inside it.

    A picker is a text box next to a Browse button, and only the picker as a
    whole carries the name given to it. Without this the edit box the focus
    actually lands in reads as an unlabelled one.
    """
    picker.SetName(name)
    text = picker.GetTextCtrl() if hasattr(picker, "GetTextCtrl") else None
    if text is not None:
        text.SetName(name)
    return picker


def _row(sizer, label_ctrl, ctrl):
    box = wx.BoxSizer(wx.HORIZONTAL)
    box.Add(label_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
    box.Add(ctrl, 1)
    sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, config):
        super().__init__(parent, title="Settings")
        self.config = config
        # The main window, so handlers can announce results on its status bar.
        self.frame = parent

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self)
        self.notebook.SetName("Settings pages")
        self.notebook.AddPage(self._downloads_page(), "Downloads")
        self.notebook.AddPage(self._torrents_page(), "Torrents")
        self.notebook.AddPage(self._soulseek_page(), "Soulseek")
        self.notebook.AddPage(self._window_page(), "Window")
        self.notebook.AddPage(self._accounts_page(), "Accounts")

        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(
            self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
            0,
            wx.ALL | wx.ALIGN_RIGHT,
            8,
        )
        self.SetSizerAndFit(sizer)
        # A dialog with a default button hands it the focus on open, so
        # Settings used to start on OK -- past every setting in it. Start on
        # the first control of the first page instead, so the dialog reads
        # from the top and Tab moves forward through it rather than wrapping
        # round. wxEVT_INIT_DIALOG arrives from ShowModal, after the default
        # button has taken focus, which is the point at which taking it back
        # sticks.
        self.notebook.SetSelection(0)
        self.Bind(wx.EVT_INIT_DIALOG, self._on_init_dialog)
        self.focus_first_control()

    def _on_init_dialog(self, event):
        event.Skip()
        self.focus_first_control()

    def first_control(self):
        """The control Settings opens on: the download folder box.

        A picker is an edit box next to a Browse button, and the edit box is
        what makes the path readable and typable straight away -- focusing
        the picker itself would land on the button. wxGTK can still build a
        picker without one, in which case the picker is all there is to
        focus, so this never assumes the box exists.
        """
        picker = self.dir_picker
        text = picker.GetTextCtrl() if hasattr(picker, "GetTextCtrl") else None
        return text or picker

    def focus_first_control(self):
        self.first_control().SetFocus()

    # -- pages ---------------------------------------------------------------

    def _choice(self, parent, choices, current, name):
        control = wx.Choice(parent, choices=[label for label, _v in choices])
        control.SetName(name)
        values = [value for _label, value in choices]
        control.SetSelection(values.index(current) if current in values else 0)
        return control

    def _heading(self, parent, label):
        heading = wx.StaticText(parent, label=label)
        heading.SetName(label)
        font = heading.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        return heading

    def _downloads_page(self):
        page = wx.Panel(self.notebook)
        page.SetName("Downloads settings")
        sizer = wx.BoxSizer(wx.VERTICAL)
        config = self.config

        dir_label = wx.StaticText(page, label="&Download folder:")
        # The style is spelled out because wxGTK leaves the edit box out of
        # its default: the download folder would be a bare Browse button
        # there, with no way to read or type the path, while Windows and
        # macOS get a box. The Torrents page asks for it the same way.
        self.dir_picker = _name_picker(
            wx.DirPickerCtrl(
                page,
                path=config["download_dir"],
                message="Choose download folder",
                style=wx.DIRP_USE_TEXTCTRL | wx.DIRP_DIR_MUST_EXIST,
            ),
            "Download folder",
        )

        self.audio_only_check = wx.CheckBox(page, label="Download &audio only")
        self.audio_only_check.SetValue(bool(config["audio_only"]))

        fmt_label = wx.StaticText(page, label="Audio f&ormat:")
        self.format_choice = self._choice(
            page, AUDIO_FORMAT_CHOICES, config["audio_format"], "Audio format"
        )
        self.format_choice.SetHelpText(
            "Used when downloading audio only. Original keeps the site's own "
            "file untouched."
        )

        video_fmt_label = wx.StaticText(page, label="&Video format:")
        self.video_format_choice = self._choice(
            page, VIDEO_FORMAT_CHOICES, config["video_format"], "Video format"
        )
        self.video_format_choice.SetHelpText(
            "Container used when downloading video. Original keeps whatever "
            "the site serves; AVI is re-encoded, which takes longer."
        )

        self.auto_clear_check = wx.CheckBox(
            page, label="Clear &finished downloads and uploads automatically"
        )
        self.auto_clear_check.SetValue(bool(config["auto_clear_finished"]))
        self.auto_clear_check.SetHelpText(
            "Takes a download out of the Finished downloads list once it has "
            "completed, and an upload out of Finished uploads once the other "
            "side has the file. Downloads that failed or were cancelled are "
            "kept either way, so their error can still be read. Torrents that "
            "are still seeding keep their row until seeding stops."
        )

        conc_label = wx.StaticText(page, label="&Concurrent downloads:")
        self.conc_spin = wx.SpinCtrl(
            page, min=1, max=32, initial=int(config["max_concurrent"])
        )
        self.conc_spin.SetName("Concurrent downloads")
        self.conc_spin.SetHelpText(
            "More simultaneous downloads can use substantially more CPU and memory, especially when audio or video must be converted."
        )

        search_label = wx.StaticText(page, label="Search &timeout per site (seconds):")
        self.search_spin = wx.SpinCtrl(
            page, min=1, max=120, initial=int(config["search_timeout_s"])
        )
        self.search_spin.SetName("Search timeout per site in seconds")

        sub_label = wx.StaticText(page, label="Subscription &interval (hours):")
        self.sub_spin = wx.SpinCtrl(
            page, min=1, max=168, initial=int(config["sub_check_hours"])
        )
        self.sub_spin.SetName("Subscription interval in hours")

        sizer.Add(dir_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.dir_picker, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.audio_only_check, 0, wx.ALL, 8)
        sizer.Add(self.auto_clear_check, 0, wx.ALL, 8)
        _row(sizer, fmt_label, self.format_choice)
        _row(sizer, video_fmt_label, self.video_format_choice)
        _row(sizer, conc_label, self.conc_spin)
        _row(sizer, search_label, self.search_spin)
        _row(sizer, sub_label, self.sub_spin)
        page.SetSizer(sizer)
        return page

    def _torrents_page(self):
        page = wx.Panel(self.notebook)
        page.SetName("Torrent settings")
        sizer = wx.BoxSizer(wx.VERTICAL)
        config = self.config

        self.torrent_engine_check = wx.CheckBox(
            page, label="Download torrents in blind&DL"
        )
        self.torrent_engine_check.SetValue(bool(config["torrent_engine"]))
        self.torrent_engine_check.SetHelpText(
            "On: blindDL downloads the torrent itself and shows progress in "
            "the Downloads tab. Off: the magnet opens in your own BitTorrent "
            "client, as it always has."
        )

        self.engine_status = wx.StaticText(page, label=self._engine_status())

        torrent_dir_label = wx.StaticText(
            page, label="Torrent download &folder (blank = same as downloads):"
        )
        self.torrent_dir_picker = _name_picker(
            wx.DirPickerCtrl(
                page,
                path=config["torrent_dir"],
                message="Choose the folder torrents download into",
                style=wx.DIRP_USE_TEXTCTRL | wx.DIRP_DIR_MUST_EXIST,
            ),
            "Torrent download folder",
        )

        down_label = wx.StaticText(
            page, label="Download &limit, KB per second (0 = unlimited):"
        )
        self.torrent_down_spin = wx.SpinCtrl(
            page, min=0, max=1000000, initial=int(config["torrent_max_down_kib"])
        )
        self.torrent_down_spin.SetName("Torrent download limit in kilobytes per second")

        up_label = wx.StaticText(
            page, label="&Upload limit, KB per second (0 = unlimited):"
        )
        self.torrent_up_spin = wx.SpinCtrl(
            page, min=0, max=1000000, initial=int(config["torrent_max_up_kib"])
        )
        self.torrent_up_spin.SetName("Torrent upload limit in kilobytes per second")

        active_label = wx.StaticText(page, label="Torrents downloading at &once:")
        self.torrent_active_spin = wx.SpinCtrl(
            page, min=1, max=50, initial=int(config["torrent_max_active"])
        )
        self.torrent_active_spin.SetName("Torrents downloading at once")

        conn_label = wx.StaticText(page, label="Peer connectio&n limit:")
        self.torrent_conn_spin = wx.SpinCtrl(
            page, min=10, max=5000, initial=int(config["torrent_max_connections"])
        )
        self.torrent_conn_spin.SetName("Peer connection limit")

        ratio_label = wx.StaticText(
            page, label="Stop seeding at &ratio (0 = keep seeding):"
        )
        self.torrent_ratio_text = wx.TextCtrl(
            page, value=f"{float(config['torrent_seed_ratio']):g}"
        )
        self.torrent_ratio_text.SetName("Stop seeding at ratio")
        self.torrent_ratio_text.SetHelpText(
            "A ratio of 2 uploads twice what was downloaded, then stops. "
            "0 keeps seeding until blindDL exits."
        )

        minutes_label = wx.StaticText(
            page, label="Stop seeding after &minutes (0 = no time limit):"
        )
        self.torrent_minutes_spin = wx.SpinCtrl(
            page, min=0, max=100000, initial=int(config["torrent_seed_minutes"])
        )
        self.torrent_minutes_spin.SetName("Stop seeding after minutes")

        port_label = wx.StaticText(
            page, label="Incoming &port (0 = pick one at random):"
        )
        self.torrent_port_spin = wx.SpinCtrl(
            page, min=0, max=65535, initial=int(config["torrent_port"])
        )
        self.torrent_port_spin.SetName("Incoming port")

        enc_label = wx.StaticText(page, label="&Encryption:")
        self.torrent_enc_choice = self._choice(
            page, ENCRYPTION_CHOICES, config["torrent_encryption"], "Encryption"
        )
        self.torrent_enc_choice.SetHelpText(
            "Hides the protocol from an internet provider that slows "
            "BitTorrent down. Requiring it drops peers that cannot encrypt."
        )

        self.torrent_dht_check = wx.CheckBox(
            page, label="Find peers without a trac&ker (DHT, PEX, local)"
        )
        self.torrent_dht_check.SetValue(bool(config["torrent_dht"]))
        self.torrent_dht_check.SetHelpText(
            "Private trackers turn these off for their own torrents "
            "whatever this says, so a private tracker stays private."
        )

        self.torrent_forward_check = wx.CheckBox(
            page, label="Ask the router to for&ward the port (UPnP, NAT-PMP)"
        )
        self.torrent_forward_check.SetValue(bool(config["torrent_port_forward"]))
        self.torrent_forward_check.SetHelpText(
            "Lets other peers connect to you, which is what makes a slow torrent fast."
        )

        self.torrent_sequential_check = wx.CheckBox(
            page, label="Download pieces in order, so files &play early"
        )
        self.torrent_sequential_check.SetValue(bool(config["torrent_sequential"]))
        self.torrent_sequential_check.SetHelpText(
            "Slower overall, but the start of a file arrives first."
        )

        self.torrent_delete_check = wx.CheckBox(
            page, label="Delete partly downloaded files when a torrent is cancelle&d"
        )
        self.torrent_delete_check.SetValue(bool(config["torrent_delete_partial"]))

        proxy_label = wx.StaticText(page, label="Torrent pro&xy (blank = direct):")
        self.torrent_proxy_text = wx.TextCtrl(page, value=config["torrent_proxy"])
        self.torrent_proxy_text.SetName("Torrent proxy")
        self.torrent_proxy_text.SetHelpText(
            "Something like socks5://host:1080, or "
            "socks5://user:password@host:1080. Carries peer traffic as well "
            "as tracker announces."
        )

        version_label = wx.StaticText(
            page, label="Report as qBittorrent &version (blank = newest):"
        )
        self.torrent_version_text = wx.TextCtrl(
            page, value=config["torrent_client_version"]
        )
        self.torrent_version_text.SetName("Report as qBittorrent version")
        self.torrent_version_text.SetHelpText(
            "blindDL joins swarms as the current qBittorrent release, which "
            "trackers that check the client accept. Type a version such as "
            "5.2.3 to pin one."
        )

        sizer.Add(self.torrent_engine_check, 0, wx.ALL, 8)
        sizer.Add(self.engine_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(torrent_dir_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.torrent_dir_picker, 0, wx.EXPAND | wx.ALL, 8)
        _row(sizer, down_label, self.torrent_down_spin)
        _row(sizer, up_label, self.torrent_up_spin)
        _row(sizer, active_label, self.torrent_active_spin)
        _row(sizer, conn_label, self.torrent_conn_spin)
        _row(sizer, ratio_label, self.torrent_ratio_text)
        _row(sizer, minutes_label, self.torrent_minutes_spin)
        _row(sizer, port_label, self.torrent_port_spin)
        _row(sizer, enc_label, self.torrent_enc_choice)
        sizer.Add(self.torrent_dht_check, 0, wx.ALL, 8)
        sizer.Add(self.torrent_forward_check, 0, wx.ALL, 8)
        sizer.Add(self.torrent_sequential_check, 0, wx.ALL, 8)
        sizer.Add(self.torrent_delete_check, 0, wx.ALL, 8)
        _row(sizer, proxy_label, self.torrent_proxy_text)
        _row(sizer, version_label, self.torrent_version_text)
        page.SetSizer(sizer)

        self.torrent_engine_check.Bind(wx.EVT_CHECKBOX, self._on_engine_toggle)
        self._on_engine_toggle()
        return page

    def _engine_status(self):
        """One line saying whether the engine can run, and as what."""
        if not torrent_engine.available():
            return (
                "libtorrent is not installed yet. Turn this on and "
                "blindDL will offer to install it."
            )
        try:
            major, minor, patch = torrent_engine.client_version(
                self.config, allow_network=False
            )
        except Exception:  # noqa: BLE001 - a status line must not fail
            return f"libtorrent {torrent_engine.version()} is ready."
        return (
            f"libtorrent {torrent_engine.version()} is ready. Swarms see "
            f"blindDL as qBittorrent {major}.{minor}.{patch}."
        )

    def _on_engine_toggle(self, _event=None):
        """Grey out the swarm settings when nothing in blindDL will use them."""
        enabled = self.torrent_engine_check.GetValue()
        for control in (
            self.torrent_dir_picker,
            self.torrent_down_spin,
            self.torrent_up_spin,
            self.torrent_active_spin,
            self.torrent_conn_spin,
            self.torrent_ratio_text,
            self.torrent_minutes_spin,
            self.torrent_port_spin,
            self.torrent_enc_choice,
            self.torrent_dht_check,
            self.torrent_forward_check,
            self.torrent_sequential_check,
            self.torrent_delete_check,
            self.torrent_proxy_text,
            self.torrent_version_text,
        ):
            control.Enable(enabled)

    def _soulseek_page(self):
        page = wx.ScrolledWindow(self.notebook, style=wx.VSCROLL)
        page.SetName("Soulseek settings")
        sizer = wx.BoxSizer(wx.VERTICAL)
        config = self.config

        self.soulseek_enabled_check = wx.CheckBox(
            page, label="&Enable Soulseek search, downloads, and sharing"
        )
        self.soulseek_enabled_check.SetValue(bool(config["soulseek_enabled"]))
        self.soulseek_enabled_check.SetHelpText(
            "Adds separate Soulseek audio, video, book, and torrent choices "
            "to Search and keeps the account online to upload shared files."
        )

        username_label = wx.StaticText(page, label="&Username:")
        self.soulseek_username_text = wx.TextCtrl(
            page, value=config["soulseek_username"]
        )
        self.soulseek_username_text.SetName("Soulseek username")

        password_label = wx.StaticText(page, label="&Password:")
        self.soulseek_password_text = wx.TextCtrl(
            page, value=config["soulseek_password"], style=wx.TE_PASSWORD
        )
        self.soulseek_password_text.SetName("Soulseek password")

        self.soulseek_account_button = wx.Button(page, label="Sign in or sign &up")
        self.soulseek_account_button.SetName("Soulseek sign in or sign up")
        self.soulseek_account_button.SetHelpText(
            "Signs in to an existing Soulseek account. If the username is "
            "unused, Soulseek creates a new account with this password."
        )
        self.soulseek_account_button.Bind(wx.EVT_BUTTON, self._on_soulseek_account)
        self.soulseek_account_status = wx.StaticText(
            page,
            label=(
                "Use existing credentials, or enter an unused username to "
                "create an account."
            ),
        )
        self.soulseek_account_status.SetName("Soulseek account status")

        description_label = wx.StaticText(page, label="Profile &description:")
        self.soulseek_description_text = wx.TextCtrl(
            page, value=config["soulseek_description"]
        )
        self.soulseek_description_text.SetName("Soulseek profile description")

        self.soulseek_share_library_check = wx.CheckBox(
            page, label="&Share the blindDL Library with everyone"
        )
        self.soulseek_share_library_check.SetValue(
            bool(config["soulseek_share_library"])
        )
        self.soulseek_share_library_check.SetHelpText(
            "Shares the Downloads folder shown on the Downloads settings "
            "page. New completed downloads are added automatically."
        )

        self.soulseek_block_leechers_check = wx.CheckBox(
            page, label="Refuse uploads to users who share &nothing"
        )
        self.soulseek_block_leechers_check.SetValue(
            bool(config["soulseek_block_leechers"])
        )
        self.soulseek_block_leechers_check.SetHelpText(
            "Peers whose own share is empty are told the file is not shared. "
            "Friends and free-slot priority users can always download from "
            "you. Turn this off to upload to everyone."
        )

        shared_label = wx.StaticText(page, label="Additional shared &folders:")
        self.soulseek_folders_list = wx.ListBox(
            page,
            choices=list(config.get("soulseek_shared_folders", [])),
            style=wx.LB_EXTENDED,
        )
        self.soulseek_folders_list.SetName("Additional Soulseek shared folders")
        self.soulseek_folders_list.SetMinSize((-1, 80))
        self.soulseek_folders_list.SetHelpText(
            "Folders in this list are publicly searchable and downloadable "
            "through Soulseek in addition to the blindDL Library."
        )
        self.soulseek_add_folder_btn = wx.Button(page, label="&Add folder...")
        self.soulseek_remove_folder_btn = wx.Button(page, label="&Remove selected")
        self.soulseek_add_folder_btn.Bind(wx.EVT_BUTTON, self._on_soulseek_add_folder)
        self.soulseek_remove_folder_btn.Bind(
            wx.EVT_BUTTON, self._on_soulseek_remove_folders
        )
        folder_buttons = wx.BoxSizer(wx.HORIZONTAL)
        folder_buttons.Add(self.soulseek_add_folder_btn, 0, wx.RIGHT, 8)
        folder_buttons.Add(self.soulseek_remove_folder_btn, 0)

        priority_label = wx.StaticText(page, label="Free-slot priority &users:")
        self.soulseek_priority_list = wx.ListBox(
            page,
            choices=list(config.get("soulseek_priority_users", [])),
            style=wx.LB_EXTENDED,
        )
        self.soulseek_priority_list.SetName("Soulseek free-slot priority users")
        self.soulseek_priority_list.SetMinSize((-1, 60))
        self.soulseek_priority_list.SetHelpText(
            "These users move ahead in your upload queue. Add one from Search, Messages, or the user browser."
        )
        self.soulseek_remove_priority_btn = wx.Button(
            page, label="Remove selected free-slot &priority"
        )
        self.soulseek_remove_priority_btn.Bind(
            wx.EVT_BUTTON, self._on_soulseek_remove_priority
        )

        listen_label = wx.StaticText(page, label="Incoming &port:")
        self.soulseek_listen_spin = wx.SpinCtrl(
            page, min=1, max=65535, initial=int(config["soulseek_listen_port"])
        )
        self.soulseek_listen_spin.SetName("Soulseek incoming port")

        obfuscated_port_label = wx.StaticText(page, label="&Obfuscated incoming port:")
        self.soulseek_obfuscated_port_spin = wx.SpinCtrl(
            page, min=1, max=65535, initial=int(config["soulseek_obfuscated_port"])
        )
        self.soulseek_obfuscated_port_spin.SetName("Soulseek obfuscated incoming port")

        self.soulseek_upnp_check = wx.CheckBox(
            page, label="Ask the router to &forward both ports with UPnP"
        )
        self.soulseek_upnp_check.SetValue(bool(config["soulseek_upnp"]))
        self.soulseek_upnp_check.SetHelpText(
            "Lets peers connect to you for downloads and uploads without "
            "manual router configuration."
        )

        self.soulseek_obfuscate_check = wx.CheckBox(
            page, label="Prefer &obfuscated peer connections"
        )
        self.soulseek_obfuscate_check.SetValue(bool(config["soulseek_obfuscate"]))

        slots_label = wx.StaticText(page, label="Simultaneous &uploads:")
        self.soulseek_slots_spin = wx.SpinCtrl(
            page, min=1, max=100, initial=int(config["soulseek_upload_slots"])
        )
        self.soulseek_slots_spin.SetName("Soulseek simultaneous uploads")

        results_label = wx.StaticText(page, label="Maximum search &results:")
        self.soulseek_results_spin = wx.SpinCtrl(
            page, min=25, max=10000, initial=int(config["soulseek_max_results"])
        )
        self.soulseek_results_spin.SetName("Soulseek maximum search results")
        self.soulseek_results_spin.SetHelpText(
            "Lower values make large Soulseek searches faster to sort and lighter on memory and CPU."
        )

        down_label = wx.StaticText(
            page, label="Download limit, &KiB per second (0 = unlimited):"
        )
        self.soulseek_down_spin = wx.SpinCtrl(
            page, min=0, max=1000000, initial=int(config["soulseek_max_download_kib"])
        )
        self.soulseek_down_spin.SetName("Soulseek download speed limit")

        up_label = wx.StaticText(
            page, label="Upload limit, KiB per &second (0 = unlimited):"
        )
        self.soulseek_up_spin = wx.SpinCtrl(
            page, min=0, max=1000000, initial=int(config["soulseek_max_upload_kib"])
        )
        self.soulseek_up_spin.SetName("Soulseek upload speed limit")

        sizer.Add(self.soulseek_enabled_check, 0, wx.ALL, 8)
        _row(sizer, username_label, self.soulseek_username_text)
        _row(sizer, password_label, self.soulseek_password_text)
        sizer.Add(self.soulseek_account_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.soulseek_account_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        _row(sizer, description_label, self.soulseek_description_text)
        sizer.Add(self.soulseek_share_library_check, 0, wx.ALL, 8)
        sizer.Add(self.soulseek_block_leechers_check, 0, wx.ALL, 8)
        sizer.Add(shared_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.soulseek_folders_list, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(folder_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(priority_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.soulseek_priority_list, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(
            self.soulseek_remove_priority_btn,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            8,
        )

        network = wx.FlexGridSizer(cols=4, vgap=4, hgap=8)
        network.Add(listen_label, 0, wx.ALIGN_CENTER_VERTICAL)
        network.Add(self.soulseek_listen_spin, 0)
        network.Add(obfuscated_port_label, 0, wx.ALIGN_CENTER_VERTICAL)
        network.Add(self.soulseek_obfuscated_port_spin, 0)
        network.Add(slots_label, 0, wx.ALIGN_CENTER_VERTICAL)
        network.Add(self.soulseek_slots_spin, 0)
        network.Add(results_label, 0, wx.ALIGN_CENTER_VERTICAL)
        network.Add(self.soulseek_results_spin, 0)
        sizer.Add(network, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.soulseek_upnp_check, 0, wx.ALL, 8)
        sizer.Add(self.soulseek_obfuscate_check, 0, wx.ALL, 8)
        _row(sizer, down_label, self.soulseek_down_spin)
        _row(sizer, up_label, self.soulseek_up_spin)

        controls = (
            self.soulseek_username_text,
            self.soulseek_password_text,
            self.soulseek_account_button,
            self.soulseek_description_text,
            self.soulseek_share_library_check,
            self.soulseek_block_leechers_check,
            self.soulseek_folders_list,
            self.soulseek_add_folder_btn,
            self.soulseek_remove_folder_btn,
            self.soulseek_priority_list,
            self.soulseek_remove_priority_btn,
            self.soulseek_listen_spin,
            self.soulseek_obfuscated_port_spin,
            self.soulseek_upnp_check,
            self.soulseek_obfuscate_check,
            self.soulseek_slots_spin,
            self.soulseek_results_spin,
            self.soulseek_down_spin,
            self.soulseek_up_spin,
        )

        def enable_soulseek(_event=None):
            enabled = self.soulseek_enabled_check.GetValue()
            for control in controls:
                control.Enable(enabled)

        self.soulseek_enabled_check.Bind(wx.EVT_CHECKBOX, enable_soulseek)
        enable_soulseek()
        page.SetSizer(sizer)
        page.SetScrollRate(0, 12)
        return page

    def _on_soulseek_account(self, event):
        username = self.soulseek_username_text.GetValue().strip()
        password = self.soulseek_password_text.GetValue()
        if not username or not password:
            self.soulseek_account_status.SetLabel("Enter both a username and password.")
            wx.MessageBox(
                "Enter both a Soulseek username and password.",
                "Soulseek account",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return
        self.soulseek_account_button.Disable()
        self.soulseek_account_status.SetLabel("Signing in to Soulseek...")
        threading.Thread(
            target=self._check_soulseek_account,
            args=(username, password),
            daemon=True,
            name="blinddl-soulseek-account",
        ).start()

    def _check_soulseek_account(self, username, password):
        try:
            soulseek_backend.verify_account(username, password)
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            wx.CallAfter(self._soulseek_account_failed, str(exc))
            return
        wx.CallAfter(self._soulseek_account_ready, username)

    def _soulseek_account_ready(self, username):
        self.soulseek_account_button.Enable(self.soulseek_enabled_check.GetValue())
        self.soulseek_account_status.SetLabel(
            f"Signed in as {username}. New usernames are now registered."
        )
        wx.MessageBox(
            f"Signed in as {username}.\n\n"
            "If this username was unused, Soulseek has created the account. "
            "Choose OK in Settings to save it in blindDL.",
            "Soulseek account ready",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _soulseek_account_failed(self, error):
        self.soulseek_account_button.Enable(self.soulseek_enabled_check.GetValue())
        self.soulseek_account_status.SetLabel(f"Sign in failed: {error}")
        wx.MessageBox(
            f"Soulseek could not sign in or create that account:\n{error}",
            "Soulseek account",
            wx.OK | wx.ICON_ERROR,
            self,
        )

    def _on_soulseek_add_folder(self, event):
        dialog = wx.DirDialog(
            self,
            "Choose a folder to share on Soulseek",
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
            existing = {
                self.soulseek_folders_list.GetString(index).casefold()
                for index in range(self.soulseek_folders_list.GetCount())
            }
            if path.casefold() not in existing:
                self.soulseek_folders_list.Append(path)
        finally:
            dialog.Destroy()

    def _on_soulseek_remove_folders(self, event):
        for index in reversed(self.soulseek_folders_list.GetSelections()):
            self.soulseek_folders_list.Delete(index)

    def _on_soulseek_remove_priority(self, event):
        for index in reversed(self.soulseek_priority_list.GetSelections()):
            self.soulseek_priority_list.Delete(index)

    def _window_page(self):
        page = wx.Panel(self.notebook)
        page.SetName("Window settings")
        sizer = wx.BoxSizer(wx.VERTICAL)
        config = self.config

        self.tray_check = wx.CheckBox(
            page, label="Closing the window hides blindDL in the s&ystem tray"
        )
        self.tray_check.SetValue(bool(config["minimize_to_tray"]))
        self.tray_check.SetHelpText(
            "Downloads, seeding torrents and subscription checks keep "
            "running. Click the blue B tray icon, press Windows plus B, or "
            "launch blindDL again to restore the existing window. Alt plus F4 "
            "hides it too. File, Exit and Control plus Q always exit. blindDL "
            "stays visible if Windows cannot install its tray icon."
        )

        self.tray_minimize_check = wx.CheckBox(
            page, label="&Minimizing the window hides it in the system tray"
        )
        self.tray_minimize_check.SetValue(bool(config["tray_on_minimize"]))
        self.tray_minimize_check.SetHelpText(
            "Off puts the minimized window on the taskbar as usual."
        )

        self.start_maximized_check = wx.CheckBox(
            page, label="&Start the window maximized"
        )
        self.start_maximized_check.SetValue(bool(config["start_maximized"]))
        self.start_maximized_check.SetHelpText(
            "Opens blindDL with the window filling the screen."
        )

        self.speak_status_check = wx.CheckBox(
            page, label="S&peak status messages as they appear"
        )
        self.speak_status_check.SetValue(bool(config["speak_status"]))
        self.speak_status_check.SetHelpText(
            "Says what the status bar says -- a search finishing, a download "
            "failing -- through the screen reader and on a Braille display, "
            "so it does not have to be checked with NVDA plus End. Messages "
            "wait their turn rather than interrupting what is being read."
        )

        update_label = (
            "&Automatically download and install BlindDL updates"
            if getattr(sys, "frozen", False)
            else "&Update download tools automatically"
        )
        self.update_check = wx.CheckBox(page, label=update_label)
        self.update_check.SetValue(bool(config["auto_update"]))
        self.update_check.SetHelpText(
            "Checks at startup and every "
            f"{int(config['update_check_hours'])} hours after that. Released "
            "builds download and verify an update automatically, wait for "
            "active and queued downloads to finish, then install and restart."
        )

        cookies_label = wx.StaticText(page, label="Use cookies from &browser:")
        self.cookies_choice = self._choice(
            page,
            BROWSER_COOKIE_CHOICES,
            config["cookies_from_browser"],
            "Browser cookies",
        )
        self.cookies_choice.SetHelpText(
            "Lets yt-dlp read an existing signed-in browser profile when a "
            "site requires login."
        )

        cookies_file_label = wx.StaticText(page, label="Cookies &file:")
        self.cookies_file_picker = wx.FilePickerCtrl(
            page,
            path=config["cookies_file"],
            message="Select a cookies.txt file",
            wildcard="Cookies files (*.txt)|*.txt|All files (*.*)|*.*",
        )
        self.cookies_file_picker.SetName("Cookies file")
        self.cookies_file_picker.SetHelpText(
            "A Netscape cookies.txt used for YouTube and other sites that "
            "need a sign-in. Copy from browser exports one, or leave blank "
            "to read the browser above live."
        )
        self.cookies_copy_btn = wx.Button(page, label="Copy from &browser")
        self.cookies_copy_btn.SetName("Copy cookies from browser")
        self.cookies_copy_btn.SetHelpText(
            "Exports cookies from a signed-in browser to a cookies.txt file."
        )
        self.cookies_copy_btn.Bind(wx.EVT_BUTTON, self._on_copy_cookies)
        cookies_file_box = wx.BoxSizer(wx.HORIZONTAL)
        cookies_file_box.Add(self.cookies_file_picker, 1, wx.RIGHT, 6)
        cookies_file_box.Add(self.cookies_copy_btn, 0)

        sizer.Add(self.tray_check, 0, wx.ALL, 8)
        sizer.Add(self.tray_minimize_check, 0, wx.ALL, 8)
        sizer.Add(self.start_maximized_check, 0, wx.ALL, 8)
        sizer.Add(self.speak_status_check, 0, wx.ALL, 8)
        sizer.Add(self.update_check, 0, wx.ALL, 8)
        _row(sizer, cookies_label, self.cookies_choice)
        _row(sizer, cookies_file_label, cookies_file_box)
        page.SetSizer(sizer)
        return page

    def _accounts_page(self):
        page = wx.Panel(self.notebook)
        page.SetName("Account settings")
        sizer = wx.BoxSizer(wx.VERTICAL)
        config = self.config

        self.lyrics_check = wx.CheckBox(page, label="Embed synced Deezer &lyrics")
        self.lyrics_check.SetValue(bool(config["sideb_lyrics"]))

        arl_label = wx.StaticText(page, label="Deezer A&RL cookie:")
        self.arl_text = wx.TextCtrl(
            page, value=config["deezer_arl"], style=wx.TE_PASSWORD
        )
        self.arl_text.SetName("Deezer ARL cookie")
        self.arl_text.SetHelpText(
            "Paste the arl cookie value copied from your Deezer login here. "
            "It is a code you paste, not a file to browse for."
        )
        self.arl_paste_btn = wx.Button(page, label="&Paste")
        self.arl_paste_btn.SetName("Paste Deezer ARL")
        self.arl_paste_btn.SetHelpText(
            "Pastes the Deezer arl cookie value from the clipboard."
        )
        self.arl_paste_btn.Bind(wx.EVT_BUTTON, self._on_arl_paste)
        self.arl_browser_btn = wx.Button(page, label="From &browser")
        self.arl_browser_btn.SetName("Copy Deezer ARL from browser")
        self.arl_browser_btn.SetHelpText(
            "Reads the arl cookie from a browser signed into Deezer."
        )
        self.arl_browser_btn.Bind(wx.EVT_BUTTON, self._on_arl_from_browser)

        am_label = wx.StaticText(page, label="Apple Music cookies &file:")
        am_box = wx.BoxSizer(wx.HORIZONTAL)
        self.am_cookies_picker = wx.FilePickerCtrl(
            page,
            path=config["apple_music_cookies"],
            message="Select Apple Music cookies.txt file",
            wildcard="Cookies files (*.txt)|*.txt|All files (*.*)|*.*",
        )
        self.am_cookies_picker.SetName("Apple Music cookies file")
        am_from_browser = wx.Button(page, label="&Copy from browser")
        am_from_browser.SetHelpText(
            "Export Apple Music cookies from the browser selected on the General tab."
        )
        am_from_browser.Bind(wx.EVT_BUTTON, self._on_am_copy_cookies)
        am_box.Add(self.am_cookies_picker, 1, wx.RIGHT, 6)
        am_box.Add(am_from_browser, 0)

        annas_label = wx.StaticText(page, label="Anna's Archive &membership key:")
        self.annas_text = wx.TextCtrl(
            page, value=config["annas_archive_key"], style=wx.TE_PASSWORD
        )
        self.annas_text.SetName("Anna's Archive membership key")
        self.annas_text.SetHelpText(
            "Optional. With a key, book downloads use the fast partner "
            "servers; without one they come from the public LibGen mirrors."
        )

        self.adult_sites_check = wx.CheckBox(page, label="Enable &adult sites")
        self.adult_sites_check.SetValue(bool(config["adult_sites_enabled"]))
        self.adult_sites_check.SetHelpText(
            "Enables adult-site search results and adult URL downloads."
        )

        onlyfans_label = wx.StaticText(page, label="OnlyFans auth &JSON file:")
        self.onlyfans_auth_picker = wx.FilePickerCtrl(
            page,
            path=config["onlyfans_auth_file"],
            message="Choose an ofd-compatible OnlyFans auth JSON file",
            wildcard="JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_USE_TEXTCTRL,
        )
        self.onlyfans_auth_picker.SetName("OnlyFans auth JSON file")

        justforfans_label = wx.StaticText(page, label="JustForFans auth JSON &file:")
        self.justforfans_auth_picker = wx.FilePickerCtrl(
            page,
            path=config["justforfans_auth_file"],
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

        sizer.Add(self._heading(page, "Deezer"), 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.lyrics_check, 0, wx.ALL, 8)
        deezer_format_label = wx.StaticText(page, label="Default &format:")
        self.deezer_format_choice = self._choice(
            page,
            DEEZER_FORMAT_CHOICES,
            config.get("deezer_format", "flac"),
            "Deezer default format",
        )
        self.deezer_format_choice.SetHelpText(
            "FLAC takes Deezer's lossless master, falling back to MP3 320 "
            "where it is unavailable. MP3 320 asks for that bitrate directly."
        )
        _row(sizer, deezer_format_label, self.deezer_format_choice)
        arl_row = wx.BoxSizer(wx.HORIZONTAL)
        arl_row.Add(arl_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        arl_box = wx.BoxSizer(wx.HORIZONTAL)
        arl_box.Add(self.arl_text, 1, wx.RIGHT, 6)
        arl_box.Add(self.arl_paste_btn, 0, wx.RIGHT, 6)
        arl_box.Add(self.arl_browser_btn, 0)
        arl_row.Add(arl_box, 1)
        sizer.Add(arl_row, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(self._heading(page, "Apple Music"), 0, wx.TOP | wx.LEFT, 12)
        am_row = wx.BoxSizer(wx.HORIZONTAL)
        am_row.Add(am_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        am_row.Add(am_box, 1)
        sizer.Add(am_row, 0, wx.EXPAND | wx.ALL, 8)

        am_format_label = wx.StaticText(page, label="Output &format:")
        self.am_format_choice = self._choice(
            page,
            APPLE_FORMAT_CHOICES,
            config.get("apple_music_format", "m4a"),
            "Apple Music output format",
        )
        self.am_format_choice.SetHelpText(
            "M4A keeps Apple's AAC untouched. MP3 re-encodes it through "
            "LAME; V0 is roughly the same bitrate as the original."
        )
        _row(sizer, am_format_label, self.am_format_choice)

        sizer.Add(self._heading(page, "Anna's Archive"), 0, wx.TOP | wx.LEFT, 12)
        _row(sizer, annas_label, self.annas_text)

        sizer.Add(self._heading(page, "Adult sites"), 0, wx.TOP | wx.LEFT, 12)
        sizer.Add(self.adult_sites_check, 0, wx.ALL, 8)
        sizer.Add(onlyfans_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.onlyfans_auth_picker, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(justforfans_label, 0, wx.TOP | wx.LEFT, 8)
        sizer.Add(self.justforfans_auth_picker, 0, wx.EXPAND | wx.ALL, 8)
        page.SetSizer(sizer)
        return page

    def _on_arl_paste(self, event):
        """Paste the clipboard text into the Deezer ARL field."""
        announce = getattr(self.frame, "announce", None)
        if not wx.TheClipboard.Open():
            if announce is not None:
                announce("Could not open the clipboard.")
            return
        try:
            data = wx.TextDataObject()
            ok = wx.TheClipboard.GetData(data)
        finally:
            wx.TheClipboard.Close()
        if ok:
            self.arl_text.SetValue(data.GetText().strip())
            if announce is not None:
                announce("Pasted the Deezer ARL cookie.")
        elif announce is not None:
            announce("The clipboard has no text to paste.")

    def _on_am_copy_cookies(self, event):
        import os
        import tempfile

        from .. import browser_cookies

        descriptor, out = tempfile.mkstemp(suffix=".txt", prefix="am_cookies_")
        os.close(descriptor)
        try:
            label = browser_cookies.export_apple_music_cookies(
                out,
                preferred=self.config.get("cookies_from_browser") or None,
            )
        except browser_cookies.CookieExportError as exc:
            try:
                os.remove(out)
            except OSError:
                pass
            wx.MessageBox(
                "Could not export Apple Music cookies from any browser:\n\n"
                + "\n".join(exc.errors),
                "blindDL",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return
        except Exception as exc:  # noqa: BLE001 - still clean up the temp file
            try:
                os.remove(out)
            except OSError:
                pass
            wx.MessageBox(
                f"Could not export Apple Music cookies: {exc}",
                "blindDL",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return

        self.am_cookies_picker.SetPath(out)
        self.config["apple_music_cookies"] = out
        self.frame.announce(f"Apple Music cookies exported from {label}.")

    def _on_copy_cookies(self, event):
        """Export a general cookies.txt for YouTube and other signed-in sites."""
        import os
        import tempfile

        from .. import browser_cookies

        descriptor, out = tempfile.mkstemp(suffix=".txt", prefix="cookies_")
        os.close(descriptor)
        try:
            label = browser_cookies.export_cookies(
                out,
                preferred=self.config.get("cookies_from_browser") or None,
            )
        except browser_cookies.CookieExportError as exc:
            try:
                os.remove(out)
            except OSError:
                pass
            wx.MessageBox(
                "Could not export cookies from any browser:\n\n"
                + "\n".join(exc.errors),
                "blindDL",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return
        except Exception as exc:  # noqa: BLE001 - still clean up the temp file
            try:
                os.remove(out)
            except OSError:
                pass
            wx.MessageBox(
                f"Could not export cookies: {exc}",
                "blindDL",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return

        self.cookies_file_picker.SetPath(out)
        self.config["cookies_file"] = out
        announce = getattr(self.frame, "announce", None)
        if announce is not None:
            announce(f"Cookies exported from {label}.")

    def _on_arl_from_browser(self, event):
        """Read the Deezer arl cookie from a signed-in browser."""
        from .. import browser_cookies

        announce = getattr(self.frame, "announce", None)
        try:
            value, label = browser_cookies.extract_cookie_value(
                "arl", ["deezer.com"],
                preferred=self.config.get("cookies_from_browser") or None,
            )
        except browser_cookies.CookieExportError as exc:
            wx.MessageBox(
                "Could not read the Deezer arl cookie from any browser:\n\n"
                + "\n".join(exc.errors),
                "blindDL",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return
        self.arl_text.SetValue(value)
        if announce is not None:
            announce(f"Copied the Deezer ARL cookie from {label}.")

    # -- saving --------------------------------------------------------------

    def apply(self):
        """Write the dialog values back into the config object."""
        self.config["download_dir"] = self.dir_picker.GetPath()
        self.config["audio_only"] = self.audio_only_check.GetValue()
        self.config["audio_format"] = AUDIO_FORMAT_CHOICES[
            self.format_choice.GetSelection()
        ][1]
        self.config["video_format"] = VIDEO_FORMAT_CHOICES[
            self.video_format_choice.GetSelection()
        ][1]
        self.config["auto_clear_finished"] = self.auto_clear_check.GetValue()
        self.config["max_concurrent"] = self.conc_spin.GetValue()
        self.config["search_timeout_s"] = self.search_spin.GetValue()
        self.config["sub_check_hours"] = self.sub_spin.GetValue()

        self.config["torrent_engine"] = self.torrent_engine_check.GetValue()
        self.config["torrent_dir"] = self.torrent_dir_picker.GetPath().strip()
        self.config["torrent_max_down_kib"] = self.torrent_down_spin.GetValue()
        self.config["torrent_max_up_kib"] = self.torrent_up_spin.GetValue()
        self.config["torrent_max_active"] = self.torrent_active_spin.GetValue()
        self.config["torrent_max_connections"] = self.torrent_conn_spin.GetValue()
        self.config["torrent_seed_ratio"] = _positive_float(
            self.torrent_ratio_text.GetValue(), self.config["torrent_seed_ratio"]
        )
        self.config["torrent_seed_minutes"] = self.torrent_minutes_spin.GetValue()
        self.config["torrent_port"] = self.torrent_port_spin.GetValue()
        self.config["torrent_encryption"] = ENCRYPTION_CHOICES[
            self.torrent_enc_choice.GetSelection()
        ][1]
        self.config["torrent_dht"] = self.torrent_dht_check.GetValue()
        self.config["torrent_port_forward"] = self.torrent_forward_check.GetValue()
        self.config["torrent_sequential"] = self.torrent_sequential_check.GetValue()
        self.config["torrent_delete_partial"] = self.torrent_delete_check.GetValue()
        self.config["torrent_proxy"] = self.torrent_proxy_text.GetValue().strip()
        self.config["torrent_client_version"] = (
            self.torrent_version_text.GetValue().strip()
        )

        self.config["soulseek_enabled"] = self.soulseek_enabled_check.GetValue()
        self.config["soulseek_username"] = (
            self.soulseek_username_text.GetValue().strip()
        )
        self.config["soulseek_password"] = self.soulseek_password_text.GetValue()
        self.config["soulseek_description"] = (
            self.soulseek_description_text.GetValue().strip()
        )
        self.config["soulseek_share_library"] = (
            self.soulseek_share_library_check.GetValue()
        )
        self.config["soulseek_block_leechers"] = (
            self.soulseek_block_leechers_check.GetValue()
        )
        self.config["soulseek_shared_folders"] = [
            self.soulseek_folders_list.GetString(index)
            for index in range(self.soulseek_folders_list.GetCount())
        ]
        self.config["soulseek_priority_users"] = [
            self.soulseek_priority_list.GetString(index)
            for index in range(self.soulseek_priority_list.GetCount())
        ]
        self.config["soulseek_listen_port"] = self.soulseek_listen_spin.GetValue()
        self.config["soulseek_obfuscated_port"] = (
            self.soulseek_obfuscated_port_spin.GetValue()
        )
        self.config["soulseek_upnp"] = self.soulseek_upnp_check.GetValue()
        self.config["soulseek_obfuscate"] = self.soulseek_obfuscate_check.GetValue()
        self.config["soulseek_upload_slots"] = self.soulseek_slots_spin.GetValue()
        self.config["soulseek_max_results"] = self.soulseek_results_spin.GetValue()
        self.config["soulseek_max_download_kib"] = self.soulseek_down_spin.GetValue()
        self.config["soulseek_max_upload_kib"] = self.soulseek_up_spin.GetValue()

        self.config["minimize_to_tray"] = self.tray_check.GetValue()
        self.config["tray_on_minimize"] = self.tray_minimize_check.GetValue()
        self.config["start_maximized"] = self.start_maximized_check.GetValue()
        self.config["speak_status"] = self.speak_status_check.GetValue()
        self.config["auto_update"] = self.update_check.GetValue()
        # Kept in saved configs for backward compatibility; automatic update
        # is now one unambiguous setting.
        self.config["auto_install_update"] = self.update_check.GetValue()
        self.config["cookies_from_browser"] = BROWSER_COOKIE_CHOICES[
            self.cookies_choice.GetSelection()
        ][1]
        self.config["cookies_file"] = self.cookies_file_picker.GetPath().strip()

        self.config["sideb_lyrics"] = self.lyrics_check.GetValue()
        self.config["adult_sites_enabled"] = self.adult_sites_check.GetValue()
        self.config["onlyfans_auth_file"] = self.onlyfans_auth_picker.GetPath().strip()
        self.config["justforfans_auth_file"] = (
            self.justforfans_auth_picker.GetPath().strip()
        )
        self.config["deezer_arl"] = self.arl_text.GetValue().strip()
        self.config["deezer_format"] = DEEZER_FORMAT_CHOICES[
            self.deezer_format_choice.GetSelection()
        ][1]
        self.config["apple_music_cookies"] = self.am_cookies_picker.GetPath().strip()
        self.config["apple_music_format"] = APPLE_FORMAT_CHOICES[
            self.am_format_choice.GetSelection()
        ][1]
        self.config["annas_archive_key"] = self.annas_text.GetValue().strip()
        self.config.save()


def _positive_float(text, fallback):
    """A seeding ratio typed by hand, or the old value when it makes no sense."""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback
