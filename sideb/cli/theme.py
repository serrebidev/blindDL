"""Shared visual language for every interactive screen."""

from __future__ import annotations

import colorsys

import questionary


def _make_swatch(hue_deg: float, sat: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb(hue_deg / 360, light, sat)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


ACCENT_HUE = 269
ACCENT = _make_swatch(ACCENT_HUE, 0.60, 0.55)

WORKER_COLORS = [
    _make_swatch(32, 0.58, 0.58),
    _make_swatch(96, 0.30, 0.52),
    _make_swatch(205, 0.35, 0.58),
    _make_swatch(14, 0.55, 0.52),
    _make_swatch(275, 0.28, 0.60),
    _make_swatch(48, 0.55, 0.55),
]

RESULT_STYLE = {
    "ok": "dim",
    "skip": f"dim {_make_swatch(48, 0.55, 0.55)}",
    "fail": f"dim {_make_swatch(14, 0.55, 0.52)}",
}

STAGE_COLORS = {
    "cooldown": f"dim {_make_swatch(205, 0.35, 0.58)}",
    "rate-limited": _make_swatch(48, 0.55, 0.55),
}

STAGE_SYMBOLS = {
    "searching": "~",
    "downloading": ">",
    "remuxing": "R",
    "tagging": "#",
    "lyrics": "@",
    "sleep": "z",
    "cooldown": "\u03bb",
    "rate-limited": "!",
}

RESULT_SYMBOLS = {"ok": "ok", "skip": "~", "fail": "x"}

BAR_FULL = "\u2588"
BAR_EMPTY = "\u2591"

SIDEB_STYLE = questionary.Style(
    [
        ("qmark", f"fg:{ACCENT} bold"),
        ("question", "bold"),
        ("answer", f"fg:{ACCENT} bold"),
        ("pointer", f"fg:{ACCENT} bold"),
        ("highlighted", f"fg:{ACCENT} bold"),
        ("selected", f"fg:{ACCENT}"),
        ("separator", "fg:#6c6c6c"),
        ("instruction", "fg:#808080"),
        ("text", ""),
        ("disabled", "fg:#585858 italic"),
    ]
)


# ---------------------------------------------------------------------------
# Small text helpers shared by multiple screens
# ---------------------------------------------------------------------------


def fmt_count(val: int | str) -> str:
    """Human-readable count: 1_200_000 -> '1.2M', 8_400 -> '8.4K'."""
    if isinstance(val, str):
        return val
    try:
        val = int(val)
    except (TypeError, ValueError):
        return str(val)
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(val)


def ellipsize(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "\u2026"


def spread(left: str, right: str, width: int, min_gap: int = 2) -> str:
    gap = max(min_gap, width - len(left) - len(right))
    return f"{left}{' ' * gap}{right}"


def two_line_choice(title: str, subtitle: str, value):
    fragments = [("bold", title)]
    if subtitle:
        fragments.append(("", "\n"))
        fragments.append(("fg:#767676", f"  {subtitle}"))
    return questionary.Choice(title=fragments, value=value)


_TAGLINE = "Deezer \u00b7 YouTube \u00b7 synced lyrics"


def _app_version() -> str:
    try:
        from sideb import __version__
        return __version__
    except ImportError:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("sideb")
        except PackageNotFoundError:
            return "dev"


# Logo

_TITLE_SEGMENTS = [("S I D E", f"bold {ACCENT}"), (" \u2502 ", "dim"), ("B", "bold white")]

_BIG_BANNER_ART = [
    "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557    \u2588\u2588\u2588\u2588\u2588\u2588\u2557 ",
    "\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d    \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557",
    "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2557      \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d",
    "\u255a\u2550\u2550\u2550\u2550\u2588\u2588\u2551\u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u255d      \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557",
    "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557    \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d",
    "\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d\u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d    \u255a\u2550\u2550\u2550\u2550\u2550\u255d "
]
_BIG_BANNER_WIDTH = 39
_BIG_BANNER_HEIGHT = len(_BIG_BANNER_ART)

_SHEEN_SAT = 0.60
_SHEEN_L_DARK, _SHEEN_L_LIGHT = 0.25, 0.95


def _sheen_hex(row: int, col: int) -> str:
    v = row / _BIG_BANNER_HEIGHT
    h = 1 - (col / _BIG_BANNER_WIDTH)
    t = v * 0.80 + h * 0.20
    t = min(t ** 0.60, 1.0)
    lightness = _SHEEN_L_LIGHT + (_SHEEN_L_DARK - _SHEEN_L_LIGHT) * t
    return _make_swatch(277, _SHEEN_SAT, lightness)


def print_big_banner(console, mode: str | None = None) -> None:
    from rich.text import Text

    body = mode if mode else _TAGLINE

    console.print()
    for row_i, line in enumerate(_BIG_BANNER_ART):
        row = Text()
        for col_i, ch in enumerate(line):
            if ch == " ":
                row.append(" ")
            else:
                row.append(ch, style=f"bold {_sheen_hex(row_i, col_i)}")
        console.print(row)
    console.print(Text(body, style="dim"))
    console.print()


def print_banner(console, mode: str | None = None) -> None:
    from rich.text import Text

    title_plain = "".join(s for s, _ in _TITLE_SEGMENTS)
    version_tag = f"v{_app_version()}"

    if mode:
        info_segments = [("\u203a ", "dim"), (mode, ACCENT)]
    else:
        info_segments = [(_TAGLINE, "dim")]
    info_plain = "".join(s for s, _ in info_segments)

    inner_width = max(len(title_plain) + 1 + len(version_tag), len(info_plain))
    title_gap = max(2, inner_width - len(title_plain) - len(version_tag))
    info_pad = inner_width - len(info_plain)

    top = "\u256d" + "\u2500" * (inner_width + 2) + "\u256e"
    bot = "\u2570" + "\u2500" * (inner_width + 2) + "\u256f"
    blank = "\u2502 " + " " * inner_width + " \u2502"

    console.print()
    console.print(Text(top, style="dim"))

    title_row = Text()
    title_row.append("\u2502 ", style="dim")
    for seg, style in _TITLE_SEGMENTS:
        title_row.append(seg, style=style)
    title_row.append(" " * title_gap)
    title_row.append(version_tag, style="dim")
    title_row.append(" \u2502", style="dim")
    console.print(title_row)

    console.print(Text(blank, style="dim"))

    info_row = Text()
    info_row.append("\u2502 ", style="dim")
    for seg, style in info_segments:
        info_row.append(seg, style=style)
    info_row.append(" " * info_pad)
    info_row.append(" \u2502", style="dim")
    console.print(info_row)

    console.print(Text(bot, style="dim"))
    console.print()
