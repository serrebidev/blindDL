# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Accessible Soulseek public and private room chat."""

from datetime import datetime
import threading

import wx

from .. import soulseek_backend


def _clock(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


class ChatPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self._alive = True
        self._rooms = []
        self._joining_private = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        room_row = wx.BoxSizer(wx.HORIZONTAL)
        room_label = wx.StaticText(self, label="&Room:")
        self.room_combo = wx.ComboBox(self, style=wx.CB_DROPDOWN)
        self.room_combo.SetName("Soulseek room")
        self.room_combo.SetHelpText(
            "Choose a listed room or type a room name, then choose Join."
        )
        self.refresh_button = wx.Button(self, label="&Refresh rooms")
        self.join_button = wx.Button(self, label="&Join")
        self.leave_button = wx.Button(self, label="&Leave")
        self.private_check = wx.CheckBox(self, label="&Private room")
        self.private_check.SetName("Private Soulseek room")
        self.private_check.SetHelpText(
            "Creates your own private room, or joins someone else's private "
            "room when they have invited you."
        )
        for control in (
            room_label,
            self.room_combo,
            self.refresh_button,
            self.join_button,
            self.leave_button,
            self.private_check,
        ):
            room_row.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        self.rooms_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.rooms_list.SetName("Soulseek chat rooms")
        self.rooms_list.SetHelpText(
            "Public rooms and your remembered public or private rooms. "
            "Select a room to put its name in the Room box; activate it to join."
        )
        for index, heading in enumerate(("Room", "Type", "Status", "Users")):
            self.rooms_list.InsertColumn(index, heading)
        self.rooms_list.SetColumnWidth(0, 300)
        self.rooms_list.SetColumnWidth(1, 90)
        self.rooms_list.SetColumnWidth(2, 110)
        self.rooms_list.SetColumnWidth(3, 80)
        self.rooms_list.SetMinSize((-1, 160))

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Soulseek room chat transcript")
        self.list.SetHelpText("Messages received and sent in Soulseek rooms.")
        for index, heading in enumerate(("Time", "Room", "User", "Message")):
            self.list.InsertColumn(index, heading)
        self.list.SetColumnWidth(0, 80)
        self.list.SetColumnWidth(1, 150)
        self.list.SetColumnWidth(2, 150)
        self.list.SetColumnWidth(3, 470)

        message_row = wx.BoxSizer(wx.HORIZONTAL)
        message_label = wx.StaticText(self, label="&Message:")
        self.message_text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.message_text.SetName("Soulseek room message")
        self.message_text.SetHelpText("Type a room message and press Enter to send.")
        self.send_button = wx.Button(self, label="&Send")
        message_row.Add(message_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        message_row.Add(self.message_text, 1, wx.RIGHT, 6)
        message_row.Add(self.send_button, 0)

        sizer.Add(room_row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.rooms_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(message_row, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self.refresh_button.Bind(wx.EVT_BUTTON, self.on_refresh)
        self.join_button.Bind(wx.EVT_BUTTON, self.on_join)
        self.leave_button.Bind(wx.EVT_BUTTON, self.on_leave)
        self.send_button.Bind(wx.EVT_BUTTON, self.on_send)
        self.message_text.Bind(wx.EVT_TEXT_ENTER, self.on_send)
        self.rooms_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_room_selected)
        self.rooms_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_join)

        self._show_rooms(soulseek_backend.rooms_snapshot())
        for message in soulseek_backend.room_messages_snapshot():
            self._append_message(message)
        self.on_refresh()

    def focus_input(self):
        self.room_combo.SetFocus()

    def shutdown(self):
        self._alive = False

    def handle_soulseek_event(self, event):
        if not self._alive:
            return
        if event.get("type") == "rooms":
            self._show_rooms(event.get("rooms", []))
        elif event.get("type") == "room_message":
            self._append_message(event["message"])

    def _show_rooms(self, rooms):
        self._rooms = [dict(room) for room in rooms]
        value = self.room_combo.GetValue()
        remembered = list(self.frame.config.get("soulseek_rooms", []) or [])
        names = []
        seen = set()
        for name in [*remembered, *(room["name"] for room in rooms)]:
            key = str(name).casefold()
            if key and key not in seen:
                seen.add(key)
                names.append(str(name))
        self.room_combo.Clear()
        if names:
            self.room_combo.AppendItems(names)
        self.room_combo.SetValue(value)
        self._show_room_list()

    def _show_room_list(self):
        remembered = list(self.frame.config.get("soulseek_rooms", []) or [])
        private = {
            str(name).casefold()
            for name in self.frame.config.get("soulseek_private_rooms", []) or []
        }
        remembered_keys = {str(name).casefold() for name in remembered}
        by_name = {str(room["name"]).casefold(): dict(room) for room in self._rooms}
        for name in remembered:
            key = str(name).casefold()
            by_name.setdefault(key, {
                "name": str(name),
                "private": key in private,
                "joined": False,
                "user_count": 0,
            })
        rows = sorted(
            by_name.values(),
            key=lambda room: (
                str(room["name"]).casefold() not in remembered_keys,
                not bool(room.get("joined")),
                -int(room.get("user_count") or 0),
                str(room["name"]).casefold(),
            ),
        )
        self.rooms_list.DeleteAllItems()
        for room in rows:
            key = str(room["name"]).casefold()
            is_private = bool(room.get("private")) or key in private
            row = self.rooms_list.InsertItem(
                self.rooms_list.GetItemCount(), str(room["name"])
            )
            self.rooms_list.SetItem(row, 1, "Private" if is_private else "Public")
            self.rooms_list.SetItem(
                row, 2, "Joined" if room.get("joined") else "Remembered"
                if key in remembered_keys
                else "Available"
            )
            self.rooms_list.SetItem(row, 3, str(int(room.get("user_count") or 0)))

    def on_room_selected(self, event):
        row = event.GetIndex()
        room = self.rooms_list.GetItemText(row, 0)
        room_type = self.rooms_list.GetItemText(row, 1)
        self.room_combo.SetValue(room)
        self.private_check.SetValue(room_type == "Private")

    def _append_message(self, message):
        row = self.list.InsertItem(
            self.list.GetItemCount(), _clock(message["timestamp"])
        )
        self.list.SetItem(row, 1, message.get("room", ""))
        self.list.SetItem(row, 2, message.get("user", ""))
        self.list.SetItem(row, 3, message.get("message", ""))
        self.list.EnsureVisible(row)

    def _worker(self, action, success, *args):
        try:
            result = action(*args)
        except Exception as exc:  # noqa: BLE001 - presented in the UI
            wx.CallAfter(self._failed, str(exc))
            return
        wx.CallAfter(success, result)

    def _failed(self, error):
        if self._alive:
            self.frame.announce(f"Soulseek chat error: {error}")

    def on_refresh(self, event=None):
        self.frame.announce("Refreshing Soulseek rooms...")
        threading.Thread(
            target=self._worker,
            args=(soulseek_backend.refresh_rooms, self._refreshed, self.frame.config),
            daemon=True,
            name="blinddl-soulseek-rooms",
        ).start()

    def _refreshed(self, rooms):
        if not self._alive:
            return
        self._show_rooms(rooms)
        self.frame.announce(f"Found {len(rooms)} Soulseek rooms.")

    def on_join(self, event=None):
        room = self.room_combo.GetValue().strip()
        if not room:
            self.frame.announce("Enter a Soulseek room name.")
            return
        self.frame.announce(f"Joining {room}...")
        self._joining_private = self.private_check.GetValue()
        threading.Thread(
            target=self._worker,
            args=(
                soulseek_backend.join_room,
                self._joined,
                room,
                self.frame.config,
                self._joining_private,
            ),
            daemon=True,
            name="blinddl-soulseek-join-room",
        ).start()

    def _joined(self, room):
        if not self._alive:
            return
        saved = list(self.frame.config.get("soulseek_rooms", []) or [])
        if room.casefold() not in {value.casefold() for value in saved}:
            saved.append(room)
            self.frame.config["soulseek_rooms"] = saved
        private_rooms = list(
            self.frame.config.get("soulseek_private_rooms", []) or []
        )
        private_rooms = [
            value for value in private_rooms
            if value.casefold() != room.casefold()
        ]
        if self._joining_private:
            private_rooms.append(room)
        self.frame.config["soulseek_private_rooms"] = private_rooms
        self.frame.config.save()
        self.room_combo.SetValue(room)
        self._show_rooms(soulseek_backend.rooms_snapshot())
        self.frame.announce(f"Joined Soulseek room {room}.")
        self.message_text.SetFocus()

    def on_leave(self, event=None):
        room = self.room_combo.GetValue().strip()
        if not room:
            self.frame.announce("Select a Soulseek room.")
            return
        threading.Thread(
            target=self._worker,
            args=(soulseek_backend.leave_room, self._left, room, self.frame.config),
            daemon=True,
            name="blinddl-soulseek-leave-room",
        ).start()

    def _left(self, room):
        if not self._alive:
            return
        self.frame.config["soulseek_rooms"] = [
            value
            for value in self.frame.config.get("soulseek_rooms", []) or []
            if value.casefold() != room.casefold()
        ]
        self.frame.config["soulseek_private_rooms"] = [
            value
            for value in self.frame.config.get("soulseek_private_rooms", []) or []
            if value.casefold() != room.casefold()
        ]
        self.frame.config.save()
        self._show_rooms(soulseek_backend.rooms_snapshot())
        self.frame.announce(f"Left Soulseek room {room}.")

    def on_send(self, event=None):
        room = self.room_combo.GetValue().strip()
        message = self.message_text.GetValue().strip()
        if not room or not message:
            self.frame.announce("Select a room and enter a message.")
            return
        threading.Thread(
            target=self._worker,
            args=(
                soulseek_backend.send_room_message,
                self._sent,
                room,
                message,
                self.frame.config,
            ),
            daemon=True,
            name="blinddl-soulseek-room-message",
        ).start()

    def _sent(self, result):
        if self._alive:
            self.message_text.Clear()
            self.message_text.SetFocus()
