"""Track selection for YouTube search results — lightweight picker that shows
top N candidates and lets the user choose one to download."""

from __future__ import annotations

import questionary

from sideb.cli.theme import SIDEB_STYLE, two_line_choice
from sideb.utils.console import console


async def display_youtube_track_selection(
    candidates: list[dict],
    query: str,
) -> str | None:
    """Show top N search results and let the user pick one.

    Returns the selected videoId, or None if cancelled.
    """
    n = len(candidates)
    plural = "" if n == 1 else "s"
    console.print(f"\n[bold]Search results for \"{query}\"[/bold] [dim]({n} result{plural})[/dim]\n")

    choices = []
    for c in candidates:
        parts = [c["artist"]]
        if c.get("album"):
            parts.append(c["album"])
        if c.get("duration"):
            parts.append(c["duration"])
        subtitle = " \u00b7 ".join(parts)
        choices.append(two_line_choice(c["title"], subtitle, c["videoId"]))

    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title="None of these \u2014 cancel", value=None))

    result = await questionary.select(
        "Select a track to download:",
        choices=choices,
        style=SIDEB_STYLE,
    ).ask_async()
    return result
