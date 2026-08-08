"""Interactive queue & batch download workflow.

Three-phase flow:
  1. Collect — user adds artist/playlist/track URLs, fetches Deezer metadata
  2. Resolve — batch-resolve YouTube URLs for all pending tracks
  3. Download — download all non-downloaded tracks
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import questionary

from sideb.app.main import Application
from sideb.cli.artist_selector import display_artist_selection, display_youtube_confirm
from sideb.cli.renderer import ProgressRenderer
from sideb.cli.theme import SIDEB_STYLE, print_big_banner, two_line_choice
from sideb.models.events import (
    TrackCompleted,
    TrackQueued,
    WorkerFinished,
    WorkerStage,
    WorkerStarted,
)
from sideb.providers.audio.youtube import is_instrumental
from sideb.models.track import Album, Artist, Track
from sideb.services.manifest import (
    UNRESOLVED,
    NOT_FOUND,
    Manifest,
    ManifestTrack,
    SourceInfo,
    export_m3u8,
    find_manifest_dirs,
    merge_tracks,
    read_manifest,
    write_manifest,
    scan_undownloaded,
)
from sideb.utils.console import console

from sideb.utils.url_resolver import resolve_yt_channel_id

_DEEZER_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/",
    re.IGNORECASE,
)


async def _prompt_youtube_channel(app: Application, artist_name: str) -> bool:
    """Pre-search prompt: user can paste a YouTube channel URL or confirm to search.
    Returns True to continue (channel cached), False to abort."""
    raw = await questionary.text(
        "YouTube channel URL or search:",
        default=artist_name,
        style=SIDEB_STYLE,
    ).ask_async()
    if not raw:
        return False

    channel_id = resolve_yt_channel_id(raw)
    if channel_id:
        app.audio_provider._artist_channel_cache[artist_name] = channel_id
        return True

    if re.search(r"(?:youtube\.com|music\.youtube\.com)", raw):
        # URL wasn't resolvable directly — fall back to searching with the
        # known artist name rather than the extracted URL fragment.
        raw = artist_name

    console.print(f"[dim]Looking up YouTube channels for \u201c{raw}\u201d\u2026[/dim]")
    yt_channels = app.audio_provider.search_artist_channels(raw, limit=5)
    if not yt_channels:
        console.print(f"  [yellow]No YouTube channels found for \u201c{raw}\u201d[/yellow]")
        return await questionary.confirm(
            f"Continue without channel selection for {artist_name}?", default=True, style=SIDEB_STYLE
        ).ask_async()

    default_id = app.audio_provider._resolve_artist_channel(raw)
    chosen_id = await display_youtube_confirm(yt_channels, selected_id=default_id)
    if not chosen_id:
        return False
    if chosen_id == "__manual__":
        channel_url = await questionary.text(
            "Paste YouTube channel URL:", style=SIDEB_STYLE
        ).ask_async()
        if not channel_url:
            return False
        chosen_id = resolve_yt_channel_id(channel_url)
        if not chosen_id:
            console.print("  [yellow]Could not extract channel ID.[/yellow]")
            return False

    app.audio_provider._artist_channel_cache[artist_name] = chosen_id
    return True


async def _collect_source(app: Application) -> tuple[str, bool] | None:
    """Ask user for a URL/query and fetch metadata.

    Returns (source_type, audio_only) or None if user wants to exit.
    """
    console.print("[dim]Paste a track, album, playlist, or artist link \u2014 or just type an artist name.[/dim]")
    url = await questionary.text(
        "Deezer URL or search:",
        style=SIDEB_STYLE,
    ).ask_async()
    if not url:
        return None

    from sideb.utils.url_resolver import resolve_url

    # Resolve short links first
    resolved_url = url
    if any(d in resolved_url for d in ("deezer.page.link", "dzr.page.link", "link.deezer.com")):
        resolved_url = await resolve_url(resolved_url)
        if resolved_url == url:
            console.print("[red]Failed to resolve Deezer link.[/red]")
            return None

    # If it's a Deezer URL
    if _DEEZER_URL_RE.search(resolved_url):
        url = resolved_url
        # Artist URL — also offer YouTube channel selection like the search path
        artist_match = _DEEZER_URL_RE.search(resolved_url)
        if artist_match and artist_match.group(1).lower() == "artist":
            try:
                info = await app.metadata_provider.get_artist_info(artist_match.group(2))
                artist_name = info.get("name", "")
            except Exception:
                artist_name = ""
            if artist_name and not await _prompt_youtube_channel(app, artist_name):
                return None
    # If not a Deezer URL, search for artists first
    else:
        console.print("[dim]Searching for artists\u2026[/dim]")
        artists_raw = await app.metadata_provider.search_artists(url, limit=5)
        if not artists_raw:
            console.print("[yellow]No artists found.[/yellow]")
            return None

        async def _fetch_artist_top(a: dict) -> dict:
            top = await app.metadata_provider.get_artist_top_tracks(str(a["id"]), limit=3)
            return {"artist": a, "tracks": top}
        artist_data = list(await asyncio.gather(*(_fetch_artist_top(a) for a in artists_raw)))

        artist_id = await display_artist_selection(artist_data, "Select artist")
        if not artist_id:
            return None

        if artist_id == "__manual__":
            url = await questionary.text(
                "Paste a Deezer or YouTube link:",
                style=SIDEB_STYLE,
            ).ask_async()
            if not url:
                return None
            # If user pasted a Deezer artist URL, still offer YouTube channel selection
            manual_match = _DEEZER_URL_RE.search(url)
            if manual_match and manual_match.group(1).lower() == "artist":
                try:
                    info = await app.metadata_provider.get_artist_info(manual_match.group(2))
                    artist_name = info.get("name", "")
                except Exception:
                    artist_name = ""
                if artist_name and not await _prompt_youtube_channel(app, artist_name):
                    return None
        else:
            sel = next((a for a in artist_data if str(a["artist"]["id"]) == str(artist_id)), None)
            artist_name = sel["artist"]["name"] if sel else "Unknown"

            if not await _prompt_youtube_channel(app, artist_name):
                return None

            url = f"https://deezer.com/artist/{artist_id}"

    try:
        console.print("[dim]Detecting source\u2026[/dim]")

        with ProgressRenderer(app.event_bus, quiet=app.settings.quiet):
            try:
                source_ctx, tracks = await app.collect(url, pre_collect=True)
            except KeyboardInterrupt:
                console.print("\n[yellow]Cancelled.[/yellow]")
                return None
    except Exception as e:
        console.print(f"[red]Error fetching metadata: {e}[/red]")
        return None

    if not tracks:
        console.print("[yellow]No tracks found for that input.[/yellow]")
        return None

    name = source_ctx.source_name or tracks[0].artist.name
    source_type = source_ctx.source_type or "track"
    console.print(f"[green]Found {len(tracks)} track(s) from {name} ({source_type})[/green]")

    # Determine manifest path
    artist_dir = name if source_type in ("artist", "album", "track") else source_ctx.source_id
    if source_type == "playlist":
        artist_dir = source_ctx.source_name or source_ctx.source_id

    # Read existing manifest or create new one
    manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
    if manifest is None:
        manifest = Manifest(
            source=SourceInfo(type=source_type, url=url, name=name),
            tracks=[],
        )

    # Build manifest tracks from Deezer data
    new_tracks = []
    for t in tracks:
        lyrics_dict = None
        if t.lyrics:
            lyrics_dict = {
                "plain": t.lyrics.plain,
                "synced": t.lyrics.synced,
                "word_synced": t.lyrics.word_synced,
                "source": t.lyrics.source,
                "instrumental": t.lyrics.instrumental,
            }
        new_tracks.append(ManifestTrack(
            id=t.id,
            title=t.title,
            artist=t.artist.name,
            album=t.album.title,
            album_type=t.album.album_type or "",
            year=t.album.release_year,
            track_number=t.track_number,
            duration=t.duration,
            isrc=t.isrc,
            youtube_video_id=UNRESOLVED,
            youtube_channel_id=None,
            downloaded=False,
            filepath=None,
            lyrics=lyrics_dict,
        ))

    manifest.tracks = merge_tracks(manifest.tracks, new_tracks)
    write_manifest(manifest, app.settings.output_dir, source_type, artist_dir)

    console.print(f"[green]Added/updated {len(new_tracks)} track(s) in manifest.[/green]")
    return source_type, False  # (source_type, audio_only)


async def _resolve_youtube(app: Application) -> int:
    """Resolve YouTube URLs for all pending tracks across all manifests.
    For each artist group, shows interactive channel selection before resolving."""

    # Scan all manifests directly for UNRESOLVED tracks
    dirs = find_manifest_dirs(app.settings.output_dir)
    pending: list[tuple[ManifestTrack, str, str | None, str | None]] = []
    for source_type, artist_dir in dirs:
        manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
        if manifest is None:
            continue
        source_artist = manifest.source.name if source_type in ("artist", "album") else None
        for t in manifest.tracks:
            if not t.downloaded and t.youtube_video_id == UNRESOLVED:
                pending.append((t, source_type, artist_dir, source_artist))

    if not pending:
        console.print("[yellow]No tracks pending YouTube resolution.[/yellow]")
        return 0

    # Group by artist — use source artist name for artist manifests to avoid
    # splitting on featured-artist differences (e.g. "MGK feat. X" vs "MGK")
    groups: dict[str, list[tuple]] = {}
    group_has_artist_source: dict[str, bool] = {}
    for t, st, ad, src_artist in pending:
        key = src_artist or t.artist
        groups.setdefault(key, []).append((t, st, ad))
        if src_artist:
            group_has_artist_source[key] = True

    console.print(f"\n[bold]Resolving YouTube URLs for {len(pending)} track(s)\u2026[/bold]")
    from collections import Counter
    source_counts = Counter(st for st, ad in {(st, ad) for _, st, ad, _ in pending})
    parts = [f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(source_counts.items()) if v > 0]
    if parts:
        console.print(f"  [dim]Sources: {', '.join(parts)}[/dim]")

    # Phase A: channel selection (sequential, interactive) — only for artist-source groups
    for artist_name, group in groups.items():
        from_artist_source = group_has_artist_source.get(artist_name, False)

        # For non-artist sources (playlist/album/track), auto-detect and skip interactive selection
        if not from_artist_source:
            auto_channel = app.audio_provider._resolve_artist_channel(artist_name)
            if auto_channel:
                app.audio_provider._artist_channel_cache[artist_name] = auto_channel
            continue

        track = group[0][0]
        try:
            deezer_tracks = await app.metadata_provider.resolve_url(
                f"https://www.deezer.com/en/track/{track.id}"
            )
            if not deezer_tracks:
                continue
        except Exception:
            continue

        # Search YouTube channels and let user pick
        yt_channels = app.audio_provider.search_artist_channels(artist_name, limit=5)
        chosen_channel_id = None
        if yt_channels:
            default_id = app.audio_provider._resolve_artist_channel(artist_name)
            chosen_channel_id = await display_youtube_confirm(
                yt_channels,
                selected_id=default_id,
                heading=f"{artist_name} \u2014 {len(group)} track{'s' if len(group) != 1 else ''} pending",
                none_label="Skip \u2014 use auto-detected channel",
            )

            if chosen_channel_id:
                if chosen_channel_id == "__manual__":
                    channel_url = await questionary.text(
                        "Paste YouTube channel URL:",
                        style=SIDEB_STYLE,
                    ).ask_async()
                    if channel_url:
                        chosen_channel_id = resolve_yt_channel_id(channel_url)
                        if not chosen_channel_id:
                            console.print("  [yellow]Could not extract channel ID.[/yellow]")
                            continue

                app.audio_provider._artist_channel_cache[artist_name] = chosen_channel_id
                # Save channel ID to all manifest tracks for this artist group
                for mt, source_type, artist_dir in group:
                    manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
                    if manifest is None:
                        continue
                    for mtrack in manifest.tracks:
                        if mtrack.id == mt.id:
                            mtrack.youtube_channel_id = chosen_channel_id
                            break
                    write_manifest(manifest, app.settings.output_dir, source_type, artist_dir)
        else:
            console.print(f"  [yellow]No YouTube channels found for {artist_name}[/yellow]")

    # Phase B: resolve all tracks with workers + live progress display
    resolved = 0

    # Re-scan pending list (some tracks may have been resolved)  
    all_pending: list[tuple[ManifestTrack, str, str | None]] = []
    dirs = find_manifest_dirs(app.settings.output_dir)
    for source_type, artist_dir in dirs:
        manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
        if manifest is None:
            continue
        for t in manifest.tracks:
            if not t.downloaded and t.youtube_video_id == UNRESOLVED:
                all_pending.append((t, source_type, artist_dir))

    if not all_pending:
        return 0

    # Skip instrumentals — no point searching YouTube for something we won't download
    if app.settings.skip_instrumental:
        all_pending = [(t, st, ad) for t, st, ad in all_pending if not is_instrumental(t.title or "")]

    if not all_pending:
        return 0

    sem = asyncio.Semaphore(app.settings.workers)
    worker_pool: asyncio.Queue[int] = asyncio.Queue()
    for wid in range(1, app.settings.workers + 1):
        await worker_pool.put(wid)

    async def _resolve_one(mt: ManifestTrack, source_type: str, artist_dir: str | None) -> bool:
        nonlocal resolved
        wid = await worker_pool.get()
        dummy = Track(id=mt.id, title=mt.title, artist=Artist(id="", name=mt.artist),
                      album=Album(id="", title="", artist=Artist(id="", name=mt.artist)), duration=0)
        app.event_bus.emit(WorkerStarted(worker_id=wid, track=dummy))
        async with sem:
            app.event_bus.emit(WorkerStage(worker_id=wid, track=dummy, stage="searching"))
            try:
                deezer_tracks = await app.metadata_provider.resolve_url(
                    f"https://www.deezer.com/en/track/{mt.id}"
                )
                if not deezer_tracks:
                    app.event_bus.emit(WorkerFinished(worker_id=wid, track=dummy))
                    await worker_pool.put(wid)
                    return False
                video_id = await app.audio_provider.search(deezer_tracks[0])
            except Exception:
                video_id = None

        manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
        if manifest:
            for mtrack in manifest.tracks:
                if mtrack.id == mt.id:
                    mtrack.youtube_video_id = video_id if video_id else NOT_FOUND
                    break
            write_manifest(manifest, app.settings.output_dir, source_type, artist_dir)

        app.event_bus.emit(WorkerFinished(worker_id=wid, track=dummy))
        app.event_bus.emit(TrackCompleted(track=dummy, filepath=Path()))
        await worker_pool.put(wid)
        if video_id:
            resolved += 1
            return True
        return False

    with ProgressRenderer(app.event_bus, quiet=app.settings.quiet):
        try:
            for i, (mt, st, ad) in enumerate(all_pending, start=1):
                dummy = Track(id=mt.id, title=mt.title, artist=Artist(id="", name=mt.artist),
                              album=Album(id="", title="", artist=Artist(id="", name=mt.artist)), duration=0)
                app.event_bus.emit(TrackQueued(track=dummy, position=i, total=len(all_pending)))
            await asyncio.gather(*(_resolve_one(mt, st, ad) for mt, st, ad in all_pending))
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return resolved

    console.print(f"[green]Resolved {resolved}/{len(all_pending)} YouTube URLs.[/green]")
    return resolved


async def _download_pending(
    app: Application,
    pending: list[tuple[ManifestTrack, str, str | None]],
) -> int:
    """Download a given list of (track, source_type, artist_dir) tuples."""
    # Skip instrumentals
    skipped_instrumental = 0
    filtered: list[tuple[ManifestTrack, str, str | None]] = []
    for t, st, ad in pending:
        if app.settings.skip_instrumental and is_instrumental(t.title or ""):
            skipped_instrumental += 1
        else:
            filtered.append((t, st, ad))
    pending = filtered

    if not pending:
        console.print("[yellow]No tracks pending download.[/yellow]")
        return 0

    msg = f"\n[bold]Downloading {len(pending)} track(s)"
    if skipped_instrumental:
        msg += f" ([yellow]{skipped_instrumental} instrumental skipped[/yellow])"
    console.print(msg + "\u2026[/bold]")
    total_downloaded = 0

    # Group by manifest
    groups: dict[tuple[str, str | None], list[ManifestTrack]] = {}
    for track, source_type, artist_dir in pending:
        key = (source_type, artist_dir)
        groups.setdefault(key, []).append(track)

    for (source_type, artist_dir), group_tracks in groups.items():
        manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
        if manifest is None:
            continue

        sem = asyncio.Semaphore(app.settings.workers)

        async def _fetch_one(mt: ManifestTrack) -> Track | None:
            async with sem:
                try:
                    deezer_tracks = await app.metadata_provider.resolve_url(
                        f"https://www.deezer.com/en/track/{mt.id}"
                    )
                    if deezer_tracks:
                        t = deezer_tracks[0]
                        t.track_number = mt.track_number
                        if mt.lyrics:
                            from sideb.models.track import Lyrics
                            t.lyrics = Lyrics(
                                plain=mt.lyrics.get("plain"),
                                synced=mt.lyrics.get("synced"),
                                word_synced=mt.lyrics.get("word_synced"),
                                source=mt.lyrics.get("source", "manifest"),
                                instrumental=mt.lyrics.get("instrumental", False),
                            )
                        return t
                except Exception:
                    pass
                return None

        results: list[Track | None] = await asyncio.gather(*(_fetch_one(mt) for mt in group_tracks))
        tracks = [t for t in results if t is not None]

        url = manifest.source.url
        match = re.search(r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/(\d+)", url, re.IGNORECASE) if url else None
        from sideb.app.pipeline import SourceContext
        source_ctx = SourceContext(
            source_type=manifest.source.type,
            source_name=manifest.source.name,
            source_id=match.group(2) if match else "",
        )

        youtube_ids: dict[str, str | None] = {t.id: None for t in tracks}
        for mt in group_tracks:
            if mt.youtube_video_id:
                for tr in tracks:
                    if tr.id == mt.id:
                        youtube_ids[tr.id] = mt.youtube_video_id
                        break

        with ProgressRenderer(app.event_bus, quiet=app.settings.quiet):
            try:
                summary = await app.download_all(tracks, youtube_ids, source_ctx, manifest)
            except KeyboardInterrupt:
                console.print("\n[yellow]Cancelled.[/yellow]")
                break
        total_downloaded += len(summary.succeeded)

    console.print(f"[green]Downloaded {total_downloaded} track(s).[/green]")
    return total_downloaded


async def _select_download_scope(app: Application) -> list[tuple[ManifestTrack, str, str | None]]:
    """Show a menu to pick which manifests to download, return filtered pending list."""
    all_pending = scan_undownloaded(app.settings.output_dir)
    all_pending = [(t, st, ad) for t, st, ad in all_pending if t.youtube_video_id is not None and t.youtube_video_id not in (UNRESOLVED, NOT_FOUND)]

    # Group by source_type + artist_dir
    dirs = find_manifest_dirs(app.settings.output_dir)
    artists = [(st, ad) for st, ad in dirs if st == "artist"]
    playlists = [(st, ad) for st, ad in dirs if st == "playlist"]
    has_singles = any(st == "singles" for st, ad in dirs)

    def _count_for(st, ad):
        return sum(1 for _, s, a in all_pending if s == st and a == ad)

    choices = []
    total = len(all_pending)
    choices.append(questionary.Choice(title=f"All ({total} pending)", value="__all__"))
    if artists:
        choices.append(questionary.Choice(
            title=f"All artists ({sum(_count_for('artist', ad) for _, ad in artists)} pending)",
            value="__artists__",
        ))
    if playlists:
        choices.append(questionary.Choice(
            title=f"All playlists ({sum(_count_for('playlist', ad) for _, ad in playlists)} pending)",
            value="__playlists__",
        ))
    if artists:
        choices.append(questionary.Separator("── Artists ──"))
        for st, ad in artists:
            c = _count_for(st, ad)
            if c:
                choices.append(questionary.Choice(title=f"{ad} ({c} pending)", value=(st, ad)))
    if playlists:
        choices.append(questionary.Separator("── Playlists ──"))
        for st, ad in playlists:
            c = _count_for(st, ad)
            if c:
                choices.append(questionary.Choice(title=f"{ad} ({c} pending)", value=(st, ad)))
    if has_singles:
        c = _count_for("singles", None)
        choices.append(questionary.Choice(title=f"Singles ({c} pending)", value=("singles", None)))

    if not choices:
        console.print("[yellow]No tracks pending download.[/yellow]")
        return []

    selected = await questionary.select(
        "Which tracks to download?",
        choices=choices + [questionary.Separator(), questionary.Choice(title="Cancel", value=None)],
        style=SIDEB_STYLE,
    ).ask_async()

    if selected is None:
        return []
    if selected == "__all__":
        return all_pending
    if selected == "__artists__":
        return [(t, st, ad) for t, st, ad in all_pending if st == "artist"]
    if selected == "__playlists__":
        return [(t, st, ad) for t, st, ad in all_pending if st == "playlist"]
    # selected is (source_type, artist_dir)
    return [(t, st, ad) for t, st, ad in all_pending if st == selected[0] and ad == selected[1]]


async def _download_all(app: Application) -> int:
    """Show download scope picker, then download selected tracks."""
    pending = await _select_download_scope(app)
    if not pending:
        return 0
    return await _download_pending(app, pending)


async def _manage_manifest(app: Application) -> None:
    """View and remove tracks from manifests."""
    dirs = find_manifest_dirs(app.settings.output_dir)
    if not dirs:
        console.print("[yellow]No manifests found.[/yellow]")
        return

    choices = []
    for source_type, artist_dir in dirs:
        label = f"{source_type}: {artist_dir or 'singles'}"
        choices.append(questionary.Choice(title=label, value=(source_type, artist_dir)))

    selected = await questionary.select(
        "Select a manifest to manage:",
        choices=choices + [questionary.Separator(), questionary.Choice(title="Cancel", value=None)],
        style=SIDEB_STYLE,
    ).ask_async()
    if selected is None:
        return
    if isinstance(selected, (list, tuple)):
        source_type, artist_dir = selected[:2]
    else:
        return
    manifest = read_manifest(app.settings.output_dir, source_type, artist_dir)
    if manifest is None:
        console.print("[yellow]Manifest not found.[/yellow]")
        return

    while True:
        track_choices = []
        for i, t in enumerate(manifest.tracks, start=1):
            resolved = bool(t.youtube_video_id) and t.youtube_video_id not in (UNRESOLVED, NOT_FOUND)
            if t.downloaded:
                sym, color = "ok", "green"
            elif resolved:
                sym, color = "yt", "cyan"
            else:
                sym, color = "--", "#6c6c6c"
            line = [
                (f"fg:{color}", f"{sym}  "),
                ("", f"{i:>3}.  {t.artist} \u2014 {t.title}"),
            ]
            track_choices.append(questionary.Choice(title=line, value=t.id))

        track_choices.append(questionary.Separator())
        track_choices.append(questionary.Choice(title="Done managing this manifest", value="__done__"))

        selected = await questionary.select(
            f"{manifest.source.type}: {manifest.source.name or 'unnamed'}  "
            f"({len(manifest.tracks)} tracks) \u2014 select a track to remove:",
            choices=track_choices,
            style=SIDEB_STYLE,
        ).ask_async()

        if selected == "__done__" or selected is None:
            break

        # Confirm removal
        target = next((t for t in manifest.tracks if t.id == selected), None)
        label = f"{target.artist} \u2014 {target.title}" if target else str(selected)
        confirm = await questionary.confirm(f"Remove \"{label}\" from manifest?", style=SIDEB_STYLE).ask_async()
        if confirm:
            manifest.tracks = [t for t in manifest.tracks if t.id != selected]
            artist_dir = manifest.source.name if manifest.source.name else None
            write_manifest(manifest, app.settings.output_dir, manifest.source.type, artist_dir)
            console.print(f"[green]Removed \"{label}\" from manifest.[/green]")


async def run_queue_mode(app: Application) -> int:
    """Run the interactive queue & batch download workflow."""
    console.clear()
    print_big_banner(console, mode="Queue & batch download")
    console.print("[dim]Collect sources first, resolve YouTube URLs, then download all at once.[/dim]\n")

    # If manifests already exist from a previous session, the menu should
    # say "Add another source" from the start, not "Add a source" — the
    # queue isn't actually empty just because this process just launched.
    collected = bool(find_manifest_dirs(app.settings.output_dir))

    while True:
        choices = [
            two_line_choice(
                "Add a source" if not collected else "Add another source",
                "Artist, album, playlist, or track",
                "add",
            ),
            two_line_choice("Resolve YouTube URLs", "Match pending tracks to a channel", "resolve"),
            two_line_choice("Download all pending", "Fetch audio for every resolved track", "download"),
            two_line_choice("View / remove manifest tracks", "Inspect or drop queued tracks", "manage"),
            two_line_choice("Export M3U8 playlists", "Write playlist files for downloaded tracks", "m3u8"),
            two_line_choice("View queue status", "Counts across all manifests", "status"),
            questionary.Separator(),
            questionary.Choice(title="Exit to main menu", value="exit"),
        ]

        action = await questionary.select(
            "What would you like to do?",
            choices=choices,
            style=SIDEB_STYLE,
        ).ask_async()

        if action is None or action == "exit":
            return 0

        elif action == "add":
            result = await _collect_source(app)
            if result:
                collected = True

        elif action == "resolve":
            await _resolve_youtube(app)

        elif action == "download":
            await _download_all(app)

        elif action == "manage":
            await _manage_manifest(app)

        elif action == "m3u8":
            dirs = find_manifest_dirs(app.settings.output_dir)
            if not dirs:
                console.print("[yellow]No manifests found to export.[/yellow]")
            else:
                exported = 0
                for source_type, artist_dir in dirs:
                    m3u_path = export_m3u8(app.settings.output_dir, source_type, artist_dir)
                    if m3u_path:
                        console.print(f"[green]Exported: {m3u_path}[/green]")
                        exported += 1
                if exported == 0:
                    console.print("[yellow]No downloaded tracks found in any manifest.[/yellow]")
                else:
                    console.print(f"[green]Exported {exported} M3U8 playlist(s).[/green]")

        elif action == "status":
            all_pending = scan_undownloaded(app.settings.output_dir)
            pending_yt = [t for t, _, _ in all_pending if t.youtube_video_id == UNRESOLVED]
            not_found = [t for t, _, _ in all_pending if t.youtube_video_id == NOT_FOUND]
            pending_dl = [t for t, _, _ in all_pending if t.youtube_video_id is not None and t.youtube_video_id not in (UNRESOLVED, NOT_FOUND)]
            dirs = find_manifest_dirs(app.settings.output_dir)
            console.print("\n[bold]Queue status[/bold]")
            console.print(f"  [dim]Manifests[/dim]                    {len(dirs)}")
            console.print(f"  [dim]Pending YouTube resolution[/dim]   {len(pending_yt)}")
            console.print(f"  [dim]Not found on YouTube[/dim]         [yellow]{len(not_found)}[/yellow]")
            console.print(f"  [dim]Pending download[/dim]             [cyan]{len(pending_dl)}[/cyan]\n")
