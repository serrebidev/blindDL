# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Readable text transcripts built from a site's caption files.

A caption file is written for a player: short timed cues, tags for colour
and position, and -- on the automatic captions -- each line repeated as it
scrolls into place. A transcript is for reading, searching and a Braille
display, so the timings come out, every line is kept exactly once, and a
pause in the speech starts a new paragraph instead of a new line.
"""

import glob
import html
import os
import re

# "00:01:02.500 --> 00:01:04.000", and the SRT spelling of the same thing
# with a comma for the decimal point. Both the WebVTT and the SubRip form
# of a timestamp parse here, so one reader takes either caption file.
_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
# Inline tags: <c>, </c>, <00:00:09.500> (the automatic captions time every
# word that way), and the position tags a player would honour.
_TAGS = re.compile(r"<[^>]*>")

# How long a gap between two cue lines becomes a paragraph break. Short
# enough to keep one sentence per paragraph in ordinary speech, long enough
# that a breath mid-sentence does not start one.
PARAGRAPH_GAP_S = 1.5

# What a line has to end with before a pause after it can start a
# paragraph. A pause in the middle of a sentence is a breath, not a break,
# and a transcript that breaks there is harder to read than one that does
# not.
_SENTENCE_END = (".", "?", "!", "…")

# Caption files yt-dlp can be asked for and this module reads.
CAPTION_EXTENSIONS = (".vtt", ".srt")
# The other shapes a site may offer captions in. One request writes one of
# these per language when a site has no vtt or srt, and none of them is
# worth keeping beside a download nobody asked for a caption file with.
OTHER_CAPTION_EXTENSIONS = (".ttml", ".dfxp", ".json3", ".srv1", ".srv2",
                            ".srv3", ".xml", ".ass", ".ssa", ".lrc")


def _timestamp(text):
    """Seconds for the first timestamp in *text*, or None."""
    match = _TIME.search(text or "")
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    value = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    if fraction:
        value += int(fraction.ljust(3, "0")) / 1000.0
    return value


def _line_text(raw):
    """One caption line with its tags, entities and spacing taken out."""
    return " ".join(html.unescape(_TAGS.sub("", raw)).split())


def cues(text):
    """Every cue in a caption file, as (start_seconds, end_seconds, lines).

    Anything that is not a cue is skipped, so a WEBVTT header, a NOTE or
    STYLE block, and the running numbers a SubRip file puts above each
    timestamp all fall away without being mistaken for speech.
    """
    found = []
    current = None
    for raw in str(text or "").splitlines():
        if not raw.strip():
            # A blank line ends the cue. Without this the number above the
            # next cue's timestamp would be read as another caption line.
            current = None
            continue
        if "-->" in raw:
            start_text, _arrow, end_text = raw.partition("-->")
            start = _timestamp(start_text)
            if start is None:
                current = None
                continue
            end = _timestamp(end_text)
            current = (start, start if end is None else max(end, start), [])
            found.append(current)
            continue
        if current is not None:
            line = _line_text(raw)
            if line:
                current[2].append(line)
    return found


def lines(cue_list):
    """Every caption line once, as (start_seconds, end_seconds, line).

    Rolling captions redraw the whole box on each cue, so a line repeats
    the one printed before it as often as not. Only a line that repeats the
    one immediately before it goes, which is what a redraw produces; the
    same words spoken again later are a new line and are kept.
    """
    result = []
    previous = None
    for start, end, cue_lines in cue_list:
        for line in cue_lines:
            if line != previous:
                result.append((start, end, line))
            previous = line
    return result


def transcript(text, gap=PARAGRAPH_GAP_S):
    """A caption file as readable paragraphs, or "" when it holds nothing.

    Paragraphs are not lines: a caption line is a fragment of a sentence,
    and reading one per line turns a paragraph of speech into a list. A
    pause longer than *gap* seconds, at a point where a sentence ended, is
    what starts a new one.
    """
    paragraphs = []
    current = []
    previous_end = None
    for start, end, line in lines(cues(text)):
        if (current and previous_end is not None
                and start - previous_end > gap
                and current[-1].rstrip().endswith(_SENTENCE_END)):
            paragraphs.append(" ".join(current))
            current = []
        current.append(line)
        previous_end = end
    if current:
        paragraphs.append(" ".join(current))
    if not paragraphs:
        return ""
    return "\n\n".join(paragraphs) + "\n"


def caption_files(media_path, extensions=CAPTION_EXTENSIONS):
    """English caption files written beside *media_path*, if any.

    yt-dlp names them after the media's own name with the language between
    ("Show [dQw4w9].en.vtt"), so the download and the captions of the same
    media find each other without anything being written down.
    """
    base = os.path.splitext(str(media_path))[0]
    stem = os.path.basename(base)
    found = []
    for extension in extensions:
        # A title carries "[" and "]", which glob would read as a pattern.
        for path in sorted(glob.glob(glob.escape(base) + ".*" + extension)):
            parts = [
                part for part in os.path.basename(path)[len(stem):].split(".")
                if part
            ]
            # "en", and the spellings "en-US", "en-GB" and "en-orig" with it.
            if len(parts) > 1 and parts[0].lower().split("-")[0] == "en":
                found.append(path)
    return found


def write_transcript(media_path):
    """Save *media_path*'s captions as a .txt beside it; return its path.

    The caption file goes once its words are safely in the transcript, so
    the download folder keeps only the media and its text. A caption file
    that reads back empty is left where it is: an empty transcript is not a
    reason to throw away the only copy of the words.
    """
    captions = caption_files(media_path)
    if not captions:
        return ""
    text = ""
    for caption in captions:
        try:
            with open(caption, encoding="utf-8", errors="replace") as handle:
                candidate = transcript(handle.read())
        except OSError:
            continue
        if len(candidate) > len(text):
            text = candidate
    if not text.strip():
        return ""
    target = os.path.splitext(str(media_path))[0] + ".txt"
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        return ""
    # Everything the one request left behind goes, including any second
    # language or a shape this reader cannot take: the words are in the
    # transcript now, and a stray caption file beside a download is nobody's.
    for caption in caption_files(
            media_path, CAPTION_EXTENSIONS + OTHER_CAPTION_EXTENSIONS):
        try:
            os.remove(caption)
        except OSError:
            pass
    return target
