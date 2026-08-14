# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Turn a locally built libtorrent extension into a relocatable wheel.

Upstream libtorrent does not currently publish CPython 3.14 wheels.  The
platform update scripts build the extension from the latest stable tag, then
use this small packager to create normal pip metadata and vendor native
dependencies with delvewheel (Windows) or auditwheel (Linux).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path


KEEP_WHEELS = 5


def urlsafe_digest(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def detect_version(repo: Path) -> str:
    """Read the PEP 440 release version declared by libtorrent's bindings."""
    setup_cfg = repo / "bindings" / "python" / "setup.cfg"
    if not setup_cfg.is_file():
        raise RuntimeError(f"libtorrent setup.cfg not found: {setup_cfg}")
    match = re.search(
        r"^version\s*=\s*(.+)$",
        setup_cfg.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"libtorrent version not found in {setup_cfg}")
    return match.group(1).strip()


def wheel_tag() -> str:
    implementation = f"cp{sys.version_info.major}{sys.version_info.minor}"
    platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"{implementation}-{implementation}-{platform_tag}"


def build_base_wheel(
    extension: Path, version: str, outdir: Path, *, tag: str | None = None
) -> Path:
    """Create the valid platform wheel that a native repair tool consumes."""
    tag = tag or wheel_tag()
    dist_info = f"libtorrent-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: libtorrent\n"
        f"Version: {version}\n"
        "Summary: Python bindings for libtorrent-rasterbar\n"
        "Home-page: https://libtorrent.org\n"
        "Author: Arvid Norberg\n"
        "License: BSD-3-Clause\n"
        f"Requires-Python: =={sys.version_info.major}.{sys.version_info.minor}.*\n"
        "\n"
        "Locally built libtorrent Python bindings for an interpreter for which\n"
        "upstream does not publish a wheel.\n"
    )
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: blindDL package_libtorrent_wheel.py\n"
        "Root-Is-Purelib: false\n"
        f"Tag: {tag}\n"
    )

    outdir.mkdir(parents=True, exist_ok=True)
    wheel_path = outdir / f"libtorrent-{version}-{tag}.whl"
    records: list[tuple[str, str, int]] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:

        def write(name: str, data: bytes) -> None:
            archive.writestr(name, data)
            records.append((name, urlsafe_digest(data), len(data)))

        write(extension.name, extension.read_bytes())
        write(f"{dist_info}/METADATA", metadata.encode())
        write(f"{dist_info}/WHEEL", wheel_metadata.encode())
        write(f"{dist_info}/top_level.txt", b"libtorrent\n")

        record_name = f"{dist_info}/RECORD"
        rows = io.StringIO()
        writer = csv.writer(rows, lineterminator="\n")
        for name, digest, size in records:
            writer.writerow([name, digest, size])
        writer.writerow([record_name, "", ""])
        archive.writestr(record_name, rows.getvalue())

    wheel_path.write_bytes(buffer.getvalue())
    return wheel_path


def repair_wheel(raw: Path, outdir: Path, add_paths: list[str]) -> Path:
    """Vendor native libraries and return the repaired wheel path."""
    before = set(outdir.glob("libtorrent-*.whl"))
    if sys.platform == "win32":
        command = [
            sys.executable,
            "-m",
            "delvewheel",
            "repair",
            "--wheel-dir",
            str(outdir),
        ]
        if add_paths:
            command += ["--add-path", ";".join(add_paths)]
        command.append(str(raw))
    elif sys.platform.startswith("linux"):
        command = [
            sys.executable,
            "-m",
            "auditwheel",
            "repair",
            "--wheel-dir",
            str(outdir),
            str(raw),
        ]
    else:
        raise RuntimeError(f"wheel repair is not supported on {sys.platform}")
    subprocess.run(command, check=True)

    candidates = set(outdir.glob("libtorrent-*.whl")) - before
    if raw in candidates:
        candidates.remove(raw)
    if len(candidates) != 1:
        raise RuntimeError(f"native wheel repair produced {len(candidates)} wheels")
    return candidates.pop()


def prune(outdir: Path, keep: int = KEEP_WHEELS) -> list[Path]:
    """Keep enough weekly builds for rollback without growing forever."""
    wheels = sorted(
        outdir.glob("libtorrent-*.whl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for stale in wheels[keep:]:
        stale.unlink()
        removed.append(stale)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--stamp", required=True, help="local version, e.g. 20260814")
    parser.add_argument(
        "--add-path",
        action="append",
        default=[],
        help="directory searched by delvewheel (repeatable; Windows only)",
    )
    args = parser.parse_args()

    extension = Path(args.extension).resolve()
    if not extension.is_file():
        raise SystemExit(f"built extension not found: {extension}")
    repo = Path(args.repo).resolve()
    outdir = Path(args.outdir).resolve()
    version = f"{detect_version(repo)}+{args.stamp}"
    staging = outdir / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    # A failed validation may rerun the updater on the same day. Native repair
    # tools reuse the deterministic output name, so remove only that exact
    # version's previous attempt before asking them to write it again.
    for previous_attempt in outdir.glob(f"libtorrent-{version}-*.whl"):
        previous_attempt.unlink()

    raw = build_base_wheel(extension, version, staging)
    repaired = repair_wheel(raw, outdir, args.add_path)
    raw.unlink()
    try:
        staging.rmdir()
    except OSError:
        pass
    for stale in prune(outdir):
        print(f"pruned: {stale.name}")
    print(f"wheel: {repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
