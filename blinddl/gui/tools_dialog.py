# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible progress window for the first-run native tool installation.

VLC, FFmpeg, Deno and Node are large downloads that blindDL installs through
the system package manager the first time it runs. That used to happen with
nothing on screen: several minutes of silence, no window to tab to, and a
single sentence at the end. This window says which tool is being installed
as it starts, and again as it finishes, so the wait is something that can be
read and heard rather than guessed at.

Nothing here blocks the application. The window is modeless and can be hidden
at any time; the installation carries on in its worker thread and speaks its
result whether the window is still open or not.
"""

import threading

import wx

from .. import speech, updater


class ExternalToolsDialog(wx.Dialog):
    """Install *packages* and report every step out loud."""

    def __init__(self, parent, packages, on_finished=None):
        super().__init__(parent, title="Installing media tools",
                         size=(600, 400))
        self.packages = tuple(packages)
        self.on_finished = on_finished
        self._alive = True
        self._busy = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.intro = intro_text(self.packages)
        intro_label = wx.StaticText(self, label=self.intro)
        intro_label.Wrap(560)
        self.log_text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log_text.SetName("Installation progress")
        # "Hide" while it runs, because closing this window stops nothing.
        self.close_btn = wx.Button(self, wx.ID_CLOSE, "&Hide")
        self.close_btn.Bind(wx.EVT_BUTTON, self._on_close_button)

        sizer.Add(intro_label, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.log_text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.close_btn, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        # Focus set before the window is on screen does not always stick, and
        # the read-only log would otherwise take it: the button is the only
        # thing here to do.
        self.close_btn.SetFocus()
        wx.CallAfter(self._focus_button)

        self._append_log(self.intro + "\n")
        speech.announce(self.intro)
        threading.Thread(
            target=self._run, daemon=True, name="blinddl-tool-install"
        ).start()

    # -- progress ----------------------------------------------------------

    def _log(self, line):
        wx.CallAfter(self._append_log, line + "\n")

    def _append_log(self, line):
        # A queued log line can arrive after the window was hidden away and
        # deleted, which leaves this Python object wrapping nothing. The
        # installation is what matters, so a late line is dropped, not raised.
        if not self._alive:
            return
        try:
            self.log_text.AppendText(line)
        except RuntimeError:
            self._alive = False

    def _focus_button(self):
        if not self._alive:
            return
        try:
            self.close_btn.SetFocus()
        except RuntimeError:
            self._alive = False

    def _progress(self, line):
        """Show a milestone and speak it.

        The log is a read-only text control, so a screen reader does not read
        what arrives in it. An installation that only wrote there would look
        exactly like one that had stopped.
        """
        self._log(line)
        speech.announce(line)

    # -- the installation --------------------------------------------------

    def _run(self):
        ok = False
        try:
            ok = updater.ensure_external_tools(
                self._log, self.packages, progress=self._progress)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._log(f"Installation failed: {exc}")
        wx.CallAfter(self._finished, ok)

    def _finished(self, ok):
        self._busy = False
        message = finished_text(ok)
        self._append_log(message + "\n")
        if self._alive:
            try:
                self.close_btn.SetLabel("&Close")
                self.GetSizer().Layout()
            except RuntimeError:
                self._alive = False
            self._focus_button()
        # Spoken whether or not the window is still open: the wait was worth
        # announcing, and hiding the window is not a request to be left in
        # the dark about how it ended.
        speech.announce(message)
        if self.on_finished is not None:
            self.on_finished(ok)

    # -- closing -----------------------------------------------------------

    def _on_close_button(self, event=None):
        self.Close()

    def _on_close(self, event):
        # Hiding is always allowed. The worker holds no reference to any
        # window control, only to _alive, so it survives this.
        if self.IsModal():
            self.EndModal(wx.ID_CLOSE)
        else:
            self.Destroy()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._alive = False
        event.Skip()


def intro_text(packages):
    """What is about to be installed, in one sentence a reader can follow."""
    names = updater.describe_external_tools(packages)
    if not names:
        return "blindDL is installing the media tools it needs."
    if len(names) == 1:
        listed = names[0]
    else:
        listed = ", ".join(names[:-1]) + " and " + names[-1]
    return (
        f"blindDL is installing {listed}. This can take several minutes. "
        "You can hide this window; the installation carries on without it."
    )


def finished_text(ok):
    if ok:
        return "Media tools installed. blindDL is ready."
    return (
        "Some media tools could not be installed. "
        "Use Help, Check for updates to try again."
    )
