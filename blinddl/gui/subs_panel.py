# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions tab: follow feeds, artists and people, and auto-grab.

Three things can be followed and they are added the same way: pick what it
is, type it, and the check that follows lists it accordingly. An artist can
be named rather than linked -- "Daft Punk" is what someone means, and
finding their catalogue page first is a step they should not have to take.
"""

import threading
from urllib.parse import urlparse

import wx

from .. import search_order, subscriptions, ytdlp_backend


SUBS_SORT_ADDED = "added"
SUBS_SORT_TITLE = "title"
SUBS_SORT_KIND = "kind"
SUBS_SORT_SITE = "site"
SUBS_SORT_CHECKED = "checked"
SUBS_SORT_STALE = "stale"
SUBS_SORT_TRACKED = "tracked"
SUBS_SORT_ENABLED = "enabled"
SUBS_SORTS = (
    SUBS_SORT_ADDED,
    SUBS_SORT_TITLE,
    SUBS_SORT_KIND,
    SUBS_SORT_SITE,
    SUBS_SORT_CHECKED,
    SUBS_SORT_STALE,
    SUBS_SORT_TRACKED,
    SUBS_SORT_ENABLED,
)
SUBS_SORT_LABELS = (
    "Date added",
    "Title",
    "What it follows",
    "Site",
    "Recently checked",
    "Needs checking",
    "Most tracked",
    "Enabled first",
)


def _site_name(url):
    url = str(url or "")
    if url.startswith(subscriptions.USER_URL_PREFIX):
        return "soulseek"
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.").casefold()


def _checked_value(sub):
    digits = "".join(char for char in str(sub.get("last_checked") or "")
                     if char.isdigit())
    return int(digits or 0)


def _sorted_subscriptions(rows, mode):
    """Return a stable, useful view order without changing check order.

    The rows are named *rows* rather than *subscriptions*: the module of
    that name is what says which kind each of them is.
    """
    if mode not in SUBS_SORTS:
        mode = SUBS_SORT_ADDED
    indexed = list(enumerate(rows))

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
    elif mode == SUBS_SORT_KIND:
        def kind_sort_key(pair):
            return (
                subscriptions.kind_label(pair[1].get("kind")),
                str(pair[1].get("title") or "").casefold(), pair[0])
        key = kind_sort_key
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


# What the text box asks for, and what it is called, for each thing that can
# be followed. A choice that renames the field next to it is what keeps one
# dialog from needing three.
_PROMPTS = {
    subscriptions.KIND_FEED: (
        "&URL, @handle, #hashtag, or playlist id:",
        "Subscription URL",
        "Follow a playlist, channel, hashtag, or search results page. "
        "Deezer and Apple Music playlist links work here too.",
    ),
    subscriptions.KIND_ARTIST: (
        "&Artist name, or a link to their page:",
        "Artist to follow",
        "Follow an artist by name, or by a Deezer or Apple Music link to "
        "their page. Their new releases are downloaded a record at a time, "
        "each into a folder of its own.",
    ),
    subscriptions.KIND_USER: (
        "Soulseek &username:",
        "Soulseek user to follow",
        "Follow what a Soulseek user shares. Anything they add to their "
        "shares afterwards is downloaded into a folder named after them.",
    ),
}


class AddSubscriptionDialog(wx.Dialog):
    def __init__(self, parent, kind=subscriptions.KIND_FEED):
        super().__init__(parent, title="Add subscription")
        sizer = wx.BoxSizer(wx.VERTICAL)

        kind_label = wx.StaticText(self, label="&Follow:")
        self.kind_choice = wx.Choice(
            self, choices=subscriptions.KIND_LABEL_LIST)
        self.kind_choice.SetName("What to follow")
        self.kind_choice.SetHelpText(
            "A link publishes items, an artist publishes releases, and a "
            "Soulseek user shares files. Each is listed the way it is "
            "published.")
        self.kind_choice.SetSelection(
            subscriptions.KINDS.index(subscriptions.normalize_kind(kind)))
        self.kind_choice.Bind(wx.EVT_CHOICE, self.on_kind_changed)

        self.url_label = wx.StaticText(self, label=_PROMPTS[
            subscriptions.KIND_FEED][0])
        self.url_text = wx.TextCtrl(self)
        self.existing_check = wx.CheckBox(
            self, label="&Download existing items")
        self.existing_check.SetName("Download existing items")

        self.order_label = wx.StaticText(self, label="Feed &order:")
        self.order_choice = wx.Choice(
            self, choices=search_order.ORDER_LABEL_LIST)
        self.order_choice.SetName("Subscription feed order")
        self.order_choice.SetHelpText(
            "Most recent is recommended for hashtags and search pages. "
            "Channels and playlists keep their natural order.")
        self.order_choice.SetSelection(
            search_order.ORDERS.index(search_order.ORDER_RECENT))

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer.Add(kind_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.kind_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.url_label, 0, wx.ALL, 8)
        sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.order_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.order_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.existing_check, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizerAndFit(sizer)
        self.on_kind_changed(None)
        self.url_text.SetFocus()

    def selected_kind(self):
        selection = self.kind_choice.GetSelection()
        if 0 <= selection < len(subscriptions.KINDS):
            return subscriptions.KINDS[selection]
        return subscriptions.KIND_FEED

    def on_kind_changed(self, event):
        """Ask for the thing that was chosen, by the name it goes by."""
        kind = self.selected_kind()
        label, name, help_text = _PROMPTS[kind]
        self.url_label.SetLabel(label)
        self.url_text.SetName(name)
        self.url_text.SetHelpText(help_text)
        # A feed order is a question about a feed. An artist's releases and
        # a person's shares have one order apiece, so it is switched off
        # rather than left there offering a choice that changes nothing.
        feed = kind == subscriptions.KIND_FEED
        self.order_choice.Enable(feed)
        self.order_label.Enable(feed)
        self.Layout()
        if event is not None:
            event.Skip()


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
        for i, heading in enumerate(("Title", "Follows", "URL", "Feed order",
                                     "Enabled", "Last checked", "Tracked")):
            self.list.InsertColumn(i, heading)
        self.list.SetColumnWidth(0, 250)
        self.list.SetColumnWidth(1, 110)
        self.list.SetColumnWidth(2, 300)
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
            kind = subscriptions.normalize_kind(sub.get("kind"))
            self.list.InsertItem(row, sub["title"])
            self.list.SetItem(row, 1, subscriptions.kind_label(kind))
            self.list.SetItem(row, 2, sub["url"])
            self.list.SetItem(
                row, 3,
                search_order.label(sub.get("order"))
                if kind == subscriptions.KIND_FEED else "")
            self.list.SetItem(row, 4, "Yes" if sub.get("enabled", True) else "No")
            self.list.SetItem(row, 5, sub.get("last_checked") or "Never")
            self.list.SetItem(row, 6, str(len(sub.get("seen_ids") or [])))
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
            selected_subs = self._selected_subs()
            if any(subscriptions.normalize_kind(sub.get("kind"))
                   == subscriptions.KIND_FEED for sub in selected_subs):
                order_menu = wx.Menu()
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

    def on_add(self, event, kind=subscriptions.KIND_FEED):
        dialog = AddSubscriptionDialog(self, kind)
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        kind = dialog.selected_kind()
        text = dialog.url_text.GetValue().strip()
        download_existing = dialog.existing_check.GetValue()
        order_selection = dialog.order_choice.GetSelection()
        order = (search_order.ORDERS[order_selection]
                 if 0 <= order_selection < len(search_order.ORDERS)
                 else search_order.ORDER_RECENT)
        dialog.Destroy()
        if not text:
            self.frame.announce(
                "Enter a username." if kind == subscriptions.KIND_USER
                else "Enter a name or a URL.")
            return
        self.follow(text, kind, download_existing, order)

    def follow(self, text, kind=subscriptions.KIND_FEED,
               download_existing=False, order=search_order.ORDER_RECENT):
        """Start following *text*, whatever kind of thing it names.

        Called by the Add dialog and by the Follow commands on the Search
        page and the Soulseek browser, so subscribing to what is already on
        screen never means typing its name out again.
        """
        kind = subscriptions.normalize_kind(kind)
        text = str(text or "").strip()
        if not text:
            self.frame.announce("Nothing to follow.")
            return
        if kind == subscriptions.KIND_FEED:
            text = ytdlp_backend.normalize_url(text)
        self.frame.announce({
            subscriptions.KIND_ARTIST: f"Looking up {text}...",
            subscriptions.KIND_USER: f"Reading {text}'s shared files...",
        }.get(kind, "Reading URL..."))
        threading.Thread(target=self._add_worker,
                         args=(text, download_existing, order, kind),
                         daemon=True).start()

    def _add_worker(self, text, download_existing,
                    order=search_order.ORDER_RECENT,
                    kind=subscriptions.KIND_FEED):
        username = ""
        url = text
        try:
            if kind == subscriptions.KIND_ARTIST:
                url, name = subscriptions.resolve_artist(text)
                items, title = subscriptions.artist_releases(url)
                title = title or name
            elif kind == subscriptions.KIND_USER:
                username = text
                url = subscriptions.USER_URL_PREFIX + username
                items, title = subscriptions.user_files(
                    username, self.frame.config)
            else:
                items, title = subscriptions.listing(
                    {"url": url, "kind": kind, "order": order},
                    self.frame.config)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            wx.CallAfter(self._add_failed, str(exc), kind)
            return
        wx.CallAfter(self._add_done, url, title, items, download_existing,
                     order, kind, username)

    def _add_failed(self, error, kind=subscriptions.KIND_FEED):
        self.frame.announce("Could not add subscription.")
        what = {
            subscriptions.KIND_ARTIST: "Could not follow that artist",
            subscriptions.KIND_USER: "Could not read that user's files",
        }.get(kind, "Could not read that URL")
        wx.MessageBox(f"{what}:\n{error}", "blindDL",
                      wx.OK | wx.ICON_ERROR, self)

    def _add_done(self, url, title, items, download_existing,
                  order=search_order.ORDER_RECENT,
                  kind=subscriptions.KIND_FEED, username=""):
        self.frame.subs.add(
            url, title, [i["id"] for i in items], order=order, kind=kind,
            username=username)
        self.refresh()
        if not download_existing:
            self.frame.announce(f"Subscribed: {title}.")
            return
        # An artist's releases are albums, and one row of the listing is a
        # whole record, so what is queued is counted in transfers rather
        # than in rows: "queued 3 items" for a discography would be a lie.
        folder = "" if kind in (
            subscriptions.KIND_ARTIST, subscriptions.KIND_USER) else title
        added, errors = self.frame.subs.queue_all(items, folder=folder)
        noun = "item" if added == 1 else "items"
        message = f"Subscribed: {title}. Queued {added} {noun}."
        if errors:
            failed = "one" if len(errors) == 1 else str(len(errors))
            message += f" {failed} could not be read."
        self.frame.announce(message)

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
