"""Artist download options — interactive prompts for filtering what to
download from an artist's discography."""

from __future__ import annotations

from datetime import date

import questionary

from sideb.cli.theme import SIDEB_STYLE
from sideb.utils.console import console


async def prompt_artist_download_options(albums: list[dict]) -> dict | None:
    """Prompt the user to choose how to download an artist's music.

    Returns a dict with:
        mode: "all" | "albums_eps" | "singles"
        selected_album_ids: list[str] | None  (for albums_eps mode)
        max_singles: int | None               (for singles mode)
        year_start: int | None
        year_end: int | None

    Returns None if the user cancels.
    """
    choice = await questionary.select(
        "Download option:",
        choices=[
            "Whole discography",
            "Select albums & EPs",
            "Singles only",
        ],
        style=SIDEB_STYLE,
    ).ask_async()

    if choice is None:
        return None

    current_year = date.today().year
    result: dict = {}
    result["mode"] = (
        "all" if choice == "Whole discography"
        else "albums_eps" if choice == "Select albums & EPs"
        else "singles"
    )
    result["selected_album_ids"] = None
    result["max_singles"] = None

    if result["mode"] == "albums_eps":
        album_choices = []
        filtered = [a for a in albums if a.get("record_type", "").lower() in ("album", "ep")]
        filtered.sort(key=lambda a: a.get("release_date", "") or "")
        for a in filtered:
            year = ""
            rd = a.get("release_date", "")
            if rd and len(rd) >= 4:
                year = f"({rd[:4]}) "
            label = f"{year}{a['title']} [{a.get('record_type', '').upper()}]"
            album_choices.append(questionary.Choice(title=label, value=str(a["id"])))

        if not album_choices:
            console.print("[yellow]No albums or EPs found for this artist.[/yellow]")
            return None

        selected = await questionary.checkbox(
            "Select albums & EPs:",
            choices=album_choices,
            style=SIDEB_STYLE,
        ).ask_async()

        if selected is None:
            return None
        if not selected:
            console.print("[yellow]No albums selected.[/yellow]")
            return None

        result["selected_album_ids"] = selected

    elif result["mode"] == "singles":
        single_count = len([a for a in albums if a.get("record_type", "").lower() == "single"])
        if single_count == 0:
            console.print("[yellow]No singles found for this artist.[/yellow]")
            return None

        max_s = await questionary.text(
            f"How many recent singles? (1\u2013{single_count}, default: 5):",
            default="5",
            style=SIDEB_STYLE,
        ).ask_async()

        if max_s is None:
            return None
        try:
            result["max_singles"] = max(1, min(int(max_s), single_count))
        except (ValueError, TypeError):
            result["max_singles"] = 5

    # --- Year range (only for whole discography) ---
    result["year_start"] = None
    result["year_end"] = None

    if result["mode"] == "all":
        console.print("  [dim]e.g. 2020-2024, or 2020 for 2020\u2013present (Enter for all)[/dim]")
        year_input = await questionary.text(
            "Year range:",
            style=SIDEB_STYLE,
        ).ask_async()

        if year_input is None:
            return None
        if year_input:
            year_input = year_input.strip()
            if "-" in year_input:
                parts = year_input.split("-", 1)
                try:
                    s = parts[0].strip()
                    e = parts[1].strip()
                    result["year_start"] = int(s) if s else None
                    result["year_end"] = int(e) if e else None
                except ValueError:
                    pass
            else:
                try:
                    result["year_start"] = int(year_input)
                    result["year_end"] = current_year
                except ValueError:
                    pass

    return result
