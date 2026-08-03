# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Side B backend: Deezer downloads with proper tags and synced lyrics.

sideb (https://github.com/mosaddiqdev/sideb) builds a finished music file
where yt-dlp and musicdl stop: metadata from Deezer, audio from YouTube
Music, tags and cover art applied, and synced lyrics embedded (LRCLIB, plus
word-level Deezer lyrics when an ARL cookie is configured). blindDL routes
deezer.com URLs here and offers it as an extra search source; everything
else stays with yt-dlp/musicdl. If a Deezer URL fails here, callers fall
back to yt-dlp so every engine that could handle a link gets a turn.

sideb is an asyncio library. Every public function here is synchronous and
spins up its own event loop, so they are safe to call from blindDL's worker
threads; sideb's objects are created fresh per call and closed again,
because its HTTP clients are bound to the loop that made them.

Imports of sideb itself are deferred into the functions so the app still
starts when sideb is not installed -- the job then fails with a clear
"No module named 'sideb'" error instead of taking blindDL down with it.
"""

import asyncio
import os
import re
import shutil

from .config import app_data_dir

SIDEB_SOURCE = "Deezer (Side B)"

_DEEZER_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(?:track|album|playlist|artist)/\d+",
    re.IGNORECASE,
)
_DEEZER_SHORTLINK_RE = re.compile(
    r"(?:deezer|dzr)\.page\.link/|link\.deezer\.com/", re.IGNORECASE)

_home_ready = False


class _YtdlpShim:
    """Strips sideb's bot-wall-triggering yt-dlp options.

    sideb hardcodes extractor_args youtube:player_skip=[webpage, configs,
    initial_data] to save time, but without the webpage player yt-dlp
    cannot run its Deno-based JS challenge solver, and YouTube answers
    "Sign in to confirm you're not a bot" on networks like this one.
    Installed only in sideb's audio module namespace, so blindDL's own
    yt-dlp backend is untouched.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def YoutubeDL(self, opts):
        opts = dict(opts)
        player_skip = (opts.get("extractor_args", {})
                       .get("youtube", {}).get("player_skip"))
        if player_skip:
            del opts["extractor_args"]
        return self._real.YoutubeDL(opts)


def _patch_sideb_ytdlp():
    from sideb.providers.audio import youtube as yt_audio

    if not isinstance(yt_audio.yt_dlp, _YtdlpShim):
        yt_audio.yt_dlp = _YtdlpShim(yt_audio.yt_dlp)


def is_deezer_url(url):
    """Anything sideb resolves through Deezer (incl. page.link shortlinks)."""
    return bool(_DEEZER_URL_RE.search(url) or _DEEZER_SHORTLINK_RE.search(url))


def _ensure_home():
    """Point the process CWD at a scratch dir under %APPDATA%/blindDL.

    sideb hardcodes its temp downloads to Path("tmp") and reads ./.env,
    ./cookies.txt and ./browser.json -- all relative to the process CWD.
    Without this its scratch would land wherever blindDL happened to be
    started from. Done once; concurrent workers all chdir to the same
    place, so the redundant calls are harmless.
    """
    global _home_ready
    if _home_ready:
        return
    home = os.path.join(app_data_dir(), "sideb-home")
    os.makedirs(home, exist_ok=True)
    # Leftovers from an interrupted run are scratch, not music.
    shutil.rmtree(os.path.join(home, "tmp"), ignore_errors=True)
    os.chdir(home)
    _patch_sideb_ytdlp()
    _home_ready = True


def _settings(config, out_dir=None):
    from pathlib import Path

    from sideb.config.settings import Settings

    return Settings(
        output_dir=Path(out_dir or config["download_dir"]),
        # sideb keeps YouTube's native container (opus, remuxed to .ogg, or
        # m4a) and does not transcode; blindDL's other audio formats are
        # yt-dlp-only, so they map to opus here -- which is also what
        # "original" asks for.
        audio_format="m4a" if config["audio_format"] == "m4a" else "opus",
        enable_lyrics=bool(config["sideb_lyrics"]),
        deezer_arl=config["deezer_arl"] or None,
        # sideb defaults this to ./cookies.txt and would point yt-dlp at a
        # file that is not there.
        cookies_file=None,
    )


def _track_to_item(track):
    return {
        "id": f"sideb:{track.id}",
        "kind": "sideb",
        "title": track.title or "Unknown title",
        "artist": track.artist.name if track.artist else "",
        "album": track.album.title if track.album else "",
        "source": SIDEB_SOURCE,
        "duration_s": track.duration,
        "url": f"https://www.deezer.com/track/{track.id}",
    }


def extract_flat(url, config):
    """Resolve a Deezer track/album/playlist/artist URL to (items, title).

    Same contract as ytdlp_backend.extract_flat so the URL panel and
    subscriptions can treat both backends alike. An artist URL resolves to
    the whole discography, matching sideb's own behaviour.
    """
    _ensure_home()
    from sideb.app.main import Application

    async def _run():
        app = Application(_settings(config))
        try:
            return await app.collect(url)
        finally:
            await app.aclose()

    ctx, tracks = asyncio.run(_run())
    if not tracks:
        raise RuntimeError(f"Side B found no tracks at: {url}")
    title = ctx.source_name or url
    return [_track_to_item(t) for t in tracks], title


def search(query, config):
    """Search Deezer's catalog; returns normalized items (kind "sideb")."""
    _ensure_home()
    from sideb.providers.metadata.deezer import DeezerMetadata

    async def _run():
        provider = DeezerMetadata(user_agent=_settings(config).user_agent)
        try:
            return await provider.search(query)
        finally:
            await provider.aclose()

    return [_track_to_item(t) for t in asyncio.run(_run())]


def download(url, out_dir, config, event_cb=None):
    """Download one Deezer track URL with tags, cover art and lyrics.

    event_cb receives sideb pipeline events (WorkerStage, TrackCompleted,
    TrackFailed...) from the download thread; sideb exposes no per-track
    percent. Cancellation is not supported -- like musicdl, the call either
    returns or raises.
    """
    _ensure_home()
    os.makedirs(out_dir, exist_ok=True)
    from sideb.app.events_bus import EventBus
    from sideb.app.main import Application

    bus = EventBus()
    if event_cb is not None:
        bus.subscribe(event_cb)

    async def _run():
        app = Application(_settings(config, out_dir), bus)
        try:
            return await app.run(url)
        finally:
            await app.aclose()

    summary = asyncio.run(_run())
    if summary.succeeded:
        return
    if summary.failed:
        raise RuntimeError(f"Side B: {summary.failed[0].error}")
    reason = (summary.skipped[0].skipped_reason
              if summary.skipped else "nothing downloaded")
    raise RuntimeError(f"Side B: {reason}")
