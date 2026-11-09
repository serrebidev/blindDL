# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT
"""Tests for the Forgejo release publisher (scripts/publish_release.py)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import publish_release


def _asset(name, size=10, **extra):
    asset = {
        "id": abs(hash(name)) % 100000,
        "name": name,
        "size": size,
        "browser_download_url": f"https://forgejo.invalid/dl/{name}",
    }
    asset.update(extra)
    return asset


def _release(tag="v1.2.3", draft=True, assets=(), **extra):
    info = {
        "id": 42,
        "tag_name": tag,
        "draft": draft,
        "body": "",
        "assets": list(assets),
        "created_at": datetime.now(UTC).isoformat(),
    }
    info.update(extra)
    return info


def _full_assets(version):
    names = []
    for templates in publish_release.PLATFORM_ASSETS.values():
        names.extend(template.format(version=version) for template in templates)
    return [_asset(name) for name in names]


class _FakeHttp:
    """Scripted transport: {(method, path): [(status, body), ...]}."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body, timeout=30):
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        key = (method, path.split("/api/v1/repos/serrebi/blindDL")[-1])
        self.calls.append((method, key[1], headers))
        responses = self.routes.get(key, [])
        if not responses:
            return 404, b'{"message": "not found"}'
        status, body = responses.pop(0)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        return status, body


def _client(routes, token="token"):
    http = _FakeHttp(routes)
    return publish_release.ForgejoClient(
        "forgejo.invalid", "serrebi/blindDL", token, http=http
    ), http


def test_version_helpers():
    assert publish_release.version_of("v1.2.3") == "1.2.3"
    assert publish_release.version_of("1.2.3") == "1.2.3"
    assert publish_release.version_key("v1.2.3") == (1, 2, 3)


def test_required_assets_without_macos():
    groups = publish_release.required_assets(skip_macos=True)
    assert all(not name.startswith("macOS") for name in groups)
    assert len(groups) == len(publish_release.PLATFORM_ASSETS) - 1


def test_missing_assets_empty_when_complete():
    info = _release(assets=_full_assets("1.2.3"))
    assert publish_release.missing_assets(info, "1.2.3") == []


def test_missing_assets_names_host_and_file():
    info = _release(assets=[])
    missing = publish_release.missing_assets(info, "1.2.3")
    assert len(missing) == 8
    assert any("Windows x64" in line and "Setup" in line for line in missing)
    assert any("Linux x64" in line and ".deb" in line for line in missing)


def test_missing_assets_flags_empty_upload():
    assets = _full_assets("1.2.3")
    assets[0] = _asset(assets[0]["name"], size=0)
    missing = publish_release.missing_assets(_release(assets=assets), "1.2.3")
    assert missing == [f"Windows x64 (build.bat): {assets[0]['name']} uploaded empty"]


def test_missing_assets_skip_macos():
    assets = [a for a in _full_assets("1.2.3") if "macos" not in a["name"]]
    assert publish_release.missing_assets(_release(assets=assets), "1.2.3", True) == []
    assert len(publish_release.missing_assets(_release(assets=assets), "1.2.3")) == 2


class _DownloadClient:
    def __init__(self, files):
        self.files = files

    def download(self, url):
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise publish_release.ReleaseError("gone")
        return self.files[name]


def _listing(names):
    return "".join(f"{'0' * 64}  {name}\n" for name in names).encode()


def test_checksum_problems_ok_when_listings_match_assets():
    assets = _full_assets("1.2.3")
    listings = [a["name"] for a in assets if a["name"].startswith("SHA256SUMS-")]
    package = next(a["name"] for a in assets if not a["name"].startswith("SHA256SUMS-"))
    # Every listing names a real artifact, which exists.
    files = {name: _listing([package]) for name in listings}
    client = _DownloadClient(files)
    assert publish_release.checksum_problems(client, _release(assets=assets)) == []


def test_checksum_problems_flags_listed_file_missing_from_release():
    package = "blindDL-v1.2.3-windows-x64.zip"
    assets = [_asset("SHA256SUMS-windows-x64.txt")]
    client = _DownloadClient({"SHA256SUMS-windows-x64.txt": _listing([package])})
    problems = publish_release.checksum_problems(client, _release(assets=assets))
    assert problems == [
        (
            f"SHA256SUMS-windows-x64.txt lists {package}, "
            "which the release does not carry"
        )
    ]


def test_checksum_problems_compares_digest_when_api_reports_one():
    package = "blindDL-v1.2.3-windows-x64.zip"
    assets = [
        _asset(package, digest="sha256:" + "ab" * 32),
        _asset("SHA256SUMS-windows-x64.txt"),
    ]
    client = _DownloadClient({"SHA256SUMS-windows-x64.txt": _listing([package])})
    problems = publish_release.checksum_problems(client, _release(assets=assets))
    assert problems == [
        f"{package} does not match its checksum in SHA256SUMS-windows-x64.txt"
    ]


def test_checksum_problems_skips_undownloadable_listing(capsys):
    assets = [_asset("SHA256SUMS-windows-x64.txt")]
    client = _DownloadClient({})
    assert publish_release.checksum_problems(client, _release(assets=assets)) == []
    assert "not verified" in capsys.readouterr().out


def test_is_newest_compares_published_releases():
    client, _http = _client({
        ("GET", "/releases"): [(200, [
            _release("v1.2.2", draft=False),
            _release("v9.9.9", draft=True),
        ])],
    })
    assert publish_release.is_newest(client, "v1.2.3") is True
    client, _http = _client({
        ("GET", "/releases"): [(200, [_release("v1.2.4", draft=False)])],
    })
    assert publish_release.is_newest(client, "v1.2.3") is False


def test_age_hours_reads_forgejo_timestamp():
    created = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    assert publish_release.age_hours(_release(created_at=created)) >= 30


def test_client_returns_none_on_404_when_allowed():
    client, _http = _client({})
    assert client.get_release_by_tag("v0.0.0") is None


def test_client_raises_api_error_with_message():
    client, _http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(401, {"message": "bad token"})],
    })
    with pytest.raises(publish_release.ReleaseError, match="401.*bad token"):
        client.get_release_by_tag("v1.2.3")


def test_client_raises_on_invalid_json():
    client, _http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(200, b"not json")],
    })
    with pytest.raises(publish_release.ReleaseError, match="invalid JSON"):
        client.get_release_by_tag("v1.2.3")


def test_create_draft_refuses_published_release():
    client, _http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(200, _release(draft=False))],
    })
    with pytest.raises(publish_release.ReleaseError, match="already published"):
        publish_release.create_draft(client, "v1.2.3", "", [])


def test_create_draft_resumes_existing_draft(tmp_path, capsys):
    package = tmp_path / "blindDL-v1.2.3-windows-x64.zip"
    package.write_bytes(b"package")
    draft = _release(assets=[])
    uploaded = _asset(package.name, size=7)
    client, http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(200, draft), (200, _release(assets=[uploaded]))],
        ("POST", "/releases/42/assets"): [(200, uploaded)],
    })
    result = publish_release.create_draft(client, "v1.2.3", "", [package])
    assert result["assets"] == [uploaded]
    assert "resuming" in capsys.readouterr().out
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "/releases/42/assets"


def test_upload_assets_skips_identical_file(tmp_path, capsys):
    package = tmp_path / "a.zip"
    package.write_bytes(b"12345")
    info = _release(assets=[_asset("a.zip", size=5)])
    client, http = _client({})
    publish_release.upload_assets(client, info, [package])
    assert "already attached" in capsys.readouterr().out
    assert http.calls == []


def test_upload_assets_replaces_differing_file(tmp_path):
    package = tmp_path / "a.zip"
    package.write_bytes(b"12345")
    old = _asset("a.zip", size=9)
    new = _asset("a.zip", size=5)
    client, http = _client({
        ("DELETE", f"/releases/42/assets/{old['id']}"): [(204, b"")],
        ("POST", "/releases/42/assets"): [(200, new)],
        ("GET", "/releases/tags/v1.2.3"): [(200, _release(assets=[new]))],
    })
    publish_release.upload_assets(client, _release(assets=[old]), [package])
    methods = [(method, path) for method, path, _headers in http.calls]
    assert methods[0] == ("DELETE", f"/releases/42/assets/{old['id']}")
    assert methods[1] == ("POST", "/releases/42/assets")


def test_download_sends_token_for_draft_assets():
    client, http = _client({})
    try:
        client.download("https://forgejo.invalid/dl/SHA256SUMS-x.txt")
    except publish_release.ReleaseError:
        pass
    _method, _path, headers = http.calls[-1]
    assert headers["Authorization"] == "token token"


def test_delete_draft_only_deletes_drafts():
    client, _http = _client({
        ("GET", "/releases/tags/v9.9.9"): [(404, b"{}")],
    })
    assert publish_release.delete_draft(client, "v9.9.9") == 0
    client, _http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(200, _release(draft=False))],
    })
    assert publish_release.delete_draft(client, "v1.2.3") == 1
    client, _http = _client({
        ("GET", "/releases/tags/v1.2.3"): [(200, _release(draft=True))],
        ("DELETE", "/releases/42"): [(204, b"")],
    })
    assert publish_release.delete_draft(client, "v1.2.3") == 0


def test_collect_assets_expands_globs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.zip").write_text("a")
    (tmp_path / "b.zip").write_text("b")
    assert publish_release.collect_assets(["*.zip"]) == [
        Path("a.zip"), Path("b.zip")
    ]
    with pytest.raises(publish_release.ReleaseError, match="not found"):
        publish_release.collect_assets(["missing.zip"])


def test_multipart_stream_round_trips_file(tmp_path):
    package = tmp_path / "a.zip"
    package.write_bytes(b"0123456789")
    stream = publish_release._MultipartStream("attachment", package, "application/octet-stream")
    try:
        raw = stream.read()
        while True:
            chunk = stream.read(3)
            if not chunk:
                break
            raw += chunk
        assert len(raw) == stream.length
        assert raw.startswith(b"------blinddlforgejo")
        assert b'filename="a.zip"' in raw
        assert b"0123456789" in raw
        assert raw.endswith(b"--\r\n")
        assert "multipart/form-data" in stream.content_type
    finally:
        stream.close()


def test_sweep_reports_no_drafts(capsys):
    client, _http = _client({("GET", "/releases"): [(200, [])]})
    assert publish_release.sweep(client, skip_macos=True) == 0
    assert "No draft releases" in capsys.readouterr().out


def test_sweep_leaves_held_drafts_alone(capsys):
    draft = _release(body="<!-- no-autopublish -->", assets=[])
    client, _http = _client({
        ("GET", "/releases"): [(200, [draft])],
        ("GET", "/releases/tags/v1.2.3"): [(200, draft)],
    })
    assert publish_release.sweep(client, skip_macos=True) == 0
    assert "on purpose" in capsys.readouterr().out


def test_sweep_fails_loudly_on_stale_incomplete_draft():
    created = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    draft = _release(assets=[], created_at=created)
    client, _http = _client({
        ("GET", "/releases"): [(200, [draft])],
        ("GET", "/releases/tags/v1.2.3"): [(200, draft)],
    })
    assert publish_release.sweep(client, skip_macos=True) == 1
