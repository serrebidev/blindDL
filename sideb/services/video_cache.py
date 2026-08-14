"""Global video ID to file path registry.

Prevents re-downloading the same YouTube video when it appears in multiple
artist collections (e.g. a collab track). On cache hit: copy the file and
re-embed metadata for the new artist context.

Registry file: <output_dir>/.video_cache.json
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

def _cache_file(output_dir: Path) -> Path:
    return output_dir / ".video_cache.json"


# The registry parsed once per version of the file on disk, keyed by its
# path. It grows by one entry per track downloaded and never shrinks, and it
# was read twice and rewritten once for every track: an album re-parsed it
# hundreds of times, and a discography spent seconds doing nothing but
# reading its own bookkeeping.
_LOADED: dict[Path, tuple[float, int, dict]] = {}


def _load(output_dir: Path) -> dict:
    path = _cache_file(output_dir)
    try:
        stamp = path.stat()
    except OSError:
        return {}
    remembered = _LOADED.get(path)
    if (remembered is not None
            and remembered[0] == stamp.st_mtime
            and remembered[1] == stamp.st_size):
        return remembered[2]
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    _LOADED[path] = (stamp.st_mtime, stamp.st_size, cache)
    return cache


def _flush(cache: dict, output_dir: Path) -> None:
    path = _cache_file(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    # Remember what was just written, so the next read of an unchanged file
    # costs a stat instead of a parse. Anything that edits the file behind
    # us changes its time or its size, and is read again.
    try:
        stamp = path.stat()
    except OSError:
        _LOADED.pop(path, None)
    else:
        _LOADED[path] = (stamp.st_mtime, stamp.st_size, cache)


def find_live_path(video_id: str, output_dir: Path) -> Path | None:
    cache = _load(output_dir)
    entry = cache.get(video_id)
    if not entry:
        return None
    for stored in entry.get("paths", []):
        p = Path(stored)
        if p.exists() and p.stat().st_size > 10_000:
            return p
    return None


def register(video_id: str, file_path: Path, output_dir: Path) -> None:
    cache = _load(output_dir)
    stored = str(file_path.resolve())
    entry = cache.setdefault(video_id, {"paths": []})
    if stored not in entry["paths"]:
        entry["paths"].append(stored)
    _flush(cache, output_dir)


def copy_and_retag(source_path: Path, dest_path: Path, output_dir: Path) -> bool:
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_path), str(dest_path))
        return True
    except Exception:
        return False