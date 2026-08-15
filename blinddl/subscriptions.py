# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions: follow playlists/channels and auto-download new items.

State lives in %APPDATA%/blindDL/subscriptions.json. A background thread
periodically re-lists each subscription URL via yt-dlp flat extraction and
queues anything not in the stored seen-ids list.
"""

import copy
import json
import os
import threading
import time
import uuid

from .config import app_data_dir
from . import search_order, sideb_backend, ytdlp_backend

MAX_SEEN_IDS = 5000


class SubscriptionStore:
    def __init__(self, config, queue, notify=None):
        self.config = config
        self.queue = queue
        # notify(message: str) is used for user-visible status announcements.
        self.notify = notify
        self.path = os.path.join(app_data_dir(), "subscriptions.json")
        self.subs = []
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.subs = []
            return
        if not isinstance(saved, list):
            self.subs = []
            return
        self.subs = []
        for item in saved:
            if not isinstance(item, dict):
                continue
            sub_id = str(item.get("id") or "").strip()
            url = str(item.get("url") or "").strip()
            if not sub_id or not url:
                continue
            row = dict(item)
            row["id"] = sub_id
            row["url"] = url
            row["title"] = str(item.get("title") or url)
            row["enabled"] = bool(item.get("enabled", True))
            if not isinstance(row.get("seen_ids"), list):
                row["seen_ids"] = []
            else:
                row["seen_ids"] = [
                    str(value)
                    for value in row["seen_ids"]
                    if isinstance(value, (str, int, float))
                    and not isinstance(value, bool)
                ]
            self.subs.append(row)

    def save(self):
        with self._lock:
            subs = copy.deepcopy(self.subs)
        temporary = self.path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as f:
                json.dump(subs, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass

    # -- CRUD -------------------------------------------------------------

    def add(self, url, title, seen_ids,
            order=search_order.ORDER_RECENT):
        order = search_order.normalize(order)
        remembered = list(dict.fromkeys(seen_ids))
        if order == search_order.ORDER_RECENT:
            # A newest-first feed arrives newest to oldest. Store it the other
            # way round so slicing from the end retains the newest IDs.
            remembered.reverse()
        sub = {
            "id": uuid.uuid4().hex,
            "url": url,
            "title": title or url,
            "enabled": True,
            "seen_ids": remembered[-MAX_SEEN_IDS:],
            "last_checked": None,
            # Newest first is the useful subscription default: it follows new
            # uploads instead of a hashtag/search page's changing trend list.
            # Older saved rows omit this and retain their old best-match
            # behaviour in check_one.
            "order": order,
            "created_at": time.time(),
        }
        with self._lock:
            self.subs.append(sub)
        self.save()
        return sub

    def remove(self, sub_id):
        with self._lock:
            self.subs = [s for s in self.subs if s["id"] != sub_id]
        self.save()

    def set_enabled(self, sub_id, enabled):
        sub = self.get(sub_id)
        if sub is not None:
            sub["enabled"] = enabled
            self.save()

    def set_order(self, sub_id, order):
        sub = self.get(sub_id)
        if sub is not None:
            sub["order"] = search_order.normalize(order)
            self.save()

    def get(self, sub_id):
        with self._lock:
            for sub in self.subs:
                if sub["id"] == sub_id:
                    return sub
        return None

    def snapshot(self):
        with self._lock:
            return [dict(s) for s in self.subs]

    # -- checking ---------------------------------------------------------

    def check_one(self, sub_id, audio_only=None):
        """Check a single subscription; queues newly published items.

        Returns (new_count, error_message).
        """
        with self._check_lock:
            return self._check_one(sub_id, audio_only)

    def _check_one(self, sub_id, audio_only=None):
        sub = self.get(sub_id)
        if sub is None:
            return 0, "Subscription not found."
        try:
            if sideb_backend.is_deezer_url(sub["url"]):
                items, title = sideb_backend.extract_flat(
                    sub["url"], self.config)
            else:
                items, title = ytdlp_backend.extract_flat(
                    sub["url"], cookies_from_browser=
                    self.config["cookies_from_browser"],
                    cookies_file=self.config.get("cookies_file"),
                    limit=ytdlp_backend.SUBSCRIPTION_FEED_LIMIT,
                    order=search_order.normalize(
                        sub.get("order", search_order.ORDER_RELEVANCE)))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return 0, str(exc)
        if title:
            sub["title"] = title
        # Keep insertion order: the trim below has to drop the oldest ids,
        # and a set would hand back an arbitrary order.
        seen_ids = list(sub.get("seen_ids") or [])
        seen = set(seen_ids)
        new_ids = []
        new_count = 0
        # A subscription is a channel or a playlist, so what it publishes
        # goes in a folder named after it, the same way a channel URL
        # downloaded by hand does.
        folder = sub.get("title") or title or ""
        with self.queue.batch_additions():
            for item in items:
                if item["id"] in seen:
                    continue
                if item.get("kind") == "sideb":
                    self.queue.add_sideb(item["url"], item["title"],
                                         folder=folder)
                else:
                    self.queue.add_ytdlp(
                        item["url"], item["title"], audio_only=audio_only,
                        folder=folder
                    )
                seen.add(item["id"])
                new_ids.append(item["id"])
                new_count += 1
        if search_order.normalize(
                sub.get("order", search_order.ORDER_RELEVANCE)
        ) == search_order.ORDER_RECENT:
            # Keep persisted history oldest-to-newest even though the feed is
            # newest-first. The queue above still follows the requested order.
            new_ids.reverse()
        seen_ids.extend(new_ids)
        sub["seen_ids"] = seen_ids[-MAX_SEEN_IDS:]
        sub["last_checked"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()
        return new_count, ""

    def check_all(self):
        for sub in self.snapshot():
            if not sub.get("enabled", True):
                continue
            count, error = self.check_one(sub["id"])
            if self.notify:
                if error:
                    self.notify(f"Subscription check failed for {sub['title']}: {error}")
                elif count:
                    self.notify(f"{sub['title']}: queued {count} new item(s).")

    # -- background loop ----------------------------------------------------

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="blinddl-subscriptions")
            self._thread.start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def wake(self):
        """Re-apply the configured interval (e.g. after settings change)."""
        self._wake.set()

    def _loop(self):
        # First check shortly after startup so a fresh launch catches up.
        self._wake.wait(30)
        while not self._stop:
            self._wake.clear()
            try:
                self.check_all()
            except Exception:  # noqa: BLE001 - never kill the loop
                pass
            interval = max(1, int(self.config["sub_check_hours"])) * 3600
            self._wake.wait(interval)
