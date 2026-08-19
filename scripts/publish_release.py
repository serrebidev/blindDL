# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT
"""Publish a blindDL release once every platform artifact is in place.

A blindDL release is assembled from three machines: Windows x64 is built
locally with build.bat, Linux x64 over SSH with
tools/build_linux_release.sh, and the two macOS architectures by
.github/workflows/release.yml. Whoever finished last had to remember to
take the release out of draft by hand, and v0.24.24 sat invisible for a
day because nobody did: a draft release is hidden from the Releases page
and skipped by the /releases/latest endpoint the updater reads, so the
world still saw v0.24.22.

This script is that final step, done by machine. It refuses to publish a
release that is missing an artifact, and it publishes one that is
complete, so a finished release cannot stay hidden and a half-built one
cannot go out.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every file a finished release carries, grouped by the machine that
# builds it, so a missing platform names the host to go and check.
PLATFORM_ASSETS = {
    "Windows x64 (build.bat)": (
        "blindDL-Setup-v{version}-windows-x64.exe",
        "blindDL-v{version}-windows-x64.zip",
        "SHA256SUMS-windows-x64.txt",
    ),
    "Linux x64 (tools/build_linux_release.sh)": (
        "blindDL-v{version}-linux-x64.tar.gz",
        "blinddl_{version}_amd64.deb",
        "SHA256SUMS-linux-x64.txt",
    ),
    "macOS x64 (GitHub Actions)": (
        "blindDL-v{version}-macos-x64.dmg",
        "SHA256SUMS-macos-x64.txt",
    ),
    "macOS arm64 (GitHub Actions)": (
        "blindDL-v{version}-macos-arm64.dmg",
        "SHA256SUMS-macos-arm64.txt",
    ),
}

# A draft still incomplete after this long is not mid-build any more;
# something went wrong and it needs a person.
STALE_HOURS = 24

# Put this in a release body to keep the guard's hands off it.
HOLD_MARKER = "<!-- no-autopublish -->"


def gh(*arguments: str) -> str:
    """Run a gh command and return its output."""
    result = subprocess.run(
        ("gh",) + arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def declared_version() -> str:
    """The version this checkout says it is."""
    source = (ROOT / "blinddl" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise RuntimeError("blinddl/__init__.py has no __version__")
    return match.group(1)


def version_of(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag))


def release(tag: str) -> dict:
    """Look up one release, draft or published."""
    return json.loads(
        gh(
            "release",
            "view",
            tag,
            "--json",
            "tagName,isDraft,createdAt,body,assets",
        )
    )


def release_list() -> list[dict]:
    return json.loads(
        gh("release", "list", "--limit", "60", "--json", "tagName,isDraft")
    )


def missing_assets(info: dict, version: str) -> list[str]:
    """Everything a finished release should carry and this one does not."""
    present = {asset["name"]: asset for asset in info["assets"]}
    missing = []
    for platform, templates in PLATFORM_ASSETS.items():
        for template in templates:
            name = template.format(version=version)
            asset = present.get(name)
            if asset is None:
                missing.append(f"{platform}: {name} was never uploaded")
            elif asset.get("state") != "uploaded":
                missing.append(
                    f"{platform}: {name} is still {asset.get('state')!r}"
                )
            elif not asset.get("size"):
                missing.append(f"{platform}: {name} uploaded empty")
    return missing


def checksum_problems(tag: str, info: dict) -> list[str]:
    """Check each artifact against the checksum its builder published.

    GitHub reports the sha256 it computed on upload, so this catches an
    artifact that arrived corrupted or was rebuilt without refreshing its
    SHA256SUMS file. Assets GitHub has not digested are left alone.
    """
    digests = {
        asset["name"]: (asset.get("digest") or "").removeprefix("sha256:")
        for asset in info["assets"]
    }
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        try:
            gh(
                "release",
                "download",
                tag,
                "--pattern",
                "SHA256SUMS-*.txt",
                "--dir",
                scratch,
                "--clobber",
            )
        except RuntimeError as error:
            print(f"  note: checksums were not verified ({error})")
            return []
        for listing in sorted(Path(scratch).glob("SHA256SUMS-*.txt")):
            for line in listing.read_text(encoding="utf-8").splitlines():
                expected, _, name = line.strip().partition("  ")
                if not name:
                    continue
                recorded = digests.get(name)
                if recorded is None:
                    problems.append(
                        f"{listing.name} lists {name}, which the release "
                        "does not carry"
                    )
                elif recorded and recorded != expected:
                    problems.append(
                        f"{name} does not match its checksum in {listing.name}"
                    )
    return problems


def is_newest(tag: str) -> bool:
    """Whether this tag outranks every already published release."""
    published = [
        version_key(entry["tagName"])
        for entry in release_list()
        if not entry["isDraft"] and entry["tagName"] != tag
    ]
    return all(version_key(tag) > other for other in published)


def age_hours(info: dict) -> float:
    created = datetime.fromisoformat(info["createdAt"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def report(problems: list[str]) -> None:
    for problem in problems:
        print(f"  - {problem}")


def publish(tag: str, info: dict) -> int:
    """Verify a complete release and take it out of draft."""
    problems = checksum_problems(tag, info)
    if problems:
        print(f"{tag} has all its artifacts but they do not verify:")
        report(problems)
        return 1
    latest = is_newest(tag)
    gh(
        "release",
        "edit",
        tag,
        "--draft=false",
        "--latest" if latest else "--latest=false",
    )
    marked = " as the latest release" if latest else ""
    print(f"Published {tag}{marked} with {len(info['assets'])} artifacts.")
    return 0


def settle(tag: str, wait_minutes: float, version: str) -> tuple[dict, list[str]]:
    """Wait for the remaining build hosts to finish uploading."""
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        info = release(tag)
        missing = missing_assets(info, version)
        if not missing or time.monotonic() >= deadline:
            return info, missing
        print(f"{tag} is waiting on {len(missing)} artifact(s):")
        report(missing)
        print("  checking again in a minute")
        time.sleep(60)


def publish_one(
    tag: str,
    wait_minutes: float,
    check_only: bool,
    verify_source_version: bool,
) -> int:
    version = version_of(tag)
    if verify_source_version:
        declared = declared_version()
        if declared != version:
            print(
                f"{tag} does not match the source it was tagged from: "
                f"blinddl/__init__.py says {declared}."
            )
            return 1

    info = release(tag)
    if not info["isDraft"]:
        missing = missing_assets(info, version)
        if missing:
            print(f"{tag} is already published but incomplete:")
            report(missing)
            return 1
        print(f"{tag} is already published and complete.")
        return 0

    info, missing = settle(tag, 0 if check_only else wait_minutes, version)
    if missing:
        print(f"{tag} is not ready to publish:")
        report(missing)
        print(
            "Left as a draft. Build the missing platforms, upload them, "
            f"then run: python scripts/publish_release.py {tag}"
        )
        return 1

    if check_only:
        print(f"{tag} has every artifact and is ready to publish.")
        return 0
    return publish(tag, info)


def sweep() -> int:
    """Catch any complete release that was left sitting in draft."""
    tags = [entry["tagName"] for entry in release_list() if entry["isDraft"]]
    if not tags:
        print("No draft releases.")
        return 0

    status = 0
    for tag in tags:
        info = release(tag)
        if HOLD_MARKER in (info.get("body") or ""):
            print(f"{tag} is held as a draft on purpose; leaving it alone.")
            continue
        missing = missing_assets(info, version_of(tag))
        if not missing:
            print(f"{tag} is complete but was left in draft.")
            status |= publish(tag, info)
            continue
        hours = age_hours(info)
        if hours >= STALE_HOURS:
            print(
                f"::error::{tag} has been an incomplete draft for "
                f"{hours:.0f} hours and needs attention:"
            )
            report(missing)
            status = 1
        else:
            print(
                f"::warning::{tag} is {hours:.1f} hours old and still "
                f"waiting on {len(missing)} artifact(s):"
            )
            report(missing)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a release once every platform artifact is in place."
    )
    parser.add_argument(
        "tag", nargs="?", help="the release tag, such as v0.24.24"
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="publish every complete draft release instead of one tag",
    )
    parser.add_argument(
        "--wait-minutes",
        type=float,
        default=0,
        help="how long to wait for the other build hosts to upload",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report whether the release is complete without publishing it",
    )
    parser.add_argument(
        "--verify-source-version",
        action="store_true",
        help="require blinddl/__init__.py to match the tag being published",
    )
    options = parser.parse_args()

    if options.sweep:
        if options.tag:
            parser.error("--sweep looks at every draft, so it takes no tag")
        return sweep()
    if not options.tag:
        parser.error("give a release tag, or --sweep")

    return publish_one(
        options.tag,
        options.wait_minutes,
        options.check_only,
        options.verify_source_version,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
