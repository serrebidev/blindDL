# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Tags for downloaded music files.

The vendored musicdl writes three tags and nothing else: title, album and
artist. A file that arrives with only those three is a file a music library
cannot file -- no album artist to group it under, no track number to order
it by, no year, no artwork. FreeQobuz is the worst affected because Qobuz
serves bare FLAC: the catalogue entry blindDL searched carries the whole
release -- ISRC, track and disc numbers, label, composer, copyright, release
date, cover art -- and every one of those fields used to be thrown away the
moment the download started.

So the first and largest source of tags here is the search result blindDL
already holds. Nothing is fetched to write it and nothing can fail.

What the site does not know is then asked of MusicBrainz, and after that of
TheAudioDB. MusicBrainz is asked by ISRC first, which is an exact identifier
for one recording rather than a guess at a name, and only falls back to an
artist-and-title search when there is no ISRC. It also carries the
MusicBrainz ids themselves, which is what lets Picard, Plex, Jellyfin and
Lidarr file a track with confidence rather than by fuzzy name match.

Both services are optional, both are strictly gap-filling, and neither can
fail a download: a lookup that errors or times out simply leaves those
fields empty. An existing tag is never overwritten.
"""

import os
import re
import threading
import time
from urllib.parse import quote

import requests

from . import APP_NAME, __version__, music_match

# MusicBrainz requires a real User-Agent naming the application and a way to
# contact whoever runs it; anonymous clients get blocked.
_USER_AGENT = (
    f"{APP_NAME}/{__version__} "
    "( https://github.com/serrebidev/blindDL )"
)
_MB_ROOT = "https://musicbrainz.org/ws/2"
# MusicBrainz allows one request per second per application, averaged. The
# lock serialises blindDL's own concurrent downloads so a queue of ten cannot
# burst ten lookups at once and earn a block for everybody.
_MB_MIN_INTERVAL_S = 1.1
_mb_lock = threading.Lock()
_mb_last_call = 0.0

# TheAudioDB's documented free key. It is a shared test key, not a secret,
# and it is rate limited upstream -- which is why it is the last source
# asked and never the one a tag depends on.
_AUDIODB_KEY = "123"
_AUDIODB_ROOT = "https://www.theaudiodb.com/api/v1/json"

# Apple's public search endpoint. No key, no account, no scraping, and its
# album data is better kept than TheAudioDB's, which is edited by hand. It
# is asked after MusicBrainz because MusicBrainz is the one that knows
# identity -- the ids a library files by -- while this knows what a record
# was called, when it came out, what shelf it belongs on and what the sleeve
# looks like, which is exactly what an ISRC lookup does not carry.
_ITUNES_URL = "https://itunes.apple.com/search"
# Below this the candidate is a different song that merely searches alike,
# and its album and year would be worse than none.
_ITUNES_MIN_SCORE = 70.0

# Synced lyrics, by the same open service the Deezer downloader already
# uses. Qobuz and the other music sites send none at all, so without this a
# FreeQobuz download is the one file in a library with no words.
_LRCLIB_URL = "https://lrclib.net/api/get"

HTTP_TIMEOUT_S = 12
# Cover art larger than this is not embedded. Some services answer with a
# multi-megabyte master; a music file should not triple in size for artwork
# no player displays at that resolution.
MAX_COVER_BYTES = 8 * 1024 * 1024

# Every tag blindDL knows how to write, in one vocabulary. The per-format
# writers below map these onto FLAC's Vorbis comments, MP3's ID3 frames and
# MP4's atoms.
TAG_FIELDS = (
    "title", "artist", "album", "albumartist", "date", "tracknumber",
    "tracktotal", "discnumber", "disctotal", "genre", "isrc", "label",
    "composer", "copyright", "musicbrainz_trackid", "musicbrainz_albumid",
    "musicbrainz_artistid", "musicbrainz_releasegroupid",
)

# What each of the two catalogue lookups is actually able to contribute. A
# lookup whose every field is already known, on a download that already has
# its sleeve, is a round trip that can only confirm what is there -- and it
# is paid once per file, on a queue that may be an entire album.
_ITUNES_FILLS = ("album", "albumartist", "genre", "date", "tracknumber",
                 "tracktotal", "discnumber", "disctotal")
_AUDIODB_FILLS = ("genre", "date", "albumartist", "label",
                  "musicbrainz_albumid")

_YEAR_RE = re.compile(r"^(\d{4})")
# A date a tag can carry: a year, optionally narrowed to month and day.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _text(value):
    """One clean string, or "" for the several ways a field can be absent.

    musicdl spells a missing field "NULL" rather than leaving it empty, and
    a JSON API can answer with null, 0 or an empty list for the same thing.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("title") or "")
    text = str(value).strip()
    if text.lower() in ("null", "none", "nan", "0", ""):
        return ""
    return text


def _number(value):
    """A positive integer as a string, or "". Track 0 is not a track."""
    text = _text(value)
    if not text.isdigit() or int(text) <= 0:
        return ""
    return str(int(text))


def _missing(tags, fields):
    """Whether any of *fields* is still blank."""
    return any(not tags.get(field) for field in fields)


def _fill(tags, field, value):
    """Write *value* only where nothing is known yet.

    Every source after the first is filling gaps, so a later and less
    certain answer can never displace an earlier and more certain one.
    """
    if field in tags and tags[field]:
        return
    text = _text(value)
    if text:
        tags[field] = text


def tags_from_song_info(song_info):
    """The tags a search result already carries, before anything is fetched.

    The generic fields every musicdl source fills are read first, then the
    raw catalogue entry the source stashed in ``raw_data['search']``, which
    for Qobuz is the entire release and for most other sources is thin. It
    is read defensively: an unknown source simply contributes nothing extra
    rather than raising in the middle of a download.
    """
    tags = {}
    _fill(tags, "title", getattr(song_info, "song_name", ""))
    _fill(tags, "artist", getattr(song_info, "singers", ""))
    _fill(tags, "album", getattr(song_info, "album", ""))
    cover = _text(getattr(song_info, "cover_url", ""))

    raw = getattr(song_info, "raw_data", None)
    item = (raw or {}).get("search") if isinstance(raw, dict) else None
    if isinstance(item, dict):
        cover = _qobuz_shaped(tags, item) or cover
    return tags, cover


def _qobuz_shaped(tags, item):
    """Read a Qobuz-shaped catalogue track. Returns a cover URL or "".

    Qobuz nests the release under ``album`` and the performer beside it, and
    the same shape is close enough to what several other sources answer with
    that reading it by key rather than by source costs nothing: a source
    without these keys contributes nothing and is no worse off.
    """
    album = item.get("album") if isinstance(item.get("album"), dict) else {}

    _fill(tags, "title", item.get("title"))
    _fill(tags, "artist", (item.get("performer") or {}).get("name")
          if isinstance(item.get("performer"), dict) else item.get("performer"))
    _fill(tags, "album", album.get("title"))
    _fill(tags, "albumartist", album.get("artist"))
    _fill(tags, "composer", item.get("composer"))
    _fill(tags, "isrc", item.get("isrc"))
    _fill(tags, "copyright", item.get("copyright"))
    _fill(tags, "label", album.get("label"))
    _fill(tags, "genre", album.get("genre"))
    tracknumber = _number(item.get("track_number"))
    if tracknumber:
        _fill(tags, "tracknumber", tracknumber)
    discnumber = _number(item.get("media_number"))
    if discnumber:
        _fill(tags, "discnumber", discnumber)
    tracktotal = _number(album.get("tracks_count"))
    if tracktotal:
        _fill(tags, "tracktotal", tracktotal)
    disctotal = _number(album.get("media_count"))
    if disctotal:
        _fill(tags, "disctotal", disctotal)
    # Qobuz dates arrive as YYYY-MM-DD, and the original release date is the
    # one a library sorts by -- the stream and download dates can be decades
    # later for a reissue. ``released_at`` is a Unix timestamp rather than a
    # date, so it is not a candidate: writing it would tag a 2013 record as
    # the year 1366.
    for field in ("release_date_original", "release_date_stream",
                  "release_date_download"):
        date = _text(album.get(field))
        if _DATE_RE.match(date):
            _fill(tags, "date", date)
            break

    image = album.get("image") if isinstance(album.get("image"), dict) else {}
    return _text(image.get("large") or image.get("small")
                 or image.get("thumbnail"))


def _mb_get(path, params):
    """One rate-limited MusicBrainz call. None on any failure.

    The sleep is held under the lock deliberately: the limit is on the
    application as a whole, so two download threads must queue behind each
    other rather than each keep its own honest-looking interval.
    """
    global _mb_last_call
    with _mb_lock:
        wait = _MB_MIN_INTERVAL_S - (time.monotonic() - _mb_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.get(
                f"{_MB_ROOT}/{path}",
                params={**params, "fmt": "json"},
                headers={"User-Agent": _USER_AGENT},
                timeout=HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            return response.json()
        except Exception:  # noqa: BLE001 - a lookup never fails a download
            return None
        finally:
            _mb_last_call = time.monotonic()


def _mb_escape(text):
    """Escape one value for a Lucene query, and quote it."""
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)
    return f'"{escaped}"'


def lookup_musicbrainz(tags):
    """Fill what MusicBrainz knows and the site did not.

    An ISRC identifies exactly one recording, so it is asked first and its
    answer is trusted. Without one, the artist and title are searched and
    the top match is taken only when MusicBrainz itself scores it at least
    90 -- below that a search for a common title returns a different
    recording with the same name, and wrong tags are worse than none.
    """
    isrc = tags.get("isrc", "")
    recordings, exact = [], False
    if isrc:
        # The isrc resource accepts a narrower set of inc values than a
        # recording lookup does: asking it for release-groups is a 400, and
        # a 400 here would silently cost every tag this branch exists for.
        body = _mb_get(f"isrc/{quote(isrc, safe='')}",
                       {"inc": "artist-credits+releases"})
        recordings = (body or {}).get("recordings") or []
        exact = bool(recordings)
    if not recordings:
        title, artist = tags.get("title", ""), tags.get("artist", "")
        if not title:
            return
        query = f"recording:{_mb_escape(title)}"
        if artist:
            query += f" AND artist:{_mb_escape(artist)}"
        body = _mb_get("recording", {
            "query": query, "limit": 3,
            "inc": "artist-credits+releases+release-groups",
        })
        candidates = (body or {}).get("recordings") or []
        recordings = [
            candidate for candidate in candidates
            if int(candidate.get("score") or 0) >= 90
        ][:1]
    if not recordings:
        return

    recording = recordings[0]
    _fill(tags, "title", recording.get("title"))
    _fill(tags, "musicbrainz_trackid", recording.get("id"))
    credits = recording.get("artist-credit") or []
    if credits:
        artist = (credits[0] or {}).get("artist") or {}
        _fill(tags, "artist", _credited_artist(credits) or artist.get("name"))
        _fill(tags, "musicbrainz_artistid", artist.get("id"))
    _fill(tags, "isrc", (recording.get("isrcs") or [""])[0])

    # Album fields come only from an ISRC match. A name search finds the
    # right song under the wrong record often enough to matter -- a cover on
    # a compilation scores as highly as the original -- and the recording's
    # only listed release is then that compilation. The song's own site
    # nearly always named the album correctly, and no album beats a wrong
    # one, so an inferred match contributes identifiers and nothing else.
    if not exact:
        return
    releases = recording.get("releases") or []
    if not releases:
        return
    release = _best_release(releases)
    _fill(tags, "album", release.get("title"))
    _fill(tags, "musicbrainz_albumid", release.get("id"))
    _fill(tags, "date", release.get("date"))
    group = release.get("release-group") or {}
    _fill(tags, "musicbrainz_releasegroupid", group.get("id"))
    _fill(tags, "date", group.get("first-release-date"))
    media = release.get("media") or []
    if media:
        track = ((media[0] or {}).get("track") or [{}])[0]
        _fill(tags, "tracknumber", _number(track.get("number")))
        _fill(tags, "tracktotal", _number((media[0] or {}).get("track-count")))


def _credited_artist(credits):
    """Rebuild the full credit line, join phrases and all.

    MusicBrainz splits a collaboration into parts with the wording between
    them held separately: "Daft Punk", " feat. ", "Pharrell Williams". The
    join phrase carries its own spacing and must not be trimmed, or the
    credit comes out run together as "Daft Punkfeat.Pharrell Williams".
    """
    line = ""
    for part in credits:
        if not isinstance(part, dict):
            continue
        name = _text(part.get("name")
                     or (part.get("artist") or {}).get("name"))
        if not name:
            continue
        line += name + str(part.get("joinphrase") or "")
    return line.strip()


def _best_release(releases):
    """The release a track is most likely actually from.

    MusicBrainz returns a recording's releases in no useful order, and the
    first one is routinely a karaoke record, a covers compilation or a
    regional reissue -- a search for a well-known song can otherwise tag it
    with an album its performers had nothing to do with. Preference goes to
    an official, non-compilation release, and then to the earliest one,
    which is the original rather than a later repackaging.
    """
    def rank(release):
        group = release.get("release-group") or {}
        secondary = [str(kind).lower()
                     for kind in (group.get("secondary-types") or [])]
        date = _text(release.get("date")) or "9999"
        return (
            "compilation" in secondary,
            _text(release.get("status")).lower() != "official",
            str(group.get("primary-type") or "").lower()
            not in ("album", "single", "ep"),
            date,
        )

    return sorted(releases, key=rank)[0]


def lookup_itunes(tags):
    """Fill what Apple's catalogue knows. Returns a cover URL, or "".

    Apple answers a plain search, so unlike an ISRC there is nothing to
    guarantee the top hit is the same recording. The candidates are scored
    the same way search results are, and a weak best is treated as no
    answer: a wrong album and a wrong year are worse than neither.
    """
    title, artist = tags.get("title", ""), tags.get("artist", "")
    if not title:
        return ""
    term = f"{artist} {title}".strip()
    try:
        response = requests.get(
            _ITUNES_URL,
            params={"term": term, "entity": "song", "limit": 5},
            headers={"User-Agent": _USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
    except Exception:  # noqa: BLE001 - a lookup never fails a download
        return ""

    best, best_score = None, 0.0
    for entry in results:
        score = music_match.score_music(
            term, _text(entry.get("trackName")),
            _text(entry.get("artistName")),
            _text(entry.get("collectionName")))
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < _ITUNES_MIN_SCORE:
        return ""

    _fill(tags, "album", best.get("collectionName"))
    _fill(tags, "albumartist", best.get("collectionArtistName")
          or best.get("artistName"))
    _fill(tags, "genre", best.get("primaryGenreName"))
    _fill(tags, "date", _text(best.get("releaseDate"))[:10])
    _fill(tags, "tracknumber", _number(best.get("trackNumber")))
    _fill(tags, "tracktotal", _number(best.get("trackCount")))
    _fill(tags, "discnumber", _number(best.get("discNumber")))
    _fill(tags, "disctotal", _number(best.get("discCount")))
    # Apple hands back a 100-pixel thumbnail and serves any size from the
    # same path; the same swap is how the Apple Music backend gets sleeve
    # art worth embedding.
    artwork = _text(best.get("artworkUrl100"))
    return artwork.replace("100x100bb", "600x600bb")


def fetch_lyrics(title, artist, album="", duration_s=0):
    """Synced lyrics for one track from LRCLIB, or "".

    The duration is sent because LRCLIB matches on it: two recordings of the
    same song under the same name are told apart by how long they run, and
    without it a studio track can come back with a live version's timings.
    """
    if not title:
        return ""
    try:
        response = requests.get(
            _LRCLIB_URL,
            params={"track_name": title, "artist_name": artist,
                    "album_name": album, "duration": int(duration_s or 0)},
            headers={"User-Agent": _USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
        )
        if response.status_code != 200:
            return ""
        body = response.json()
    except Exception:  # noqa: BLE001 - a lookup never fails a download
        return ""
    return _text(body.get("syncedLyrics") or body.get("plainLyrics"))


def _audiodb_get(path, params):
    """One TheAudioDB call. None on any failure."""
    try:
        response = requests.get(
            f"{_AUDIODB_ROOT}/{_AUDIODB_KEY}/{path}",
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001 - a lookup never fails a download
        return None


def lookup_theaudiodb(tags):
    """Fill genre, year and album artist from TheAudioDB. Returns a cover URL.

    Asked last and only for what is still missing. TheAudioDB is edited by
    hand and its album entries are where its strength is, which is exactly
    where MusicBrainz's ISRC answer is thinnest: a recording lookup rarely
    carries a genre and never carries album artwork.
    """
    artist = tags.get("albumartist") or tags.get("artist", "")
    album, title = tags.get("album", ""), tags.get("title", "")
    if not artist:
        return ""

    if album:
        body = _audiodb_get("searchalbum.php", {"s": artist, "a": album})
        entries = (body or {}).get("album") or []
        if entries:
            entry = entries[0]
            _fill(tags, "genre", entry.get("strGenre"))
            _fill(tags, "date", entry.get("intYearReleased"))
            _fill(tags, "albumartist", entry.get("strArtist"))
            _fill(tags, "label", entry.get("strLabel"))
            _fill(tags, "musicbrainz_albumid", entry.get("strMusicBrainzID"))
            return _text(entry.get("strAlbumThumb"))

    if title:
        body = _audiodb_get("searchtrack.php", {"s": artist, "t": title})
        entries = (body or {}).get("track") or []
        if entries:
            entry = entries[0]
            _fill(tags, "genre", entry.get("strGenre"))
            _fill(tags, "album", entry.get("strAlbum"))
            _fill(tags, "tracknumber", _number(entry.get("intTrackNumber")))
            return _text(entry.get("strTrackThumb"))
    return ""


def fetch_cover(url):
    """The artwork bytes and their MIME type, or (None, "")."""
    if not url.startswith("http"):
        return None, ""
    try:
        response = requests.get(
            url, headers={"User-Agent": _USER_AGENT},
            timeout=HTTP_TIMEOUT_S, stream=True)
        response.raise_for_status()
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_COVER_BYTES:
            return None, ""
        data = b""
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data += chunk
            if len(data) > MAX_COVER_BYTES:
                return None, ""
    except Exception:  # noqa: BLE001 - artwork is never worth a failure
        return None, ""
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    return None, ""


def _year(date):
    """The four-digit year inside a date, for the formats that want only it."""
    match = _YEAR_RE.search(date or "")
    return match.group(1) if match else ""


def write_tags(path, tags, cover=None, cover_mime="image/jpeg"):
    """Write *tags* to the audio file at *path*, keeping what it already has.

    Returns the number of fields written. An existing non-empty tag is left
    alone: some sources hand back a properly tagged file already, and what
    that file says about itself beats what a catalogue search inferred.
    """
    import mutagen

    audio = mutagen.File(path)
    if audio is None:
        return 0
    kind = audio.__class__.__name__
    if kind == "MP3":
        return _write_id3(path, tags, cover, cover_mime)
    if kind == "MP4":
        return _write_mp4(audio, tags, cover, cover_mime)
    if kind in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC"):
        return _write_vorbis(audio, tags, cover, cover_mime)
    return 0


def _write_vorbis(audio, tags, cover, cover_mime):
    from mutagen.flac import Picture

    if audio.tags is None:
        audio.add_tags()
    written = 0
    for field in TAG_FIELDS:
        value = tags.get(field, "")
        key = field.upper()
        if not value or audio.tags.get(key):
            continue
        audio.tags[key] = [value]
        written += 1
    # Vorbis spells the release year on its own as well, and some players
    # read only that.
    year = _year(tags.get("date", ""))
    if year and not audio.tags.get("YEAR"):
        audio.tags["YEAR"] = [year]
        written += 1
    lyrics = tags.get("lyrics", "")
    if lyrics and not audio.tags.get("LYRICS"):
        audio.tags["LYRICS"] = [lyrics]
        written += 1
    if cover and not getattr(audio, "pictures", None):
        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime
        picture.desc = "Cover"
        picture.data = cover
        if hasattr(audio, "add_picture"):
            audio.add_picture(picture)
            written += 1
    audio.save()
    return written


_ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB",
    "albumartist": "TPE2", "date": "TDRC", "genre": "TCON",
    "isrc": "TSRC", "label": "TPUB", "composer": "TCOM",
    "copyright": "TCOP",
}
# ID3 has no frame of its own for these, so they travel as the TXXX
# descriptions Picard writes and every library that reads Picard's files
# understands.
_ID3_TXXX = {
    "musicbrainz_trackid": "MusicBrainz Release Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
}


def _write_id3(path, tags, cover, cover_mime):
    from mutagen.id3 import APIC, ID3, TXXX, USLT, Frames

    try:
        id3 = ID3(path)
    except Exception:  # noqa: BLE001 - no ID3 header on the file yet
        id3 = ID3()
    written = 0
    for field, frame_id in _ID3_FRAMES.items():
        value = tags.get(field, "")
        if not value or id3.getall(frame_id):
            continue
        id3.add(Frames[frame_id](encoding=3, text=[value]))
        written += 1
    # Track and disc numbers share a frame each, written as "number/total".
    for frame_id, number, total in (
            ("TRCK", tags.get("tracknumber", ""), tags.get("tracktotal", "")),
            ("TPOS", tags.get("discnumber", ""), tags.get("disctotal", ""))):
        if not number or id3.getall(frame_id):
            continue
        id3.add(Frames[frame_id](
            encoding=3, text=[f"{number}/{total}" if total else number]))
        written += 1
    existing = {frame.desc for frame in id3.getall("TXXX")}
    for field, description in _ID3_TXXX.items():
        value = tags.get(field, "")
        if not value or description in existing:
            continue
        id3.add(TXXX(encoding=3, desc=description, text=[value]))
        written += 1
    lyrics = tags.get("lyrics", "")
    if lyrics and not id3.getall("USLT"):
        id3.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        written += 1
    if cover and not id3.getall("APIC"):
        id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover",
                     data=cover))
        written += 1
    id3.save(path, v2_version=3)
    return written


_MP4_ATOMS = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
    "albumartist": "aART", "date": "\xa9day", "genre": "\xa9gen",
    "composer": "\xa9wrt", "copyright": "cprt",
}


def _write_mp4(audio, tags, cover, cover_mime):
    from mutagen.mp4 import MP4Cover, MP4FreeForm

    if audio.tags is None:
        audio.add_tags()
    written = 0
    for field, atom in _MP4_ATOMS.items():
        value = tags.get(field, "")
        if not value or audio.tags.get(atom):
            continue
        audio.tags[atom] = [value]
        written += 1
    for atom, number, total in (
            ("trkn", tags.get("tracknumber", ""), tags.get("tracktotal", "")),
            ("disk", tags.get("discnumber", ""), tags.get("disctotal", ""))):
        if not number or audio.tags.get(atom):
            continue
        audio.tags[atom] = [(int(number), int(total) if total else 0)]
        written += 1
    for field in ("isrc", "label", "musicbrainz_trackid",
                  "musicbrainz_albumid", "musicbrainz_artistid",
                  "musicbrainz_releasegroupid"):
        value = tags.get(field, "")
        atom = f"----:com.apple.iTunes:{field}"
        if not value or audio.tags.get(atom):
            continue
        audio.tags[atom] = [MP4FreeForm(value.encode("utf-8"))]
        written += 1
    lyrics = tags.get("lyrics", "")
    if lyrics and not audio.tags.get("\xa9lyr"):
        audio.tags["\xa9lyr"] = [lyrics]
        written += 1
    if cover and not audio.tags.get("covr"):
        image_format = (MP4Cover.FORMAT_PNG if cover_mime == "image/png"
                        else MP4Cover.FORMAT_JPEG)
        audio.tags["covr"] = [MP4Cover(cover, imageformat=image_format)]
        written += 1
    audio.save()
    return written


def tag_download(path, song_info, online=True):
    """Tag one finished download as fully as blindDL can. Never raises.

    Called after the bytes are safely on disk, so nothing here can lose a
    download: a service that is down, an unwritable file or an audio format
    mutagen does not know all end the same way, with the file left exactly
    as it was found.

    Returns the number of tag fields written.
    """
    if not path or not os.path.isfile(path):
        return 0

    def attempt(call, default=""):
        """Run one lookup. A service that is down contributes nothing."""
        try:
            return call()
        except Exception:  # noqa: BLE001 - gap filling, not a requirement
            return default

    try:
        tags, cover_url = tags_from_song_info(song_info)
        # A site that sent its own words is believed over any lookup.
        lyrics = _text(getattr(song_info, "lyric", ""))
        if online:
            # MusicBrainz first: it is the one that knows identity, and the
            # ids it carries are what a library files by. Apple next, for
            # the record itself -- what it was called, when it came out,
            # what shelf it belongs on, what the sleeve looks like -- none
            # of which an identifier lookup returns. TheAudioDB last, for
            # whatever those two still left blank.
            attempt(lambda: lookup_musicbrainz(tags))
            # Each is asked on its own account and its artwork taken only
            # if nothing better is already in hand. Chaining them behind the
            # cover instead would mean a site that sent a sleeve -- which
            # Qobuz always does -- silently skipped both lookups and kept
            # the blank genre and album artist they exist to fill.
            itunes_cover = ""
            if _missing(tags, _ITUNES_FILLS) or not cover_url:
                itunes_cover = attempt(lambda: lookup_itunes(tags))
            audiodb_cover = ""
            if _missing(tags, _AUDIODB_FILLS) or not (cover_url
                                                      or itunes_cover):
                audiodb_cover = attempt(lambda: lookup_theaudiodb(tags))
            cover_url = cover_url or itunes_cover or audiodb_cover
            if not lyrics:
                lyrics = attempt(lambda: fetch_lyrics(
                    tags.get("title", ""), tags.get("artist", ""),
                    tags.get("album", ""),
                    getattr(song_info, "duration_s", 0) or 0))
        if lyrics:
            tags["lyrics"] = lyrics
        cover, cover_mime = fetch_cover(cover_url) if cover_url else (None, "")
        return write_tags(path, tags, cover, cover_mime or "image/jpeg")
    except Exception:  # noqa: BLE001 - a tag is never worth a lost download
        return 0
