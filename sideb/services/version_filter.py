"""Song version prioritization and filtering.

Implements the multi-factor scoring used to pick the best version of a track
when Deezer returns multiple album versions of the same song (studio album
vs. single vs. compilation vs. remaster, etc). See ARCHITECTURE.md
"Appendix: Song Filtering & Version Prioritization".
"""

from __future__ import annotations

from sideb.models.track import Track

_ALBUM_TYPE_PRIORITY = {
    "album": 4,        # Studio album — highest priority
    "ep": 3,            # Extended play
    "compilation": 2,   # Best-of / greatest hits
    "single": 1,        # Single release
    "": 0,               # Unknown
}


def version_score(track: Track, *, prefer_original_release: bool = True) -> tuple:
    """Multi-factor sort key. Higher (or, for year, earlier-when-preferred) wins."""
    album_type_score = _ALBUM_TYPE_PRIORITY.get(track.album.album_type.lower(), 0)
    year = track.album.release_year or 0
    # When prefer_original_release, earlier years should sort first -> negate.
    year_key = -year if prefer_original_release else year
    return (
        album_type_score,
        int(track.has_featured_artist),
        int(track.explicit),
        int(track.is_deluxe),
        int(track.is_original_release),
        year_key,
        track.metadata_quality,
        track.duration,
        int(track.id) if track.id.isdigit() else 0,
    )


def pick_best_version(tracks: list[Track], *, prefer_original_release: bool = True) -> Track:
    """Given multiple Track objects representing the same underlying song,
    return the single best version by the scoring above."""
    if not tracks:
        raise ValueError("pick_best_version() requires at least one track")
    return max(tracks, key=lambda t: version_score(t, prefer_original_release=prefer_original_release))


def dedupe_by_isrc(tracks: list[Track], *, prefer_original_release: bool = True) -> list[Track]:
    """Group tracks sharing an ISRC (the same underlying recording) and keep
    only the best-scoring version of each. Tracks without an ISRC are kept
    as-is (deduplication needs a reliable identity key)."""
    groups: dict[str, list[Track]] = {}
    passthrough: list[Track] = []
    for t in tracks:
        if t.isrc:
            groups.setdefault(t.isrc, []).append(t)
        else:
            passthrough.append(t)

    result = [
        pick_best_version(group, prefer_original_release=prefer_original_release)
        for group in groups.values()
    ]
    result.extend(passthrough)
    return result
