"""Interactive CLI mode: rich prompts, spinners, and a guided flow. See
ARCHITECTURE.md section 8 ("Interactive Mode Flow")."""

from __future__ import annotations

import asyncio
import re

import questionary
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

from sideb.app.main import Application
from sideb.cli.artist_download import prompt_artist_download_options
from sideb.cli.artist_selector import display_artist_selection, display_youtube_confirm
from sideb.cli.noninteractive import _print_summary
from sideb.cli.renderer import ProgressRenderer
from sideb.cli.theme import SIDEB_STYLE, print_big_banner, two_line_choice
from sideb.cli.track_selector import display_youtube_track_selection
from sideb.config.settings import load_settings
from sideb.models.track import Track
from sideb.utils.console import console, error_console
from sideb.utils.url_resolver import resolve_yt_channel_id

_YT_URL_RE = re.compile(r"(?:youtube\.com|music\.youtube\.com|youtu\.be)/", re.IGNORECASE)

_DEEZER_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/(\d+)",
    re.IGNORECASE,
)

_QMARK = "\u203a"  # ›


async def _run_yt_liked(app) -> int:
    """Download liked songs flow — prompt for count, resolve, download."""
    max_liked = 1000  # ytmusicapi max
    count_str = await questionary.text(
        f"How many recent liked songs? (1\u2013{max_liked}, default: 25):",
        default="25",
        style=SIDEB_STYLE,
    ).ask_async()
    if count_str is None:
        return 0
    try:
        limit = max(1, min(int(count_str), max_liked))
    except (ValueError, TypeError):
        limit = 25

    if not app._yt_provider.has_oauth():
        error_console.print("No YouTube Music OAuth token found.")
        return 1

    with ProgressRenderer(app.event_bus):
        tracks = await app._yt_provider.get_liked_songs(limit, event_bus=app.event_bus)

    if not tracks:
        error_console.print("No liked songs found. Your YouTube Music auth token may be expired or invalid.")
        return 1

    console.print(f"Found [bold]{len(tracks)}[/bold] liked song(s).")
    proceed = await questionary.confirm("Proceed with download?", default=True, style=SIDEB_STYLE).ask_async()
    if not proceed:
        return 0

    from sideb.app.pipeline import SourceContext
    source_ctx = SourceContext(source_type="playlist", source_name="YouTube Liked Songs")
    with ProgressRenderer(app.event_bus):
        results = await app.pipeline.run(tracks, source_ctx=source_ctx)

    from sideb.app.main import RunSummary
    summary = RunSummary(total=len(tracks), results=results)
    _print_summary(summary, json_output=False)
    return 0 if not summary.failed else 2


async def _resolve_artist_with_opts(
    app,
    url: str,
    albums: list[dict],
    opts: dict,
    *,
    on_progress=None,
) -> list[Track]:
    """Resolve artist tracks based on user-selected download options."""
    year_start = opts.get("year_start")
    year_end = opts.get("year_end")

    # Filter albums by year range first (avoids fetching unwanted tracks)
    if year_start or year_end:
        filtered = []
        for a in albums:
            rd = a.get("release_date", "")
            if rd and len(rd) >= 4:
                try:
                    y = int(rd[:4])
                    if (year_start is None or y >= year_start) and (year_end is None or y <= year_end):
                        filtered.append(a)
                except ValueError:
                    filtered.append(a)
            else:
                filtered.append(a)
        albums = filtered

    if not albums:
        return []

    if opts["mode"] == "all":
        if year_start or year_end:
            # Year range set — fetch only albums in range (more efficient)
            return await app.resolve_artist_albums(
                [str(a["id"]) for a in albums], on_progress=on_progress,
            )
        return await app.resolve(url, on_progress=on_progress)

    selected_ids: set[str] = set()
    if opts["mode"] == "albums_eps":
        selected_ids = set(opts.get("selected_album_ids") or [])
        selected_ids &= {str(a["id"]) for a in albums}
    elif opts["mode"] == "singles":
        singles = sorted(
            [a for a in albums if a.get("record_type", "").lower() == "single"],
            key=lambda a: a.get("release_date", "") or "",
            reverse=True,
        )
        max_s = opts.get("max_singles") or 5
        selected_ids = set(str(a["id"]) for a in singles[:max_s])

    if not selected_ids:
        return []

    album_chunks = [a for a in albums if str(a["id"]) in selected_ids]
    return await app.resolve_artist_albums(
        [str(a["id"]) for a in album_chunks], on_progress=on_progress,
    )


def _print_diagnostics(results: list[str]) -> list[str]:
    """Render diagnostics.run_all()'s plain "  OK  msg" / "  FAIL  msg" lines
    with color, same plain-text markers used everywhere else in the app.
    Returns the raw failing lines (diagnostics.py's contract is untouched —
    only how we *display* its output changes)."""
    failed = []
    for r in results:
        if r.startswith("  OK"):
            console.print(f"  [green]ok[/green]   [dim]{r[6:].strip()}[/dim]")
        elif r.startswith("  FAIL"):
            failed.append(r)
            console.print(f"  [red]fail[/red] {r[8:].strip()}")
        else:
            console.print(r)
    return failed


def _mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "\u2022" * len(value)
    return value[:keep] + "\u2022" * min(8, len(value) - keep)


def _print_settings() -> None:
    """Show the settings actually in effect right now (after env / .env are
    applied) — not just a pointer to the README. Secrets are masked, not
    printed in full."""
    settings = load_settings()
    rows = [
        ("Output folder", str(settings.output_dir)),
        ("Audio format", f"{settings.audio_format}{'  (audio only)' if settings.audio_only else ''}"),
        ("Workers", str(settings.workers)),
        ("Lyrics", "off" if not settings.enable_lyrics else settings.lyrics_mode),
        ("Deezer ARL", _mask(settings.deezer_arl) if settings.deezer_arl else "[dim]not set[/dim]"),
        ("Cookies file", str(settings.cookies_file) if settings.cookies_file else "[dim]not set[/dim]"),
        ("Cookies from browser", settings.cookies_from_browser or "[dim]not set[/dim]"),
        ("Proxy", settings.proxy or "[dim]not set[/dim]"),
    ]
    console.print("\n[bold]Current settings[/bold] [dim](env vars / .env, prefixed SIDEB_)[/dim]\n")
    for label, value in rows:
        console.print(f"  {label:<22}{value}")
    console.print("\n  [dim]Full list of options: README.md \u2014 override with SIDEB_<NAME>=value or .env[/dim]\n")


async def _run_diagnostics() -> int:
    from sideb.cli.diagnostics import run_all

    settings = load_settings()
    console.print("\n[bold]Running connection checks[/bold]\n")
    results = await run_all(settings.deezer_arl, settings.cookies_file)
    failed = _print_diagnostics(results)
    passed = len(results) - len(failed)
    summary_style = "green" if not failed else "yellow"
    console.print(f"\n  [{summary_style}]{passed}/{len(results)} checks passed[/{summary_style}]\n")
    return 0


async def _prompt_youtube_channel(app: Application, artist_name: str) -> bool:
    """Pre-search prompt: user can paste a YouTube channel URL or confirm to search.
    Returns True to continue (channel cached), False to abort."""
    raw = await questionary.text(
        "YouTube channel URL or search:",
        default=artist_name,
        qmark=_QMARK,
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
            "Paste YouTube channel URL:", qmark=_QMARK, style=SIDEB_STYLE
        ).ask_async()
        if not channel_url:
            return False
        chosen_id = resolve_yt_channel_id(channel_url)
        if not chosen_id:
            console.print("  [yellow]Could not extract channel ID.[/yellow]")
            return False

    app.audio_provider._artist_channel_cache[artist_name] = chosen_id
    return True


async def _run_interactive() -> int:
    print_big_banner(console)
    console.print()

    action = await questionary.select(
        "What would you like to do?",
        choices=[
            two_line_choice("Download music", "Paste a Deezer link or search name", "Download music"),
            two_line_choice("YouTube Music", "Search or paste a YouTube link", "YouTube Music"),
            two_line_choice("Queue & batch download", "Collect several sources, download all at once", "Queue & Batch Download"),
            two_line_choice("Check connections", "Test Deezer, ARL, and YouTube cookies", "Check connections"),
            two_line_choice("Configure settings", "Where options live", "Configure settings"),
        ],
        style=SIDEB_STYLE,
        qmark=_QMARK,
    ).ask_async()

    if action is None:
        return 1

    if action == "Check connections":
        return await _run_diagnostics()

    if action == "Configure settings":
        _print_settings()
        return 0

    if action == "Queue & Batch Download":
        from sideb.cli.queue_mode import run_queue_mode

        settings = load_settings()
        app = Application(settings)
        try:
            return await run_queue_mode(app)
        finally:
            await app.aclose()

    is_youtube = action == "YouTube Music"

    if is_youtube:
        # --- YouTube Music flow ---
        settings = load_settings()
        has_oauth = settings.ytmusic_oauth_file.exists()
        choices = [
            two_line_choice("Search or paste a link", "YouTube video, playlist, or search query", "search"),
        ]
        if has_oauth:
            choices.append(
                two_line_choice("Your liked songs", "Download your YouTube Music liked songs", "liked"),
            )
        else:
            choices.append(
                two_line_choice("Authenticate YouTube Music", "Paste browser headers from DevTools to access your library", "oauth"),
            )
        yt_action = await questionary.select(
            "YouTube Music:",
            choices=choices,
            style=SIDEB_STYLE,
            qmark=_QMARK,
        ).ask_async()
        if yt_action is None:
            return 0

        if yt_action == "oauth":
            path = settings.ytmusic_oauth_file
            path.write_text("{}\n")
            from sideb.cli.noninteractive import _open_editor
            _open_editor(path)
            console.print("\n[bold]YouTube Music browser authentication[/bold]")
            console.print()
            console.print(f"  Editor opened with [bold]{path.resolve()}[/bold]")
            console.print()
            console.print("  1. Open [bold]music.youtube.com[/bold] in Chrome, sign in")
            console.print("  2. Click [bold]Library[/bold] on the left sidebar")
            console.print("  3. Click the [bold]Header Copier[/bold] extension icon")
            console.print("  4. Pick a request with [bold]/youtubei/v1/[/bold] in the path")
            console.print("     (NOT the main page request \u2014 pick an XHR/fetch one)")
            console.print("  5. Click [bold]Copy JSON[/bold]")
            console.print("  6. Paste into Notepad ([bold]Ctrl+V[/bold]), save ([bold]Ctrl+S[/bold]), close")
            console.print()
            console.print("  Then run sideb again and pick [bold]Your liked songs[/bold].")
            return 0

        if yt_action == "liked":
            settings = load_settings(metadata_source="youtube")
            app = Application(settings)
            try:
                return await _run_yt_liked(app)
            finally:
                await app.aclose()

        console.print("[dim]Paste a YouTube video/playlist link or type a search query.[/dim]")
        url = await questionary.text(
            "YouTube URL or search:",
            qmark=_QMARK,
            style=SIDEB_STYLE,
        ).ask_async()
        if not url:
            return 1

        settings = load_settings(metadata_source="youtube")

    else:
        # --- Deezer / auto-detect flow ---
        run_checks = await questionary.confirm(
            "Run connection checks first?", default=True, style=SIDEB_STYLE
        ).ask_async()
        if run_checks:
            from sideb.cli.diagnostics import run_all

            diagnostics_settings = load_settings()
            console.print("\n[bold]Checking connections[/bold]\n")
            diag_results = await run_all(diagnostics_settings.deezer_arl, diagnostics_settings.cookies_file)
            failed = _print_diagnostics(diag_results)
            if failed:
                proceed = await questionary.confirm(
                    "Some checks failed. Continue anyway?", default=False, style=SIDEB_STYLE
                ).ask_async()
                if not proceed:
                    return 0

        console.print("[dim]Paste a track, album, playlist, or artist link \u2014 or just type an artist name.[/dim]")
        url = await questionary.text(
            "Deezer URL or search:",
            qmark=_QMARK,
            style=SIDEB_STYLE,
        ).ask_async()
        if not url:
            return 1

        settings = load_settings()

    app = Application(settings)
    try:
        if is_youtube:
            if not _YT_URL_RE.search(url):
                candidates = await app._yt_provider.search_candidates(url, limit=5)
                if not candidates:
                    error_console.print("No tracks found.")
                    return 1
                video_id = await display_youtube_track_selection(candidates, url)
                if not video_id:
                    return 0
                url = f"https://music.youtube.com/watch?v={video_id}"
            resolved_url = url
        else:
            # Resolve short links first
            from sideb.utils.url_resolver import resolve_url

            resolved_url = url
            if any(d in resolved_url for d in ("deezer.page.link", "dzr.page.link", "link.deezer.com")):
                resolved_url = await resolve_url(resolved_url)
                if resolved_url == url:
                    error_console.print("[red]Failed to resolve Deezer link.[/red]")
                    return 1

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
                        return 0
            # Otherwise, search for artists
            else:
                console.print("[dim]Searching for artists\u2026[/dim]")
                artists_raw = await app.metadata_provider.search_artists(url, limit=5)
                if not artists_raw:
                    error_console.print("No artists found.")
                    return 1

                async def _fetch_artist_top(a: dict) -> dict:
                    top = await app.metadata_provider.get_artist_top_tracks(str(a["id"]), limit=3)
                    return {"artist": a, "tracks": top}

                artist_data = list(await asyncio.gather(*(_fetch_artist_top(a) for a in artists_raw)))

                artist_id = await display_artist_selection(artist_data, "Select artist")
                if not artist_id:
                    return 0

                if artist_id == "__manual__":
                    url = await questionary.text(
                        "Paste a Deezer or YouTube link:",
                        qmark=_QMARK,
                        style=SIDEB_STYLE,
                    ).ask_async()
                    if not url:
                        return 0
                    manual_match = _DEEZER_URL_RE.search(url)
                    if manual_match and manual_match.group(1).lower() == "artist":
                        try:
                            info = await app.metadata_provider.get_artist_info(manual_match.group(2))
                            artist_name = info.get("name", "")
                        except Exception:
                            artist_name = ""
                        if artist_name and not await _prompt_youtube_channel(app, artist_name):
                            return 0
                else:
                    sel = next((a for a in artist_data if str(a["artist"]["id"]) == str(artist_id)), None)
                    artist_name = sel["artist"]["name"] if sel else "Unknown"

                    if not await _prompt_youtube_channel(app, artist_name):
                        return 0

                    url = f"https://deezer.com/artist/{artist_id}"

        # --- Artist download options (interactive selection) ---
        is_artist = False
        artist_id_for_opts = None
        artist_match = _DEEZER_URL_RE.search(url)
        if artist_match and artist_match.group(1).lower() == "artist":
            is_artist = True
            artist_id_for_opts = artist_match.group(2)

        artist_opts = None
        if is_artist:
            console.print("[dim]Fetching albums\u2026[/dim]")
            albums = await app.metadata_provider.get_artist_albums(artist_id_for_opts or "")
            if not albums:
                error_console.print("No albums found for this artist.")
                return 1
            artist_opts = await prompt_artist_download_options(albums)
            if artist_opts is None:
                return 0

        resolve_progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[dim]{task.description}[/dim]"),
            BarColumn(bar_width=24, complete_style="cyan", finished_style="green"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        task_id = resolve_progress.add_task("Resolving tracks\u2026", total=None)
        resolve_progress.start()

        def on_progress(current: int, total: int, msg: str) -> None:
            resolve_progress.update(task_id, description=msg, total=total, completed=current)

        if artist_opts is not None:
            tracks = await _resolve_artist_with_opts(
                app, url, albums, artist_opts, on_progress=on_progress,
            )
        else:
            tracks = await app.resolve(url, on_progress=on_progress)
        resolve_progress.stop()
        if not tracks:
            error_console.print("No tracks found for that URL/query.")
            return 1

        else:
            console.print(f"\nFound [bold]{len(tracks)}[/bold] track(s).")
            proceed = await questionary.confirm(
                "Proceed with download?", default=True, style=SIDEB_STYLE
            ).ask_async()
            if not proceed:
                return 0

        console.print("[dim]Preparing pipeline\u2026[/dim]\n")
        source_ctx = await app._detect_source(url)
        with ProgressRenderer(app.event_bus):
            results = await app.pipeline.run(tracks, source_ctx=source_ctx)

        from sideb.app.main import RunSummary

        summary = RunSummary(total=len(tracks), results=results)
        _print_summary(summary, json_output=False)
        return 0 if not summary.failed else 2
    finally:
        await app.aclose()


def main() -> int:
    return asyncio.run(_run_interactive())
