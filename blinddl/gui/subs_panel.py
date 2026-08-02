# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions tab: follow playlists/channels and auto-grab new items."""

import threading

import wx

from .. import sideb_backend, ytdlp_backend


class AddSubscriptionDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Add subscription")
        sizer = wx.BoxSizer(wx.VERTICAL)

        url_label = wx.StaticText(self, label="&URL:")
        self.url_text = wx.TextCtrl(self)
        self.url_text.SetName("Subscription URL")
        self.existing_check = wx.CheckBox(
            self, label="&Download existing items")
        self.existing_check.SetName("Download existing items")

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer.Add(url_label, 0, wx.ALL, 8)
        sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.existing_check, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.url_text.SetFocus()


class SubsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Subscriptions")
        self.list.SetHelpText(
            "Select subscriptions. Context Menu opens actions.")
        for i, heading in enumerate(("Title", "URL", "Enabled",
                                     "Last checked", "Tracked")):
            self.list.InsertColumn(i, heading)
        self.list.SetColumnWidth(0, 250)
        self.list.SetColumnWidth(1, 300)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)

        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.refresh()

    # -- display -----------------------------------------------------------

    def refresh(self):
        self.list.DeleteAllItems()
        for row, sub in enumerate(self.frame.subs.snapshot()):
            self.list.InsertItem(row, sub["title"])
            self.list.SetItem(row, 1, sub["url"])
            self.list.SetItem(row, 2, "Yes" if sub.get("enabled", True) else "No")
            self.list.SetItem(row, 3, sub.get("last_checked") or "Never")
            self.list.SetItem(row, 4, str(len(sub.get("seen_ids") or [])))

    def _selected_subs(self):
        subs = self.frame.subs.snapshot()
        return [subs[row] for row in self._selected_rows()
                if row < len(subs)]

    def _selected_rows(self):
        rows = []
        row = self.list.GetFirstSelected()
        while row != -1:
            rows.append(row)
            row = self.list.GetNextSelected(row)
        return rows

    def on_context_menu(self, event):
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            if not self._selected_rows():
                focused = self.list.GetFocusedItem()
                if focused >= 0:
                    self.list.Select(focused)
        else:
            row, _flags = self.list.HitTest(
                self.list.ScreenToClient(position))
            if row != wx.NOT_FOUND and not self.list.IsSelected(row):
                for selected_row in self._selected_rows():
                    self.list.Select(selected_row, False)
                self.list.Select(row)

        selected = bool(self._selected_subs())
        menu = wx.Menu()
        add_item = menu.Append(wx.ID_ADD, "&Add subscription...")
        menu.Bind(wx.EVT_MENU, self.on_add, add_item)
        if selected:
            menu.AppendSeparator()
            check_item = menu.Append(wx.ID_ANY, "&Check now")
            enable_item = menu.Append(wx.ID_ANY, "&Enable")
            disable_item = menu.Append(wx.ID_ANY, "&Disable")
            remove_item = menu.Append(wx.ID_REMOVE, "&Remove")
            menu.Bind(wx.EVT_MENU, self.on_check_now, check_item)
            menu.Bind(wx.EVT_MENU, self.on_enable, enable_item)
            menu.Bind(wx.EVT_MENU, self.on_disable, disable_item)
            menu.Bind(wx.EVT_MENU, self.on_remove, remove_item)
        menu.AppendSeparator()
        select_all = menu.Append(wx.ID_SELECTALL, "Select &all")
        clear_selection = menu.Append(wx.ID_ANY, "&Clear selection")
        select_all.Enable(
            self.list.GetSelectedItemCount() < self.list.GetItemCount())
        clear_selection.Enable(selected)
        menu.Bind(wx.EVT_MENU, self._select_all, select_all)
        menu.Bind(wx.EVT_MENU, self._clear_selection, clear_selection)
        self.list.PopupMenu(menu)
        menu.Destroy()

    def _select_all(self, event):
        for row in range(self.list.GetItemCount()):
            self.list.Select(row)
        self.frame.announce(
            f"Selected {self.list.GetSelectedItemCount()} subscriptions.")

    def _clear_selection(self, event):
        for row in self._selected_rows():
            self.list.Select(row, False)
        self.frame.announce("Selection cleared.")

    # -- actions -----------------------------------------------------------

    def on_add(self, event):
        dialog = AddSubscriptionDialog(self)
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        url = dialog.url_text.GetValue().strip()
        download_existing = dialog.existing_check.GetValue()
        dialog.Destroy()
        if not url:
            self.frame.announce("Enter a URL.")
            return
        self.frame.announce("Reading URL...")
        threading.Thread(target=self._add_worker,
                         args=(url, download_existing), daemon=True).start()

    def _add_worker(self, url, download_existing):
        try:
            if sideb_backend.is_deezer_url(url):
                items, title = sideb_backend.extract_flat(
                    url, self.frame.config)
            else:
                items, title = ytdlp_backend.extract_flat(
                    url, cookies_from_browser=
                    self.frame.config["cookies_from_browser"])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._add_failed, str(exc))
            return
        wx.CallAfter(self._add_done, url, title, items, download_existing)

    def _add_failed(self, error):
        self.frame.announce("Could not add subscription.")
        wx.MessageBox(f"Could not read that URL:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _add_done(self, url, title, items, download_existing):
        self.frame.subs.add(url, title, [i["id"] for i in items])
        if download_existing:
            for item in items:
                if item.get("kind") == "sideb":
                    self.frame.queue.add_sideb(item["url"], item["title"])
                else:
                    self.frame.queue.add_ytdlp(item["url"], item["title"])
            noun = "item" if len(items) == 1 else "items"
            self.frame.announce(
                f"Subscribed: {title}. Queued {len(items)} {noun}.")
        else:
            self.frame.announce(f"Subscribed: {title}.")
        self.refresh()

    def on_remove(self, event):
        subs = self._selected_subs()
        if not subs:
            self.frame.announce("Select a subscription.")
            return
        prompt = (f"Remove {subs[0]['title']}?" if len(subs) == 1
                  else f"Remove {len(subs)} subscriptions?")
        answer = wx.MessageBox(prompt,
                               "blindDL", wx.YES_NO | wx.ICON_QUESTION, self)
        if answer == wx.YES:
            for sub in subs:
                self.frame.subs.remove(sub["id"])
            self.refresh()
            noun = "subscription" if len(subs) == 1 else "subscriptions"
            self.frame.announce(f"Removed {len(subs)} {noun}.")

    def _set_enabled(self, enabled):
        subs = self._selected_subs()
        if not subs:
            self.frame.announce("Select a subscription.")
            return
        for sub in subs:
            self.frame.subs.set_enabled(sub["id"], enabled)
        self.refresh()
        noun = "subscription" if len(subs) == 1 else "subscriptions"
        self.frame.announce(
            f"{'Enabled' if enabled else 'Disabled'} {len(subs)} {noun}.")

    def on_enable(self, event):
        self._set_enabled(True)

    def on_disable(self, event):
        self._set_enabled(False)

    def on_check_now(self, event):
        subs = self._selected_subs()
        if not subs:
            self.frame.announce("Select a subscription.")
            return
        noun = "subscription" if len(subs) == 1 else "subscriptions"
        self.frame.announce(f"Checking {len(subs)} {noun}...")
        threading.Thread(target=self._check_worker,
                         args=([(sub["id"], sub["title"])
                                for sub in subs],),
                         daemon=True).start()

    def _check_worker(self, subscriptions):
        total = 0
        errors = []
        for sub_id, title in subscriptions:
            count, error = self.frame.subs.check_one(sub_id)
            total += count
            if error:
                errors.append(f"{title}: {error}")
        wx.CallAfter(self._check_done, total, errors)

    def _check_done(self, count, errors):
        self.refresh()
        if errors:
            noun = "check" if len(errors) == 1 else "checks"
            self.frame.announce(f"{len(errors)} {noun} failed.")
            wx.MessageBox("Check failed:\n" + "\n".join(errors), "blindDL",
                          wx.OK | wx.ICON_ERROR, self)
        elif count:
            noun = "item" if count == 1 else "items"
            self.frame.announce(f"Queued {count} new {noun}.")
        else:
            self.frame.announce("No new items.")
