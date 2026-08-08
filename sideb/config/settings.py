"""Application configuration.

Precedence (highest to lowest): CLI flags > environment variables > .env file > defaults.
Environment variables are prefixed with ``SIDEB_`` (e.g. ``SIDEB_WORKERS=8``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Type-safe, environment-overridable application settings."""

    model_config = SettingsConfigDict(
        env_prefix="SIDEB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Output ---
    output_dir: Path = Path("./downloads")
    audio_format: Literal["opus", "m4a"] = "opus"
    audio_only: bool = False  # skip tagging/lyrics, just save the raw audio
    remux_to_ogg: bool = True  # remux .webm (Opus) → .ogg for better music player support

    # --- Performance ---
    workers: int = Field(default=4, ge=1, le=16)
    track_timeout: int = 120  # seconds per track before a worker aborts
    concurrent_fragments: int = Field(default=10, ge=1, le=20)  # yt-dlp concurrent fragment downloads

    # --- Cookies / auth ---
    cookies_file: Path | None = Path("./cookies.txt")
    cookies_from_browser: str | None = None  # "chrome" | "firefox" | ...
    deezer_arl: str | None = None  # enables word-level Deezer lyrics
    ytmusic_oauth_file: Path = Path("./browser.json")  # ytmusicapi browser auth (liked songs, playlists)

    # --- Lyrics ---
    enable_lyrics: bool = True
    lyrics_mode: Literal["synced", "word", "both"] = "synced"
    metadata_source: Literal["deezer", "youtube"] = "deezer"
    yt_single_duration_threshold: int = 600  # skip Deezer enrichment for YouTube tracks longer than this (seconds)

    # --- Duration tolerances (seconds) used by the YouTube search passes ---
    isrc_duration_tolerance: int = 5
    title_duration_tolerance: int = 8
    channel_duration_tolerance: int = 8

    # --- Version handling ---
    skip_instrumental: bool = True
    prefer_original_release: bool = True

    # --- Retry ---
    download_retries: int = Field(default=3, ge=0, le=10)  # retry failed tracks with jittered exponential backoff (default: 3)

    # --- Network ---
    proxy: str | None = None
    user_agent: str = "sideb/0.1.0 (+https://github.com/sideb-project/sideb)"
    yt_sleep: float = 10.0  # seconds to sleep between YouTube video requests (avoid rate limiting; default 10s per yt-dlp docs)
    yt_sleep_random: float = 15.0  # if set, sleep is randomized between yt_sleep and yt_sleep_random seconds (e.g. 10-15s range)

    # --- Output verbosity ---
    quiet: bool = False
    json_output: bool = False

    # --- Dry run ---
    dry_run: bool = False  # preview folder structure without downloading

    # --- Queue mode ---
    collect_only: bool = False  # fetch metadata to manifest without downloading
    pre_collect: bool = False  # fetch metadata + lyrics to manifest without downloading
    resolve: bool = False  # resolve YouTube URLs for all pending tracks
    download_all: bool = False  # download all pending tracks from all manifests
    export_m3u8: bool = False  # export M3U8 playlist from manifest


def load_settings(**overrides: object) -> Settings:
    """Build a Settings instance, applying explicit overrides (e.g. from CLI flags)
    on top of environment/`.env` values."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings.model_validate(clean)
