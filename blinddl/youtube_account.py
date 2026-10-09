# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT
"""Signing in to YouTube the way SmartTube does: a code entered at
google.com/device, then YouTube's TV interface for the user's own lists.

Google only honours these tokens for TV clients, so yt-dlp cannot use them
to download; what they give blindDL is the signed-in feeds -- subscriptions,
Watch later, liked videos and history -- whose videos then download like any
other public video. The refresh token keeps the sign-in alive indefinitely,
until the user removes "YouTube on TV" from their Google account.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qs, urlparse

from .config import app_data_dir

# YouTube on TV's own public client, the one SmartTube signs in as.
CLIENT_ID = (
    "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com")
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
SCOPE = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"
DEVICE_CODE_URL = "https://www.youtube.com/o/oauth2/device/code"
TOKEN_URL = "https://www.youtube.com/o/oauth2/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
BROWSE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
# ponytail: pinned TV client version; bump it if YouTube starts refusing it.
TV_CLIENT = {"clientName": "TVHTML5", "clientVersion": "7.20250923.13.00",
             "hl": "en", "gl": "US"}
SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/subscriptions"
MAX_PAGES = 10
TIMEOUT = 20

# browseId and title for each signed-in list, keyed by how it is linked.
FEEDS = {
    "subscriptions": ("FEsubscriptions", "YouTube subscriptions"),
    "WL": ("VLWL", "Watch later"),
    "LL": ("VLLL", "Liked videos"),
    "history": ("FEhistory", "YouTube history"),
}

_lock = threading.Lock()


class SignInError(Exception):
    """The sign-in failed, was refused, or has been withdrawn."""


def _token_path():
    return os.path.join(app_data_dir(), "youtube_account.json")


def _post(url, payload, headers=None):
    request = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _post_token(payload):
    """POST to the token endpoint; OAuth errors come back as HTTP 4xx."""
    try:
        return _post(TOKEN_URL, payload)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except ValueError:
            raise SignInError(f"YouTube answered HTTP {exc.code}.") from exc


def _load():
    try:
        with open(_token_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


def _save(data):
    path = _token_path()
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def signed_in():
    return _load() is not None


def start_sign_in():
    """Ask for a code. Returns user_code, verification_url, device_code,
    interval and expires_in."""
    answer = _post(DEVICE_CODE_URL, {
        "client_id": CLIENT_ID, "scope": SCOPE,
        "device_id": uuid.uuid4().hex, "device_model": "ytlr::",
    })
    if "user_code" not in answer:
        raise SignInError(answer.get("error") or "YouTube gave no code.")
    return answer


def finish_sign_in(device_code):
    """True once the user has entered the code, False while still waiting.

    Raises SignInError when the code expired or the user said no.
    """
    answer = _post_token({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": device_code,
        "grant_type": "http://oauth.net/grant_type/device/1.0",
    })
    error = answer.get("error")
    if error in ("authorization_pending", "slow_down"):
        return False
    if error == "expired_token":
        raise SignInError("The code expired. Start the sign-in again.")
    if error == "access_denied":
        raise SignInError("The sign-in was declined on google.com/device.")
    if error or not answer.get("refresh_token"):
        raise SignInError(f"YouTube refused the sign-in: {error or answer}")
    answer["expires_at"] = time.time() + int(answer.get("expires_in", 3600))
    with _lock:
        _save(answer)
    return True


def sign_out():
    """Forget the sign-in here, and withdraw it at Google too."""
    with _lock:
        data = _load()
        try:
            os.remove(_token_path())
        except OSError:
            pass
    if data:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{REVOKE_URL}?token={data['refresh_token']}", b""),
                timeout=TIMEOUT).close()
        except OSError:
            pass  # gone here either way; the user can remove it at Google


def access_token():
    """A current access token, refreshed when it is about to run out."""
    with _lock:
        data = _load()
        if data is None:
            raise SignInError("Not signed in to YouTube.")
        if data.get("expires_at", 0) - 300 > time.time():
            return data["access_token"]
        answer = _post_token({
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "refresh_token": data["refresh_token"],
            "grant_type": "refresh_token",
        })
        if "access_token" not in answer:
            raise SignInError(
                "The YouTube sign-in has expired or was removed. Sign in "
                "again in Settings, Accounts.")
        data["access_token"] = answer["access_token"]
        data["expires_at"] = time.time() + int(answer.get("expires_in", 3600))
        _save(data)
        return data["access_token"]


def feed_for(url):
    """The FEEDS key a youtube.com link names, or None."""
    parsed = urlparse(str(url or "").strip())
    if not parsed.netloc.lower().endswith("youtube.com"):
        return None
    path = parsed.path.rstrip("/").lower()
    if path == "/feed/subscriptions":
        return "subscriptions"
    if path == "/feed/history":
        return "history"
    if path == "/playlist":
        listed = (parse_qs(parsed.query).get("list") or [""])[0]
        if listed in ("WL", "LL"):
            return listed
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _text(node):
    node = node or {}
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(run.get("text", "") for run in node.get("runs", []))


def _seconds(clock):
    try:
        parts = [int(part) for part in clock.split(":")]
    except ValueError:
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _tile_to_item(tile):
    """A blindDL item from one TV video tile, or None for anything else."""
    if tile.get("contentType") != "TILE_CONTENT_TYPE_VIDEO":
        return None
    video_id = tile.get("contentId")
    if not video_id:
        return None
    meta = (tile.get("metadata") or {}).get("tileMetadataRenderer") or {}
    uploader = ""
    lines = meta.get("lines") or []
    if lines:
        items = (lines[0].get("lineRenderer") or {}).get("items") or []
        if items:
            uploader = _text(
                (items[0].get("lineItemRenderer") or {}).get("text"))
    duration = None
    header = (tile.get("header") or {}).get("tileHeaderRenderer") or {}
    for overlay in header.get("thumbnailOverlays") or []:
        status = overlay.get("thumbnailOverlayTimeStatusRenderer")
        if status:
            duration = _seconds(_text(status.get("text")))
    return {
        "id": video_id,
        "title": _text(meta.get("title")) or "Unknown title",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "duration": duration,
        "uploader": uploader,
    }


def parse_page(page):
    """(items, continuation token or None) from one TV browse answer."""
    items, continuation = [], None
    for node in _walk(page):
        tile = node.get("tileRenderer")
        if tile:
            item = _tile_to_item(tile)
            if item:
                items.append(item)
        more = node.get("nextContinuationData")
        if more and more.get("continuation"):
            continuation = more["continuation"]
    return items, continuation


def list_feed(key, limit=None):
    """(items, title) for one signed-in list, newest first, as YouTube has it."""
    browse_id, title = FEEDS[key]
    headers = {"Authorization": f"Bearer {access_token()}"}
    payload = {"context": {"client": TV_CLIENT}, "browseId": browse_id}
    items, seen = [], set()
    for _page in range(MAX_PAGES):
        try:
            page = _post(BROWSE_URL, payload, headers)
        except urllib.error.HTTPError as exc:
            raise SignInError(
                f"YouTube refused the {title} list (HTTP {exc.code}).") from exc
        found, continuation = parse_page(page)
        for item in found:
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if not continuation or (limit and len(items) >= limit):
            break
        payload = {"context": {"client": TV_CLIENT},
                   "continuation": continuation}
    return (items[:limit] if limit else items), title

