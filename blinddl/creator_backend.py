# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Authorized, non-DRM downloads from subscription creator platforms.

Authentication stays in user-selected JSON files. OnlyFans accepts the simple
format documented by the MIT-licensed ``ofd`` project. JustForFans uses the
same fields where possible plus the signed-in account's numeric ``user_id``.
DRM device keys, license requests, and media decryption are intentionally out
of scope.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from lxml import html as lxml_html

from . import ytdlp_backend


ONLYFANS_DOMAINS = ("onlyfans.com",)
JUSTFORFANS_DOMAINS = ("justfor.fans",)
ONLYFANS_RULES_URL = (
    "https://raw.githubusercontent.com/DATAHOARDERS/dynamic-rules/"
    "main/onlyfans.json"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


def _host_matches(host, domains):
    host = (host or "").casefold().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in domains)


def provider_for_url(url):
    host = urlparse(url).hostname
    if _host_matches(host, ONLYFANS_DOMAINS):
        return "onlyfans"
    if _host_matches(host, JUSTFORFANS_DOMAINS):
        return "justforfans"
    return None


def _config_value(config, key):
    if config is None:
        return ""
    try:
        return str(config[key] or "").strip()
    except (KeyError, TypeError):
        return ""


def _load_auth(path, label):
    if not path:
        raise RuntimeError(
            f"Choose a {label} auth JSON file in Settings first.")
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"The {label} auth JSON file was not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"The {label} auth JSON file could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"The {label} auth JSON must contain an object.")
    return value


def _cookies(value):
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items() if item}
    if not isinstance(value, str):
        return {}
    result = {}
    for part in value.split(";"):
        if "=" in part:
            key, item = part.strip().split("=", 1)
            if key:
                result[key] = item
    return result


def _safe_component(value, fallback="media"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")[:150].rstrip(" .")
    if not value or value.upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
    }:
        return fallback
    return value


def _extension(url, media_type):
    suffix = Path(urlparse(url).path).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix or ""):
        return suffix
    return {
        "photo": ".jpg",
        "gif": ".gif",
        "audio": ".m4a",
        "video": ".mp4",
    }.get(media_type, ".bin")


def _onlyfans_auth(path):
    value = _load_auth(path, "OnlyFans")
    cookie_values = _cookies(value.get("cookie"))
    missing = [
        name for name, present in (
            ("cookie with auth_id", cookie_values.get("auth_id")),
            ("cookie with sess", cookie_values.get("sess")),
            ("x_bc", value.get("x_bc")),
            ("user_agent", value.get("user_agent")),
        ) if not present
    ]
    if missing:
        raise RuntimeError(
            "OnlyFans auth JSON is missing: " + ", ".join(missing) + ".")
    return {
        "cookies": cookie_values,
        "x_bc": str(value["x_bc"]),
        "user_agent": str(value["user_agent"]),
    }


def _onlyfans_rules():
    response = requests.get(ONLYFANS_RULES_URL, timeout=30)
    response.raise_for_status()
    value = response.json()
    required = (
        "static_param", "checksum_indexes", "checksum_constant",
        "format", "app_token",
    )
    if not isinstance(value, dict) or any(name not in value for name in required):
        raise RuntimeError("OnlyFans signing rules are incomplete.")
    return value


def _onlyfans_headers(url, auth, rules):
    timestamp = str(int(round(time.time())))
    parsed = urlparse(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    message = "\n".join((
        str(rules["static_param"]), timestamp, target, "0",
    )).encode("utf-8")
    # OnlyFans defines SHA-1 as part of its request-signing wire protocol;
    # this is not used to protect local credentials or stored data.
    digest = hashlib.sha1(message, usedforsecurity=False).hexdigest()
    checksum = (
        sum(ord(digest[int(index)]) for index in rules["checksum_indexes"])
        + int(rules["checksum_constant"])
    )
    return {
        "accept": "application/json, text/plain, */*",
        "app-token": str(rules["app_token"]),
        "sign": str(rules["format"]).format(digest, abs(checksum)),
        "time": timestamp,
        "user-agent": auth["user_agent"],
        "referer": url,
        "x-bc": auth["x_bc"],
    }


def _onlyfans_session(auth):
    session = requests.Session()
    session.cookies.update(auth["cookies"])
    return session


def _onlyfans_json(session, auth, rules, url):
    response = session.get(
        url, headers=_onlyfans_headers(url, auth, rules), timeout=30)
    if response.status_code in (401, 403):
        raise RuntimeError(
            "OnlyFans credentials expired or do not permit this content.")
    response.raise_for_status()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("OnlyFans returned an invalid API response.") from exc


def _onlyfans_posts(session, auth, rules, user, archived=False):
    count_key = "archivedPostsCount" if archived else "postsCount"
    count = max(0, int(user.get(count_key) or 0))
    endpoint = "posts/archived" if archived else "posts"
    posts = []
    for offset in range(0, count, 10):
        query = urlencode({
            "limit": 10,
            "offset": offset,
            "order": "publish_date_desc",
            "skip_users_dups": 0,
        })
        url = (
            f"https://onlyfans.com/api2/v2/users/{user['id']}/"
            f"{endpoint}?{query}"
        )
        page = _onlyfans_json(session, auth, rules, url)
        if not isinstance(page, list) or not page:
            break
        posts.extend(item for item in page if isinstance(item, dict))
    return posts


def _onlyfans_items(posts, creator, page_url, auth_file):
    items = []
    protected = 0
    creator_name = (
        creator.get("name") or creator.get("username") or "OnlyFans creator")
    for post in posts:
        post_id = str(post.get("id") or "")
        for index, media in enumerate(post.get("media") or (), 1):
            if not isinstance(media, dict):
                continue
            files = media.get("files") or {}
            full = files.get("full") or {}
            direct_url = full.get("url") if isinstance(full, dict) else None
            if not direct_url:
                if files.get("drm"):
                    protected += 1
                continue
            media_id = str(media.get("id") or index)
            media_type = str(media.get("type") or "media")
            title = f"{creator_name} - post {post_id} - {media_type} {index}"
            filename = (
                _safe_component(f"{creator_name}_{post_id}_{media_type}_{media_id}")
                + _extension(direct_url, media_type)
            )
            items.append({
                "id": f"adult:onlyfans:{post_id}:{media_id}",
                "kind": "adult",
                "provider": "onlyfans",
                "title": title,
                "artist": str(creator_name),
                "source": "OnlyFans",
                "duration_s": media.get("duration"),
                "file_size": "",
                "url": page_url,
                "direct_url": direct_url,
                "post_id": post_id,
                "media_id": media_id,
                "media_type": media_type,
                "filename": filename,
                "auth_file": auth_file,
            })
    return items, protected


def inspect_onlyfans(url, auth_file):
    auth = _onlyfans_auth(auth_file)
    rules = _onlyfans_rules()
    session = _onlyfans_session(auth)
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if not parts:
            raise ValueError("Paste an OnlyFans creator or post URL.")
        if parts[0].isdigit():
            post = _onlyfans_json(
                session, auth, rules,
                f"https://onlyfans.com/api2/v2/posts/{parts[0]}",
            )
            if not isinstance(post, dict):
                raise RuntimeError("OnlyFans returned no post.")
            creator = post.get("author") or {}
            posts = [post]
        else:
            creator = _onlyfans_json(
                session, auth, rules,
                f"https://onlyfans.com/api2/v2/users/{parts[0]}",
            )
            if not isinstance(creator, dict) or not creator.get("id"):
                raise RuntimeError("OnlyFans returned no creator.")
            posts = _onlyfans_posts(session, auth, rules, creator)
            posts.extend(_onlyfans_posts(
                session, auth, rules, creator, archived=True))
        items, protected = _onlyfans_items(
            posts, creator, url, os.path.abspath(auth_file))
    finally:
        session.close()
    if not items:
        if protected:
            raise RuntimeError(
                "This OnlyFans URL contains only DRM-protected media, which "
                "blindDL does not decrypt.")
        raise RuntimeError("OnlyFans returned no downloadable media.")
    creator_name = creator.get("name") or creator.get("username") or "OnlyFans"
    title = str(creator_name)
    if protected:
        title += f" ({protected} DRM-protected item(s) skipped)"
    return items, title


def _justforfans_auth(path):
    value = _load_auth(path, "JustForFans")
    cookie_values = _cookies(value.get("cookie") or value.get("cookies"))
    missing = [
        name for name, present in (
            ("cookie with userhash4", cookie_values.get("userhash4")),
            ("user_id", value.get("user_id")),
        ) if not present
    ]
    if missing:
        raise RuntimeError(
            "JustForFans auth JSON is missing: " + ", ".join(missing) + ".")
    return {
        "cookies": cookie_values,
        "user_id": str(value["user_id"]),
        "user_agent": str(value.get("user_agent") or _UA),
        "accept": str(value.get("accept") or "text/html,application/xhtml+xml"),
    }


def _justforfans_session(auth):
    session = requests.Session()
    session.cookies.update(auth["cookies"])
    session.headers.update({
        "User-Agent": auth["user_agent"],
        "Accept": auth["accept"],
    })
    return session


def _jff_response(session, url, params=None):
    response = session.get(url, params=params, timeout=30)
    if response.status_code in (401, 403):
        raise RuntimeError(
            "JustForFans credentials expired or do not permit this content.")
    response.raise_for_status()
    return response


def _json_object_after(text, marker):
    decoder = json.JSONDecoder()
    for match in re.finditer(marker, text, re.IGNORECASE | re.DOTALL):
        start = text.find("{", match.end())
        if start < 0:
            continue
        try:
            value, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _jff_video_url(block):
    value = _json_object_after(block, r"MakeMovieVideoJS\s*\(")
    if not value:
        return ""
    candidates = []
    fallback = ""
    for key, item in value.items():
        if not isinstance(item, str) or not item.startswith(("http://", "https://")):
            continue
        if str(key).casefold() == "all":
            fallback = item
            continue
        match = re.search(r"\d+", str(key))
        if match:
            candidates.append((int(match.group()), item))
    return max(candidates, default=(0, fallback))[1]


def _jff_post_url(node, block, base_url):
    for href in node.xpath(".//@href"):
        if "post=" in href:
            return urljoin(base_url, href)
    match = re.search(
        r"location\.href\s*=\s*['\"]([^'\"]+)", block,
        re.IGNORECASE,
    )
    return urljoin(base_url, html.unescape(match.group(1))) if match else base_url


def _jff_cards(text, creator_name, auth_file, base_url):
    try:
        root = lxml_html.fromstring(f"<div>{text}</div>")
    except (TypeError, ValueError):
        return [], 0
    nodes = root.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), "
        "' mbsc-card ')][@id]"
    )
    items = []
    protected = 0
    seen = set()
    for node in nodes:
        block = lxml_html.tostring(node, encoding="unicode")
        post_url = _jff_post_url(node, block, base_url)
        post_id = (parse_qs(urlparse(post_url).query).get("post") or
                   [str(node.get("id") or "post")])[0]
        media = []
        video_url = _jff_video_url(block)
        if video_url:
            if ".mpd" in urlparse(video_url).path.casefold():
                protected += 1
            else:
                media.append(("video", video_url))
        large = []
        small = []
        for image in node.xpath(".//img"):
            classes = set((image.get("class") or "").split())
            image_url = image.get("data-lazy") or image.get("src") or ""
            if not image_url:
                continue
            if "expandable" in classes:
                large.append(urljoin(base_url, image_url))
            elif "galThumb" in classes:
                small.append(urljoin(base_url, image_url))
        for image_url in large or small:
            media.append(("photo", image_url))
        for index, (media_type, direct_url) in enumerate(media, 1):
            identity = (post_id, media_type, direct_url)
            if identity in seen:
                continue
            seen.add(identity)
            media_key = f"{media_type}:{index}"
            title = f"{creator_name} - post {post_id} - {media_type} {index}"
            filename = (
                _safe_component(f"{creator_name}_{post_id}_{media_type}_{index}")
                + _extension(direct_url, media_type)
            )
            items.append({
                "id": f"adult:justforfans:{post_id}:{media_key}",
                "kind": "adult",
                "provider": "justforfans",
                "title": title,
                "artist": creator_name,
                "source": "JustForFans",
                "duration_s": None,
                "file_size": "",
                "url": post_url,
                "direct_url": direct_url,
                "media_key": media_key,
                "media_type": media_type,
                "filename": filename,
                "auth_file": auth_file,
            })
    return items, protected


def _jff_creator_id(session, creator_name):
    profile_url = f"https://justfor.fans/{creator_name}"
    page = _jff_response(session, profile_url).text
    match = re.search(
        r"GetStats2\s*\(\s*UserID\s*\)\s*\{\s*var\s+Hash\s*=\s*"
        r"['\"]([^'\"]+)",
        page, re.IGNORECASE,
    )
    if not match:
        raise RuntimeError(
            "JustForFans did not expose this creator's profile identifier.")
    response = _jff_response(
        session,
        "https://justfor.fans/ajax/getAssetCount.php",
        params={"User": creator_name, "Ver": match.group(1)},
    )
    try:
        value = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("JustForFans returned invalid creator data.") from exc
    creator_id = re.sub(r"\D", "", str(value.get("UserID") or ""))
    if not creator_id:
        raise RuntimeError("JustForFans returned no creator identifier.")
    return creator_id


def inspect_justforfans(url, auth_file):
    auth = _justforfans_auth(auth_file)
    session = _justforfans_session(auth)
    absolute_auth_file = os.path.abspath(auth_file)
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if not parts or parts[0].casefold() in {"home", "login", "join"}:
            raise ValueError("Paste a JustForFans creator or post URL.")
        creator_name = parts[0]
        if parse_qs(urlparse(url).query).get("post"):
            page = _jff_response(session, url).text
            items, protected = _jff_cards(
                page, creator_name, absolute_auth_file, url)
        else:
            creator_id = _jff_creator_id(session, creator_name)
            items = []
            protected = 0
            seen_ids = set()
            for cursor in range(0, 2000, 10):
                response = _jff_response(
                    session,
                    "https://justfor.fans/ajax/getPosts.php",
                    params={
                        "Type": "One",
                        "UserID": auth["user_id"],
                        "PosterID": creator_id,
                        "StartAt": cursor,
                        "Page": "Profile",
                        "UserHash4": auth["cookies"]["userhash4"],
                        "SplitTest": 0,
                    },
                )
                page_items, page_protected = _jff_cards(
                    response.text, creator_name, absolute_auth_file, url)
                protected += page_protected
                new_items = [item for item in page_items if item["id"] not in seen_ids]
                if not new_items:
                    break
                for item in new_items:
                    seen_ids.add(item["id"])
                items.extend(new_items)
    finally:
        session.close()
    if not items:
        if protected:
            raise RuntimeError(
                "This JustForFans URL contains only DASH-protected media, "
                "which blindDL does not decrypt.")
        raise RuntimeError("JustForFans returned no downloadable media.")
    title = creator_name
    if protected:
        title += f" ({protected} protected item(s) skipped)"
    return items, title


def inspect_url(url, config=None):
    provider = provider_for_url(url)
    if provider == "onlyfans":
        return inspect_onlyfans(
            url, _config_value(config, "onlyfans_auth_file"))
    if provider == "justforfans":
        return inspect_justforfans(
            url, _config_value(config, "justforfans_auth_file"))
    raise ValueError(f"No creator-platform provider is registered for: {url}")


def _cookie_header(cookies):
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _download_url(url, filename, out_dir, headers, progress_cb, cancel_event):
    lowered = str(url).casefold()
    if ".mpd" in lowered:
        raise RuntimeError("DASH-protected media is not supported.")
    if ".m3u8" in lowered:
        ytdlp_backend.download(
            url, out_dir, audio_only=False, progress_cb=progress_cb,
            cancel_event=cancel_event, http_headers=headers,
        )
        return
    os.makedirs(out_dir, exist_ok=True)
    destination = os.path.join(out_dir, _safe_component(filename))
    if os.path.isfile(destination):
        size = os.path.getsize(destination)
        if progress_cb is not None:
            progress_cb(size, size)
        return
    partial = destination + ".part"
    received = 0
    try:
        with requests.get(
                url, headers=headers, stream=True, timeout=(30, 300)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            with open(partial, "wb") as stream:
                for chunk in response.iter_content(1024 * 256):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ytdlp_backend.DownloadCancelled()
                    if not chunk:
                        continue
                    stream.write(chunk)
                    received += len(chunk)
                    if progress_cb is not None:
                        progress_cb(received, total)
        if total and received != total:
            raise RuntimeError(
                "The download ended before the whole file arrived.")
        os.replace(partial, destination)
    except Exception:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass
        raise


def _refresh_onlyfans(payload):
    auth = _onlyfans_auth(payload["auth_file"])
    rules = _onlyfans_rules()
    session = _onlyfans_session(auth)
    try:
        post = _onlyfans_json(
            session, auth, rules,
            f"https://onlyfans.com/api2/v2/posts/{payload['post_id']}",
        )
    finally:
        session.close()
    for media in post.get("media") or ():
        if str(media.get("id")) != str(payload["media_id"]):
            continue
        files = media.get("files") or {}
        direct_url = (files.get("full") or {}).get("url")
        if direct_url:
            return direct_url, auth
        if files.get("drm"):
            raise RuntimeError(
                "This OnlyFans item is DRM-protected; blindDL does not decrypt it.")
    raise RuntimeError("OnlyFans no longer returned this media item.")


def _refresh_justforfans(payload):
    auth = _justforfans_auth(payload["auth_file"])
    session = _justforfans_session(auth)
    try:
        page = _jff_response(session, payload["url"]).text
    finally:
        session.close()
    items, _protected = _jff_cards(
        page, payload.get("artist") or "JustForFans",
        payload["auth_file"], payload["url"],
    )
    for item in items:
        if item["media_key"] == payload["media_key"]:
            return item["direct_url"], auth
    raise RuntimeError("JustForFans no longer returned this media item.")


def download(payload, out_dir, progress_cb=None, cancel_event=None):
    if payload["provider"] == "onlyfans":
        direct_url, auth = _refresh_onlyfans(payload)
    elif payload["provider"] == "justforfans":
        direct_url, auth = _refresh_justforfans(payload)
    else:
        raise ValueError(f"Unknown creator provider: {payload['provider']}")
    headers = {
        "User-Agent": auth["user_agent"],
        "Referer": payload["url"],
        "Cookie": _cookie_header(auth["cookies"]),
    }
    _download_url(
        direct_url, payload["filename"], out_dir, headers,
        progress_cb, cancel_event,
    )
