# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Native Deezer backend: FLAC / MP3 320 downloads unlocked by an ARL cookie.

sideb only ever pulls audio from YouTube Music. With a Deezer ARL cookie
(from a Premium/HiFi account) blindDL can instead fetch Deezer's own
encrypted streams -- FLAC for the flac audio-format setting, MP3 320 for
everything else -- decrypt them (BF_CBC_STRIPE, the scheme deemix and
streamrip use), and tag the result with mutagen.

The download queue tries this first whenever an ARL is configured and
falls back to Side B if the account cannot serve the requested quality.
"""

import hashlib
import os
import threading

import requests
from Crypto.Cipher import Blowfish

from .ytdlp_backend import DownloadCancelled

_GW_URL = "https://www.deezer.com/ajax/gw-light.php"
_GET_URL = "https://media.deezer.com/v1/get_url"
_TRACK_ID_RE = __import__("re").compile(r"deezer\.com/(?:[a-z]{2}/)?track/(\d+)")
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 "
               "Safari/537.36")
# BF_CBC_STRIPE: every third 2048-byte block is Blowfish-CBC encrypted,
# cipher restarted per block with this fixed IV. The key comes from the
# track id XORed with this constant.
_IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"
_KEY_SECRET = b"g4el58wc0zvf9na1"
_CHUNK = 2048
HTTP_TIMEOUT_S = 30

# Preference order per blindDL audio-format setting: flac and "original"
# get lossless first, everything else gets MP3 320. No silent drop to
# 128 kbps -- if the account cannot serve these, the caller falls back to
# Side B.
_PREFERRED_FORMATS = {
    "flac": ["FLAC", "MP3_320"],
    # Deezer's own master is the FLAC, so "no conversion" means taking it.
    "original": ["FLAC", "MP3_320"],
}
_DEFAULT_FORMATS = ["MP3_320"]

_sessions = {}  # arl -> authenticated HTTP session and streaming credentials
_sessions_lock = threading.Lock()


class DeezerQualityError(RuntimeError):
    """The account cannot serve any of the requested formats."""


def _new_http_session(arl):
    """Create the cookie jar Deezer binds its CSRF token to."""
    http = requests.Session()
    http.headers.update({"User-Agent": _USER_AGENT})
    http.cookies.set("arl", arl, domain=".deezer.com")
    return http


def _gw_call(http, method, api_token, payload=None):
    response = http.post(
        _GW_URL,
        params={"method": method, "input": "3",
                "api_version": "1.0", "api_token": api_token or "null"},
        json=payload or {},
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"Deezer {method} failed: {data['error']}")
    return data.get("results") or {}


def _login(arl):
    """Exchange the ARL for an API token and a streaming license token."""
    with _sessions_lock:
        session = _sessions.get(arl)
    if session is not None:
        return session
    # checkForm is bound to the sid cookie set by getUserData. Reusing this
    # Session for later gateway calls is mandatory; sending only the ARL again
    # produces VALID_TOKEN_REQUIRED even though the ARL itself is valid.
    http = _new_http_session(arl)
    results = _gw_call(http, "deezer.getUserData", None)
    user = results.get("USER") or {}
    if str(user.get("USER_ID", "0")) == "0":
        raise RuntimeError("Deezer ARL cookie is invalid or has expired.")
    session = {
        "api_token": results.get("checkForm"),
        "license_token": (user.get("OPTIONS") or {}).get("license_token"),
        "http": http,
        # requests.Session is not documented as thread-safe. blindDL can run
        # several Deezer workers at once, so serialize its gateway calls.
        "http_lock": threading.Lock(),
    }
    if not session["api_token"] or not session["license_token"]:
        raise RuntimeError("Deezer did not hand out streaming credentials "
                           "for this account.")
    with _sessions_lock:
        _sessions[arl] = session
    return session


def _track_id(url):
    match = _TRACK_ID_RE.search(url)
    if not match:
        raise RuntimeError(f"Not a Deezer track URL: {url}")
    return match.group(1)


def _blowfish_key(track_id):
    digest = hashlib.md5(str(track_id).encode()).hexdigest()
    return bytes(ord(digest[i]) ^ ord(digest[i + 16]) ^ _KEY_SECRET[i]
                 for i in range(16))


def _decrypt_stream(response, track_id, dest_path, progress_cb, cancel_event):
    key = _blowfish_key(track_id)
    total = int(response.headers.get("Content-Length") or 0)
    downloaded = 0
    index = 0
    with open(dest_path, "wb") as out:
        for chunk in response.iter_content(_CHUNK):
            if cancel_event is not None and cancel_event.is_set():
                out.close()
                os.remove(dest_path)
                raise DownloadCancelled()
            if len(chunk) == _CHUNK and index % 3 == 0:
                cipher = Blowfish.new(key, Blowfish.MODE_CBC, _IV)
                chunk = cipher.decrypt(chunk)
            out.write(chunk)
            downloaded += len(chunk)
            index += 1
            if progress_cb is not None:
                progress_cb(downloaded, total)


def _cover_bytes(picture_md5):
    if not picture_md5:
        return None
    url = (f"https://e-cdns-images.dzcdn.net/images/cover/{picture_md5}"
           "/1000x1000-000000-100-0-0.jpg")
    try:
        response = requests.get(url, headers={"User-Agent": _USER_AGENT},
                                timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        return response.content
    except requests.RequestException:
        return None


def _fetch_deezer_lyrics(meta, arl):
    """Enhanced Deezer LRC via the ARL, preferring word timestamps."""
    try:
        import asyncio

        from sideb.models.track import Album, Artist, Track
        from sideb.providers.lyrics.deezer import DeezerLyrics

        artist = Artist(id=str(meta.get("ART_ID") or ""),
                        name=meta.get("ART_NAME") or "")
        album = Album(id=str(meta.get("ALB_ID") or ""),
                      title=meta.get("ALB_TITLE") or "", artist=artist)
        track = Track(
            id=str(meta.get("SNG_ID") or ""),
            title=meta.get("SNG_TITLE") or "",
            artist=artist,
            album=album,
            duration=int(meta.get("DURATION") or 0),
        )

        async def _run():
            provider = DeezerLyrics(arl=arl, user_agent=_USER_AGENT)
            try:
                return await provider.get_lyrics(track)
            finally:
                await provider.aclose()

        lyrics = asyncio.run(_run())
        if lyrics is not None:
            return lyrics.word_synced or lyrics.synced or lyrics.plain
    except Exception:  # noqa: BLE001 - lyrics are optional; LRCLIB follows
        pass
    return None


def _fetch_lrclib_lyrics(meta):
    """Synced LRC text from LRCLIB, or None."""
    try:
        response = requests.get(
            "https://lrclib.net/api/get",
            params={"track_name": meta.get("SNG_TITLE", ""),
                    "artist_name": meta.get("ART_NAME", ""),
                    "album_name": meta.get("ALB_TITLE", ""),
                    "duration": int(meta.get("DURATION") or 0)},
            headers={"User-Agent": f"blindDL ({_USER_AGENT})"},
            timeout=HTTP_TIMEOUT_S,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get("syncedLyrics") or data.get("plainLyrics") or None
    except (requests.RequestException, ValueError):
        return None


def _fetch_lyrics(meta, arl):
    """Prefer Deezer's ARL-backed lyrics, then fall back to LRCLIB."""
    return (_fetch_deezer_lyrics(meta, arl)
            or _fetch_lrclib_lyrics(meta))


def _tag_flac(path, meta, cover, lyrics):
    from mutagen.flac import FLAC, Picture

    audio = FLAC(path)
    audio["title"] = meta.get("SNG_TITLE", "")
    audio["artist"] = meta.get("ART_NAME", "")
    audio["album"] = meta.get("ALB_TITLE", "")
    for tag, field in (("date", "PHYSICAL_RELEASE_DATE"),
                       ("tracknumber", "TRACK_NUMBER"),
                       ("discnumber", "DISK_NUMBER"),
                       ("isrc", "ISRC")):
        if meta.get(field):
            audio[tag] = str(meta[field])
    if lyrics:
        audio["lyrics"] = lyrics
    if cover:
        picture = Picture()
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.data = cover
        audio.add_picture(picture)
    audio.save()


def _tag_mp3(path, meta, cover, lyrics):
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPOS, TRCK, TSRC, USLT

    try:
        tags = ID3(path)
    except Exception:  # noqa: BLE001 - no ID3 header yet
        tags = ID3()
    tags.add(TIT2(encoding=3, text=meta.get("SNG_TITLE", "")))
    tags.add(TPE1(encoding=3, text=meta.get("ART_NAME", "")))
    tags.add(TALB(encoding=3, text=meta.get("ALB_TITLE", "")))
    if meta.get("PHYSICAL_RELEASE_DATE"):
        tags.add(TDRC(encoding=3, text=str(meta["PHYSICAL_RELEASE_DATE"])))
    if meta.get("TRACK_NUMBER"):
        tags.add(TRCK(encoding=3, text=str(meta["TRACK_NUMBER"])))
    if meta.get("DISK_NUMBER"):
        tags.add(TPOS(encoding=3, text=str(meta["DISK_NUMBER"])))
    if meta.get("ISRC"):
        tags.add(TSRC(encoding=3, text=str(meta["ISRC"])))
    if lyrics:
        tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                      desc="Cover", data=cover))
    tags.save(path, v2_version=3)


def _sanitize(name):
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return name.strip() or "track"


def download(url, out_dir, config, progress_cb=None, cancel_event=None):
    """Download one Deezer track URL as FLAC or MP3 320.

    progress_cb receives (downloaded_bytes, total_bytes). Raises
    DownloadCancelled, DeezerQualityError, or RuntimeError.
    """
    arl = (config["deezer_arl"] or "").strip()
    if not arl:
        raise RuntimeError("No Deezer ARL cookie configured.")
    track_id = _track_id(url)
    session = _login(arl)
    with session["http_lock"]:
        results = _gw_call(session["http"], "deezer.pageTrack",
                           session["api_token"], {"sng_id": track_id})
    meta = results.get("DATA") or {}
    track_token = meta.get("TRACK_TOKEN")
    if not track_token:
        raise RuntimeError(
            f"Deezer gave no stream token for track {track_id} "
            "(region-locked or unavailable).")

    wanted = _PREFERRED_FORMATS.get(config["audio_format"], _DEFAULT_FORMATS)
    response = requests.post(
        _GET_URL,
        json={
            "license_token": session["license_token"],
            "media": [{"type": "FULL", "formats": [
                {"cipher": "BF_CBC_STRIPE", "format": fmt}
                for fmt in wanted]}],
            # media.deezer.com changed this field from a singular token to a
            # list. The old spelling now returns HTTP 422 with a text body.
            "track_tokens": [track_token],
        },
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json",
                 "Origin": "https://www.deezer.com",
                 "Referer": "https://www.deezer.com/"},
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = []
    for entry in payload.get("data") or []:
        for media in entry.get("media") or []:
            for source in media.get("sources") or []:
                candidates.append((media.get("format"), source))
    if not candidates:
        details = payload.get("errors")
        suffix = f" Deezer reported: {details}" if details else ""
        raise DeezerQualityError(
            f"This Deezer account cannot serve "
            f"{' or '.join(wanted)} for this track.{suffix}")

    os.makedirs(out_dir, exist_ok=True)
    fmt, source = candidates[0]
    fmt = fmt or wanted[0]
    ext = "flac" if fmt == "FLAC" else "mp3"
    artist = _sanitize(meta.get("ART_NAME") or "Unknown artist")
    title = _sanitize(meta.get("SNG_TITLE") or track_id)
    dest = os.path.join(out_dir, artist, f"{artist} - {title}.{ext}")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with requests.get(source["url"], stream=True,
                      headers={"User-Agent": _USER_AGENT},
                      timeout=HTTP_TIMEOUT_S) as stream:
        stream.raise_for_status()
        _decrypt_stream(stream, track_id, dest, progress_cb, cancel_event)

    cover = _cover_bytes(meta.get("ALB_PICTURE"))
    lyrics = _fetch_lyrics(meta, arl) if config["sideb_lyrics"] else None
    if ext == "flac":
        _tag_flac(dest, meta, cover, lyrics)
    else:
        _tag_mp3(dest, meta, cover, lyrics)
    return dest
