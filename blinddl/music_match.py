# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""How well a music result answers what was actually typed.

Every other search category in blindDL ranks and filters what its sites
answer with. Music did not: it concatenated whatever came back in
alphabetical order of site name, so a search put the best answer wherever
its site happened to fall in the alphabet, and kept rows that matched
nothing at all.

The book scorer this could have borrowed is the wrong shape for music. It
weighs the whole query against the whole title, which suits "moby dick
melville" naming a book, but a music search is usually an artist, or a song,
or both -- and the artist is a separate field. Scored that way a long song
title by exactly the right artist lands *below* a short unrelated one,
which is the opposite of useful.

So each word of the query is matched against the best word available in
either field, and a word only counts once it is genuinely close. That
tolerates the ordinary way people type an artist's name: a search for
"naomi streamer" finds Naomi Striemer, because "streamer" and "striemer"
are one transposition apart, while a Chinese choir recording that shares no
word with either scores near zero and is dropped.
"""

import re
from difflib import SequenceMatcher

# Below this, a result is noise rather than a weak answer, and showing it
# costs more than leaving it out: a screen reader user pages through every
# row, so an unranked list of a hundred is worse than a ranked list of ten.
MIN_MATCH_SCORE = 55.0
# How close two words must be before they count as the same word at all.
# "striemer"/"streamer" is 0.88 and is meant to count; "dreamers"/"streamer"
# is 0.75 and is not the same word, it just looks like one.
_TOKEN_FLOOR = 0.82
# A word this short has to match exactly. Fuzzy matching on two or three
# letters makes everything look like everything.
_SHORT_TOKEN = 3

_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _tokens(text):
    """The words of *text*, punctuation dropped and case folded."""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(part) for part in text if part)
    if not text:
        return []
    return [word for word in _WORD_RE.sub(" ", str(text).casefold()).split()
            if word]


def _best_token_ratio(word, candidates):
    """How well *word* is answered by the closest of *candidates*, 0.0-1.0."""
    best = 0.0
    for candidate in candidates:
        if candidate == word:
            return 1.0
        # A short word that is not an exact match is not a match. Nor is one
        # whose length is nowhere near, which is also the cheap way to skip
        # most comparisons before doing the expensive one.
        if len(word) <= _SHORT_TOKEN or len(candidate) <= _SHORT_TOKEN:
            continue
        if abs(len(candidate) - len(word)) > 3:
            continue
        ratio = SequenceMatcher(None, word, candidate).ratio()
        if ratio > best:
            best = ratio
    return best if best >= _TOKEN_FLOOR else 0.0


def score_music(query, title, artist="", album=""):
    """How well one music result answers *query*, 0-100.

    Every word of the query is looked for across the title, the artist and
    the album, and scores as well as the closest word it finds. The result
    is the share of the query that was actually answered, so a search for an
    artist ranks all of that artist's songs together whatever they are
    called, and a search for a song ranks it above another song by someone
    whose name merely looks similar.
    """
    query_words = _tokens(query)
    if not query_words:
        return 0.0
    fields = _tokens(title) + _tokens(artist) + _tokens(album)
    if not fields:
        return 0.0
    matched = sum(_best_token_ratio(word, fields) for word in query_words)
    score = 100.0 * matched / len(query_words)
    # Among results that answer the query equally, the one that says it in
    # fewer words is the more likely to be the thing itself rather than a
    # remix, a medley or a compilation that mentions it in passing.
    if score >= MIN_MATCH_SCORE and len(fields) > len(query_words):
        score -= min(5.0, 0.1 * (len(fields) - len(query_words)))
    return round(score, 2)


def rank_music(items, query, floor=MIN_MATCH_SCORE, allow_empty=False):
    """Score *items*, drop what answers nothing, best first.

    Each item keeps its score on the row, so the results list can order the
    whole search by it once several sites have answered rather than by which
    site happened to reply first.

    With *allow_empty* the floor is final and a site that matched nothing
    contributes nothing. Without it, an empty result is replaced by the
    unfiltered rows: a search that no site could answer well should still
    show their best guesses rather than claim there is nothing, and the
    ranking has already put the closest first. One site's worth of rows
    wants the strict form, since three dozen others are still answering; a
    whole search's worth wants the forgiving one.
    """
    for index, item in enumerate(items):
        item["score"] = score_music(query, item.get("title", ""),
                                    item.get("artist", ""),
                                    item.get("album", ""))
        item.setdefault("_arrived", index)
    kept = [item for item in items if item["score"] >= floor]
    if not kept and not allow_empty:
        kept = list(items)
    return sorted(kept, key=lambda item: (-item["score"], item["_arrived"]))
