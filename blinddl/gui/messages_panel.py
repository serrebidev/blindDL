# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible Soulseek private messages.

The reply target follows the transcript: focusing a message makes its
sender the person the next message goes to, and the message field carries
that person's name, so Tab from a message lands in a field that says who
it writes to. Friends live on their own tab; selecting a friend there
addresses this tab the same way.
"""

import threading

import wx

from .. import soulseek_backend
from .chat_panel import _clock


class MessagesPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._alive = True

        # Focusable controls are created in Tab order -- transcript, message
        # field, Send, recipient -- whatever order the sizer below lays them
        # out in visually: focus a message, Tab once, and you are writing
        # the reply to whoever sent it.
        transcript_label = wx.StaticText(self, label="Private &messages:")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Soulseek private message transcript")
        self.list.SetHelpText(
            "Private Soulseek messages received and sent. Selecting a "
            "message addresses your next message to that person."
        )
        for index, heading in enumerate(("Time", "Direction", "User", "Message")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 80)
        self.list.SetColumnWidth(1, 90)
        self.list.SetColumnWidth(2, 170)
        self.list.SetColumnWidth(3, 470)

        message_label = wx.StaticText(self, label="&Message:")
        self.message_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.send_button = wx.Button(self, label="&Send")

        recipient_label = wx.StaticText(self, label="&Recipient:")
        self.recipient_text = wx.TextCtrl(self)
        self.recipient_text.SetName("Soulseek message recipient")
        self.recipient_text.SetHelpText(
            "Who the next message goes to. Selecting a message above fills "
            "this in; type a different Soulseek username to write to "
            "somebody else."
        )

        sizer = wx.BoxSizer(wx.VERTICAL)
        recipient_row = wx.BoxSizer(wx.HORIZONTAL)
        recipient_row.Add(recipient_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        recipient_row.Add(self.recipient_text, 1)
        message_row = wx.BoxSizer(wx.HORIZONTAL)
        message_row.Add(message_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        message_row.Add(self.message_text, 1, wx.RIGHT, 6)
        message_row.Add(self.send_button, 0)
        sizer.Add(recipient_row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(transcript_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(message_row, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self._update_target_name()
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_message_selected)
        self.recipient_text.Bind(wx.EVT_TEXT, self._on_recipient_typed)
        self.send_button.Bind(wx.EVT_BUTTON, self.on_send)
        self.message_text.Bind(wx.EVT_TEXT_ENTER, self.on_send)

        for message in soulseek_backend.private_messages_snapshot():
            self._append_message(message)

    def focus_input(self):
        # With a target chosen, writing is the point of this tab; without
        # one, the recipient field is the first thing needed.
        if self._selected_username():
            self.message_text.SetFocus()
        else:
            self.recipient_text.SetFocus()

    def shutdown(self):
        self._alive = False

    def handle_soulseek_event(self, event):
        if not self._alive:
            return
        if event.get("type") == "private_message":
            self._append_message(event["message"])

    def _append_message(self, message):
        row = self.list.InsertItem(
            self.list.GetItemCount(), _clock(message["timestamp"])
        )
        self.list.SetItem(row, 1, "To" if message.get("outgoing") else "From")
        self.list.SetItem(row, 2, message.get("user", ""))
        self.list.SetItem(row, 3, message.get("message", ""))
        self.list.EnsureVisible(row)

    def set_recipient(self, username):
        """Address the next message to *username* and say so on the field."""
        self.recipient_text.SetValue(str(username or ""))
        self._update_target_name()

    def on_message_selected(self, event):
        # The User column is the other party in both directions: the sender
        # of a received message, the addressee of a sent one.
        username = self.list.GetItemText(event.GetIndex(), 2)
        if username:
            self.set_recipient(username)

    def _on_recipient_typed(self, event):
        self._update_target_name()
        event.Skip()

    def _update_target_name(self):
        username = self._selected_username()
        if username:
            self.message_text.SetName(f"Soulseek private message to {username}")
            self.message_text.SetHelpText(
                f"Type a private message to {username} and press Enter to send."
            )
        else:
            self.message_text.SetName("Soulseek private message")
            self.message_text.SetHelpText(
                "Select a message or enter a recipient first, then type a "
                "private message and press Enter to send."
            )

    def _worker(self, action, success, *args):
        try:
            result = action(*args)
        except Exception as exc:  # noqa: BLE001 - presented in the UI
            wx.CallAfter(self._failed, str(exc))
            return
        wx.CallAfter(success, result)

    def _failed(self, error):
        if self._alive:
            self.frame.announce(f"Soulseek messages error: {error}")

    def _selected_username(self):
        return self.recipient_text.GetValue().strip()

    def on_send(self, event=None):
        username = self._selected_username()
        message = self.message_text.GetValue().strip()
        if not username or not message:
            self.frame.announce("Choose a recipient and enter a message.")
            return
        threading.Thread(
            target=self._worker,
            args=(
                soulseek_backend.send_private_message,
                self._sent,
                username,
                message,
                self.frame.config,
            ),
            daemon=True,
            name="blinddl-soulseek-private-message",
        ).start()

    def _sent(self, result):
        if self._alive:
            self.message_text.Clear()
            self.message_text.SetFocus()
