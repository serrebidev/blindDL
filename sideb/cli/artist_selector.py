"""Artist / channel selection picker for ambiguous search results."""

from __future__ import annotations

import questionary

from sideb.cli.theme import SIDEB_STYLE, ellipsize, fmt_count
from sideb.utils.console import console

_MAX_NAME_COL = 34


def _top_tracks(entry: dict, limit: int = 3) -> str:
    tracks = entry.get("tracks", [])[:limit]
    if not tracks:
        return ""
    return " \u00b7 ".join(t.get("title", "?") for t in tracks)  # " · "


def _build_rows(
    artists: list[dict], metric_label: str, default_id: str | None
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return (value, fragments) pairs — one two-line row per artist.

    Line 1 is just the artist name (bold). Line 2 is the audience size
    and top tracks dimmed together — so questionary's answer line shows
    only the clean name, not a spread-out name + count.
    """

    term_width = console.size.width or 100
    line_width = min(72, max(40, term_width - 6))

    rows: list[tuple[str, list[tuple[str, str]]]] = []
    for entry in artists:
        artist = entry.get("artist", entry)
        artist_id = artist.get("id")
        raw_name = artist.get("name", "Unknown")
        name = ellipsize(raw_name, _MAX_NAME_COL)
        is_default = default_id is not None and str(artist_id) == str(default_id)
        if is_default:
            name += " \u00b7 suggested"

        count = artist.get("nb_fan") or artist.get("subscribers") or 0
        meta = f"{fmt_count(count)} {metric_label}"

        tracks = _top_tracks(entry, limit=3)

        parts = [meta]
        if tracks:
            parts.append(tracks)
        line2 = " \u00b7 ".join(parts)
        line2 = ellipsize(line2, line_width - 2)

        fragments: list[tuple[str, str]] = [
            ("bold", name),
            ("", "\n"),
            ("fg:#767676", f"  {line2}"),
        ]

        rows.append((artist_id, fragments))

    return rows


def _choices_with_spacing(rows: list[tuple[str, list[tuple[str, str]]]]) -> list:
    """Insert a blank separator between each two-line row so rows don't run
    together — breathing room matters more once each item is 2 lines tall."""
    choices: list[questionary.Choice | questionary.Separator] = []
    for i, (val, line) in enumerate(rows):
        if i > 0:
            choices.append(questionary.Separator(" "))
        choices.append(questionary.Choice(title=line, value=val))
    return choices


_CANCEL = "__cancel__"
_MANUAL = "__manual__"


async def display_artist_selection(
    artists: list[dict],
    title: str = "Select artist",
    metric_label: str = "fans",
) -> str | None:
    """Show one unified list of candidate artists and let the user pick.

    Returns the selected artist's Deezer ID as a string, ``_MANUAL`` if the
    user wants to paste a URL instead, or None if cancelled.
    """
    plural = "" if len(artists) == 1 else "es"
    console.print(f"\n[bold]{title}[/bold] [dim]({len(artists)} match{plural})[/dim]\n")

    rows = _build_rows(artists, metric_label, default_id=None)
    choices = _choices_with_spacing(rows)
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title="Paste a link instead", value=_MANUAL))
    choices.append(questionary.Choice(title="None of these \u2014 cancel", value=_CANCEL))

    result = await questionary.select(
        "Which artist is it?",
        choices=choices,
        style=SIDEB_STYLE,
    ).ask_async()
    if result in (None, _CANCEL):
        return None
    return result


async def display_youtube_confirm(
    artists: list[dict],
    selected_id: str | None = None,
    *,
    heading: str = "Matching YouTube channel",
    none_label: str = "None of these \u2014 cancel",
) -> str | None:
    """Show candidate YouTube channels for the already-chosen artist and let
    the user confirm or override the auto-detected channel.

    Returns the selected channel ID, ``_MANUAL`` if the user wants to paste a
    YouTube link instead, or None if the "none" option is chosen.
    """
    console.print(f"\n[bold]{heading}[/bold]\n")

    rows = _build_rows(artists, "subscribers", default_id=selected_id)
    choices = _choices_with_spacing(rows)
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title="Paste a YouTube link instead", value=_MANUAL))
    choices.append(questionary.Choice(title=none_label, value=_CANCEL))

    result = await questionary.select(
        "Use which channel for the audio source?",
        choices=choices,
        style=SIDEB_STYLE,
    ).ask_async()
    if result in (None, _CANCEL):
        return None
    return result
