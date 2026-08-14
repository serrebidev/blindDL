# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Apple Music backend: search, resolve, and download in-process.

Searching uses the iTunes Search API, which is public, credential-free, and
serves the same catalogue Apple Music's own pages use. Downloading runs the
Apple Music web pipeline entirely inside blindDL -- anonymous developer
token, catalog metadata, Widevine L3 license exchange, then a CENC
decrypt-and-remux to M4A (yt-dlp's HLS downloader assembles the encrypted
fragmented MP4 and ffmpeg's MOV demuxer decrypts it) -- the same technique
the gamdl / AppleMusicDecrypt family of tools pioneered. No external
downloader is needed; the only tools involved are the yt-dlp and ffmpeg
blindDL already bundles.

A full track still needs an Apple Music subscription. Export your browser
cookies while logged in at music.apple.com (Settings, Accounts, Apple Music,
Copy from browser) and point the cookies file setting at the export. Without
cookies, search and URL resolution keep working; downloads raise a clear
error instead.
"""

import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse

import requests

from . import search_kind
from .search_kind import KIND_ALBUM, KIND_ARTIST, KIND_BEST, KIND_TRACK

_SEARCH_SOURCE = "Apple Music"
_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
_ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
_AMP_API_URL = "https://amp-api.music.apple.com"

# Output formats for Apple Music downloads. ``m4a`` keeps Apple's AAC
# untouched; the MP3 options re-encode it through libmp3lame (present in the
# ffmpeg blindDL ships). V0 and V2 are LAME's VBR quality settings (V0 is
# roughly the same bitrate as the original 256 kbps AAC), and 320k is a
# constant bitrate.
APPLE_OUTPUT_FORMATS = {
    "m4a": {"label": "M4A (AAC, original)", "ext": "m4a",
            "ffmpeg_args": []},
    "mp3_v0": {"label": "MP3 V0 (~245 kbps VBR)", "ext": "mp3",
                "ffmpeg_args": ["-q:a", "0"]},
    "mp3_v2": {"label": "MP3 V2 (~190 kbps VBR)", "ext": "mp3",
                "ffmpeg_args": ["-q:a", "2"]},
    "mp3_320": {"label": "MP3 320 kbps (CBR)", "ext": "mp3",
                 "ffmpeg_args": ["-b:a", "320k"]},
}

# music.apple.com / geo.music.apple.com / embed.music.apple.com media
# links: /{cc}/{type}/... where type is a known media type, so a bare
# music.apple.com page never gets routed to this backend.
_APPLE_URL_RE = re.compile(
    r"(?:geo\.|embed\.)?music\.apple\.com/(?:[a-z]{2}/)?"
    r"(?:songs?|albums?|playlists?|artists?|music-videos?|posts?|stations?"
    r"|library-songs?|library-albums?|library-playlists?)/",
    re.IGNORECASE)
# Legacy iTunes storefront links: itunes.apple.com/us/album/{slug}/id123
_ITUNES_STORE_URL_RE = re.compile(
    r"itunes\.apple\.com/[^/]+/(?:album|playlist|artist)/", re.IGNORECASE)
_ITUNES_ID_PART_RE = re.compile(r"^id(\d+)$", re.IGNORECASE)

_MEDIA_TYPE_MAP = {
    "song": "song", "songs": "song",
    "album": "album", "albums": "album",
    "playlist": "playlist", "playlists": "playlist",
    "artist": "artist", "artists": "artist",
    "music-video": "music_video", "music-videos": "music_video",
    "musicvideo": "music_video",
    "post": "post", "station": "station", "stations": "station",
    "library-song": "song", "library-songs": "song",
    "library-album": "album", "library-playlist": "playlist",
    "library-playlists": "playlist",
}

_UNSUPPORTED_MEDIA_TYPES = ("artist", "music_video", "post", "station")


def is_apple_music_url(url):
    """True for any URL blindDL can hand to the Apple Music backend."""
    url = str(url or "")
    return bool(_APPLE_URL_RE.search(url) or _ITUNES_STORE_URL_RE.search(url))


def parse_apple_url(url):
    """Split an Apple Music link into its parts.

    Returns a dict with ``storefront``, ``media_type`` (song/album/playlist/
    ...), ``media_id`` and an optional ``sub_id`` (the ``?i=`` song id on an
    album page), or None when the URL is not a supported Apple Music link.
    """
    url = str(url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host.endswith("music.apple.com") or host == "itunes.apple.com"):
        return None
    query = parse_qs(parsed.query)
    sub_id = (query.get("i") or [None])[0]
    path = parsed.path or ""
    media_id = None
    media_type = None
    if host == "itunes.apple.com":
        parts = [p for p in path.strip("/").split("/") if p]
        # /{country}/{type}/{slug}/id{id} or /{country}/{type}/id{id}
        if len(parts) >= 3:
            storefront, media_type = parts[0], parts[1]
            for part in parts[2:]:
                match = _ITUNES_ID_PART_RE.match(part)
                if match:
                    media_id = match.group(1)
                    break
    else:
        # music.apple.com/{cc}/{type}/{slug}/{id} or /{cc}/{type}/{id}
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) >= 2:
            storefront = parts[0]
            media_type = parts[1]
            # Catalog ids are numeric (songs, albums, artists) or prefixed
            # (playlists: pl.*, stations: ra.*, curators: sp.*).
            if re.fullmatch(r"\d+|[a-z]{2}\.[A-Za-z0-9._-]+",
                            parts[-1] or "", re.IGNORECASE):
                media_id = parts[-1]
    if not media_id:
        return None
    media_type = _MEDIA_TYPE_MAP.get(str(media_type or "").lower())
    if media_type is None:
        return None
    return {
        "storefront": storefront,
        "media_type": media_type,
        "media_id": media_id,
        "sub_id": sub_id,
        "url": url,
    }


# What the iTunes Search API is asked to match for each Search tab search
# type: an entity to return, and the one field to match on. Best match sends
# no attribute at all, which is what makes it search everything.
_ITUNES_SEARCH_KINDS = {
    KIND_BEST: ("song", None),
    KIND_TRACK: ("song", "songTerm"),
    KIND_ARTIST: ("song", "artistTerm"),
    KIND_ALBUM: ("album", "albumTerm"),
}


def supports_kind(kind):
    """The iTunes API matches one named field per search, so all four work."""
    return search_kind.normalize(kind) in _ITUNES_SEARCH_KINDS


def search(query, config=None, order=None, kind=KIND_BEST):
    """Search Apple Music through iTunes' public, credential-free API.

    The catalogue behind music.apple.com is the one the iTunes Search API
    serves, so a hit's trackViewUrl resolves to the same song there. It
    returns up to 200 tracks, which is what the Search tab lists.

    *kind* is the Search tab's search type. Album returns one row per
    release, which downloads as every track on it; the others return tracks,
    matched on the whole entry or on just the title or artist.
    """
    kind = search_kind.normalize(kind)
    entity, attribute = _ITUNES_SEARCH_KINDS.get(
        kind, _ITUNES_SEARCH_KINDS[KIND_BEST]
    )
    params = {
        "term": query,
        "media": "music",
        "entity": entity,
        "limit": 200,
    }
    if attribute:
        params["attribute"] = attribute
    try:
        response = requests.get(_ITUNES_SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []
    items = []
    for entry in results:
        if kind == KIND_ALBUM:
            url = entry.get("collectionViewUrl") or ""
            if url:
                items.append(_album_item(entry, url))
            continue
        url = entry.get("trackViewUrl") or ""
        if not url:
            continue
        items.append(_track_item(entry, url))
    return items


def _album_item(collection, url):
    """One row for a whole album, which downloads as all of its tracks."""
    tracks = int(collection.get("trackCount") or 0)
    title = collection.get("collectionName") or "Unknown album"
    return {
        "id": f"applemusic:album:{collection.get('collectionId') or url}",
        # Resolved to its tracks by the Search tab before anything is
        # queued; the queue only ever sees ordinary Apple Music items.
        "kind": "applemusic_album",
        "title": title,
        "artist": collection.get("artistName") or "",
        "album": title,
        "source": _SEARCH_SOURCE,
        "duration_s": 0,
        "tracks": tracks,
        "format": search_kind.album_type_label(tracks),
        "url": url,
        "artwork_url": _larger_artwork(collection.get("artworkUrl100") or ""),
    }


def _track_item(track, url):
    return {
        "id": f"applemusic:{track.get('trackId') or url}",
        "kind": "applemusic",
        "title": track.get("trackName") or "Unknown title",
        "artist": track.get("artistName") or "",
        "album": track.get("collectionName") or "",
        "source": _SEARCH_SOURCE,
        "duration_s": int(track.get("trackTimeMillis") or 0) // 1000,
        "url": url,
        "preview_url": track.get("previewUrl") or "",
        "artwork_url": _larger_artwork(track.get("artworkUrl100") or ""),
    }


def _larger_artwork(artwork_url):
    return artwork_url.replace("100x100bb", "600x600bb")


def _lookup(media_id, entity="song"):
    """One iTunes lookup call; returns the results list or []."""
    try:
        response = requests.get(
            _ITUNES_LOOKUP_URL,
            params={"id": media_id, "entity": entity, "limit": 200},
            timeout=30,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []
    return results or []


def _lookup_song_item(media_id, url):
    """Resolve a single song's metadata through the iTunes lookup API."""
    results = _lookup(media_id, entity="song")
    track = next(
        (result for result in results
         if str(result.get("trackId")) == str(media_id)),
        results[0] if results else None,
    )
    if not track or not track.get("trackViewUrl"):
        return None
    return _track_item(track, url)


def _collection_items(info, url):
    """Resolve an album or playlist link to its track list.

    The iTunes lookup API returns the release as its first result and every
    track after it, which is exactly what the URL tab's picker needs. Falls
    back to an empty list when the link cannot be resolved, and the caller
    then returns a single placeholder item.
    """
    results = _lookup(info["media_id"], entity="song")
    if len(results) < 2:
        return []
    collection = results[0]
    items = []
    for track in results[1:]:
        track_url = track.get("trackViewUrl") or ""
        if not track_url:
            continue
        item = _track_item(track, track_url)
        item["album"] = item["album"] or collection.get("collectionName") or ""
        items.append(item)
    return items


def _placeholder_item(url, media_type):
    """A single un-resolved item so the queue can still proceed."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return {
        "id": f"applemusic:{url}",
        "kind": "applemusic",
        "title": slug or f"Apple Music {media_type}",
        "artist": "",
        "source": _SEARCH_SOURCE,
        "duration_s": 0,
        "url": url,
    }


def extract_flat(url, config=None):
    """Resolve an Apple Music URL into queue items with real metadata.

    Songs resolve through the iTunes lookup API. Albums and playlists come
    back as one item per track, so the URL tab's picker can choose which
    songs to queue; when the link cannot be resolved a single placeholder
    item keeps the queue flowing.
    """
    info = parse_apple_url(url)
    if info is None:
        raise RuntimeError(f"Not an Apple Music URL: {url}")
    media_type = info["media_type"]
    if media_type in _UNSUPPORTED_MEDIA_TYPES:
        raise RuntimeError(
            "blindDL supports Apple Music songs, albums, and playlists only.")
    if media_type == "song" or info.get("sub_id"):
        media_id = info["sub_id"] or info["media_id"]
        item = _lookup_song_item(media_id, url) or _placeholder_item(url, "song")
        return [item], item["title"]
    items = _collection_items(info, url)
    if not items:
        # iTunes lookup does not know playlist ids; fall back to the catalog
        # API with an anonymous developer token.
        items = _catalog_collection_items(info)
    if items:
        return items, items[0].get("album") or f"Apple Music {media_type}"
    item = _placeholder_item(url, media_type)
    return [item], item["title"]


def _anonymous_api():
    """Apple Music catalog API with an anonymous developer token.

    The token is generated client-side from the music.apple.com bundle, so
    catalog lookups (playlist/album track lists) work without cookies.
    """
    from musicdl.modules.utils import appleutils

    return appleutils.AppleMusicClientAPIUtils.create(
        storefront="us", language="en-US")


def _catalog_collection_items(info):
    """Resolve an album/playlist link to its tracks via the catalog API.

    Used when the iTunes lookup API cannot answer (playlist ids are
    ``pl.*`` and unknown to it). Returns [] when the link cannot be
    resolved, so the caller can fall back to a placeholder item.
    """
    try:
        api = _anonymous_api()
    except Exception:  # noqa: BLE001 - network/token failure
        return []
    try:
        if info["media_type"] == "playlist":
            collection = api.getplaylist(
                info["media_id"], limit_tracks=300)["data"][0]
        else:
            collection = _catalog_album(api, info["media_id"])
    except Exception:  # noqa: BLE001 - not found, restricted, offline
        return []
    name = _attr(collection, "name")
    storefront = info["storefront"] or "us"
    items = []
    for track in _relationships_tracks(collection):
        track_id = _track_id(track)
        if not track_id:
            continue
        attrs = track.get("attributes") or {}
        items.append({
            "id": f"applemusic:{track_id}",
            "kind": "applemusic",
            "title": attrs.get("name") or "Unknown title",
            "artist": attrs.get("artistName") or "",
            "album": attrs.get("albumName") or name or "",
            "source": _SEARCH_SOURCE,
            "duration_s": int(attrs.get("durationInMillis") or 0) // 1000,
            "url": f"https://music.apple.com/{storefront}/song/{track_id}",
            "preview_url": (attrs.get("previews") or [{}])[0].get("url") or "",
            "artwork_url": ((attrs.get("artwork") or {}).get("url") or ""),
        })
    return items


# -- downloading -----------------------------------------------------------


def _cookies_from_file(path):
    """Parse a Netscape cookies.txt into a {name: value} dict.

    Handles both plain cookies and the ``#HttpOnly_``-prefixed lines modern
    exporters write.
    """
    cookies = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if not line.startswith("#HttpOnly_"):
                        continue
                fields = line.split("\t")
                if len(fields) < 7:
                    continue
                name = fields[5].strip()
                value = fields[6].strip()
                if name:
                    cookies[name] = value
    except OSError as exc:
        raise RuntimeError(
            f"Could not read the Apple Music cookies file: {exc}") from exc
    return cookies


def _authenticated_api(config):
    """Build the authenticated Apple Music API and iTunes metadata client.

    Raises a RuntimeError with a user-actionable message when the cookies
    file is missing or does not carry a media-user-token.
    """
    from musicdl.modules.utils import appleutils

    cookies_path = str((config or {}).get("apple_music_cookies") or "")
    if not cookies_path or not os.path.isfile(cookies_path):
        raise RuntimeError(
            "No Apple Music cookies file configured. Export your browser "
            "cookies while logged in at music.apple.com (Settings, Accounts, "
            "Apple Music, Copy from browser), then set the path in Settings.")
    cookies = _cookies_from_file(cookies_path)
    if not cookies.get("media-user-token"):
        raise RuntimeError(
            "Your Apple Music cookies file has no media-user-token. Export "
            "fresh cookies while logged in at music.apple.com.")
    try:
        api = appleutils.AppleMusicClientAPIUtils.createfromnetscapecookies(
            cookies=cookies, language="en-US")
    except Exception as exc:  # noqa: BLE001 - expired or revoked token
        raise RuntimeError(
            f"Apple Music sign-in failed: {exc}") from exc
    itunes_api = None
    try:
        itunes_api = appleutils.AppleMusicClientItunesApiUtils(
            storefront=api.storefront or "us", language=api.language)
    except Exception:  # noqa: BLE001 - metadata dates are a nice-to-have
        itunes_api = None
    return api, itunes_api


def _catalog_song(api, media_id):
    song = api.getsong(str(media_id))
    return song["data"][0]


def _catalog_album(api, album_id):
    """Fetch an album resource with its track list.

    ``getalbum`` does not request the tracks relationship, so this asks the
    catalog endpoint for ``include[tracks]`` and returns the unwrapped
    album resource.
    """
    response = api.client.get(
        f"{_AMP_API_URL}/v1/catalog/{api.storefront}/albums/{album_id}",
        params={
            "include[tracks]": "data",
            "extend": "extendedAssetUrls",
            "l": api.language,
        },
    )
    response.raise_for_status()
    return response.json()["data"][0]


def _relationships_tracks(collection):
    return ((collection.get("relationships") or {})
            .get("tracks", {}).get("data", []) or [])


def _track_id(track):
    play_params = (track.get("attributes") or {}).get("playParams") or {}
    return play_params.get("catalogId") or track.get("id")


def _attr(resource, name):
    return (resource.get("attributes") or {}).get(name) or ""


def _safe_name(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "").strip())
    return cleaned or "Unknown"


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Cancelled.")


def _download_stream(stream_url, decryption_key, out_path, cancel_event=None):
    """Download, decrypt, and remux one Apple Music CENC stream to M4A.

    Apple's AAC stream is a fragmented MP4 served over HLS and encrypted
    with CENC (EXT-X-KEY METHOD=ISO-23001-7), so there is no AES-128 key for
    ffmpeg's HLS demuxer to apply. Instead yt-dlp's HLS downloader first
    assembles the encrypted fragments (init segment and byteranges) into one
    MP4, then ffmpeg's MOV demuxer decrypts it with -decryption_key and
    remuxes it. Both tools ship with blindDL.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.hls import HlsFD

    tmp_dir = tempfile.mkdtemp(prefix="blinddl-am-")
    encrypted_path = os.path.join(tmp_dir, "encrypted.mp4")
    try:
        _check_cancelled(cancel_event)
        with YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "overwrites": True,
            "noprogress": True,
            "nopart": True,
            "allow_unplayable_formats": True,
            "concurrent_fragment_downloads": 8,
        }) as ydl:
            downloader = HlsFD(ydl, ydl.params)
            success, _ = downloader.download(
                encrypted_path,
                {"url": stream_url, "ext": "mp4", "protocol": "m3u8"},
            )
        if not success or not os.path.isfile(encrypted_path):
            raise RuntimeError("Apple Music stream download failed.")
        _check_cancelled(cancel_event)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if decryption_key:
            command += ["-decryption_key", decryption_key]
        command += ["-i", encrypted_path, "-c", "copy", "-movflags",
                    "+faststart", out_path]
        _run_ffmpeg(
            command, "ffmpeg could not decode the Apple Music stream")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_ffmpeg(command, what):
    """Run an ffmpeg command, turning failures into user-facing errors."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg was not found; blindDL needs it for Apple Music "
            "downloads.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out.") from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"{what}: {detail[-400:]}")


def _convert_m4a_to_mp3(m4a_path, mp3_path, output_format):
    """Re-encode a tagged M4A to MP3, carrying tags and cover art.

    ffmpeg copies the M4A's tags and attached picture into ID3 (verified:
    TIT2/TPE1/TALB/TRCK/TCON/TCOM and an APIC frame all survive), so the
    finished file needs no further tagging.
    """
    command = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", m4a_path, "-map_metadata", "0", "-id3v2_version", "3",
                "-c:v", "copy", "-c:a", "libmp3lame"]
               + list(output_format["ffmpeg_args"]) + [mp3_path])
    _run_ffmpeg(command, "ffmpeg could not convert the Apple Music track "
                         "to MP3")


def _write_tags(out_path, item):
    """Tag the finished M4A and embed cover art and synced lyrics."""
    from mutagen.mp4 import MP4, MP4Cover

    try:
        tags = MP4(out_path)
        if item.media_tags is not None:
            tags.update(item.media_tags.asmp4tags())
        cover_url = item.cover_url
        if cover_url:
            try:
                response = requests.get(cover_url, timeout=30)
                response.raise_for_status()
                tags["covr"] = [
                    MP4Cover(response.content,
                             imageformat=MP4Cover.FORMAT_JPEG)
                ]
            except (requests.RequestException, OSError):
                pass
        tags.save()
    except Exception:  # noqa: BLE001 - a tagging failure must not lose audio
        pass


def _write_lrc(out_path, item):
    """Write the synced-lyrics sidecar next to the finished file."""
    if item.lyrics is None or not item.lyrics.synced:
        return out_path
    try:
        with open(os.path.splitext(out_path)[0] + ".lrc", "w",
                  encoding="utf-8", newline="\n") as handle:
            handle.write(item.lyrics.synced)
    except OSError:
        pass


def _download_song(api, itunes_api, metadata, playlist_metadata, target_dir,
                   cancel_event=None, audio_format="m4a"):
    """Download one song's HLS stream, decrypt it, tag it, and save it.

    The stream always decrypts to an M4A first (that is what the Widevine
    pipeline yields). With ``audio_format`` set to an MP3 variant the tagged
    M4A is re-encoded through ffmpeg's LAME encoder and the M4A is removed.
    """
    from musicdl.modules.utils import appleutils

    _check_cancelled(cancel_event)
    try:
        item = appleutils.AppleMusicClientDownloadSongUtils.getdownloaditem(
            song_metadata=metadata,
            playlist_metadata=playlist_metadata,
            codec=appleutils.SongCodec.AAC_LEGACY,
            apple_music_api=api,
            itunes_api=itunes_api,
            use_wrapper=False,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the queue
        raise RuntimeError(
            f"Could not prepare the Apple Music track: {exc}") from exc
    stream = item.stream_info.audio_track if item.stream_info else None
    if stream is None or not stream.stream_url:
        raise RuntimeError("Apple Music returned no playable stream.")
    decryption_key = (
        item.decryption_key.audio_track.key
        if item.decryption_key and item.decryption_key.audio_track else None
    )
    tags = item.media_tags
    title = _attr(metadata, "name") or "Unknown title"
    track_no = getattr(tags, "track", None) if tags is not None else None
    if track_no:
        title = f"{int(track_no):02d} {title}"
    output_format = (APPLE_OUTPUT_FORMATS.get(audio_format)
                     or APPLE_OUTPUT_FORMATS["m4a"])
    out_path = os.path.join(
        target_dir, f"{_safe_name(title)}.{output_format['ext']}")
    _check_cancelled(cancel_event)
    if output_format["ext"] == "m4a":
        _download_stream(stream.stream_url, decryption_key, out_path,
                         cancel_event)
        if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            raise RuntimeError("Apple Music produced an empty file.")
        _write_tags(out_path, item)
        _write_lrc(out_path, item)
        return
    # MP3 output: decrypt and tag a temporary M4A, then re-encode it.
    tmp_dir = tempfile.mkdtemp(prefix="blinddl-am-out-")
    m4a_path = os.path.join(tmp_dir, "track.m4a")
    try:
        _download_stream(stream.stream_url, decryption_key, m4a_path,
                         cancel_event)
        if not os.path.isfile(m4a_path) or os.path.getsize(m4a_path) <= 0:
            raise RuntimeError("Apple Music produced an empty file.")
        _write_tags(m4a_path, item)
        _check_cancelled(cancel_event)
        _convert_m4a_to_mp3(m4a_path, out_path, output_format)
        if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            raise RuntimeError("Apple Music produced an empty MP3.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _write_lrc(out_path, item)
    return out_path


def download(url, out_dir, config=None, progress_cb=None, cancel_event=None):
    """Download an Apple Music song, album, or playlist in-process.

    Requires configured Apple Music cookies with a media-user-token (an
    active subscription). A single song lands in ``out_dir``; album and
    playlist tracks land in a subfolder named after the release.
    """
    os.makedirs(out_dir, exist_ok=True)
    api, itunes_api = _authenticated_api(config)
    info = parse_apple_url(url)
    if info is None:
        raise RuntimeError(f"Not an Apple Music URL: {url}")
    media_type = info["media_type"]
    if media_type in _UNSUPPORTED_MEDIA_TYPES:
        raise RuntimeError(
            "blindDL supports Apple Music songs, albums, and playlists only.")
    audio_format = (config or {}).get("apple_music_format", "m4a")
    if media_type == "song" or info.get("sub_id"):
        media_id = info["sub_id"] or info["media_id"]
        metadata = _catalog_song(api, media_id)
        return _download_song(
            api, itunes_api, metadata, None, out_dir, cancel_event,
            audio_format=audio_format,
        )
    if media_type == "album":
        collection = _catalog_album(api, info["media_id"])
        kind_name = "Album"
    else:
        collection = api.getplaylist(
            info["media_id"], limit_tracks=300)["data"][0]
        kind_name = "Playlist"
    tracks = _relationships_tracks(collection)
    if not tracks:
        raise RuntimeError(f"The Apple Music {kind_name.lower()} has no tracks.")
    target_dir = os.path.join(out_dir, _safe_name(
        _attr(collection, "name") or kind_name))
    os.makedirs(target_dir, exist_ok=True)
    for track in tracks:
        _check_cancelled(cancel_event)
        metadata = _catalog_song(api, _track_id(track))
        _download_song(api, itunes_api, metadata, collection, target_dir,
                       cancel_event, audio_format=audio_format)
    return target_dir


def download_track(url, out_dir, config=None, progress_cb=None,
                   cancel_event=None):
    """Alias for download."""
    return download(url, out_dir, config, progress_cb, cancel_event)
