# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Audio description: finding the one that exists, or making one.

Description is a voice saying what happens on screen during the gaps in
the talking. Three things can provide it, in descending order of how much
they can be trusted: a described track the video itself carries, a
described version somebody published as its own video, and -- only when
neither exists -- one made here, by reading the picture at each cut.

The last of those is invented. Every file it writes says so at the top,
because a confident wrong description is worse than an honest gap.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

# ffmpeg's scene score at which two frames count as different pictures.
# High enough to ignore a camera panning, low enough to catch a hard cut.
SCENE_THRESHOLD = 0.4

# How many frames are read in one request, and the most shots described
# from one video. Order and a batch together are what let the description
# carry on from shot to shot instead of listing disconnected stills.
BATCH_SIZE = 6
MAX_SHOTS = 240

# The frame is scaled to this width before it is read: enough to see what
# is happening, small enough that a long video is not hundreds of megabytes
# of screenshots.
FRAME_WIDTH = 512

# A single command's own limit. Describing a long video runs several of
# these, so this bounds one stuck call rather than the whole job.
RUN_TIMEOUT_S = 600

# A screen reader's middling speed, for working out how long a pause has
# to be. There is no way to ask a screen reader when it has finished, so
# the player estimates instead of guessing exactly.
WORDS_PER_SECOND = 2.5
MINIMUM_READING_S = 2.0

# What a described version tends to call itself. The word is spelled both
# ways -- "described" and "description" -- and only the stem is common to
# both, so that is what is matched. "description" on its own is far too
# common in ordinary titles to be worth matching on.
_DESCRIBED_WORD = re.compile(r"descri(?:b|pt)", re.I)
_DESCRIBED_TITLE = re.compile(
    r"(?:audio[\s-]?descri(?:b|pt)"
    r"|descri(?:b|pt)\w*\s+(?:version|track|audio)"
    r"|\[ad\]|\(ad\))",
    re.I)

# Words that say nothing about which video this is. The first set is
# grammar; the second is the packaging every upload wears -- "official
# trailer", "full video", "live stream" -- which two unrelated videos can
# share without being about the same thing at all. Without both, a search
# for one trailer offers every other trailer that says it is described.
_STOP_WORDS = frozenset(
    "the a an and or of to in on for with at by from is are was were this "
    "that its it as you your we our".split())
_COMMON_WORDS = frozenset(
    "official trailer trailers video videos clip clips full part parts "
    "movie movies film films episode episodes series season live hd hq "
    "new best complete watch audio described description version track "
    "subtitle subtitles final extended".split())

_TITLE_WORD = re.compile(r"[a-z0-9']+")
_TIMESTAMP_LINE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[|\-–—:]\s*(.+?)\s*$")
_SHOWINFO_TIME = re.compile(r"pts_time:([0-9.]+)")


def _clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def looks_described(*values):
    """Whether any of these strings says it is an audio description."""
    return any(_DESCRIBED_WORD.search(str(value or "")) for value in values)


def described_tracks(formats):
    """The described audio streams in a media's own format list.

    A described track is an ordinary audio stream carrying a label that
    says so -- "descriptive", "English descriptive" -- because nothing
    about the stream itself gives it away. Reading the labels is what
    finds it, which is also why this takes the whole format dicts rather
    than their ids.
    """
    found = []
    for fmt in formats or ():
        if not isinstance(fmt, dict):
            continue
        if str(fmt.get("vcodec") or "none") not in ("none", ""):
            continue
        if looks_described(fmt.get("format_note"), fmt.get("format"),
                           fmt.get("language"), fmt.get("audio_track")):
            found.append(fmt)
    return found


def title_words(title):
    """The words of a title worth matching another title against."""
    return {
        word for word in _TITLE_WORD.findall(str(title or "").lower())
        if len(word) > 2
        and word not in _STOP_WORDS
        and word not in _COMMON_WORDS
    }


def candidate_versions(title, results, limit=8):
    """Which search results are the described version of *title*.

    Described versions are published as their own videos rather than as
    extra tracks, so a search is the only way to find them -- and a search
    for a title turns up plenty that is not one. A candidate has to say it
    is described and to share a real word with the title, and the ones
    sharing most of it come first.
    """
    wanted = title_words(title)
    found = []
    for item in results or ():
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("title") or "")
        if not _DESCRIBED_TITLE.search(candidate):
            continue
        shared = wanted & title_words(candidate)
        if not shared:
            continue
        found.append({
            "title": candidate,
            "url": str(item.get("url") or ""),
            "uploader": str(item.get("uploader") or ""),
            "duration": item.get("duration"),
            "shared": len(shared),
        })
    found.sort(key=lambda item: -item["shared"])
    return found[:limit]


def described_versions(title, search=None, limit=8):
    """Search for the described versions of *title* that were published.

    A search that cannot be made raises, because "nobody published one"
    and "the search could not be made" are different answers and only one
    of them means the user should stop looking.
    """
    if search is None:
        from . import ytdlp_backend

        def search(query):
            return ytdlp_backend.search(query, count=60)

    return candidate_versions(
        title, search(f"{title} audio described"), limit=limit)


def described_track(url, config=None, info=None):
    """The described track on *url* itself, or None.

    This is a person's description where it exists at all, which is why it
    is looked for before anything else: a described version of the same
    video made by somebody else is second best to the one the publisher
    wrote.
    """
    if info is None:
        from . import ytdlp_backend

        config = config or {}
        info = ytdlp_backend.inspect_media(
            url,
            cookies_from_browser=config.get("cookies_from_browser"),
            cookies_file=config.get("cookies_file"),
        )
    formats = list(info.get("formats") or [])
    formats.extend(info.get("requested_downloads") or [])
    tracks = described_tracks(formats)
    if tracks:
        return tracks[0]
    if looks_described(info.get("format_note"), info.get("format"),
                       info.get("description_track")):
        return info
    return None


def description_files(media_path):
    """Where the two halves of a description live: text and timings."""
    base = os.path.splitext(str(media_path))[0]
    return base + ".desc.txt", base + ".desc.json"


def load_description(media_path):
    """The timed description beside *media_path*, as [(seconds, text)]."""
    _readable, timed = description_files(media_path)
    try:
        with open(timed, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    moments = []
    for entry in data.get("moments") or ():
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if text:
            moments.append((max(0.0, at), text))
    moments.sort(key=lambda moment: moment[0])
    return moments


def write_description(media_path, moments, source="machine"):
    """Write the readable text and the timed file; return the text's path.

    The text file is what a person reads, so it says at the top who wrote
    it. The timed file is what the player reads out at the right moment,
    and carries the same fact in its ``source``.
    """
    moments = [(max(0.0, float(at)), str(text).strip())
               for at, text in moments if str(text).strip()]
    text_path, timed = description_files(media_path)
    lines = []
    if source == "machine":
        lines.append(
            "Audio description written by a machine, not checked by "
            "anyone.\nIt is a reading of the picture, not a substitute for "
            "description written by a person.\n")
    lines.extend(f"{_clock(at)}  {text}" for at, text in moments)
    try:
        with open(text_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        with open(timed, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({
                "source": source,
                "moments": [{"at": round(at, 2), "text": text}
                            for at, text in moments],
            }, handle, indent=2)
    except OSError as exc:
        raise RuntimeError(f"Could not save the description: {exc}") from exc
    return text_path


def reading_seconds(text):
    """How long *text* takes to read out, near enough.

    A screen reader runs at its own rate and cannot be asked when it has
    finished, so the pause that holds the video is an estimate. Two and a
    half words a second is a middling screen reader at its usual setting.
    """
    words = len(str(text or "").split())
    return max(MINIMUM_READING_S, words / WORDS_PER_SECOND)


def shot_times(media_path, run=None, limit=MAX_SHOTS):
    """The seconds at which the picture changes, cut by cut.

    Frames are taken at the cuts rather than on a clock: a fixed interval
    either misses a cut entirely or describes the same shot five times,
    and a description of the same room five times is worse than none.
    """
    runner = run or _run
    output = runner([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(media_path),
        "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-f", "null", "-",
    ])
    times = [0.0]
    for match in _SHOWINFO_TIME.finditer(output or ""):
        at = float(match.group(1))
        # Half a second apart is the same cut seen twice, not two shots.
        if at - times[-1] >= 0.5 and len(times) < limit:
            times.append(at)
    return times


def extract_frame(media_path, at, target, run=None):
    """Write one frame of *media_path* at *at* seconds to *target*."""
    (run or _run)([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{float(at):.3f}", "-i", str(media_path),
        "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-1", "-q:v", "4",
        str(target),
    ])
    return target


_PROMPT = (
    "You are writing audio description for a video: a voice that says what "
    "is happening on screen, for someone who cannot see it. Each line below "
    "gives the moment in the video and the image file taken at that moment. "
    "Read the images in order, then answer with one line per image, in the "
    "form 'MM:SS | what is happening', describing only what you can see and "
    "carrying on from the picture before it (\"he turns to the camera\", "
    "\"the same room, now empty\"). Write nothing for an image with nothing "
    "worth saying, and no introduction, summary or commentary.\n\n"
)


def description_prompt(batch):
    """The request for one batch, as (seconds, frame path) pairs in order."""
    lines = [_PROMPT]
    lines.extend(f"{_clock(at)} - {path}\n" for at, path in batch)
    return "".join(lines)


def parse_description(text):
    """The moments in an answer, as [(seconds, words)]."""
    moments = []
    for line in str(text or "").splitlines():
        match = _TIMESTAMP_LINE.match(line)
        if match is None:
            continue
        minutes, seconds, words = match.groups()
        words = words.strip()
        if words:
            moments.append((int(minutes) * 60 + int(seconds), words))
    return moments


def make_description(media_path, log=None, run=None, ask=None):
    """Describe *media_path* from its own pictures; return the text's path.

    Claude reads the frames -- the shots, in order, a few at a time -- and
    answers with what each one shows. Everything it writes is labelled
    machine-made, because it is a reading rather than a description
    somebody wrote knowing what the video was about.
    """
    say = log or (lambda _line: None)
    runner = run or _run
    asker = ask or ask_claude
    times = shot_times(media_path, run=runner)
    if len(times) < 1:
        raise RuntimeError("That file had no picture to describe.")
    say(f"Looking at {len(times)} shots...")
    moments = []
    with tempfile.TemporaryDirectory(prefix="blinddl-describe-") as folder:
        for start in range(0, len(times), BATCH_SIZE):
            batch = []
            for at in times[start:start + BATCH_SIZE]:
                target = os.path.join(folder, f"shot-{at:09.2f}.jpg")
                extract_frame(media_path, at, target, run=runner)
                batch.append((at, target))
            say(f"Describing shots {start + 1} to {start + len(batch)}...")
            moments.extend(parse_description(asker(description_prompt(batch))))
    if not moments:
        raise RuntimeError(
            "Nothing came back from the description. Check that the Claude "
            "command line tool is installed and signed in.")
    return write_description(media_path, moments, source="machine")


def ask_claude(prompt, run=None):
    """Ask the Claude command line to read the frames and answer."""
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError(
            "Making a description needs the Claude command line tool, "
            "which was not found. Finding one that somebody else published "
            "still works.")
    return (run or _run)(
        [executable, "-p", prompt, "--allowedTools", "Read"])


def _run(command):
    """Run one command and return what it printed, or raise what it said."""
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=RUN_TIMEOUT_S)
    except OSError as exc:
        raise RuntimeError(f"Could not run {command[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{command[0]} was still running after "
            f"{RUN_TIMEOUT_S // 60} minutes and was stopped.") from exc
    if result.returncode != 0:
        said = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{command[0]} failed: {said[-1] if said else result.returncode}")
    return result.stdout or ""
