"""Embeds fetched lyrics into an already-tagged audio file.

Behavior depends on `lyrics_mode` (see ARCHITECTURE.md section 6, table
under "Post-Process"):

  synced (default) -> line-synced LRC only. Compatible with all players.
  word              -> word-level (enhanced LRC) only.
  both              -> line-synced LRC for compatibility, plus word-level in
                       a separate custom tag for players that support it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from sideb.models.track import Lyrics

LyricsMode = Literal["synced", "word", "both"]


class Embedder:
    def embed(self, filepath: Path, lyrics: Lyrics, *, mode: LyricsMode = "synced") -> bool:
        """Returns True if any lyrics content was written."""
        if lyrics.instrumental:
            return False

        suffix = filepath.suffix.lower()
        if suffix in (".opus", ".ogg"):
            return self._embed_vorbis(filepath, lyrics, mode)
        elif suffix == ".webm":
            return False
        elif suffix == ".m4a":
            return self._embed_m4a(filepath, lyrics, mode)
        raise ValueError(f"Unsupported audio container for lyrics embedding: {suffix}")

    def _embed_vorbis(self, filepath: Path, lyrics: Lyrics, mode: LyricsMode) -> bool:
        audio = OggOpus(filepath)
        wrote = False

        if mode == "synced":
            if lyrics.synced:
                audio["LYRICS"] = lyrics.synced
                wrote = True
        elif mode == "word":
            if lyrics.word_synced:
                audio["LYRICS"] = lyrics.word_synced
                wrote = True
        elif mode == "both":
            if lyrics.synced:
                audio["LYRICS"] = lyrics.synced
                wrote = True
            if lyrics.word_synced:
                audio["RICH_SYNCED_LYRICS"] = lyrics.word_synced
                wrote = True

        if lyrics.plain:
            audio["UNSYNCEDLYRICS"] = lyrics.plain
            wrote = True

        if wrote:
            audio.save()
        return wrote

    def _embed_m4a(self, filepath: Path, lyrics: Lyrics, mode: LyricsMode) -> bool:
        audio = MP4(filepath)
        wrote = False

        primary = None
        if mode == "synced":
            primary = lyrics.synced
        elif mode == "word":
            primary = lyrics.word_synced
        elif mode == "both":
            primary = lyrics.synced or lyrics.word_synced
            if lyrics.word_synced:
                audio["----:com.apple.iTunes:RICH_SYNCED"] = lyrics.word_synced.encode("utf-8")
                wrote = True

        chosen = primary or lyrics.plain
        if chosen:
            audio["\xa9lyr"] = chosen
            wrote = True

        if wrote:
            audio.save()
        return wrote
