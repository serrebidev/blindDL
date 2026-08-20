# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Native Deezer backend: search, URL inspection, and ARL-backed downloads.

Search and URL inspection use the public Deezer REST API (no auth needed)
and are always available. Downloads use the authenticated gateway API
(BF_CBC_STRIPE decryption) when an ARL cookie is configured, unlocking
FLAC and MP3 320.

The download queue tries this first whenever an ARL is configured and
falls back to Side B if the account cannot serve the requested quality.
"""

import hashlib
import os
import threading

import requests
# Deezer itself defines Blowfish as the cipher for this stream format.
from Crypto.Cipher import Blowfish  # nosec B413

from . import music_tags, search_kind, search_order
from .config import app_data_dir
from .search_kind import (
    ARTIST_SCOPE_ALBUMS,
    ARTIST_SCOPE_ALL,
    ARTIST_SCOPE_PLAYLISTS,
    KIND_ALBUM,
    KIND_ARTIST,
    KIND_BEST,
    KIND_TRACK,
)
from .search_order import ORDER_POPULAR, ORDER_RECENT, ORDER_RELEVANCE
from .ytdlp_backend import DownloadCancelled

_GW_URL = "https://www.deezer.com/ajax/gw-light.php"
_GET_URL = "https://media.deezer.com/v1/get_url"
_API_URL = "https://api.deezer.com"
_TRACK_ID_RE = __import__("re").compile(r"deezer\.com/(?:[a-z]{2}/)?track/(\d+)")
_DEEZER_URL_RE = __import__("re").compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/(\d+)",
    __import__("re").IGNORECASE)
_SEARCH_SOURCE = "Deezer"
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

# Preference order per the dedicated Deezer format setting: flac gets
# lossless first (Deezer's own master), mp3_320 asks for 320 kbps directly.
# No silent drop to 128 kbps -- if the account cannot serve these, the
# caller falls back to Side B.
_PREFERRED_FORMATS = {
    "flac": ["FLAC", "MP3_320"],
    "mp3_320": ["MP3_320"],
}
# FLAC is the default when the setting is missing or unrecognized.
_DEFAULT_FORMATS = ["FLAC", "MP3_320"]
# What playback asks for, cheapest first. Nothing is kept, so the smallest
# stream that starts soonest is the right one; the better qualities are
# only there for an account whose plan withholds 128.
_PLAYBACK_FORMATS = ["MP3_128", "MP3_320", "FLAC"]
# How many decrypted tracks the playback cache keeps before the oldest go.
PLAYBACK_CACHE_FILES = 12
# /search/track caps a request at 100 rows; two pages reach the 200 blindDL
# lists per search.
_SEARCH_LIMIT = 100
_SEARCH_TARGET = 200
# How many artists an artist search looks up before taking their tracks.
# More than one, because "Bowie" is several artists and the first is not
# always the one meant; few enough that the search stays one round trip
# each rather than a dozen.
_ARTIST_SEARCH_LIMIT = 5
# An "All" artist search splits its 200-row budget between the three kinds
# of result so songs do not crowd albums and playlists out entirely.
_ARTIST_SCOPE_ALL_BUDGET = _SEARCH_TARGET // 3

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
        # Two workers with the same ARL can both miss the cache and build a
        # session. Keep the first one and close the loser's HTTP client, so a
        # race neither overwrites a live session nor leaks its connection.
        existing = _sessions.setdefault(arl, session)
        if existing is not session:
            session["http"].close()
            return existing
    return session


def _track_id(url):
    match = _TRACK_ID_RE.search(url)
    if not match:
        raise RuntimeError(f"Not a Deezer track URL: {url}")
    return match.group(1)


def _blowfish_key(track_id):
    # Deezer's stream format specifies this derivation; it is not a password
    # or integrity hash.
    digest = hashlib.md5(
        str(track_id).encode(), usedforsecurity=False
    ).hexdigest()
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
                cipher = Blowfish.new(  # nosec B304
                    key, Blowfish.MODE_CBC, _IV
                )
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
    """Synced LRC text from LRCLIB, or None.

    The request itself lives with the rest of the tagging so that a Qobuz
    download and a Deezer one ask the same service the same way; this only
    puts Deezer's own field names in front of it.
    """
    return music_tags.fetch_lyrics(
        meta.get("SNG_TITLE", ""), meta.get("ART_NAME", ""),
        meta.get("ALB_TITLE", ""), meta.get("DURATION") or 0) or None


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


def is_deezer_url(url):
    """Whether *url* points at a Deezer track/album/playlist/artist."""
    return bool(_DEEZER_URL_RE.search(url))


def _api_get(path, params=None):
    # `next` links returned by the API are absolute URLs; only prefix the
    # base for the relative paths callers build by hand.
    url = (path if path.startswith(("http://", "https://"))
           else f"{_API_URL}{path}")
    resp = requests.get(
        url, params=params or {},
        headers={"User-Agent": _USER_AGENT}, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Deezer API: {data['error'].get('message', 'unknown error')}")
    return data


def _track_to_item(data):
    artist = (data.get("artist") or {}).get("name", "")
    album = (data.get("album") or {}).get("title", "")
    return {
        "id": f"deezer:{data['id']}",
        "kind": "deezer",
        "title": data.get("title") or data.get("name") or "Unknown title",
        "artist": artist,
        "album": album,
        # The catalogue ids behind the two names above. They are what lets a
        # row be opened as its album's track list or its artist's releases,
        # so a search result is a place in the catalogue and not a dead end.
        # A track from an endpoint that names neither carries "".
        "artist_id": str((data.get("artist") or {}).get("id") or ""),
        "album_id": str((data.get("album") or {}).get("id") or ""),
        "source": _SEARCH_SOURCE,
        "duration_s": data.get("duration", 0),
        # Deezer's own popularity figure for the track. Its search endpoint
        # ignores the documented `order` parameter -- every value comes back
        # in the same sequence -- so this is what "most popular" is answered
        # with instead.
        "rank": int(data.get("rank") or 0),
        "url": data.get("link", f"https://www.deezer.com/track/{data['id']}"),
    }


def _album_to_item(data):
    """One row for a whole album, which downloads as all of its tracks."""
    artist = (data.get("artist") or {}).get("name", "")
    tracks = int(data.get("nb_tracks") or 0)
    return {
        "id": f"deezer:album:{data['id']}",
        # The queue never sees this kind: pressing Enter on an album row
        # resolves it to its tracks first, and those are ordinary Deezer
        # items. It is what tells the Search tab to do that resolving.
        "kind": "deezer_album",
        "title": data.get("title") or "Unknown album",
        "artist": artist,
        "album": data.get("title") or "",
        "artist_id": str((data.get("artist") or {}).get("id") or ""),
        "album_id": str(data.get("id") or ""),
        "source": _SEARCH_SOURCE,
        # An album's rows carry no single duration, so the Type column is
        # where its size is said: "Album, 12 tracks" -- or "Single" or "EP"
        # when Deezer says the release is one of those.
        "duration_s": 0,
        "tracks": tracks,
        "record_type": str(data.get("record_type") or ""),
        "format": search_kind.album_type_label(
            tracks, data.get("record_type")),
        "rank": 0,
        "url": data.get("link", f"https://www.deezer.com/album/{data['id']}"),
    }


def _playlist_to_item(data):
    """One row for a whole playlist, which downloads as all of its tracks."""
    tracks = int(data.get("nb_tracks") or 0)
    user = data.get("user") or {}
    curator = user.get("name", "") if isinstance(user, dict) else ""
    return {
        "id": f"deezer:playlist:{data['id']}",
        # Like an album row: pressing Enter resolves it to its tracks
        # before anything reaches the queue.
        "kind": "deezer_playlist",
        "title": data.get("title") or "Unknown playlist",
        "artist": curator,
        "album": "",
        "source": _SEARCH_SOURCE,
        "duration_s": 0,
        "tracks": tracks,
        "format": search_kind.playlist_type_label(tracks),
        "rank": 0,
        "url": data.get("link", f"https://www.deezer.com/playlist/{data['id']}"),
    }


def supports_order(order, kind=KIND_BEST):
    """Deezer publishes a rank per track, and no release date to sort on.

    Album search is the exception both ways: /search/album returns neither a
    date nor a popularity figure, so an album search can only be answered by
    best match.
    """
    order = search_order.normalize(order)
    if order == ORDER_RECENT:
        return False
    return not (search_kind.is_album(kind) and order == ORDER_POPULAR)


def supports_kind(kind):
    """Deezer's field syntax and album endpoint cover every search type."""
    return search_kind.normalize(kind) in search_kind.KINDS


def _search_albums(query):
    """Album rows for *query*, from Deezer's own album search endpoint."""
    items = []
    seen = set()
    try:
        for index in range(0, _SEARCH_TARGET, _SEARCH_LIMIT):
            page = _api_get(
                "/search/album",
                {"q": query, "limit": _SEARCH_LIMIT, "index": index},
            )
            batch = page.get("data", [])
            if not batch:
                break
            for album in batch:
                item = _album_to_item(album)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
            if len(batch) < _SEARCH_LIMIT:
                break
    except Exception:
        # A failed page stops the crawl; whatever arrived is still shown.
        pass
    return items


def _search_artist_tracks(query, limit=_SEARCH_TARGET):
    """Tracks by the artists whose *names* match *query*.

    Deezer does have a documented ``artist:"..."`` query term, and it does
    not work: it matches the words loosely and separately, so asking for
    Daft Punk answers with Pan Da Punk and Manny Da Prince and never with
    Daft Punk at all. Its artist endpoint is exact, so an artist search
    looks the artist up there and then takes what they are known for.
    """
    try:
        found = _api_get(
            "/search/artist", {"q": query, "limit": _ARTIST_SEARCH_LIMIT}
        ).get("data", [])
    except Exception:
        return []
    items = []
    seen = set()
    for artist in found:
        if len(items) >= limit:
            break
        artist_name = artist.get("name") or ""
        try:
            top = _api_get(
                f"/artist/{artist['id']}/top", {"limit": _SEARCH_LIMIT}
            ).get("data", [])
        except Exception:
            # One artist that cannot be read must not lose the others.
            continue
        for track in top:
            entry = dict(track)
            # /artist/{id}/top names the artist on the request, not on each
            # track, so the rows would otherwise arrive without one.
            entry.setdefault(
                "artist", {"id": artist["id"], "name": artist_name})
            item = _track_to_item(entry)
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return items[:limit]


def _search_artist_albums(query, limit=_SEARCH_TARGET):
    """Albums by the artists whose names match *query*.

    Same two-step look-up as the track search: find the artists first,
    then take their releases. The album endpoint paginates, so a long
    discography is followed through its ``next`` links.
    """
    try:
        found = _api_get(
            "/search/artist", {"q": query, "limit": _ARTIST_SEARCH_LIMIT}
        ).get("data", [])
    except Exception:
        return []
    items = []
    seen = set()
    for artist in found:
        if len(items) >= limit:
            break
        artist_name = artist.get("name") or ""
        try:
            next_path = f"/artist/{artist['id']}/albums?limit={_SEARCH_LIMIT}"
            while next_path and len(items) < limit:
                page = _api_get(next_path)
                for album in page.get("data", []):
                    entry = dict(album)
                    # /artist/{id}/albums names the artist on the request,
                    # not on each release, so inject it like the tracks do.
                    entry.setdefault(
                        "artist", {"id": artist["id"], "name": artist_name})
                    item = _album_to_item(entry)
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        items.append(item)
                next_path = page.get("next")
        except Exception:
            # One artist that cannot be read must not lose the others.
            continue
    return items[:limit]


def _search_artist_playlists(query, limit=_SEARCH_TARGET):
    """Playlists whose titles match *query* ("playlists by them").

    Deezer has no artist-playlists endpoint, so this searches the playlist
    catalogue by the artist's name -- the same playlists Deezer's own
    search box surfaces for an artist.
    """
    items = []
    seen = set()
    try:
        for index in range(0, limit, _SEARCH_LIMIT):
            page = _api_get(
                "/search/playlist",
                {"q": query, "limit": _SEARCH_LIMIT, "index": index},
            )
            batch = page.get("data", [])
            if not batch:
                break
            for playlist in batch:
                item = _playlist_to_item(playlist)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
            if len(batch) < _SEARCH_LIMIT:
                break
    except Exception:
        # A failed page stops the crawl; whatever arrived is still shown.
        pass
    return items[:limit]


def search(query, config=None, order=ORDER_RELEVANCE, kind=KIND_BEST,
           artist_scope=ARTIST_SCOPE_ALL):
    """Search Deezer via the public API.  Returns normalized items.

    *kind* is the Search tab's search type. Album asks Deezer's album
    endpoint and returns one row per album, artist asks its artist endpoint
    and returns what those artists are known for, and track title keeps only
    the results whose title really does contain what was typed.

    An artist search takes *artist_scope* further: songs, albums, playlists,
    or all three. Each scope looks the artist up first (Deezer's
    ``artist:"..."`` term matches loosely and uselessly), then takes that
    part of their work.

    The API caps a search at 100 results per request, so two pages are
    fetched to reach the 200 blindDL lists. The API takes an `order`
    parameter and quietly disregards it on track search, so the ordering is
    done here on the rank each row carries.
    """
    kind = search_kind.normalize(kind)
    artist_scope = search_kind.normalize_artist_scope(artist_scope)
    if kind == KIND_ALBUM:
        return _search_albums(query)
    if kind == KIND_ARTIST:
        if artist_scope == ARTIST_SCOPE_ALBUMS:
            return _search_artist_albums(query)
        if artist_scope == ARTIST_SCOPE_PLAYLISTS:
            return _search_artist_playlists(query)
        if artist_scope == ARTIST_SCOPE_ALL:
            # Split the budget between the three kinds so songs do not
            # crowd the albums and playlists out of the list entirely.
            songs = _search_artist_tracks(query, _ARTIST_SCOPE_ALL_BUDGET)
            if search_order.normalize(order) == ORDER_POPULAR:
                songs.sort(key=lambda item: -item["rank"])
            combined = list(songs)
            combined.extend(
                _search_artist_albums(query, _ARTIST_SCOPE_ALL_BUDGET))
            combined.extend(
                _search_artist_playlists(query, _ARTIST_SCOPE_ALL_BUDGET))
            return combined
        # ARTIST_SCOPE_SONGS
        items = _search_artist_tracks(query)
        if search_order.normalize(order) == ORDER_POPULAR:
            items.sort(key=lambda item: -item["rank"])
        return items
    items = []
    seen = set()
    try:
        for index in range(0, _SEARCH_TARGET, _SEARCH_LIMIT):
            page = _api_get(
                "/search/track",
                {"q": query, "limit": _SEARCH_LIMIT, "index": index},
            )
            batch = page.get("data", [])
            if not batch:
                break
            for track in batch:
                item = _track_to_item(track)
                if item["id"] in seen:
                    continue
                # Deezer's own ``track:"..."`` term matches the words of the
                # query anywhere, including in the word "track" itself, so
                # the narrowing a title search asks for is done here.
                if kind == KIND_TRACK and not search_kind.matches(
                    item["title"], query
                ):
                    continue
                seen.add(item["id"])
                items.append(item)
            if len(batch) < _SEARCH_LIMIT:
                break
    except Exception:
        # A failed page stops the crawl; whatever arrived is still shown.
        pass
    if search_order.normalize(order) == ORDER_POPULAR:
        items.sort(key=lambda item: -item["rank"])
    return items


def extract_flat(url, config=None):
    """Resolve a Deezer URL to (items, title) via the public API.

    Same contract as sideb_backend.extract_flat and ytdlp_backend.extract_flat.
    """
    match = _DEEZER_URL_RE.search(url)
    if not match:
        raise RuntimeError(f"Not a Deezer URL: {url}")
    kind, obj_id = match.group(1).lower(), match.group(2)

    if kind == "track":
        data = _api_get(f"/track/{obj_id}")
        return [_track_to_item(data)], data.get("title") or url

    if kind == "album":
        album = _api_get(f"/album/{obj_id}")
        tracks_data = _api_get(f"/album/{obj_id}/tracks")
        items = []
        for t in tracks_data.get("data", []):
            t_copy = dict(t)
            t_copy["album"] = {
                "id": album.get("id", obj_id), "title": album.get("title", "")}
            t_copy["artist"] = album.get("artist", {})
            items.append(_track_to_item(t_copy))
        return items, album.get("title") or url

    if kind == "playlist":
        playlist = _api_get(f"/playlist/{obj_id}")
        items = []
        # /playlist/{id}/tracks pages 25 tracks at a time and returns bare
        # track objects (no {"track": ...} wrapper). Follow the API's own
        # `next` links until the playlist is exhausted.
        next_path = f"/playlist/{obj_id}/tracks"
        seen = set()
        while next_path and next_path not in seen:
            seen.add(next_path)
            page = _api_get(next_path)
            for entry in page.get("data", []):
                if isinstance(entry, dict) and isinstance(
                        entry.get("track"), dict):
                    entry = entry["track"]
                if isinstance(entry, dict):
                    items.append(_track_to_item(entry))
            next_path = page.get("next")
        return items, playlist.get("title") or url

    if kind == "artist":
        artist = _api_get(f"/artist/{obj_id}")
        artist_name = artist.get("name", "")
        # Fetch the full discography: paginate through all albums, then
        # collect every track. Large catalogues can take a few seconds.
        album_ids = []
        next_path = f"/artist/{obj_id}/albums?limit=100"
        while next_path:
            page = _api_get(next_path)
            for alb in page.get("data", []):
                album_ids.append(str(alb["id"]))
            next_path = page.get("next")
        items = []
        for alb_id in album_ids:
            try:
                tracks_data = _api_get(f"/album/{alb_id}/tracks")
            except Exception:
                continue
            for t in tracks_data.get("data", []):
                t_copy = dict(t)
                t_copy["artist"] = {"id": obj_id, "name": artist_name}
                if "album" not in t_copy:
                    t_copy["album"] = {"id": alb_id}
                items.append(_track_to_item(t_copy))
        if not items:
            # Fall back to top tracks when discography is empty (rare).
            top = _api_get(f"/artist/{obj_id}/top", {"limit": 50})
            for t in top.get("data", []):
                t_copy = dict(t)
                t_copy["artist"] = {"id": obj_id, "name": artist_name}
                items.append(_track_to_item(t_copy))
        return items, artist_name or url

    raise RuntimeError(f"Unsupported Deezer URL kind: {kind}")


# -- browsing the catalogue -------------------------------------------------
#
# A search result names an album and an artist, and until now those were two
# strings in two columns. These two calls turn them back into places: the
# album a track came off, and everything its artist has released. They are
# what the Search tab's "Show album tracks" and "Show artist's releases" do.


def album_items(album_id):
    """(track rows, album title) for one Deezer album id.

    The rows come back in the order the release lists them, which is the
    running order of the album and not a relevance ranking.
    """
    album_id = str(album_id or "").strip()
    if not album_id:
        raise RuntimeError("No Deezer album id on that result.")
    return extract_flat(f"https://www.deezer.com/album/{album_id}")


def artist_albums(artist_id, limit=_SEARCH_TARGET):
    """(album rows, artist name) for one Deezer artist id.

    Everything Deezer lists for them -- albums, EPs, singles and
    compilations alike, each row saying which it is -- so a discography can
    be walked one release at a time instead of as several hundred tracks.
    """
    artist_id = str(artist_id or "").strip()
    if not artist_id:
        raise RuntimeError("No Deezer artist id on that result.")
    artist = _api_get(f"/artist/{artist_id}")
    name = artist.get("name") or ""
    items = []
    seen = set()
    next_path = f"/artist/{artist_id}/albums?limit={_SEARCH_LIMIT}"
    while next_path and len(items) < limit:
        page = _api_get(next_path)
        batch = page.get("data", [])
        if not batch:
            break
        for album in batch:
            entry = dict(album)
            # /artist/{id}/albums names the artist on the request rather
            # than on each release, so the rows would otherwise arrive
            # without one -- and without the id that opens this page again.
            entry.setdefault("artist", {"id": artist_id, "name": name})
            item = _album_to_item(entry)
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        next_path = page.get("next")
    return items[:limit], name


def _media_candidates(payload):
    """Flatten Deezer's stream response to (format, source) pairs."""
    candidates = []
    for entry in payload.get("data") or []:
        for media in entry.get("media") or []:
            media_format = str(media.get("format") or "").upper() or None
            for source in media.get("sources") or []:
                if isinstance(source, dict) and source.get("url"):
                    candidates.append((media_format, source))
    return candidates


def _request_media_sources(session, fmt, track_token):
    """Ask Deezer for one quality, keeping its fallback behavior explicit."""
    response = requests.post(
        _GET_URL,
        json={
            "license_token": session["license_token"],
            "media": [{"type": "FULL", "formats": [{
                "cipher": "BF_CBC_STRIPE", "format": fmt,
            }]}],
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
    return _media_candidates(payload), payload.get("errors")


def _sanitize(name):
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return name.strip() or "track"


def playback_cache_dir():
    """Where decrypted tracks live while they are being played."""
    path = os.path.join(app_data_dir(), "deezer-playback")
    os.makedirs(path, exist_ok=True)
    return path


def _prune_playback_cache(keep=PLAYBACK_CACHE_FILES):
    """Keep the cache to the last few tracks played."""
    try:
        entries = sorted(
            (entry for entry in os.scandir(playback_cache_dir())
             if entry.is_file()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for entry in entries[keep:]:
        try:
            os.remove(entry.path)
        except OSError:
            pass


def playback_file(url, config, cancel_event=None):
    """Decrypt one Deezer track to a local file and return its path.

    Deezer's own stream is Blowfish-encrypted, so no player can open it
    directly -- but blindDL already holds the key, and decrypting is what
    the downloader does anyway. Full playback of a Deezer track therefore
    comes from Deezer itself whenever an ARL cookie is configured, instead
    of hunting for a YouTube match that may not exist, may be the wrong
    recording, or may be refused by YouTube's bot wall.

    MP3 128 is asked for first: it is what a free account is always allowed
    and it is a third of the bytes of MP3 320, which is the difference
    between playback starting in a second and in five. Playing is not
    keeping, so quality here is not the download setting's business.

    Raises RuntimeError when no ARL is configured or Deezer will not serve
    the track, which is the caller's cue to fall back to YouTube.
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

    candidates = []
    chosen = ""
    for requested_format in _PLAYBACK_FORMATS:
        returned, _details = _request_media_sources(
            session, requested_format, track_token)
        matching = [
            candidate for candidate in returned
            if candidate[0] is None or candidate[0] == requested_format
        ]
        if matching:
            candidates = matching
            chosen = requested_format
            break
    if not candidates:
        raise RuntimeError(
            "Deezer would not serve a playable stream for this track.")

    fmt, source = candidates[0]
    fmt = (fmt or chosen).upper()
    extension = "flac" if fmt == "FLAC" else "mp3"
    dest = os.path.join(playback_cache_dir(), f"{track_id}.{extension}")
    # The same track played twice in a row is already sitting here.
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        os.utime(dest, None)
        return dest
    partial = dest + ".part"
    try:
        with requests.get(source["url"], stream=True,
                          headers={"User-Agent": _USER_AGENT},
                          timeout=HTTP_TIMEOUT_S) as stream:
            stream.raise_for_status()
            _decrypt_stream(stream, track_id, partial, None, cancel_event)
        os.replace(partial, dest)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    _prune_playback_cache()
    return dest


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

    wanted = _PREFERRED_FORMATS.get(
        config.get("deezer_format", "flac"), _DEFAULT_FORMATS)
    candidates = []
    fallback_candidates = []
    details = None
    # Send one quality per request. Apart from making the priority explicit,
    # this prevents the gateway from choosing MP3_320 merely because it was
    # the first source in a response that also contained FLAC.
    for requested_format in wanted:
        returned, details = _request_media_sources(
            session, requested_format, track_token)
        matching = [
            candidate for candidate in returned
            if candidate[0] is None or candidate[0] == requested_format
        ]
        if matching:
            candidates = matching
            break
        # Some gateway responses report a usable fallback under the wrong
        # request. Keep it as a last resort, but never prefer it over a
        # matching result from a later quality request.
        fallback_candidates.extend(returned)
    if not candidates:
        candidates = fallback_candidates
    if not candidates:
        suffix = f" Deezer reported: {details}" if details else ""
        raise DeezerQualityError(
            f"This Deezer account cannot serve "
            f"{' or '.join(wanted)} for this track.{suffix}")

    os.makedirs(out_dir, exist_ok=True)
    fmt, source = candidates[0]
    fmt = (fmt or wanted[0]).upper()
    ext = "flac" if fmt == "FLAC" else "mp3"
    artist = _sanitize(meta.get("ART_NAME") or "Unknown artist")
    title = _sanitize(meta.get("SNG_TITLE") or track_id)
    dest = os.path.join(out_dir, artist, f"{artist} - {title}.{ext}")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Decrypt into a staging file and move it into place only when complete,
    # exactly as playback does: a dropped connection must not leave a
    # truncated file at the final path for the library to mistake for a
    # finished download.
    partial = dest + ".part"
    try:
        with requests.get(source["url"], stream=True,
                          headers={"User-Agent": _USER_AGENT},
                          timeout=HTTP_TIMEOUT_S) as stream:
            stream.raise_for_status()
            _decrypt_stream(stream, track_id, partial, progress_cb, cancel_event)
        os.replace(partial, dest)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise

    cover = _cover_bytes(meta.get("ALB_PICTURE"))
    lyrics = _fetch_lyrics(meta, arl) if config["sideb_lyrics"] else None
    if ext == "flac":
        _tag_flac(dest, meta, cover, lyrics)
    else:
        _tag_mp3(dest, meta, cover, lyrics)
    return dest
