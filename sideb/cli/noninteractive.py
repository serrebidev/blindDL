"""Non-interactive, scriptable CLI mode: `sideb <url> [flags]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from sideb import __version__
from sideb.app.main import Application, RunSummary, _YT_RE
from sideb.cli.theme import RESULT_STYLE, RESULT_SYMBOLS
from sideb.config.settings import load_settings
from sideb.cli.queue_mode import _resolve_youtube, _download_all
from sideb.app.pipeline import SourceContext
from sideb.services.manifest import Manifest, ManifestTrack, SourceInfo, export_m3u8, write_manifest
from sideb.utils.console import console, error_console

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


_SIGINT_EXIT_CODE = 130


def _open_editor(filepath: Path) -> None:
    """Open a file in the user's preferred text editor.

    Checks $VISUAL then $EDITOR, falling back to OS default:
      - Windows: notepad
      - macOS:   open -t
      - Linux:   sensible-editor, vi
    """
    import os
    import shutil
    import subprocess

    for var in ("VISUAL", "EDITOR"):
        editor = os.environ.get(var)
        if editor:
            subprocess.Popen([editor, str(filepath.resolve())])
            return
    if os.name == "nt":
        subprocess.Popen(["notepad", str(filepath.resolve())])
    elif shutil.which("sensible-editor"):
        subprocess.Popen(["sensible-editor", str(filepath.resolve())])
    else:
        subprocess.Popen(["vi", str(filepath.resolve())])


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _serialize_lyrics(lyrics) -> dict | None:
    """Convert a Lyrics object to a serializable dict for manifest storage."""
    if lyrics is None:
        return None
    return {
        "plain": lyrics.plain,
        "synced": lyrics.synced,
        "word_synced": lyrics.word_synced,
        "source": lyrics.source,
        "instrumental": lyrics.instrumental,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sideb",
        description="Side B — download music with synced lyrics from Deezer + YouTube Music.",
    )
    parser.add_argument("url", nargs="?", help="Deezer track/album/playlist/artist URL, or a search query")
    parser.add_argument("--output", type=Path, help="Output directory (default: ./downloads)")
    parser.add_argument("--format", choices=["opus", "m4a"], help="Audio container format")
    parser.add_argument("--workers", type=int, help="Number of concurrent download workers")
    parser.add_argument("--fragments", type=int, dest="concurrent_fragments", help="Concurrent fragment downloads per track (yt-dlp)")
    parser.add_argument("--no-lyrics", action="store_true", help="Skip lyrics entirely")
    parser.add_argument(
        "--lyrics-mode", choices=["synced", "word", "both"], help="Lyrics embedding mode"
    )
    parser.add_argument("--audio-only", action="store_true", help="Skip tagging and lyrics")
    parser.add_argument("--retry", type=int, default=3, choices=range(0, 11), help="Retry failed tracks up to N times with jittered backoff (default: 3, use 0 to disable)")
    parser.add_argument("--no-remux", action="store_false", dest="remux_to_ogg", help="Skip .webm to .ogg remux (keep native WebM container)")
    parser.add_argument("--cookies", type=Path, dest="cookies_file", help="Netscape cookie file for yt-dlp")
    parser.add_argument(
        "--cookies-from-browser", choices=["chrome", "firefox", "edge", "safari", "brave"]
    )
    parser.add_argument("--deezer-arl", help="Deezer ARL cookie, enables word-level lyrics")
    parser.add_argument("--source", choices=["deezer", "youtube"], default="deezer", help="Metadata source (default: deezer)")
    parser.add_argument("--proxy", help="HTTP(S) proxy URL")
    parser.add_argument("--yt-sleep", type=float, default=10.0,
                        help="Seconds to sleep between YouTube video requests to avoid rate limiting (default: 10)")
    parser.add_argument("--yt-sleep-random", type=float, default=15.0,
                        help="If set, sleep is randomized between --yt-sleep and this value (default: 15, giving 10-15s range)")
    parser.add_argument("--dry-run", action="store_true", help="Preview folder structure without downloading")
    parser.add_argument("--collect-only", action="store_true", help="Fetch metadata and save to manifest without downloading")
    parser.add_argument("--pre-collect", action="store_true", help="Fetch metadata + lyrics and save to manifest without downloading")
    parser.add_argument("--resolve", action="store_true", help="Resolve YouTube URLs for all pending tracks in manifests")
    parser.add_argument("--download-all", action="store_true", help="Download all pending tracks from all manifests")
    parser.add_argument("--export-m3u8", action="store_true", help="Export M3U8 playlist from manifest for the given URL")
    parser.add_argument("--yt-oauth-setup", action="store_true", help="One-time YouTube Music auth setup (copy headers from browser via Headers Copier extension)")
    parser.add_argument("--liked-songs", type=int, nargs="?", const=25, default=None, metavar="N",
                        help="Download your N most recent liked songs from YouTube Music")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary")
    parser.add_argument("--version", action="version", version=f"sideb {__version__}")
    return parser


def _settings_overrides(args: argparse.Namespace) -> dict:
    return dict(
        output_dir=args.output,
        audio_format=args.format,
        workers=args.workers,
        concurrent_fragments=args.concurrent_fragments,
        metadata_source=args.source,
        enable_lyrics=(False if args.no_lyrics else None),
        lyrics_mode=args.lyrics_mode,
        audio_only=(True if args.audio_only else None),
        download_retries=args.retry,
        remux_to_ogg=args.remux_to_ogg,
        cookies_file=args.cookies_file,
        cookies_from_browser=args.cookies_from_browser,
        deezer_arl=args.deezer_arl,
        proxy=args.proxy,
        yt_sleep=args.yt_sleep,
        yt_sleep_random=args.yt_sleep_random,
        dry_run=(True if args.dry_run else None),
        collect_only=(True if args.collect_only else None),
        pre_collect=(True if args.pre_collect else None),
        resolve=(True if args.resolve else None),
        download_all=(True if args.download_all else None),
        export_m3u8=(True if args.export_m3u8 else None),
        quiet=(True if args.quiet else None),
        json_output=(True if args.json_output else None),
    )


def _print_summary(summary: RunSummary, *, json_output: bool, dry_run: bool = False) -> None:
    if json_output:
        payload = {
            "total": summary.total,
            "succeeded": len(summary.succeeded),
            "skipped": len(summary.skipped),
            "failed": len(summary.failed),
            "with_lyrics": len(summary.with_lyrics),
            "paths": [str(r.filepath) for r in summary.succeeded if r.filepath] if dry_run else None,
            "failures": [
                {"title": r.track.title, "artist": r.track.artist.name, "error": r.error}
                for r in summary.failed
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    if dry_run:
        console.print("\n[bold]Dry run \u2014 would create:[/bold]")
        for r in summary.succeeded:
            if r.filepath:
                console.print(f"  \u00b7 {r.filepath}")
    else:
        console.print()
        parts = [f"[green]{len(summary.succeeded)} downloaded[/green]"]
        if summary.skipped:
            parts.append(f"[yellow]{len(summary.skipped)} skipped[/yellow]")
        if summary.failed:
            parts.append(f"[red]{len(summary.failed)} failed[/red]")
        console.print("  " + ", ".join(parts))
        if summary.with_lyrics:
            console.print(f"  [dim]Lyrics embedded: {len(summary.with_lyrics)}[/dim]")

    if summary.failed:
        style = RESULT_STYLE["fail"]
        sym = RESULT_SYMBOLS["fail"]
        console.print(f"\n  [{style}]{sym}[/]  [bold {style}]Failed[/]")
        for r in summary.failed:
            console.print(f"  [{style}]{sym}[/]  {r.track.title} [{style}]\u2014 {_strip_ansi(r.error or '')}[/]")


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(**_settings_overrides(args))
    app = Application(settings)
    try:
        if args.yt_oauth_setup:
            path = settings.ytmusic_oauth_file
            path.write_text("{}\n")
            _open_editor(path)
            console.print("[bold]YouTube Music browser authentication[/bold]")
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
            console.print("  Then run sideb again.")
            return 0

        if args.liked_songs is not None:
            limit = args.liked_songs
            if not app._yt_provider.has_oauth():
                error_console.print("No YouTube Music auth token found. Run --yt-oauth-setup first.")
                return 1
            from sideb.cli.renderer import ProgressRenderer
            with ProgressRenderer(app.event_bus, quiet=settings.quiet):
                tracks = await app._yt_provider.get_liked_songs(limit, event_bus=app.event_bus)
            if not tracks:
                error_console.print("No liked songs found.")
                return 1
            console.print(f"Found [bold]{len(tracks)}[/bold] liked song(s).")
            source_ctx = SourceContext(source_type="playlist", source_name="YouTube Liked Songs")
            with ProgressRenderer(app.event_bus, quiet=settings.quiet):
                results = await app.pipeline.run(tracks, source_ctx=source_ctx)
            from sideb.app.main import RunSummary
            summary = RunSummary(total=len(tracks), results=results)
            _print_summary(summary, json_output=settings.json_output)
            return 0 if not summary.failed else 2

        # YouTube search query — auto-pick first result
        if settings.metadata_source == "youtube" and args.url and not _YT_RE.search(args.url):
            console.print("[dim]Searching YouTube Music\u2026[/dim]")
            candidates = await app._yt_provider.search_candidates(args.url, limit=5)
            if not candidates:
                error_console.print("No tracks found.")
                return _SIGINT_EXIT_CODE
            best = candidates[0]
            args.url = f"https://music.youtube.com/watch?v={best['videoId']}"
            console.print(f"[dim]Auto-selected: {best['title']} \u2014 {best['artist']}[/dim]")

        if settings.resolve:
            await _resolve_youtube(app)
            return 0

        if settings.download_all:
            await _download_all(app)
            return 0

        if settings.export_m3u8:
            source_ctx, tracks = await app.collect(args.url)
            if not tracks:
                error_console.print("No tracks found for that URL/query.")
                return 1
            artist_dir = source_ctx.source_name if source_ctx.source_name else None
            m3u_path = export_m3u8(settings.output_dir, source_ctx.source_type or "track", artist_dir)
            if m3u_path:
                console.print(f"[green]Exported M3U8 playlist: {m3u_path}[/green]")
            else:
                console.print("[yellow]No downloaded tracks found to export.[/yellow]")
            return 0

        if settings.collect_only:
            source_ctx, tracks = await app.collect(args.url)
            if not tracks:
                error_console.print("No tracks found for that URL/query.")
                return 1
            artist_dir = source_ctx.source_name if source_ctx.source_name else None
            manifest = Manifest(
                source=SourceInfo(type=source_ctx.source_type or "track", url=args.url, name=source_ctx.source_name or ""),
                tracks=[
                    ManifestTrack(id=t.id, title=t.title, artist=t.artist.name, album=t.album.title,
                                  album_type=t.album.album_type or "", year=t.album.release_year,
                                  track_number=t.track_number, duration=t.duration, isrc=t.isrc)
                    for t in tracks
                ],
            )
            write_manifest(manifest, settings.output_dir, source_ctx.source_type or "track", artist_dir)
            console.print(f"[green]Saved {len(tracks)} track(s) to manifest.[/green]")
            return 0

        if settings.pre_collect:
            source_ctx, tracks = await app.collect(args.url, pre_collect=True)
            if not tracks:
                error_console.print("No tracks found for that URL/query.")
                return 1
            artist_dir = source_ctx.source_name if source_ctx.source_name else None
            manifest = Manifest(
                source=SourceInfo(type=source_ctx.source_type or "track", url=args.url, name=source_ctx.source_name or ""),
                tracks=[
                    ManifestTrack(
                        id=t.id, title=t.title, artist=t.artist.name, album=t.album.title,
                        album_type=t.album.album_type or "", year=t.album.release_year,
                        track_number=t.track_number, duration=t.duration, isrc=t.isrc,
                        lyrics=_serialize_lyrics(t.lyrics) if t.lyrics else None,
                    )
                    for t in tracks
                ],
            )
            write_manifest(manifest, settings.output_dir, source_ctx.source_type or "track", artist_dir)
            lyrics_count = sum(1 for t in tracks if t.lyrics)
            console.print(f"[green]Saved {len(tracks)} track(s) to manifest ({lyrics_count} with lyrics).[/green]")
            return 0

        from sideb.cli.renderer import ProgressRenderer
        with ProgressRenderer(app.event_bus, quiet=settings.quiet):
            try:
                summary = await app.run(args.url)
            except KeyboardInterrupt:
                console.print("\n[yellow]Cancelled.[/yellow]")
                return _SIGINT_EXIT_CODE
        if summary.total == 0:
            error_console.print("No tracks found for that URL/query.")
            return 1
        _print_summary(summary, json_output=settings.json_output, dry_run=settings.dry_run)
        return 0 if not summary.failed else 2
    finally:
        await app.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
