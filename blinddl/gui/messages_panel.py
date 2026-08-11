# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible Soulseek friends and private messages."""

import threading

import wx

from .. import soulseek_backend
from .chat_panel import _clock


class MessagesPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._alive = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        recipient_row = wx.BoxSizer(wx.HORIZONTAL)
        recipient_label = wx.StaticText(self, label="&Recipient or friend:")
        self.recipient_text = wx.TextCtrl(self)
        self.recipient_text.SetName("Soulseek message recipient")
        self.recipient_text.SetHelpText(
            "Type a Soulseek username, or select a friend below."
        )
        self.add_friend_button = wx.Button(self, label="&Add friend")
        self.remove_friend_button = wx.Button(self, label="&Remove friend")
        self.browse_button = wx.Button(self, label="&Browse")
        self.slot_button = wx.Button(self, label="Free &slot")
        self.profile_button = wx.Button(self, label="&Profile")
        recipient_row.Add(recipient_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        recipient_row.Add(self.recipient_text, 1, wx.RIGHT, 6)
        recipient_row.Add(self.add_friend_button, 0, wx.RIGHT, 6)
        recipient_row.Add(self.remove_friend_button, 0)
        recipient_row.Add(self.browse_button, 0, wx.LEFT, 6)
        recipient_row.Add(self.slot_button, 0, wx.LEFT, 6)
        recipient_row.Add(self.profile_button, 0, wx.LEFT, 6)

        friends_label = wx.StaticText(self, label="&Friends:")
        self.friends_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.friends_list.SetName("Soulseek friends")
        self.friends_list.SetHelpText("Select a friend to address a private message.")
        self.friends_list.InsertColumn(0, "Username")
        self.friends_list.InsertColumn(1, "Status")
        self.friends_list.SetColumnWidth(0, 260)
        self.friends_list.SetColumnWidth(1, 120)
        self.friends_list.SetMinSize((-1, 125))

        transcript_label = wx.StaticText(self, label="Private &messages:")
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Soulseek private message transcript")
        self.list.SetHelpText("Private Soulseek messages received and sent.")
        for index, heading in enumerate(("Time", "Direction", "User", "Message")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 80)
        self.list.SetColumnWidth(1, 90)
        self.list.SetColumnWidth(2, 170)
        self.list.SetColumnWidth(3, 470)

        message_row = wx.BoxSizer(wx.HORIZONTAL)
        message_label = wx.StaticText(self, label="&Message:")
        self.message_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.message_text.SetName("Soulseek private message")
        self.message_text.SetHelpText("Type a private message and press Enter to send.")
        self.send_button = wx.Button(self, label="&Send")
        message_row.Add(message_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        message_row.Add(self.message_text, 1, wx.RIGHT, 6)
        message_row.Add(self.send_button, 0)

        sizer.Add(recipient_row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(friends_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.friends_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(transcript_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(message_row, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self.add_friend_button.Bind(wx.EVT_BUTTON, self.on_add_friend)
        self.remove_friend_button.Bind(wx.EVT_BUTTON, self.on_remove_friend)
        self.browse_button.Bind(
            wx.EVT_BUTTON,
            lambda event: self.frame.open_soulseek_user(self._selected_username()),
        )
        self.slot_button.Bind(
            wx.EVT_BUTTON,
            lambda event: self.frame.give_soulseek_free_slot(
                self._selected_username()
            ),
        )
        self.profile_button.Bind(
            wx.EVT_BUTTON,
            lambda event: self.frame.view_soulseek_profile(
                self._selected_username()
            ),
        )
        self.friends_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_friend_selected)
        self.send_button.Bind(wx.EVT_BUTTON, self.on_send)
        self.message_text.Bind(wx.EVT_TEXT_ENTER, self.on_send)

        self._show_friends(soulseek_backend.friends_snapshot())
        for message in soulseek_backend.private_messages_snapshot():
            self._append_message(message)

    def focus_input(self):
        self.recipient_text.SetFocus()

    def shutdown(self):
        self._alive = False

    def handle_soulseek_event(self, event):
        if not self._alive:
            return
        if event.get("type") == "friends":
            self._show_friends(event.get("friends", []))
        elif event.get("type") == "private_message":
            self._append_message(event["message"])

    def _show_friends(self, friends):
        selected = self.recipient_text.GetValue()
        self.friends_list.DeleteAllItems()
        for friend in friends:
            row = self.friends_list.InsertItem(
                self.friends_list.GetItemCount(), friend["username"]
            )
            self.friends_list.SetItem(row, 1, friend.get("status", "Unknown"))
            if friend["username"].casefold() == selected.casefold():
                self.friends_list.Select(row)

    def _append_message(self, message):
        row = self.list.InsertItem(
            self.list.GetItemCount(), _clock(message["timestamp"])
        )
        self.list.SetItem(row, 1, "To" if message.get("outgoing") else "From")
        self.list.SetItem(row, 2, message.get("user", ""))
        self.list.SetItem(row, 3, message.get("message", ""))
        self.list.EnsureVisible(row)

    def on_friend_selected(self, event):
        self.recipient_text.SetValue(self.friends_list.GetItemText(event.GetIndex()))

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

    def on_add_friend(self, event=None):
        username = self._selected_username()
        if not username:
            self.frame.announce("Enter a Soulseek username to add.")
            return
        threading.Thread(
            target=self._worker,
            args=(
                soulseek_backend.add_friend,
                lambda friends: self._friend_changed(username, True, friends),
                username,
                self.frame.config,
            ),
            daemon=True,
            name="blinddl-soulseek-add-friend",
        ).start()

    def on_remove_friend(self, event=None):
        username = self._selected_username()
        if not username:
            self.frame.announce("Select or enter a Soulseek friend to remove.")
            return
        threading.Thread(
            target=self._worker,
            args=(
                soulseek_backend.remove_friend,
                lambda friends: self._friend_changed(username, False, friends),
                username,
                self.frame.config,
            ),
            daemon=True,
            name="blinddl-soulseek-remove-friend",
        ).start()

    def _friend_changed(self, username, added, friends):
        if not self._alive:
            return
        saved = list(self.frame.config.get("soulseek_friends", []) or [])
        if added and username.casefold() not in {value.casefold() for value in saved}:
            saved.append(username)
        elif not added:
            saved = [
                value for value in saved if value.casefold() != username.casefold()
            ]
        self.frame.config["soulseek_friends"] = saved
        self.frame.config.save()
        self._show_friends(friends)
        verb = "Added" if added else "Removed"
        self.frame.announce(f"{verb} Soulseek friend {username}.")

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
