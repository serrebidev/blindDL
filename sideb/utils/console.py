"""A single shared `rich.Console` instance so every part of the app renders
consistently (theme, width detection, etc.)."""

from __future__ import annotations

from rich.console import Console

console = Console()
error_console = Console(stderr=True, style="bold red")
