# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Fail if a Deezer ARL credential is present in repository files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".smoke_music",
    ".smoke_sideb",
    ".smoke_ytdlp",
    "__pycache__",
    "build",
    "dist",
    ".venv",
    "venv",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".iss",
    ".json",
    ".md",
    ".py",
    ".ps1",
    ".sh",
    ".spec",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Deezer ARLs are 192 hexadecimal characters. This is the decisive check and
# catches a credential even when it is not assigned to a variable named ARL.
ARL_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{192}(?![0-9a-f])", re.IGNORECASE)

# Also reject suspicious literal assignments. Short, obviously fake values are
# allowed in tests and documentation; real credentials must only be read from
# the user's local config at runtime.
ARL_LITERAL = re.compile(
    r"(?i)(?:['\"]arl['\"]|\barl)\s*[:=]\s*['\"]([^'\"]+)['\"]"
)
PLACEHOLDERS = {
    "arl",
    "fake",
    "placeholder",
    "test",
    "test-arl",
    "your-arl",
    "your_arl",
    "your-arl-here",
    "your_arl_here",
}


def iter_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
        and path.suffix.lower() in TEXT_SUFFIXES
    )


def main() -> int:
    findings: list[str] = []
    scanned = 0

    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        relative = path.relative_to(ROOT)

        if ARL_TOKEN.search(text):
            findings.append(f"{relative}: contains a 192-character ARL token")

        for match in ARL_LITERAL.finditer(text):
            value = match.group(1).strip().lower()
            if value not in PLACEHOLDERS and not value.startswith(("example", "dummy")):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    f"{relative}:{line}: contains a non-placeholder literal ARL value"
                )

    if findings:
        print("ARL credential audit failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"ARL credential audit passed ({scanned} text files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
