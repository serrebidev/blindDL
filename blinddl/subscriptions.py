# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions: follow playlists/channels and auto-download new items.

State lives in %APPDATA%/blindDL/subscriptions.json. A background thread
periodically re-lists each subscription URL via yt-dlp flat extraction and
queues anything not in the stored seen-ids list.
"""

import json
import os
import threading
import time
import uuid

from .config import app_data_dir
from . import sideb_backend, ytdlp_backend

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
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.subs = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.subs = []

    def save(self):
        with self._lock:
            subs = list(self.subs)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(subs, f, indent=2)
        except OSError:
            pass

    # -- CRUD -------------------------------------------------------------

    def add(self, url, title, seen_ids):
        sub = {
            "id": uuid.uuid4().hex,
            "url": url,
            "title": title or url,
            "enabled": True,
            "seen_ids": list(seen_ids)[-MAX_SEEN_IDS:],
            "last_checked": None,
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
        sub = self.get(sub_id)
        if sub is None:
            return 0, "Subscription not found."
        try:
            if sideb_backend.is_deezer_url(sub["url"]):
                items, title = sideb_backend.extract_flat(
                    sub["url"], self.config)
            else:
                items, title = ytdlp_backend.extract_flat(sub["url"])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return 0, str(exc)
        if title:
            sub["title"] = title
        seen = set(sub.get("seen_ids") or [])
        new_items = [i for i in items if i["id"] not in seen]
        for item in new_items:
            if item.get("kind") == "sideb":
                self.queue.add_sideb(item["url"], item["title"])
            else:
                self.queue.add_ytdlp(item["url"], item["title"],
                                     audio_only=audio_only)
            seen.add(item["id"])
        sub["seen_ids"] = list(seen)[-MAX_SEEN_IDS:]
        sub["last_checked"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()
        return len(new_items), ""

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
