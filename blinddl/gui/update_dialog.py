# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible application and source-dependency update dialog."""

import sys
import threading

import wx

from .. import speech, updater


class UpdateDialog(wx.Dialog):
    def __init__(self, parent, on_changed=None):
        super().__init__(parent, title="Check for updates", size=(600, 400))
        self.on_changed = on_changed
        self._alive = True
        self._busy = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.log_text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log_text.SetName("Update log")
        self.install_btn = wx.Button(self, label="&Download and install update")
        self.install_btn.Bind(wx.EVT_BUTTON, self._on_install)
        self.install_btn.Hide()
        self.close_btn = wx.Button(self, wx.ID_CLOSE, "&Close")
        self.close_btn.Bind(wx.EVT_BUTTON, self._on_close_button)
        self.close_btn.Disable()

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.install_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.close_btn, 0)
        sizer.Add(self.log_text, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        self.update = None
        threading.Thread(target=self._run, daemon=True).start()

    def _log(self, line):
        wx.CallAfter(self._append_log, line + "\n")

    def _append_log(self, line):
        if self._alive and not self.IsBeingDeleted():
            self.log_text.AppendText(line)

    def _progress(self, line):
        """Log a download-progress line and say it out loud.

        The log is a read-only text control: text arriving in it is not
        read by a screen reader, so a download that only wrote there would
        run in silence with no way to tell progress from a stall.
        """
        self._log(line)
        speech.announce(line)

    def _run(self):
        try:
            if getattr(sys, "frozen", False):
                self.update = updater.check_for_app_update(self._log)
                changed = False
            else:
                changed = updater.run_full_update(self._log)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._log(f"Update failed: {exc}")
            changed = False
        wx.CallAfter(self._finished, changed)

    def _finished(self, changed):
        if not self._alive:
            return
        self._busy = False
        if self.update is not None:
            self.install_btn.Show()
            self.install_btn.Enable()
            self.GetSizer().Layout()
        self.close_btn.Enable()
        (self.install_btn if self.update is not None
         else self.close_btn).SetFocus()
        if changed and self.on_changed is not None:
            self.on_changed()

    def _on_install(self, event):
        if not self._alive or self.update is None:
            return
        self._busy = True
        self.install_btn.Disable()
        self.close_btn.Disable()
        threading.Thread(
            target=self._install, daemon=True, name="blinddl-self-update"
        ).start()

    def _install(self):
        try:
            package = updater.download_app_update(
                self.update, self._log, progress=self._progress)
            exit_to_update = updater.install_app_update(
                self.update, package, self._log)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._log(f"Update failed: {exc}")
            wx.CallAfter(self._install_failed)
            return
        wx.CallAfter(self._install_started, exit_to_update)

    def _install_failed(self):
        if not self._alive:
            return
        self._busy = False
        self.install_btn.Enable()
        self.close_btn.Enable()
        self.install_btn.SetFocus()

    def _install_started(self, exit_to_update):
        if not self._alive:
            return
        self._busy = False
        if exit_to_update:
            self.EndModal(wx.ID_OK)
            return
        self.close_btn.Enable()
        self.close_btn.SetFocus()

    def _on_close_button(self, event=None):
        if not self._busy:
            self.EndModal(wx.ID_CLOSE)

    def _on_close(self, event):
        if self._busy and event.CanVeto():
            event.Veto()
            return
        if self.IsModal():
            self.EndModal(wx.ID_CLOSE)
        else:
            event.Skip()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._alive = False
        event.Skip()
