# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Subscriptions: follow a feed, an artist, or a person, and auto-download.

State lives in %APPDATA%/blindDL/subscriptions.json. A background thread
periodically re-lists every subscription and queues whatever is not in its
stored seen-ids list.

There are three things worth following, and they are listed three different
ways:

* A **link** -- a channel, a playlist, a hashtag, a search page. Listed by
  yt-dlp (or Deezer, for a deezer.com link) as the items it publishes.
* An **artist**. Listed as their *releases*, not their tracks: a new album
  by someone you follow should arrive as an album, in a folder of its own,
  and a discography flattened into nine hundred loose tracks is neither
  something to check for changes nor something to receive.
* A **Soulseek user**. Listed as the files they share, so anything they add
  to their shares arrives without having to browse them again.

Everything after the listing is common: unseen ids are queued, seen ids are
remembered, and the check is the same either way.
"""

import copy
import hashlib
import json
import ntpath
import os
import threading
import time
import uuid

from .config import app_data_dir
from . import (
    applemusic_backend,
    deezer_backend,
    search_kind,
    search_order,
    sideb_backend,
    soulseek_backend,
    ytdlp_backend,
)

MAX_SEEN_IDS = 5000
# A Soulseek share is not a feed of a few dozen recent items: someone can
# share tens of thousands of files, and every one of them has to stay
# remembered or it comes back as new. They are remembered as short digests
# of their paths rather than the paths themselves, which is what keeps a
# large share worth a few hundred kilobytes instead of several megabytes.
MAX_SEEN_SHARED = 50000

# What a subscription follows. Saved rows from before this existed have no
# kind at all, and a link is what they all were.
KIND_FEED = "feed"
KIND_ARTIST = "artist"
KIND_USER = "user"
KINDS = (KIND_FEED, KIND_ARTIST, KIND_USER)
KIND_LABELS = {
    KIND_FEED: "Link",
    KIND_ARTIST: "Artist",
    KIND_USER: "Soulseek user",
}
KIND_LABEL_LIST = [KIND_LABELS[kind] for kind in KINDS]


# A followed person has no page to link to, so the store's url column
# carries this instead: it keeps one saved row shaped like every other one.
USER_URL_PREFIX = "soulseek:user/"


def normalize_kind(kind):
    """Return a known subscription kind, falling back to a link."""
    return kind if kind in KINDS else KIND_FEED


def kind_label(kind):
    return KIND_LABELS[normalize_kind(kind)]


def _is_apple_artist(url):
    info = applemusic_backend.parse_apple_url(url)
    return bool(info and info["media_type"] == "artist")


def resolve_artist(text):
    """(artist URL, artist name) for a link to an artist or a typed name.

    A name is looked up in Deezer's artist catalogue and the best match is
    taken, because "follow Daft Punk" is what someone means, and finding
    their page first is a step they should not have to take.
    """
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("Enter an artist name, or a link to one.")
    if applemusic_backend.is_apple_music_url(text):
        info = applemusic_backend.parse_apple_url(text)
        if not info or info["media_type"] != "artist":
            raise RuntimeError("That Apple Music link is not an artist page.")
        _releases, name = applemusic_backend.artist_albums(info["media_id"])
        return text, name or text
    parsed = deezer_backend.parse_url(text)
    if parsed is not None:
        if parsed[0] != "artist":
            raise RuntimeError("That Deezer link is not an artist page.")
        _releases, name = deezer_backend.artist_albums(parsed[1])
        return text, name or text
    if "://" in text:
        raise RuntimeError(
            "Follow an artist by name, or by a Deezer or Apple Music link "
            "to their page.")
    found = deezer_backend.search_artists(text)
    if not found:
        raise RuntimeError(f"No artist found for: {text}")
    return found[0]["url"], found[0]["name"] or text


def artist_releases(url):
    """(release rows, artist name) for an artist URL.

    Albums, EPs and singles alike, newest first as the catalogue lists
    them -- one row per release, which is what a new release has to be to
    be noticed as one.

    The track counts a search page shows next to each release are left out:
    they cost a request apiece, and a check that spent Deezer's whole rate
    limit counting tracks had none left to read the new release with.
    """
    if _is_apple_artist(url):
        info = applemusic_backend.parse_apple_url(url)
        return applemusic_backend.artist_albums(info["media_id"])
    parsed = deezer_backend.parse_url(url)
    if parsed is None or parsed[0] != "artist":
        raise RuntimeError(f"Not an artist page: {url}")
    return deezer_backend.artist_albums(parsed[1], track_counts=False)


def _share_id(remote_path):
    """A short, stable id for one shared file, from its own path."""
    normalized = str(remote_path or "").replace("/", "\\").casefold()
    return hashlib.blake2s(
        normalized.encode("utf-8", "replace"), digest_size=8).hexdigest()


def user_files(username, config):
    """(file rows, username) for everything a Soulseek user shares.

    Locked files are left out: they are shares the user has not granted,
    and queueing one only produces a refusal later.
    """
    username = str(username or "").strip()
    if not username:
        raise RuntimeError("Enter a Soulseek username.")
    items = []
    seen = set()
    for directory in soulseek_backend.browse_user(username, config):
        for entry in directory.get("files") or []:
            if entry.get("locked"):
                continue
            remote_path = str(entry.get("remote_path") or "")
            if not remote_path:
                continue
            item = dict(entry)
            # A peer's own path is the only stable name one of their files
            # has; the same file re-shared under another name is a new one.
            item["id"] = _share_id(remote_path)
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
    return items, username


def listing(sub, config):
    """(items, title) for one subscription row, however it is listed.

    The Subscriptions tab lists a new subscription through this before it
    is saved, so what a check will find is exactly what adding it found.
    """
    kind = normalize_kind(sub.get("kind"))
    if kind == KIND_ARTIST:
        return artist_releases(sub["url"])
    if kind == KIND_USER:
        username = (sub.get("username")
                    or str(sub.get("url") or "").removeprefix(
                        USER_URL_PREFIX))
        return user_files(username, config)
    if sideb_backend.is_deezer_url(sub["url"]):
        return sideb_backend.extract_flat(sub["url"], config)
    if applemusic_backend.is_apple_music_url(sub["url"]):
        # yt-dlp cannot read music.apple.com at all, so a followed Apple
        # Music playlist has to go to the catalogue that can.
        return applemusic_backend.extract_flat(sub["url"], config)
    return ytdlp_backend.extract_flat(
        sub["url"],
        cookies_from_browser=config["cookies_from_browser"],
        cookies_file=config.get("cookies_file"),
        limit=ytdlp_backend.SUBSCRIPTION_FEED_LIMIT,
        order=search_order.normalize(
            sub.get("order", search_order.ORDER_RELEVANCE)))


def collection_tracks(collection, config=None):
    """The tracks of one whole album or playlist row, from its catalogue."""
    backend = (
        applemusic_backend
        if str(collection.get("kind") or "").startswith("applemusic")
        else deezer_backend
    )
    tracks, _title = backend.extract_flat(collection["url"], config)
    return tracks


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
            row["kind"] = normalize_kind(item.get("kind"))
            row["username"] = str(item.get("username") or "").strip()
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
            order=search_order.ORDER_RECENT, kind=KIND_FEED, username=""):
        order = search_order.normalize(order)
        kind = normalize_kind(kind)
        remembered = list(dict.fromkeys(seen_ids))
        if order == search_order.ORDER_RECENT:
            # A newest-first feed arrives newest to oldest. Store it the other
            # way round so slicing from the end retains the newest IDs.
            remembered.reverse()
        sub = {
            "id": uuid.uuid4().hex,
            "url": url,
            "title": title or url or username,
            "enabled": True,
            # What this follows, which decides how it is listed: a feed's
            # items, an artist's releases, or a person's shared files.
            "kind": kind,
            "username": str(username or "").strip(),
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
        with self._lock:
            sub = next((s for s in self.subs if s["id"] == sub_id), None)
            if sub is not None:
                sub["enabled"] = enabled
        if sub is not None:
            self.save()

    def set_order(self, sub_id, order):
        normalized = search_order.normalize(order)
        with self._lock:
            sub = next((s for s in self.subs if s["id"] == sub_id), None)
            if sub is not None:
                sub["order"] = normalized
        if sub is not None:
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

    def listing(self, sub):
        """(items, title) for one subscription, however it is listed."""
        return listing(sub, self.config)

    def queue_item(self, item, folder="", audio_only=None):
        """Queue one row of a listing, whatever kind of thing it is.

        A whole release is resolved to its tracks first and lands in a
        folder of its own, the same way the Search page queues an album --
        a new record by an artist you follow should arrive as a record.
        Returns how many transfers were added.
        """
        kind = str(item.get("kind") or "")
        if kind == "soulseek":
            entry = dict(item)
            # Under a folder named for the person, keeping the folder they
            # shared it in, so a followed user's new albums stay albums.
            shared = ntpath.basename(
                str(entry.get("folder") or "").rstrip("\\"))
            name = ntpath.basename(str(entry.get("remote_path") or ""))
            parts = [part for part in
                     (str(entry.get("username") or ""), shared, name)
                     if part]
            entry["target_relative_path"] = ntpath.join(*parts)
            self.queue.add_soulseek(entry, entry["title"])
            return 1
        if kind.endswith(("_album", "_playlist")):
            tracks = collection_tracks(item, self.config)
            release_folder = search_kind.collection_folder(item) or folder
            for track in tracks:
                self.queue_item(track, folder=release_folder,
                                audio_only=audio_only)
            return len(tracks)
        if kind.startswith("applemusic"):
            self.queue.add_applemusic(item["url"], item["title"],
                                      folder=folder)
            return 1
        if kind in ("sideb", "deezer"):
            self.queue.add_sideb(item["url"], item["title"], folder=folder)
            return 1
        self.queue.add_ytdlp(item["url"], item["title"],
                             audio_only=audio_only, folder=folder)
        return 1

    def queue_all(self, items, folder="", audio_only=None):
        """Queue a whole listing; returns (transfers added, errors)."""
        added = 0
        errors = []
        with self.queue.batch_additions():
            for item in items:
                try:
                    added += self.queue_item(item, folder, audio_only)
                except Exception as exc:  # noqa: BLE001 - reported back
                    errors.append(
                        str(item.get("title") or item.get("id")) + ": "
                        + str(exc))
        return added, errors

    def _check_one(self, sub_id, audio_only=None):
        sub = self.get(sub_id)
        if sub is None:
            return 0, "Subscription not found."
        try:
            items, title = self.listing(sub)
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
        # A followed channel or playlist puts what it publishes in a folder
        # named after it, the same way a channel URL downloaded by hand
        # does. An artist's releases name their own folders instead, one per
        # record, and are not swept into a folder called after the artist.
        folder = sub.get("title") or title or ""
        if normalize_kind(sub.get("kind")) == KIND_USER:
            # A person's files are filed under their name by the queue
            # itself, so the subscription does not name a folder as well.
            folder = ""
        errors = []
        with self.queue.batch_additions():
            for item in items:
                if item["id"] in seen:
                    continue
                try:
                    self.queue_item(item, folder, audio_only)
                except Exception as exc:  # noqa: BLE001 - reported back
                    # A release that could not be read this time is not
                    # marked as seen, so the next check tries it again.
                    errors.append(
                        str(item.get("title") or item.get("id")) + ": "
                        + str(exc))
                    continue
                seen.add(item["id"])
                new_ids.append(item["id"])
                new_count += 1
        if search_order.normalize(
                sub.get("order", search_order.ORDER_RELEVANCE)
        ) == search_order.ORDER_RECENT:
            # Keep persisted history oldest-to-newest even though the feed is
            # newest-first. The queue above still follows the requested order.
            new_ids.reverse()
        if normalize_kind(sub.get("kind")) == KIND_USER:
            # A browse returns the whole share, so what someone no longer
            # shares is not worth remembering -- and dropping it is what
            # keeps the oldest ids from ageing out of a big share and
            # arriving all over again as new files.
            sub["seen_ids"] = [item["id"] for item in items
                               if item["id"] in seen][-MAX_SEEN_SHARED:]
        else:
            seen_ids.extend(new_ids)
            sub["seen_ids"] = seen_ids[-MAX_SEEN_IDS:]
        sub["last_checked"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()
        return new_count, "; ".join(errors[:3])

    def check_all(self):
        for sub in self.snapshot():
            if not sub.get("enabled", True):
                continue
            count, error = self.check_one(sub["id"])
            if self.notify:
                if count:
                    self.notify(f"{sub['title']}: queued {count} new item(s).")
                if error:
                    self.notify(
                        f"Subscription check failed for {sub['title']}: "
                        f"{error}")

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
