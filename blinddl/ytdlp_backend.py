# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""yt-dlp backend: URL inspection, search, and downloading.

All functions are synchronous and meant to be called from worker threads.
Progress is reported through plain callbacks so the GUI layer can marshal
them onto the main thread.
"""

import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yt_dlp

from . import search_order
from .search_order import ORDER_POPULAR, ORDER_RECENT, ORDER_RELEVANCE

# The audio_format / video_format value meaning "leave the file alone".
ORIGINAL_FORMAT = "original"

# yt-dlp's audio-quality scale runs 0 (best) to 10 (worst) and is remapped
# per encoder, so one number asks every lossy codec for its top VBR setting:
# -q:a 0 for LAME, which is V0, and the equivalent for AAC and Vorbis. It is
# ignored when the source already is the requested codec -- that stays a
# lossless copy -- and for FLAC and WAV, which have no lossy setting.
AUDIO_QUALITY = "0"

# Opus is the one lossy codec yt-dlp's scale does not cover, so it would fall
# to libopus's own default of roughly 96 kbps. ffmpeg's libopus encoder has
# no minrate/maxrate of its own -- it takes them and ignores them -- so the
# wanted 160-400 kbps band is set as an unconstrained VBR target in the
# middle of it, which is where real music then floats.
AUDIO_EXTRA_ARGS = {"opus": ("-vbr", "on", "-b:a", "256k")}

# Long-term-storage preset: HEVC at a visually near-transparent CRF, with the
# original audio copied across untouched. hvc1 is the tag Apple players need
# before they will open HEVC in MP4.
X265_ARGS = (
    "-c:v", "libx265", "-crf", "24", "-preset", "medium",
    "-tag:v", "hvc1", "-c:a", "copy",
)


class DownloadCancelled(Exception):
    """Raised inside progress hooks when the user cancels a download."""


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _entry_to_item(entry):
    url = entry.get("webpage_url") or entry.get("url") or ""
    # Flat playlist entries sometimes only carry a bare id.
    if url and not url.startswith("http") and entry.get("ie_key") == "Youtube":
        url = f"https://www.youtube.com/watch?v={url}"
    return {
        "id": str(entry.get("id") or url),
        "title": entry.get("title") or "Unknown title",
        "url": url,
        "duration": entry.get("duration"),
        "uploader": entry.get("uploader") or entry.get("channel") or "",
    }


MAX_NESTED_DEPTH = 3
# Search results and hashtag feeds are ranked, endless, and re-shuffle
# between visits, so only the top slice is worth listing.
RANKED_FEED_LIMIT = 100
# How many results a YouTube search asks for.
SEARCH_COUNT = 200

_CHANNEL_ID_RE = re.compile(r"UC[\w-]{22}")
_PLAYLIST_ID_RE = re.compile(r"(?:PL|UU|LL|FL|OL|RD)[\w-]{10,}")
_RANKED_FEED_RE = re.compile(
    r"https?://[^/]*youtube\.com/(?:results\b|hashtag/)", re.IGNORECASE)
_HASHTAG_URL_RE = re.compile(
    r"https?://[^/]*youtube\.com/hashtag/([^/?#]+)", re.IGNORECASE)
_SEARCH_URL_RE = re.compile(
    r"https?://[^/]*youtube\.com/(?:results|search)\b", re.IGNORECASE)

# YouTube keeps the sort for a search in its `sp` parameter, which is a
# base64 protobuf rather than a word. Field 1 is the sort -- 2 is upload
# date, 3 is view count -- and the tail is the "videos only" filter yt-dlp's
# own ytsearch: prefix sends, kept so a search still returns videos rather
# than channels and playlists mixed in.
SEARCH_SORT_PARAMS = {
    ORDER_RECENT: "CAISAhAB8AEB",
    ORDER_POPULAR: "CAMSAhAB8AEB",
}
YOUTUBE_RESULTS_URL = "https://www.youtube.com/results"


def supports_order(order):
    """YouTube search sorts by upload date and by view count alike."""
    return search_order.normalize(order) in (ORDER_RELEVANCE, ORDER_RECENT,
                                             ORDER_POPULAR)


def search_url(query, order=ORDER_RELEVANCE):
    """A YouTube results URL for *query*, sorted the way *order* asks.

    Best match goes through yt-dlp's own ytsearch: prefix, which is what it
    is for. The other two need the real results page, because the prefix has
    no way to carry a sort.
    """
    sort = SEARCH_SORT_PARAMS.get(search_order.normalize(order))
    if not sort:
        return None
    return (f"{YOUTUBE_RESULTS_URL}?"
            f"{urlencode({'search_query': query, 'sp': sort})}")


def ordered_feed_url(url, order=ORDER_RELEVANCE):
    """Rewrite a ranked YouTube feed so it answers in *order*.

    Only search and hashtag feeds have an order to choose. A channel already
    publishes newest first and yt-dlp drops the sort parameters its Videos
    tab takes, and a playlist is in the order its owner put it in -- so both
    are returned untouched.

    A hashtag page takes no sort of its own, so it is asked as a search for
    that tag instead, which does. That is the same set of videos reached the
    way YouTube itself reaches it from the search box.
    """
    order = search_order.normalize(order)
    sort = SEARCH_SORT_PARAMS.get(order)
    if not sort or not url:
        return url

    hashtag = _HASHTAG_URL_RE.match(url)
    if hashtag:
        tag = hashtag.group(1).lstrip("#")
        return (f"{YOUTUBE_RESULTS_URL}?"
                f"{urlencode({'search_query': f'#{tag}', 'sp': sort})}")

    if _SEARCH_URL_RE.match(url):
        parts = urlparse(url)
        query = [(name, value) for name, value in parse_qsl(parts.query)
                 if name != "sp"]
        query.append(("sp", sort))
        return str(urlunparse(parts._replace(query=urlencode(query))))
    return url


def normalize_url(text):
    """Expand shorthand into something yt-dlp can open.

    Typing a full URL is awkward with a screen reader, so a channel handle
    ("@veritasium"), a hashtag ("#rimworld"), a bare channel or playlist id,
    or a scheme-less address are all accepted as subscription targets.
    """
    text = (text or "").strip()
    if not text or "://" in text:
        return text
    if text.startswith("@") and " " not in text:
        return f"https://www.youtube.com/{text}"
    if text.startswith("#") and " " not in text:
        return f"https://www.youtube.com/hashtag/{text[1:].lstrip('#')}"
    if _CHANNEL_ID_RE.fullmatch(text):
        return f"https://www.youtube.com/channel/{text}"
    if _PLAYLIST_ID_RE.fullmatch(text):
        return f"https://www.youtube.com/playlist?list={text}"
    if "." in text and " " not in text:
        return f"https://{text}"
    return text


def _iter_entries(entries, depth=MAX_NESTED_DEPTH):
    """Yield leaf entries, expanding nested containers as we go.

    A bare channel URL extracts as a playlist of tab playlists ("Videos",
    "Shorts", "Live"). Those tabs carry no URL of their own, so without this
    expansion a channel yields no items at all.
    """
    for entry in entries:
        if not entry:
            continue
        nested = entry.get("entries") if depth > 0 else None
        if nested is not None and entry.get("_type") in ("playlist",
                                                         "multi_video"):
            yield from _iter_entries(nested, depth - 1)
        else:
            yield entry


def extract_flat(url, cookies_from_browser=None, cookies_file=None,
                 limit=None, order=ORDER_RELEVANCE):
    """Inspect a URL without downloading.

    Returns (items, title): items is a list of normalized dicts (one entry
    for a single video, many for a playlist/channel/hashtag), title is the
    name of the video/playlist/channel itself. *limit* caps how many entries
    are listed; ranked feeds get a default cap so they stay responsive.

    *order* only reaches the feeds that have one to choose -- searches and
    hashtags. See ordered_feed_url.
    """
    url = ordered_feed_url(url, order)
    if limit is None and _RANKED_FEED_RE.match(url):
        limit = RANKED_FEED_LIMIT
    opts = {
        # "in_playlist" (rather than True) resolves the URL itself before
        # flattening, so a watch?v=...&list=... link expands to its playlist
        # and a channel expands to its tabs instead of coming back as a
        # single unusable redirect entry.
        "extract_flat": "in_playlist",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": False,
    }
    if limit:
        opts["playlistend"] = int(limit)
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (str(cookies_from_browser),)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError(f"Could not extract any information from: {url}")
    title = info.get("title") or url
    entries = info.get("entries")
    if entries is not None:
        items = [_entry_to_item(e) for e in _iter_entries(entries)]
    else:
        items = [_entry_to_item(info)]
    seen = set()
    unique = []
    for item in items:
        # Channel tabs overlap: a premiere shows up under both Live and
        # Videos, and the picker should not list it twice.
        if not item["url"] or item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique, title


def search(query, count=SEARCH_COUNT, order=ORDER_RELEVANCE):
    """Search YouTube, sorted the way *order* asks.

    Best match uses yt-dlp's ytsearch extractor. The other two go to the
    results page with a sort parameter, since the extractor cannot carry
    one; *count* becomes the page cap there.
    """
    url = search_url(query, order)
    if url is None:
        items, _title = extract_flat(f"ytsearch{count}:{query}")
    else:
        items, _title = extract_flat(url, limit=count)
    return items


def resolve_stream(url, audio_only=False, cookies_from_browser=None,
                   cookies_file=None, http_headers=None):
    """Resolve *url* to one stream that a native media player can open.

    Video previews deliberately request a progressive format containing both
    audio and video. Native desktop players cannot combine yt-dlp's separate
    adaptive audio/video URLs themselves.
    """
    primary_format = (
        "bestaudio/best"
        if audio_only else
        "best[protocol^=http][vcodec!=none][acodec!=none]/"
        "best[vcodec!=none][acodec!=none]/best"
    )
    fallback_format = "bestaudio/best" if audio_only else "best"

    opts = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": primary_format,
    }
    if http_headers:
        opts["http_headers"] = dict(http_headers)
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (str(cookies_from_browser),)

    for attempt, fmt in enumerate((primary_format, fallback_format)):
        opts["format"] = fmt
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except yt_dlp.utils.ExtractorError as exc:
            if attempt == 0 and "Requested format is not available" in str(exc):
                continue
            raise
    else:
        raise RuntimeError("No playable media stream was found.")

    if not info:
        raise RuntimeError("No playable media stream was found.")
    entries = info.get("entries")
    if entries is not None:
        info = next((entry for entry in entries if entry), None)
    if not info:
        raise RuntimeError("No playable media stream was found.")
    stream_url = info.get("url")
    if not stream_url:
        requested = info.get("requested_downloads") or ()
        stream_url = next(
            (entry.get("url") for entry in requested if entry.get("url")),
            None,
        )
    if not stream_url:
        raise RuntimeError("The site did not expose a stream for playback.")
    return str(stream_url)


def download(url, out_dir, audio_only=True, audio_format="mp3",
             video_format="mp4", progress_cb=None, cancel_event=None,
             http_headers=None, cookies_from_browser=None,
             cookies_file=None):
    """Download one URL. progress_cb receives yt-dlp progress dicts.

    audio_format and video_format both accept "original", which means the
    file is kept in whatever container the site serves: no ffmpeg pass, so
    nothing is re-encoded and the download finishes as soon as the bytes do.
    """
    os.makedirs(out_dir, exist_ok=True)

    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if progress_cb is not None:
            progress_cb(d)

    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "continuedl": True,
        "windowsfilenames": True,
        "noprogress": True,
    }
    if http_headers:
        opts["http_headers"] = dict(http_headers)
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (str(cookies_from_browser),)
    if audio_only:
        opts["format"] = "bestaudio/best"
        if audio_format and audio_format != ORIGINAL_FORMAT:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": AUDIO_QUALITY,
            }]
            extra = AUDIO_EXTRA_ARGS.get(audio_format)
            if extra:
                opts["postprocessor_args"] = {"extractaudio": list(extra)}
    else:
        # Always the best streams the site has, whatever they are encoded in;
        # the container is settled afterwards so nothing is thrown away to
        # satisfy it.
        opts["format"] = "bestvideo+bestaudio/best"
        if video_format in ("mp4", "mkv"):
            # A remux, not a re-encode: the picture and sound are the site's
            # own, moved into the container the user asked for.
            opts["merge_output_format"] = video_format
        elif video_format == "avi":
            # AVI cannot hold the codecs modern sites stream, so this one is
            # a real conversion. yt-dlp supplies the Xvid settings itself.
            opts["postprocessors"] = [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "avi",
            }]
        elif video_format == "x265":
            # Merging into Matroska first guarantees the convertor has
            # something to convert -- MP4 merges can silently fall back to
            # mkv, and yt-dlp skips a conversion that is already in target
            # format. Audio is copied, so only the picture is re-encoded.
            opts["merge_output_format"] = "mkv"
            opts["postprocessors"] = [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }]
            opts["postprocessor_args"] = {"videoconvertor": list(X265_ARGS)}
        # "original" (and anything unrecognized) leaves the container to
        # yt-dlp, which keeps the streams as they came.
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        raise
