"""Shared filesystem path utilities."""

from __future__ import annotations


def sanitize_filename(name: str) -> str:
    """Remove characters invalid in filenames/directory names on Windows."""
    invalid = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in invalid).strip().rstrip(". ")
    return cleaned or "untitled"
