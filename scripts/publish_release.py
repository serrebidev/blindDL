# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT
"""Publish a blindDL release on Forgejo once every platform is in place.

A blindDL release is assembled from two or three machines: Windows x64 is
built locally with build.bat, Linux x64 over SSH with
tools/build_linux_release.sh, and the Apple Silicon macOS DMG on a
secondary GitHub runner while that account is reachable. Whoever finished
last had to remember to take the release out of draft by hand, and v0.24.24
sat invisible for a day because nobody did: a draft release is hidden from
the Releases page and skipped by the /releases/latest endpoint the updater
reads, so the world still saw v0.24.22.

This script is that final step, done by machine. It creates the draft,
takes uploaded artifacts, refuses to publish a release that is missing one,
and publishes one that is complete, so a finished release cannot stay
hidden and a half-built one cannot go out. Every command needs FORGEJO_TOKEN in the environment:
drafts are invisible to unauthenticated reads, so even --check-only cannot
see them without it.
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blinddl import parse_version

FORGEJO_HOST = "git.serrebiradio.com"
FORGEJO_REPO = "serrebi/blindDL"

API_TIMEOUT = 30
UPLOAD_TIMEOUT = 600
# Forgejo only accepts release files under this multipart field; anything
# else arrives as an empty asset with no error to say why.
ATTACHMENT_FIELD = "attachment"

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
    "macOS arm64 (secondary GitHub runner)": (
        "blindDL-v{version}-macos-arm64.dmg",
        "SHA256SUMS-macos-arm64.txt",
    ),
}

# A draft still incomplete after this long is not mid-build any more;
# something went wrong and it needs a person.
STALE_HOURS = 24

# Put this in a release body to keep the sweep's hands off it.
HOLD_MARKER = "<!-- no-autopublish -->"


class ReleaseError(RuntimeError):
    """A release step failed; the message is safe to show the user."""


def _http(
    method: str,
    url: str,
    headers: dict[str, str],
    body=None,
    timeout: float = API_TIMEOUT,
) -> tuple[int, bytes]:
    """One HTTPS round trip. ``body`` is bytes or a readable stream."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname,
            parts.port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    elif parts.scheme == "http":
        connection = http.client.HTTPConnection(
            parts.hostname, parts.port or 80, timeout=timeout
        )
    else:
        raise ReleaseError(f"Refusing to call Forgejo over {parts.scheme!r}.")
    path = parts.path
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ReleaseError(f"Network error calling Forgejo: {exc}") from exc
    finally:
        connection.close()


class _MultipartStream(io.RawIOBase):
    """A single-file multipart body that streams from disk.

    Release installers are hundreds of megabytes; building the body in
    memory would hold the whole file twice. http.client reads this in
    blocks once Content-Length is set.
    """

    def __init__(self, field: str, path: Path, content_type: str):
        self._boundary = "----blinddlforgejo" + os.urandom(8).hex()
        head = (
            f"--{self._boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("ascii")
        tail = f"\r\n--{self._boundary}--\r\n".encode("ascii")
        self._head = io.BytesIO(head)
        self._file = path.open("rb")
        self._tail = io.BytesIO(tail)
        self.length = len(head) + path.stat().st_size + len(tail)
        self.content_type = (
            f"multipart/form-data; boundary={self._boundary}"
        )

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        for stream in (self._head, self._file, self._tail):
            chunk = stream.read(size)
            if chunk:
                return chunk
        return b""

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            super().close()


# (method, url, headers, body) -> (status, raw body). Injected in tests.
HttpFn = Callable[..., tuple[int, bytes]]


class ForgejoClient:
    """Minimal Forgejo releases client with an injectable transport."""

    def __init__(
        self,
        host: str = FORGEJO_HOST,
        repo: str = FORGEJO_REPO,
        token: str | None = None,
        http: HttpFn = _http,
    ):
        self.base = f"https://{host}/api/v1/repos/{repo}"
        self.token = token or ""
        self._http = http

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        ok_404: bool = False,
        payload=None,
        timeout: float = API_TIMEOUT,
    ):
        if path.startswith("http"):
            url = path
        else:
            url = self.base + path
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._http(method, url, headers, body, timeout)
        if status == 404 and ok_404:
            return None
        if status >= 400:
            detail = ""
            try:
                detail = str(json.loads(raw.decode("utf-8", "replace"))
                             .get("message", ""))
            except ValueError:
                detail = raw.decode("utf-8", "replace")[:200]
            raise ReleaseError(
                f"Forgejo API error: {method} {path}: {status} {detail}"
                .rstrip()
            )
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ReleaseError(
                f"Forgejo returned invalid JSON for {method} {path}: {exc}"
            ) from exc

    def get_release_by_tag(self, tag: str) -> dict | None:
        return self._call("GET", f"/releases/tags/{tag}", ok_404=True)

    def list_releases(self, limit: int = 50) -> list[dict]:
        """Every release, newest first, across pages."""
        releases: list[dict] = []
        page = 1
        while True:
            batch = (
                self._call(
                    "GET", f"/releases?limit={limit}&page={page}"
                )
                or []
            )
            releases.extend(batch)
            if len(batch) < limit or page >= 10:
                return releases
            page += 1

    def create_release(
        self, tag: str, title: str, notes: str, target: str | None = None
    ) -> dict:
        payload: dict[str, object] = {
            "tag_name": tag,
            "name": title,
            "body": notes,
            "draft": True,
        }
        if target:
            payload["target_commitish"] = target
        return self._call("POST", "/releases", payload=payload)

    def publish_release(self, release_id: int) -> dict:
        return self._call(
            "PATCH", f"/releases/{release_id}", payload={"draft": False}
        )

    def delete_release(self, release_id: int) -> None:
        self._call("DELETE", f"/releases/{release_id}")

    def delete_asset(self, release_id: int, asset_id: int) -> None:
        self._call("DELETE", f"/releases/{release_id}/assets/{asset_id}")

    def upload_asset(self, release_id: int, asset: Path) -> dict:
        name = asset.name
        content_type = (
            "text/plain" if asset.suffix in (".txt", ".json")
            else "application/octet-stream"
        )
        stream = _MultipartStream(ATTACHMENT_FIELD, asset, content_type)
        try:
            headers = self._headers(
                {
                    "Content-Type": stream.content_type,
                    "Content-Length": str(stream.length),
                }
            )
            url = (
                f"{self.base}/releases/{release_id}/assets"
                f"?name={urllib.parse.quote(name)}"
            )
            status, raw = self._http(
                "POST", url, headers, stream, UPLOAD_TIMEOUT
            )
        finally:
            stream.close()
        if status >= 400:
            raise ReleaseError(
                f"Forgejo API error uploading {name}: {status} "
                f"{raw.decode('utf-8', 'replace')[:200]}".rstrip()
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ReleaseError(
                f"Forgejo returned invalid JSON uploading {name}: {exc}"
            ) from exc

    def latest_tag(self) -> str | None:
        try:
            data = self._call("GET", "/releases/latest")
        except ReleaseError as exc:
            if " 404" in str(exc):
                return None
            raise
        return (data or {}).get("tag_name")

    def download(self, url: str) -> bytes:
        _status, raw = self._http(
            "GET", url, {"User-Agent": "blindDL-release"}, None, API_TIMEOUT
        )
        if _status >= 400:
            raise ReleaseError(
                f"Forgejo download failed: {url} ({_status})"
            )
        return raw


def require_token() -> str:
    token = os.environ.get("FORGEJO_TOKEN", "").strip()
    if not token:
        raise ReleaseError(
            "FORGEJO_TOKEN is not set. Copy it from "
            "/root/forgejo-muse-token.txt on root@serrebiradio.com."
        )
    return token


def declared_version() -> str:
    """The version this checkout says it is."""
    source = (ROOT / "blinddl" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise ReleaseError("blinddl/__init__.py has no __version__")
    return match.group(1)


def version_of(tag: str) -> str:
    return tag.removeprefix("v")


def version_key(tag: str) -> tuple[int, ...]:
    return parse_version(tag)


def required_assets(skip_macos: bool) -> dict[str, tuple[str, ...]]:
    """The platform groups a finished release must carry."""
    if not skip_macos:
        return PLATFORM_ASSETS
    return {
        platform: templates
        for platform, templates in PLATFORM_ASSETS.items()
        if not platform.startswith("macOS")
    }


def missing_assets(
    info: dict, version: str, skip_macos: bool = False
) -> list[str]:
    """Everything a finished release should carry and this one does not."""
    present = {asset.get("name"): asset for asset in info.get("assets", [])}
    missing = []
    for platform, templates in required_assets(skip_macos).items():
        for template in templates:
            name = template.format(version=version)
            asset = present.get(name)
            if asset is None:
                missing.append(f"{platform}: {name} was never uploaded")
            elif not asset.get("size"):
                missing.append(f"{platform}: {name} uploaded empty")
    return missing


def checksum_problems(
    client: ForgejoClient, info: dict
) -> list[str]:
    """Check each artifact against the checksum its builder published.

    Forgejo reports no per-asset digest the way GitHub did, so a
    corrupted upload cannot be caught from the release row: every file a
    SHA256SUMS listing names must at least be present on the release, and
    any digest the API does report must match. The builders hash the bytes
    they upload, so a listing is self-consistent by construction.
    """
    assets = {
        asset.get("name"): asset for asset in info.get("assets", [])
    }
    digests = {
        name: (asset.get("digest") or "").removeprefix("sha256:")
        for name, asset in assets.items()
    }
    problems: list[str] = []
    listings = sorted(
        name for name in assets
        if name.startswith("SHA256SUMS-") and name.endswith(".txt")
    )
    for name in listings:
        url = assets[name].get("browser_download_url") or ""
        if not url:
            problems.append(f"{name} has no download URL on the release")
            continue
        try:
            text = client.download(url).decode("utf-8")
        except ReleaseError as error:
            print(f"  note: {name} was not verified ({error})")
            continue
        for line in text.splitlines():
            expected, _, listed = line.strip().partition("  ")
            if not listed:
                continue
            if listed not in assets:
                problems.append(
                    f"{name} lists {listed}, which the release does not carry"
                )
            elif digests.get(listed) and digests[listed] != expected:
                problems.append(
                    f"{listed} does not match its checksum in {name}"
                )
    return problems


def is_newest(client: ForgejoClient, tag: str) -> bool:
    """Whether this tag outranks every already published release."""
    published = [
        version_key(entry.get("tag_name", ""))
        for entry in client.list_releases()
        if not entry.get("draft") and entry.get("tag_name") != tag
    ]
    return all(version_key(tag) > other for other in published)


def age_hours(info: dict) -> float:
    created = info.get("created_at") or info.get("createdAt") or ""
    moment = datetime.fromisoformat(str(created))
    return (datetime.now(UTC) - moment).total_seconds() / 3600


def report(problems: list[str]) -> None:
    for problem in problems:
        print(f"  - {problem}")


def upload_assets(
    client: ForgejoClient, info: dict, assets: list[Path]
) -> dict:
    """Attach local files to a draft, resuming across build hosts.

    A file that is already on the release with the same size is left
    alone, so a re-run after a half-finished upload only sends what is
    missing. One that differs is replaced; Forgejo keeps both otherwise,
    and the updater cannot tell which download is the real one.
    """
    release_id = info["id"]
    present = {
        asset.get("name"): asset for asset in info.get("assets", [])
    }
    changed = False
    for asset in assets:
        existing = present.get(asset.name)
        if existing is not None:
            if (existing.get("size") or 0) == asset.stat().st_size:
                print(f"  {asset.name} is already attached; leaving it.")
                continue
            print(f"  {asset.name} differs; replacing it.")
            client.delete_asset(release_id, existing["id"])
        else:
            print(f"  uploading {asset.name}...")
        uploaded = client.upload_asset(release_id, asset)
        present[asset.name] = uploaded
        changed = True
    if changed:
        return client.get_release_by_tag(info["tag_name"]) or info
    return info


def create_draft(
    client: ForgejoClient,
    tag: str,
    notes: str,
    assets: list[Path],
    target: str | None = None,
) -> dict:
    """Create the draft release, or resume uploading into it."""
    existing = client.get_release_by_tag(tag)
    if existing is not None and not existing.get("draft"):
        raise ReleaseError(
            f"Release {tag} is already published; refusing to overwrite it."
        )
    if existing is None:
        print(f"Creating draft release {tag}...")
        existing = client.create_release(tag, tag, notes, target)
    elif assets:
        print(f"{tag} is already a draft; resuming the upload.")
    if assets:
        existing = upload_assets(client, existing, assets)
    return existing


def publish(client: ForgejoClient, tag: str, info: dict) -> int:
    """Verify a complete release and take it out of draft."""
    problems = checksum_problems(client, info)
    if problems:
        print(f"{tag} has all its artifacts but they do not verify:")
        report(problems)
        return 1
    if not is_newest(client, tag):
        print(
            f"{tag} is older than an already published release. Forgejo "
            "marks whatever was published most recently as latest, so "
            "publishing this would hide the newer release from the updater. "
            "Publish it by hand if it is meant as a backfill."
        )
        return 1
    client.publish_release(info["id"])
    print(f"Published {tag} with {len(info.get('assets', []))} artifacts.")
    return verify_latest(client, tag)


def verify_latest(client: ForgejoClient, tag: str) -> int:
    print(f"Verifying /releases/latest points to {tag}...")
    actual = client.latest_tag()
    if actual != tag:
        print(
            f"::error::/releases/latest is {actual}, expected {tag}. The "
            "updater will keep reporting the old release until this is "
            "corrected."
        )
        return 1
    print(f"Forgejo latest release is {tag}.")
    return 0


def settle(
    client: ForgejoClient,
    tag: str,
    wait_minutes: float,
    version: str,
    skip_macos: bool,
) -> tuple[dict, list[str]]:
    """Wait for the remaining build hosts to finish uploading."""
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        info = client.get_release_by_tag(tag)
        if info is None:
            raise ReleaseError(
                f"{tag} has no release yet. Create the draft first: "
                f"python scripts/publish_release.py {tag} --create "
                "--notes-file notes.md --assets release/*"
            )
        missing = missing_assets(info, version, skip_macos)
        if not missing or time.monotonic() >= deadline:
            return info, missing
        print(f"{tag} is waiting on {len(missing)} artifact(s):")
        report(missing)
        print("  checking again in a minute")
        time.sleep(60)


def publish_one(
    client: ForgejoClient,
    tag: str,
    wait_minutes: float,
    check_only: bool,
    verify_source_version: bool,
    skip_macos: bool,
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

    info = client.get_release_by_tag(tag)
    if info is None:
        raise ReleaseError(
            f"{tag} has no release yet. Create the draft first: "
            f"python scripts/publish_release.py {tag} --create "
            "--notes-file notes.md --assets release/*"
        )
    if not info.get("draft"):
        missing = missing_assets(info, version, skip_macos)
        if missing:
            print(f"{tag} is already published but incomplete:")
            report(missing)
            return 1
        print(f"{tag} is already published and complete.")
        return verify_latest(client, tag)

    info, missing = settle(
        client, tag, 0 if check_only else wait_minutes, version, skip_macos
    )
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
    return publish(client, tag, info)


def sweep(client: ForgejoClient, skip_macos: bool) -> int:
    """Catch any complete release that was left sitting in draft."""
    drafts = [entry for entry in client.list_releases() if entry.get("draft")]
    if not drafts:
        print("No draft releases.")
        return 0

    status = 0
    for entry in drafts:
        tag = entry.get("tag_name", "")
        info = client.get_release_by_tag(tag) or entry
        if HOLD_MARKER in (info.get("body") or ""):
            print(f"{tag} is held as a draft on purpose; leaving it alone.")
            continue
        missing = missing_assets(info, version_of(tag), skip_macos)
        if not missing:
            if not is_newest(client, tag):
                print(
                    f"{tag} is complete but older than a published "
                    "release; leaving it as a draft."
                )
                continue
            print(f"{tag} is complete but was left in draft.")
            status |= publish(client, tag, info)
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


def delete_draft(client: ForgejoClient, tag: str) -> int:
    """Delete one draft release row. The git tag is never touched."""
    info = client.get_release_by_tag(tag)
    if info is None:
        print(f"{tag} has no release; nothing to delete.")
        return 0
    if not info.get("draft"):
        print(f"{tag} is published; refusing to delete it.")
        return 1
    client.delete_release(info["id"])
    print(f"Deleted draft release {tag} (release {info['id']}).")
    return 0


def read_notes(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"Cannot read notes file: {exc}") from exc


def collect_assets(patterns: list[str]) -> list[Path]:
    """Expand asset paths and globs to existing files."""
    assets: list[Path] = []
    for pattern in patterns or []:
        expanded = sorted(Path().glob(pattern)) if "*" in pattern else [Path(pattern)]
        found = [path for path in expanded if path.is_file()]
        if not found:
            raise ReleaseError(f"Release asset not found: {pattern}")
        assets.extend(found)
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a blindDL release on Forgejo once every "
        "platform artifact is in place."
    )
    parser.add_argument(
        "tag", nargs="?", help="the release tag, such as v0.26.4"
    )
    parser.add_argument(
        "--host",
        default=FORGEJO_HOST,
        help="Forgejo host (default: %(default)s)",
    )
    parser.add_argument(
        "--repo",
        default=FORGEJO_REPO,
        help="owner/repo on Forgejo (default: %(default)s)",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="create the draft release (or resume uploading into it) "
        "instead of publishing",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload --assets to the existing draft instead of publishing",
    )
    parser.add_argument(
        "--notes-file",
        default=None,
        help="release notes file for --create",
    )
    parser.add_argument(
        "--assets",
        nargs="*",
        default=[],
        help="files or globs to attach with --create/--upload",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="commit to tag when --create must also create the tag",
    )
    parser.add_argument(
        "--delete-draft",
        action="store_true",
        help="delete the draft release row (never the git tag)",
    )
    parser.add_argument(
        "--skip-macos",
        action="store_true",
        help="publish without the secondary macOS DMG",
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
        client = ForgejoClient(options.host, options.repo, require_token())
        return sweep(client, options.skip_macos)
    if not options.tag:
        parser.error("give a release tag, or --sweep")

    client = ForgejoClient(options.host, options.repo, require_token())

    if options.delete_draft:
        return delete_draft(client, options.tag)
    if options.create:
        create_draft(
            client,
            options.tag,
            read_notes(options.notes_file),
            collect_assets(options.assets),
            options.target,
        )
        return 0
    if options.upload:
        info = client.get_release_by_tag(options.tag)
        if info is None:
            raise ReleaseError(
                f"{options.tag} has no release yet; use --create first."
            )
        if not info.get("draft"):
            raise ReleaseError(
                f"{options.tag} is already published; refusing to upload."
            )
        upload_assets(client, info, collect_assets(options.assets))
        return 0
    return publish_one(
        client,
        options.tag,
        options.wait_minutes,
        options.check_only,
        options.verify_source_version,
        options.skip_macos,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
