# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""What a music search is looking for, shared by the music backends.

This is a third, separate thing from the two controls next to it on the
Search page, and the three do not overlap:

* Search type (here) decides *what a match is*: any field, a track title, an
  album title, or an artist name.
* Order (``search_order``) decides *which* of the matches a site returns.
* Sort by rearranges the rows that already arrived.

Album is the one type that changes the shape of the answer rather than the
matching: its rows are whole albums, and pressing Enter on one queues every
track on it. The other three all produce ordinary track rows.

Only Deezer and Apple Music can search a named field; everything else does
one text search and nothing else. A site that cannot is asked for its own
best match and said so afterwards, exactly as with the search order.
"""

import re

# Anything that is not a letter, digit or apostrophe separates two words.
# Accented letters count, so "Bjork" and "Björk" still split the same way
# even though they do not match each other.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

KIND_BEST = "best"
KIND_TRACK = "track"
KIND_ALBUM = "album"
KIND_ARTIST = "artist"
KINDS = (KIND_BEST, KIND_TRACK, KIND_ALBUM, KIND_ARTIST)

# What the choice reads as, in the order the choice lists them.
KIND_LABELS = {
    KIND_BEST: "Best match",
    KIND_TRACK: "Track title",
    KIND_ALBUM: "Album",
    KIND_ARTIST: "Artist",
}
KIND_LABEL_LIST = [KIND_LABELS[kind] for kind in KINDS]


def normalize(kind):
    """Return a known search type, falling back to best match.

    Backends take this straight from saved config and from callers that
    predate it, so an unknown or missing value has to mean "as before".
    """
    return kind if kind in KINDS else KIND_BEST


def label(kind):
    return KIND_LABELS.get(normalize(kind), KIND_LABELS[KIND_BEST])


def is_album(kind):
    """Whether *kind* asks for albums rather than individual tracks."""
    return normalize(kind) == KIND_ALBUM


def is_artist(kind):
    """Whether *kind* asks for one artist's work rather than any match."""
    return normalize(kind) == KIND_ARTIST


def words(text):
    """The searchable words of *text*, lowercased and stripped of punctuation.

    "Harder, Better, Faster, Stronger" and "harder better faster stronger"
    have to count as the same title, because a user typing a song name types
    the words and not the commas.
    """
    return [word for word in _WORD_RE.findall(str(text or "").casefold()) if word]


def matches(value, query):
    """Whether *value* contains every word of *query*.

    This is what narrows a search to one field where the site itself cannot.
    It is deliberately a containment test rather than a similarity score: a
    title search for "one more time" should keep "One More Time (Radio
    Edit)" and drop "Baby One More", and nothing here has to guess which of
    two near-misses the user meant.
    """
    wanted = words(query)
    if not wanted:
        return True
    found = set(words(value))
    return all(word in found for word in wanted)


def album_type_label(track_count):
    """The Type column for one album row: "Album", plus its track count."""
    try:
        count = int(track_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count == 1:
        return "Album, 1 track"
    if count > 1:
        return f"Album, {count} tracks"
    return "Album"
