# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The download queue: results kept aside until they are asked for.

A search is a place you pass through. Everything worth keeping out of one
had to be downloaded there and then, because closing the search -- or
running another -- is the end of it, and blindDL has no shelf to put a
result on. That makes every search a decision about disk space taken under
time pressure, and it makes an album found while looking for something else
either an interruption or a loss.

This is that shelf. A row goes on it with everything the Search tab knew
about it, so it can be played from there afterwards and downloaded from
there whenever, days later and across restarts, in whatever order and
however few at a time.

The store is deliberately the search result and not a download job: a queued
download has already decided what it is and where it goes, and cannot be
listened to first.
"""

from __future__ import annotations

import base64
import json
import os
import threading

from .config import app_data_dir

STATE_VERSION = 1


def _json_safe(value):
    """The saveable form of one field of a search result.

    Results carry more than JSON has: musicdl hands over enumerations and
    byte strings, and any of them can turn up nested inside a list. What
    cannot be represented raises, and the caller drops that field rather
    than the row.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__blinddl_bytes__":
                base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):  # enum.Enum, without importing enum for it
        return _json_safe(value.value)
    raise TypeError(f"unsupported saved value: {type(value).__name__}")


def _json_restore(value):
    if isinstance(value, list):
        return [_json_restore(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__blinddl_bytes__"}:
            return base64.b64decode(value["__blinddl_bytes__"], validate=True)
        return {key: _json_restore(item) for key, item in value.items()}
    return value


def _result_record(result):
    """One search result, as something that can be written to disk.

    A musicdl row's payload is a SongInfo object, and that object *is* the
    download -- dropping it would leave a row that plays and cannot be
    fetched -- so it is stored through musicdl's own dictionary form. Any
    other field that will not serialize is left out; a result that is only
    missing, say, an artwork blob is still a result.
    """
    record = {}
    song_info = result.get("song_info")
    for key, value in result.items():
        if key == "song_info":
            continue
        try:
            record[key] = _json_safe(value)
        except (TypeError, ValueError):
            continue
    if song_info is not None and hasattr(song_info, "todict"):
        record["__song_info__"] = _json_safe(song_info.todict())
    return record


def _result_from_record(record):
    result = {key: _json_restore(value) for key, value in record.items()
              if key != "__song_info__"}
    if "__song_info__" in record:
        from musicdl.modules.utils.data import SongInfo

        result["song_info"] = SongInfo.fromdict(
            _json_restore(record["__song_info__"]))
    return result


class SavedQueue:
    """The saved results, in the order they were put here.

    Threading is the same bargain the download queue makes: every method
    takes the lock, and the file is rewritten whenever the list changes, so
    a crash loses at most the entry being added.
    """

    def __init__(self, state_path=None):
        self._lock = threading.Lock()
        self.entries = []
        self._path = (
            os.path.join(app_data_dir(), "download-queue.json")
            if state_path is None
            else os.fspath(state_path) if state_path else None
        )
        self._load()

    # -- reading -------------------------------------------------------------

    def all(self):
        """Every saved entry, oldest first."""
        with self._lock:
            return list(self.entries)

    def __len__(self):
        with self._lock:
            return len(self.entries)

    @staticmethod
    def key_for(result):
        """What makes two saved rows the same row.

        The same track can be found twice in one search and again in the
        next one, and a shelf that fills up with duplicates of it is a shelf
        nobody keeps using.
        """
        for field in ("id", "url", "direct_url", "identifier"):
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip().casefold()
        return "\x00".join(
            str(result.get(field) or "").strip().casefold()
            for field in ("title", "artist", "source")
        )

    # -- writing -------------------------------------------------------------

    def add(self, result, engine, folder=""):
        """Put one result on the shelf; return whether it was not there yet."""
        key = self.key_for(result)
        entry = {
            "key": key,
            "engine": int(engine),
            "folder": str(folder or ""),
            "result": _result_record(result),
        }
        with self._lock:
            if any(existing["key"] == key for existing in self.entries):
                return False
            self.entries.append(entry)
            self._save_locked()
        return True

    def add_many(self, results, engine, folder=""):
        """Add several at once; return how many were new."""
        added = 0
        for result in results:
            if self.add(result, engine, folder=folder):
                added += 1
        return added

    def remove(self, keys):
        """Take entries off the shelf by key; return how many went."""
        wanted = set(keys)
        if not wanted:
            return 0
        with self._lock:
            kept = [entry for entry in self.entries
                    if entry["key"] not in wanted]
            removed = len(self.entries) - len(kept)
            if removed:
                self.entries = kept
                self._save_locked()
        return removed

    def clear(self):
        with self._lock:
            removed = len(self.entries)
            self.entries = []
            if removed:
                self._save_locked()
        return removed

    # -- persistence ---------------------------------------------------------

    def _load(self):
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        records = document.get("items", []) if isinstance(document, dict) else []
        for record in records:
            if not isinstance(record, dict) or not isinstance(
                    record.get("result"), dict):
                continue
            result = record["result"]
            self.entries.append({
                "key": str(record.get("key") or self.key_for(result)),
                "engine": int(record.get("engine") or 0),
                "folder": str(record.get("folder") or ""),
                "result": result,
            })

    def result_of(self, entry):
        """The live search result behind one entry.

        Raises when the row needs a package that is no longer installed,
        which the caller reports against that row rather than the whole
        queue.
        """
        return _result_from_record(entry["result"])

    def _save_locked(self):
        if not self._path:
            return
        document = {
            "version": STATE_VERSION,
            "items": [
                {
                    "key": entry["key"],
                    "engine": entry["engine"],
                    "folder": entry["folder"],
                    "result": entry["result"],
                }
                for entry in self.entries
            ],
        }
        temporary = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)),
                        exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass
