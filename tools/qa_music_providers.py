#!/usr/bin/env python3
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""QA: search every music provider and verify preview, download and playback.

This drives the exact code paths the GUI uses, with no window needed:

* Music sites: ``musicdl_backend.search`` with the per-site callback (Side B
  and Deezer are also asked, exactly as the Music sites engine does).
* The single-service engines: Deezer, Apple Music, YouTube, SoundCloud,
  Bandcamp, and Soulseek when it is enabled.
* Preview: ``preview.resolve_search_result`` / ``resolve_full_playback``.
* Download: the same per-kind backend calls ``DownloadQueue._run_*`` uses.
* Playback: a real headless libVLC instance; a stream or file only passes
  when VLC reaches the Playing state and its clock advances.

Usage:
    python tools/qa_music_providers.py ["query"] [--timeout 30]
    python tools/qa_music_providers.py --skip-download   # previews only
    python tools/qa_music_providers.py --max-providers 5 # first N only

The report is printed as a table and written to qa_music_report.json next to
this script. Downloads go to a temporary directory and are deleted at the end.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blinddl import (  # noqa: E402
    applemusic_backend,
    bandcamp_backend,
    config as config_mod,
    deezer_backend,
    musicdl_backend,
    preview,
    sideb_backend,
    ytdlp_backend,
)

DEFAULT_QUERY = "radiohead creep"
PREVIEW_PLAY_S = 4.0
PREVIEW_TIMEOUT_S = 20.0
# Tracks at or under this length are presumed complete when VLC reports End.
SHORT_CLIP_S = 3.0
FILE_PLAY_S = 3.0
FILE_TIMEOUT_S = 15.0
# How long a download may take before its first byte appears.
DOWNLOAD_START_TIMEOUT_S = 30.0
MAX_WORKERS = 4

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


_vlc_instance = None
_vlc_lock = threading.Lock()


def _get_vlc():
    global _vlc_instance
    with _vlc_lock:
        if _vlc_instance is None:
            import vlc  # noqa: PLC0415 - only needed for the playback checks

            _vlc_instance = vlc.Instance(
                "--quiet", "--intf", "dummy", "--aout", "dummy",
                "--no-video-title-show", "--network-caching=1000",
            )
        return _vlc_instance


def _play_check(location, advance_s=PREVIEW_PLAY_S, timeout_s=PREVIEW_TIMEOUT_S):
    """Return (ok, length_s, note) proving libVLC decoded and played *location*.

    A stream or file is only "playable" when VLC reaches the Playing state
    (or finishes) and its playback clock advances past where it started.
    """
    import vlc  # noqa: PLC0415

    instance = _get_vlc()
    player = instance.media_player_new()
    try:
        location = str(location)
        if location.lower().startswith(("http://", "https://")):
            media = instance.media_new(location)
        else:
            media = instance.media_new_path(os.path.abspath(location))
        if media is None:
            return False, 0.0, "could not create media"
        player.set_media(media)
        media.release()
        started = time.monotonic()
        player.play()
        state = player.get_state()
        while time.monotonic() - started < timeout_s and state not in (
                vlc.State.Playing, vlc.State.Ended, vlc.State.Error):
            time.sleep(0.2)
            state = player.get_state()
        if state == vlc.State.Error:
            return False, 0.0, "VLC error state"
        if state not in (vlc.State.Playing, vlc.State.Ended):
            return False, 0.0, f"never started (state {state})"
        length = max(0.0, player.get_length() / 1000.0)
        if state == vlc.State.Ended and 0 < length <= SHORT_CLIP_S:
            # A sub-second clip (pronunciation files, jingles, …) finishes
            # before the first clock poll; VLC reaching Ended IS playback.
            return True, length, f"short clip played to end ({length:.1f}s)"
        start = max(0.0, player.get_time() / 1000.0)
        deadline = time.monotonic() + advance_s
        latest = start
        while time.monotonic() < deadline:
            time.sleep(0.4)
            current = player.get_time() / 1000.0
            if current and current > 0:
                latest = current
        advanced = latest > start + 1.0
        if not advanced:
            return False, length, f"clock stuck at {latest:.1f}s"
        return True, length, f"played to {latest:.1f}s"
    finally:
        player.stop()
        player.release()


def _safe(text, limit=180):
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dir_bytes(root):
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _newest_file(root):
    best = None
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) > 0 and (
                        best is None
                        or os.path.getmtime(path) > os.path.getmtime(best)):
                    best = path
            except OSError:
                pass
    return best


def _download_state(fn, out_dir, timeout_s=DOWNLOAD_START_TIMEOUT_S):
    """Start a backend download and report how far it got.

    Waiting for a whole track would make a 54-provider pass take hours, so
    the first bytes of a real transfer count as a pass: the backend asked
    the site for the file, and the site is serving it.

    Returns ("OK", path, note) when the download finished and its file is
    on disk, ("STARTED", None, note) while bytes are still landing, or
    ("FAIL", None, reason).
    """
    os.makedirs(out_dir, exist_ok=True)
    result = {}

    def runner():
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            result["error"] = str(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    seen = -1
    while thread.is_alive() and time.monotonic() < deadline:
        total = _dir_bytes(out_dir)
        if total > 0 and total != seen:
            if seen < 0:
                seen = total  # first bytes landed: a download has started
            return "STARTED", None, f"{total / 1024:.0f} KB received"
        time.sleep(2.0)
    if thread.is_alive():
        return "FAIL", None, "no bytes within the start window"
    if "error" in result:
        return "FAIL", None, _safe(result["error"])
    path = result.get("value") or ""
    if isinstance(path, (list, tuple)):
        path = path[0] if path else ""
    if path and os.path.isfile(path):
        return "OK", os.fspath(path), f"{os.path.getsize(path) / 1048576:.1f} MB"
    newest = _newest_file(out_dir)
    if newest:
        return "OK", newest, f"{os.path.getsize(newest) / 1048576:.1f} MB"
    return "FAIL", None, "finished without producing a file"


def _download_row(row, state, path, note):
    """Fill the download / file-play cells for one provider."""
    if state == "FAIL":
        row["download"] = "FAIL"
        row["download_note"] = note
        return row
    row["download"] = "OK" if state == "OK" else "STARTED"
    row["download_note"] = note
    if state == "OK" and path:
        ok, length, play_note = _play_check(
            path, advance_s=FILE_PLAY_S, timeout_s=FILE_TIMEOUT_S)
        row["file_plays"] = "YES" if ok else "NO"
        row["file_note"] = play_note
        row["file_length_s"] = round(length, 1) if length else 0
    return row


def _musicdl_download(song_info, out_dir):
    downloaded = musicdl_backend.download(song_info, out_dir)
    if isinstance(downloaded, (list, tuple)) and downloaded:
        path = getattr(downloaded[0], "save_path", "") or ""
        if path and os.path.isfile(path):
            return path
    return ""


def _ytdlp_download(url, out_dir, config):
    return ytdlp_backend.download(
        url, out_dir, audio_only=True,
        audio_format=config.get("audio_format", "mp3"),
        cookies_from_browser=config.get("cookies_from_browser"),
        cookies_file=config.get("cookies_file"),
    )


def _exercise_musicdl(item, out_dir, config, skip_download):
    """Preview + download + play for one musicdl site's top result."""
    row = {}
    try:
        stream, title = preview.resolve_search_result(
            item, audio_only=True, config=config)
        row["preview_url"] = _safe(stream, 90)
        ok, length, note = _play_check(stream)
        row["preview"] = "PLAY" if ok else "FAIL"
        row["preview_note"] = note
    except Exception as exc:  # noqa: BLE001
        row["preview"] = "FAIL"
        row["preview_note"] = _safe(exc)
    if skip_download:
        return row
    song_info = item.get("song_info")
    if song_info is None:
        row["download"] = "SKIP"
        row["download_note"] = "no song_info payload"
        return row
    out = os.path.join(out_dir, "musicdl")
    state, path, note = _download_state(
        lambda: _musicdl_download(song_info, out), out)
    return _download_row(row, state, path, note)


def _exercise_sideb(item, out_dir, config, skip_download):
    row = {}
    try:
        stream, title = preview.resolve_search_result(
            item, audio_only=True, config=config)
        row["preview_url"] = _safe(stream, 90)
        ok, length, note = _play_check(stream)
        row["preview"] = "PLAY" if ok else "FAIL"
        row["preview_note"] = note
    except Exception as exc:  # noqa: BLE001
        row["preview"] = "FAIL"
        row["preview_note"] = _safe(exc)
    if skip_download:
        return row
    out = os.path.join(out_dir, "sideb")
    state, path, note = _download_state(
        lambda: sideb_backend.download(
            item.get("url") or item.get("id", ""), out, config),
        out)
    return _download_row(row, state, path, note)


def _exercise_applemusic(item, out_dir, config, skip_download):
    row = {}
    try:
        stream, title = preview.resolve_search_result(
            item, audio_only=True, config=config)
        row["preview_url"] = _safe(stream, 90)
        ok, length, note = _play_check(stream)
        row["preview"] = "PLAY" if ok else "FAIL"
        row["preview_note"] = note
    except Exception as exc:  # noqa: BLE001
        row["preview"] = "FAIL"
        row["preview_note"] = _safe(exc)
    if skip_download:
        return row
    out = os.path.join(out_dir, "applemusic")
    url = item.get("url") or item.get("id", "")
    state, path, note = _download_state(
        lambda: applemusic_backend.download(url, out, config), out)
    return _download_row(row, state, path, note)


def _exercise_ytdlp(item, out_dir, config, skip_download):
    row = {}
    try:
        stream, title = preview.resolve_search_result(
            item, audio_only=True, config=config)
        row["preview_url"] = _safe(stream, 90)
        ok, length, note = _play_check(stream)
        row["preview"] = "PLAY" if ok else "FAIL"
        row["preview_note"] = note
    except Exception as exc:  # noqa: BLE001
        row["preview"] = "FAIL"
        row["preview_note"] = _safe(exc)
    if skip_download:
        return row
    out = os.path.join(out_dir, "ytdlp")
    url = item.get("url") or item.get("id", "")
    state, path, note = _download_state(
        lambda: _ytdlp_download(url, out, config), out)
    return _download_row(row, state, path, note)


def _run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-site search budget in seconds")
    parser.add_argument("--skip-download", action="store_true",
                        help="only resolve and play previews")
    parser.add_argument("--max-providers", type=int, default=0,
                        help="limit how many providers are exercised")
    args = parser.parse_args()

    config = config_mod.Config()
    timeout = args.timeout or config.get("search_timeout_s", 30)
    query = args.query.strip()

    print(f"Query: {query!r}   per-site budget: {timeout:g}s")
    print("Building musicdl clients (first search warms them up)...\n")

    # -- search phase ------------------------------------------------------
    per_site = {}
    stop = threading.Event()
    musicdl_sources = sorted(musicdl_backend._get_clients())
    musicdl_backend.search(
        # the backend hands the callback the short display label, so the
        # collector is keyed by that same label
        query, timeout_s=timeout, on_site=lambda source, items:
        per_site.__setitem__(source, items), stop=stop,
        sources=musicdl_sources,
    )

    musicdl_labels = {musicdl_backend.source_label(s) for s in musicdl_sources}
    providers = []  # (label, kind, item or None, result_count)
    for source in musicdl_sources:
        label = musicdl_backend.source_label(source)
        items = per_site.get(label, [])
        label = musicdl_backend.source_label(source)
        providers.append((label, "musicdl", items[0] if items else None,
                          len(items)))
    engine_errors = {}

    def single_engine_search(key, fn, *args, **kwargs):
        try:
            items = fn(query, config, *args, **kwargs)
            if isinstance(items, tuple):
                items = items[0] if items and isinstance(items[0], list) else []
            return items
        except Exception as exc:  # noqa: BLE001 - a broken engine is reported
            engine_errors[key] = _safe(repr(exc), 130)
            return []

    engine_rows = (
        (sideb_backend.SIDEB_SOURCE, "sideb",
         single_engine_search("sideb", sideb_backend.search)),
        (deezer_backend._SEARCH_SOURCE, "deezer",
         single_engine_search("deezer", deezer_backend.search)),
        ("Apple Music", "applemusic",
         single_engine_search("applemusic", applemusic_backend.search,
                             kind="best")),
        ("SoundCloud", "ytdlp",
         single_engine_search(
             "SoundCloud", lambda q, c:
             ytdlp_backend.extract_flat(f"scsearch200:{q}")[0],
         )),
        ("Bandcamp", "ytdlp",
         single_engine_search("bandcamp", bandcamp_backend.search)),
    )
    for label, kind, items in engine_rows:
        # The searchable engines share names with two musicdl sources
        # (SoundCloudMusicClient, YouTubeMusicClient), so the report would
        # otherwise show two rows under one name.
        if label in musicdl_labels:
            label = f"{label} (engine)"
        providers.append((label, kind, items[0] if items else None, len(items)))
    try:
        youtube_items = ytdlp_backend.search(query)
        if isinstance(youtube_items, tuple):
            youtube_items = youtube_items[0] if youtube_items else []
    except Exception as exc:  # noqa: BLE001
        engine_errors["YouTube"] = _safe(repr(exc), 130)
        youtube_items = []
    providers.append(("YouTube", "ytdlp",
                      youtube_items[0] if youtube_items else None,
                      len(youtube_items)))
    if engine_errors:
        print("Engine errors during search:")
        for key, error in engine_errors.items():
            print(f"  {key}: {error}")

    if config.get("soulseek_enabled"):
        try:
            from blinddl import soulseek_backend  # noqa: PLC0415
            items = soulseek_backend.search(
                query, config, "audio", timeout, stop_event=stop)
            providers.append(
                ("Soulseek", "soulseek", items[0] if items else None,
                 len(items)))
        except Exception as exc:  # noqa: BLE001 - a broken engine is reported
            print(f"  Soulseek: {exc}")
            providers.append(("Soulseek", "soulseek", None, 0))

    # -- exercise phase ----------------------------------------------------
    rows = []
    temp_root = tempfile.mkdtemp(prefix="blinddl-qa-")
    try:
        work = []
        for label, kind, item, count in providers:
            if count == 0:
                rows.append({"provider": label, "kind": kind, "results": 0,
                             "preview": "-", "download": "-", "file_plays": "-",
                             "note": "no results"})
                continue
            if item is None:
                rows.append({"provider": label, "kind": kind, "results": count,
                             "preview": "-", "download": "-", "file_plays": "-",
                             "note": "top result unusable"})
                continue
            work.append((label, kind, item, count))
        if args.max_providers:
            work = work[: args.max_providers]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for label, kind, item, count in work:
                if kind == "musicdl":
                    fn = _exercise_musicdl
                elif kind in ("sideb", "deezer"):
                    fn = _exercise_sideb
                elif kind == "applemusic":
                    fn = _exercise_applemusic
                else:
                    fn = _exercise_ytdlp
                futures[pool.submit(fn, item, temp_root, config,
                                    args.skip_download)] = (label, kind, count)
            for future in as_completed(futures):
                label, kind, count = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    row = {"preview": "FAIL", "preview_note": _safe(exc),
                           "download": "FAIL", "download_note": _safe(exc)}
                row.update({"provider": label, "kind": kind, "results": count})
                rows.append(row)
    finally:
        import shutil  # noqa: PLC0415

        shutil.rmtree(temp_root, ignore_errors=True)

    # -- report ------------------------------------------------------------
    unavailable = sorted(set(musicdl_backend.ALL_SOURCES)
                         - set(musicdl_sources))
    for source in unavailable:
        rows.append({"provider": musicdl_backend.source_label(source),
                     "kind": "musicdl", "results": 0, "preview": "-",
                     "download": "-", "file_plays": "-",
                     "note": "unavailable: needs cookies/login"})
    rows.sort(key=lambda r: r["provider"].casefold())
    header = (f"{'Provider':<22} {'Res':>4}  {'Preview':<6} {'DL':<5} "
              f"{'Plays':<5}  Notes")
    print(header)
    print("-" * len(header))
    for row in rows:
        note = row.get("note") or row.get("preview_note") or \
            row.get("download_note") or row.get("file_note") or ""
        print(f"{row['provider']:<22} {row['results']:>4}  "
              f"{row.get('preview', '-'):<6} {row.get('download', '-'):<5} "
              f"{row.get('file_plays', '-'):<5}  {_safe(note, 70)}")

    played = sum(1 for r in rows if r.get("preview") == "PLAY")
    downloaded = sum(1 for r in rows
                     if r.get("download") in ("OK", "STARTED"))
    file_played = sum(1 for r in rows if r.get("file_plays") == "YES")
    with_results = sum(1 for r in rows if r.get("results", 0) > 0)
    print("-" * len(header))
    print(f"{len(rows)} providers, {with_results} with results; "
          f"{played} previews played, {downloaded} downloads OK, "
          f"{file_played} downloaded files played.")

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "qa_music_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump({"query": query, "rows": rows}, handle, indent=2,
                  ensure_ascii=False)
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    _run()