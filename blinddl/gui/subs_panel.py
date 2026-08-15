# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions tab: follow playlists/channels and auto-grab new items."""

import threading
from urllib.parse import urlparse

import wx

from .. import search_order, sideb_backend, ytdlp_backend


SUBS_SORT_ADDED = "added"
SUBS_SORT_TITLE = "title"
SUBS_SORT_SITE = "site"
SUBS_SORT_CHECKED = "checked"
SUBS_SORT_STALE = "stale"
SUBS_SORT_TRACKED = "tracked"
SUBS_SORT_ENABLED = "enabled"
SUBS_SORTS = (
    SUBS_SORT_ADDED,
    SUBS_SORT_TITLE,
    SUBS_SORT_SITE,
    SUBS_SORT_CHECKED,
    SUBS_SORT_STALE,
    SUBS_SORT_TRACKED,
    SUBS_SORT_ENABLED,
)
SUBS_SORT_LABELS = (
    "Date added",
    "Title",
    "Site",
    "Recently checked",
    "Needs checking",
    "Most tracked",
    "Enabled first",
)


def _site_name(url):
    host = urlparse(str(url or "")).hostname or ""
    return host.removeprefix("www.").casefold()


def _checked_value(sub):
    digits = "".join(char for char in str(sub.get("last_checked") or "")
                     if char.isdigit())
    return int(digits or 0)


def _sorted_subscriptions(subscriptions, mode):
    """Return a stable, useful view order without changing check order."""
    if mode not in SUBS_SORTS:
        mode = SUBS_SORT_ADDED
    indexed = list(enumerate(subscriptions))

    if mode == SUBS_SORT_ADDED:
        # Saved subscriptions from older releases have no timestamp; keeping
        # their file order avoids an arbitrary reshuffle during migration.
        def added_sort_key(pair):
            return -(pair[1].get("created_at") or 0), pair[0]
        key = added_sort_key
    elif mode == SUBS_SORT_TITLE:
        def title_sort_key(pair):
            return str(pair[1].get("title") or "").casefold(), pair[0]
        key = title_sort_key
    elif mode == SUBS_SORT_SITE:
        def site_sort_key(pair):
            return (
                _site_name(pair[1].get("url")),
                str(pair[1].get("title") or "").casefold(), pair[0])
        key = site_sort_key
    elif mode == SUBS_SORT_CHECKED:
        def checked_sort_key(pair):
            return (
                _checked_value(pair[1]) == 0,
                -_checked_value(pair[1]), pair[0])
        key = checked_sort_key
    elif mode == SUBS_SORT_STALE:
        def stale_sort_key(pair):
            return (
                _checked_value(pair[1]) != 0,
                _checked_value(pair[1]), pair[0])
        key = stale_sort_key
    elif mode == SUBS_SORT_TRACKED:
        def tracked_sort_key(pair):
            return -len(pair[1].get("seen_ids") or []), pair[0]
        key = tracked_sort_key
    else:  # enabled first
        def enabled_sort_key(pair):
            return not pair[1].get("enabled", True), pair[0]
        key = enabled_sort_key
    return [sub for _index, sub in sorted(indexed, key=key)]


class AddSubscriptionDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Add subscription")
        sizer = wx.BoxSizer(wx.VERTICAL)

        url_label = wx.StaticText(
            self,
            label="&URL, @handle, #hashtag, or playlist id:")
        self.url_text = wx.TextCtrl(self)
        self.url_text.SetName("Subscription URL")
        self.url_text.SetHelpText(
            "Subscribe to a playlist, channel, hashtag, or search results "
            "page.")
        self.existing_check = wx.CheckBox(
            self, label="&Download existing items")
        self.existing_check.SetName("Download existing items")

        order_label = wx.StaticText(self, label="Feed &order:")
        self.order_choice = wx.Choice(
            self, choices=search_order.ORDER_LABEL_LIST)
        self.order_choice.SetName("Subscription feed order")
        self.order_choice.SetHelpText(
            "Most recent is recommended for hashtags and search pages. "
            "Channels and playlists keep their natural order.")
        self.order_choice.SetSelection(
            search_order.ORDERS.index(search_order.ORDER_RECENT))

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer.Add(url_label, 0, wx.ALL, 8)
        sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(order_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.order_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.existing_check, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.url_text.SetFocus()


class SubsPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.displayed_subs = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        controls_row = wx.BoxSizer(wx.HORIZONTAL)
        sort_label = wx.StaticText(self, label="Sort &by:")
        self.sort_choice = wx.Choice(self, choices=list(SUBS_SORT_LABELS))
        self.sort_choice.SetName("Sort subscriptions")
        mode = self.frame.config.get("subs_sort", SUBS_SORT_ADDED)
        if mode not in SUBS_SORTS:
            mode = SUBS_SORT_ADDED
        self.sort_choice.SetSelection(SUBS_SORTS.index(mode))
        self.sort_choice.Bind(wx.EVT_CHOICE, self.on_sort_changed)
        controls_row.Add(sort_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        controls_row.Add(self.sort_choice, 0, wx.RIGHT, 16)

        interval_label = wx.StaticText(
            self, label="&Update interval (hours):")
        self.interval_spin = wx.SpinCtrl(
            self, min=1, max=168,
            initial=max(1, min(168, int(
                self.frame.config.get("sub_check_hours", 6)))),
        )
        self.interval_spin.SetName("Subscription update interval in hours")
        self.interval_spin.SetHelpText(
            "How often enabled subscriptions are checked automatically. "
            "Choose from 1 hour to 168 hours, then press Apply interval.")
        self.interval_button = wx.Button(self, label="&Apply interval")
        self.interval_button.SetName("Apply subscription update interval")
        self.interval_button.Bind(wx.EVT_BUTTON, self.on_interval_changed)
        controls_row.Add(
            interval_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        controls_row.Add(self.interval_spin, 0, wx.RIGHT, 4)
        controls_row.Add(self.interval_button, 0)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.list.SetName("Subscriptions")
        self.list.SetHelpText(
            "Select subscriptions. Context Menu opens actions.")
        for i, heading in enumerate(("Title", "URL", "Feed order", "Enabled",
                                     "Last checked", "Tracked")):
            self.list.InsertColumn(i, heading)
        self.list.SetColumnWidth(0, 250)
        self.list.SetColumnWidth(1, 300)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)

        sizer.Add(controls_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.refresh()

    # -- display -----------------------------------------------------------

    def refresh(self):
        selected_ids = {
            self.displayed_subs[row]["id"] for row in self._selected_rows()
            if row < len(self.displayed_subs)
        }
        mode_index = self.sort_choice.GetSelection()
        mode = (SUBS_SORTS[mode_index]
                if 0 <= mode_index < len(SUBS_SORTS) else SUBS_SORT_ADDED)
        self.displayed_subs = _sorted_subscriptions(
            self.frame.subs.snapshot(), mode)
        self.list.DeleteAllItems()
        for row, sub in enumerate(self.displayed_subs):
            self.list.InsertItem(row, sub["title"])
            self.list.SetItem(row, 1, sub["url"])
            self.list.SetItem(
                row, 2, search_order.label(sub.get("order")))
            self.list.SetItem(row, 3, "Yes" if sub.get("enabled", True) else "No")
            self.list.SetItem(row, 4, sub.get("last_checked") or "Never")
            self.list.SetItem(row, 5, str(len(sub.get("seen_ids") or [])))
            if sub["id"] in selected_ids:
                self.list.Select(row)

    def _selected_subs(self):
        return [self.displayed_subs[row] for row in self._selected_rows()
                if row < len(self.displayed_subs)]

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
            order_menu = wx.Menu()
            selected_subs = self._selected_subs()
            selected_orders = {
                search_order.normalize(sub.get("order"))
                for sub in selected_subs
            }
            for order in search_order.ORDERS:
                order_item = order_menu.AppendRadioItem(
                    wx.ID_ANY, search_order.label(order))
                order_item.Check(selected_orders == {order})
                order_menu.Bind(
                    wx.EVT_MENU,
                    lambda _event, selected_order=order:
                    self._set_feed_order(selected_order),
                    order_item,
                )
            menu.AppendSubMenu(order_menu, "Feed &order")
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

    def on_sort_changed(self, event):
        selection = self.sort_choice.GetSelection()
        mode = (SUBS_SORTS[selection]
                if 0 <= selection < len(SUBS_SORTS) else SUBS_SORT_ADDED)
        self.frame.config["subs_sort"] = mode
        save = getattr(self.frame.config, "save", None)
        if save is not None:
            save()
        self.refresh()
        label = SUBS_SORT_LABELS[SUBS_SORTS.index(mode)]
        self.frame.announce(f"Subscriptions sorted by {label}.")
        if event is not None:
            event.Skip()

    def on_interval_changed(self, event):
        hours = self.interval_spin.GetValue()
        self.frame.config["sub_check_hours"] = hours
        save = getattr(self.frame.config, "save", None)
        if save is not None:
            save()
        self.frame.subs.wake()
        noun = "hour" if hours == 1 else "hours"
        self.frame.announce(
            f"Subscriptions will update every {hours} {noun}.")
        if event is not None:
            event.Skip()

    # -- actions -----------------------------------------------------------

    def on_add(self, event):
        dialog = AddSubscriptionDialog(self)
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        url = ytdlp_backend.normalize_url(dialog.url_text.GetValue())
        download_existing = dialog.existing_check.GetValue()
        order_selection = dialog.order_choice.GetSelection()
        order = (search_order.ORDERS[order_selection]
                 if 0 <= order_selection < len(search_order.ORDERS)
                 else search_order.ORDER_RECENT)
        dialog.Destroy()
        if not url:
            self.frame.announce("Enter a URL.")
            return
        self.frame.announce("Reading URL...")
        threading.Thread(target=self._add_worker,
                         args=(url, download_existing, order),
                         daemon=True).start()

    def _add_worker(self, url, download_existing,
                    order=search_order.ORDER_RECENT):
        try:
            if sideb_backend.is_deezer_url(url):
                items, title = sideb_backend.extract_flat(
                    url, self.frame.config)
            else:
                items, title = ytdlp_backend.extract_flat(
                    url, cookies_from_browser=
                    self.frame.config["cookies_from_browser"],
                    cookies_file=self.frame.config.get("cookies_file"),
                    limit=ytdlp_backend.SUBSCRIPTION_FEED_LIMIT,
                    order=order)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._add_failed, str(exc))
            return
        wx.CallAfter(
            self._add_done, url, title, items, download_existing, order)

    def _add_failed(self, error):
        self.frame.announce("Could not add subscription.")
        wx.MessageBox(f"Could not read that URL:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _add_done(self, url, title, items, download_existing,
                  order=search_order.ORDER_RECENT):
        self.frame.subs.add(
            url, title, [i["id"] for i in items], order=order)
        if download_existing:
            with self.frame.queue.batch_additions():
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

    def _set_feed_order(self, order):
        subs = self._selected_subs()
        if not subs:
            self.frame.announce("Select a subscription.")
            return
        for sub in subs:
            self.frame.subs.set_order(sub["id"], order)
        self.refresh()
        noun = "subscription" if len(subs) == 1 else "subscriptions"
        self.frame.announce(
            f"Set {len(subs)} {noun} to {search_order.label(order)}.")

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
