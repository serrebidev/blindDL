# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Update dialog: shows live progress while dependencies are updated."""

import threading

import wx

from .. import updater


class UpdateDialog(wx.Dialog):
    def __init__(self, parent, on_changed=None):
        super().__init__(parent, title="Check for updates", size=(600, 400))
        self.on_changed = on_changed

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.log_text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log_text.SetName("Update log")
        self.close_btn = wx.Button(self, wx.ID_CLOSE, "&Close")
        self.close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.close_btn.Disable()

        sizer.Add(self.log_text, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.close_btn, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)

        threading.Thread(target=self._run, daemon=True).start()

    def _log(self, line):
        wx.CallAfter(self.log_text.AppendText, line + "\n")

    def _run(self):
        try:
            changed = updater.run_full_update(self._log)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._log(f"Update failed: {exc}")
            changed = False
        wx.CallAfter(self._finished, changed)

    def _finished(self, changed):
        self.close_btn.Enable()
        self.close_btn.SetFocus()
        if changed and self.on_changed is not None:
            self.on_changed()
