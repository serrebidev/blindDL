#!/usr/bin/env python3
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Reinstall the requirements that follow a git branch.

pip decides a requirement is already satisfied by comparing version strings.
Several of blindDL's dependencies are installed straight from a git branch and
publish new code without changing that version -- eaf_base_api has called
itself 4.1.1 across every change -- so pip keeps whatever the environment
already has and says nothing.

A build machine that reuses its environment therefore drifts, and it drifts
unevenly: a sibling package that *does* bump its version gets upgraded while
the one it depends on stays behind. That is what broke the v0.24.47 Linux
build, where a pornhub API released that morning imported a name from an
eaf_base_api five weeks older than itself. Hosted runners install into an
empty environment every time and never see it, so the failure looks like it
belongs to whichever machine happens to keep its virtual environment.

Reinstalling these from source before a release build costs a minute and
makes every build host agree. Requirements pinned to a commit cannot drift
and are left alone.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "requirements.txt"

# "name @ git+https://host/org/repo", optionally followed by "@ref".
DIRECT_URL = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*@\s*(?P<url>git\+[^\s#]+)"
)


def requirement_files(entry: Path) -> list[Path]:
    """The requirements file and everything it includes with ``-r``."""
    found: list[Path] = []
    pending = [entry]
    while pending:
        path = pending.pop(0)
        if not path.is_file() or path in found:
            continue
        found.append(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("-r "):
                pending.append((path.parent / stripped[3:].strip()).resolve())
    return found


def is_pinned(url: str) -> bool:
    """Whether the URL names a specific commit, tag or branch."""
    return "@" in url.split("://", 1)[-1]


def tracking_requirements(files: list[Path]) -> list[str]:
    specifications = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = DIRECT_URL.match(line)
            if match and not is_pinned(match.group("url")):
                specifications.append(
                    f"{match.group('name')} @ {match.group('url')}"
                )
    return specifications


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    files = requirement_files(ENTRY)
    specifications = tracking_requirements(files)
    if not specifications:
        print("No branch-following requirements to refresh.")
        return 0

    print(f"Refreshing {len(specifications)} branch-following requirements:")
    for specification in specifications:
        print(" -", specification.split(" @ ", 1)[0])
    # --no-deps keeps this to the packages that actually drift; the pass
    # below then installs anything a newer one has started depending on.
    run(
        sys.executable, "-m", "pip", "install", "--upgrade",
        "--force-reinstall", "--no-deps", *specifications,
    )
    run(sys.executable, "-m", "pip", "install", "-r", str(ENTRY))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
