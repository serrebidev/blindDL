# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible Soulseek friends list, on its own tab.

Friends used to share the Messages tab, where the list competed with the
transcript for the same screen and the same Tab order. Here a friend can be
managed on its own; selecting one addresses the Messages tab at them, and
activating one (Enter or double-click) goes straight there to write.
"""

import threading

import wx

from .. import soulseek_backend


class FriendsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._alive = True

        sizer = wx.BoxSizer(wx.VERTICAL)
        username_row = wx.BoxSizer(wx.HORIZONTAL)
        username_label = wx.StaticText(self, label="&Soulseek username:")
        self.username_text = wx.TextCtrl(self)
        self.username_text.SetName("Soulseek friend username")
        self.username_text.SetHelpText(
            "Type a Soulseek username to add as a friend, or select a friend below."
        )
        self.add_friend_button = wx.Button(self, label="&Add friend")
        self.remove_friend_button = wx.Button(self, label="&Remove friend")
        username_row.Add(username_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        username_row.Add(self.username_text, 1, wx.RIGHT, 6)
        username_row.Add(self.add_friend_button, 0, wx.RIGHT, 6)
        username_row.Add(self.remove_friend_button, 0)

        friends_label = wx.StaticText(self, label="&Friends:")
        self.friends_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.friends_list.SetName("Soulseek friends")
        self.friends_list.SetHelpText(
            "Select a friend to address a private message to them on the "
            "Messages tab. Press Enter on a friend to write to them."
        )
        self.friends_list.InsertColumn(0, "Username")
        self.friends_list.InsertColumn(1, "Status")
        self.friends_list.SetColumnWidth(0, 260)
        self.friends_list.SetColumnWidth(1, 120)

        action_row = wx.BoxSizer(wx.HORIZONTAL)
        self.browse_button = wx.Button(self, label="&Browse")
        self.slot_button = wx.Button(self, label="Free &slot")
        self.profile_button = wx.Button(self, label="&Profile")
        action_row.Add(self.browse_button, 0, wx.RIGHT, 6)
        action_row.Add(self.slot_button, 0, wx.RIGHT, 6)
        action_row.Add(self.profile_button, 0)

        sizer.Add(username_row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(friends_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.friends_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(action_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
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
        self.friends_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_friend_activated)

        self._show_friends(soulseek_backend.friends_snapshot())

    def focus_input(self):
        self.username_text.SetFocus()

    def shutdown(self):
        self._alive = False

    def handle_soulseek_event(self, event):
        if not self._alive:
            return
        if event.get("type") == "friends":
            self._show_friends(event.get("friends", []))

    def _show_friends(self, friends):
        selected = self.username_text.GetValue()
        self.friends_list.DeleteAllItems()
        for friend in friends:
            row = self.friends_list.InsertItem(
                self.friends_list.GetItemCount(), friend["username"]
            )
            self.friends_list.SetItem(row, 1, friend.get("status", "Unknown"))
            if friend["username"].casefold() == selected.casefold():
                self.friends_list.Select(row)

    def on_friend_selected(self, event):
        username = self.friends_list.GetItemText(event.GetIndex())
        self.username_text.SetValue(username)
        # Selecting a friend addresses the Messages tab at them, without
        # moving focus: writing still happens there, on purpose.
        self.frame.set_soulseek_recipient(username)

    def on_friend_activated(self, event):
        username = self.friends_list.GetItemText(event.GetIndex())
        if username:
            self.frame.message_soulseek_user(username)

    def _worker(self, action, success, *args):
        try:
            result = action(*args)
        except Exception as exc:  # noqa: BLE001 - presented in the UI
            wx.CallAfter(self._failed, str(exc))
            return
        wx.CallAfter(success, result)

    def _failed(self, error):
        if self._alive:
            self.frame.announce(f"Soulseek friends error: {error}")

    def _selected_username(self):
        return self.username_text.GetValue().strip()

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
