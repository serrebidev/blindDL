# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Regression tests for the updater and packaging fixes in this group."""

import hashlib
import importlib.util
import io
import json
import tarfile
import threading
import tomllib
import traceback
import zipfile
from pathlib import Path
from unittest import mock

import pytest

import blinddl
from blinddl import updater

ROOT = Path(__file__).resolve().parent.parent


def _load_publish_release():
    spec = importlib.util.spec_from_file_location(
        "publish_release_under_test", ROOT / "scripts" / "publish_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_result(tmp_path, result):
    path = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


def _newer_version():
    major, minor, patch = updater._version_tuple(blinddl.__version__)
    return f"{major}.{minor}.{patch + 1}"


def _release(version, names):
    return {
        "tag_name": f"v{version}",
        "html_url": "https://example.invalid/release",
        "assets": [
            {"name": name,
             "browser_download_url": f"https://example.invalid/{name}"}
            for name in names
        ],
    }


@pytest.mark.parametrize("status", ["pending", "complete", "failed", ""])
@pytest.mark.parametrize("ok", [True, False])
def test_any_result_for_the_running_version_is_forgiven(tmp_path, status, ok):
    # U1: running the requested version proves the update took, whatever
    # the helper recorded -- not only for a pending record.
    staged = tmp_path / "updates" / "v9.9.9"
    staged.mkdir(parents=True)
    (staged / "package.zip").write_bytes(b"package")
    result = _write_result(tmp_path, {
        "status": status, "ok": ok, "version": "9.9.9",
        "detail": "the folder does not hold the right version",
    })
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "__version__", "9.9.9"):
        assert updater.last_update_failure(forget=False) is None
    assert not result.exists()
    assert not staged.exists()


def test_a_failure_for_another_version_is_still_reported(tmp_path):
    _write_result(tmp_path, {
        "status": "complete", "ok": False, "version": "9.9.9",
        "detail": "folder is in use",
    })
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "__version__", "1.0.0"):
        assert "folder is in use" in updater.last_update_failure()


def test_windows_helper_verify_retries_reading_file_version():
    # U1: a freshly replaced exe can have no readable FileVersion for a
    # moment (scanner / still settling); one read must not fail the update.
    assert "$attempt -lt 10" in updater._WINDOWS_HELPER
    assert "Start-Sleep -Milliseconds 500" in updater._WINDOWS_HELPER


def test_linux_deb_update_records_a_pending_result_before_closing(tmp_path):
    # U2: BlindDL closes as soon as this returns True, so a later failure
    # must have a record to be read from on the next start.
    package = tmp_path / "blinddl_9.9.9_amd64.deb"
    package.write_bytes(b"package")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater.shutil, "which", return_value="/usr/bin/pkexec"), \
            mock.patch.object(updater.subprocess, "Popen"):
        assert updater.install_app_update(update, package)
    result = json.loads(
        (tmp_path / "updates" / updater.UPDATE_RESULT_NAME).read_text(
            encoding="utf-8"))
    assert result["status"] == "pending"
    assert result["version"] == "9.9.9"
    # Still on the old version: reported. On the new one: forgiven.
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)):
        assert "did not report finishing" in updater.last_update_failure(
            forget=False)
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "__version__", "9.9.9"):
        assert updater.last_update_failure() is None


def test_linux_tarball_update_records_a_pending_result_before_closing(tmp_path):
    package = tmp_path / "blindDL-v9.9.9-linux-x64.tar.gz"
    tree = tmp_path / "blindDL-9.9.9"
    tree.mkdir()
    (tree / "install.sh").write_text("#!/bin/sh\n")
    with tarfile.open(package, "w:gz") as archive:
        archive.add(tree, arcname=tree.name)
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater.subprocess, "Popen"):
        assert updater.install_app_update(update, package)
    result = json.loads(
        (tmp_path / "updates" / updater.UPDATE_RESULT_NAME).read_text(
            encoding="utf-8"))
    assert result["status"] == "pending"
    assert result["version"] == "9.9.9"


def test_concurrent_update_downloads_do_not_share_staging_files(tmp_path):
    payload = b"the release package"
    digest = hashlib.sha256(payload).hexdigest()
    update = updater.AppUpdate("9.9.9", "", "release.zip", "https://x/package",
                               "SHA256SUMS.txt", "https://x/checksums")
    checksum = f"{digest}  release.zip\n"
    ready = threading.Event()
    second_open = threading.Event()
    first_read = threading.Event()
    packages = threading.Barrier(2)
    staged = []
    results, errors = [], []

    def download(url, destination, digest=None, on_progress=None):
        if url == update.checksum_url:
            if threading.current_thread().name == "first-download":
                destination.write_text(checksum, encoding="utf-8")
                ready.set()
                assert second_open.wait(3)
            else:
                assert ready.wait(3)
                with destination.open("w", encoding="utf-8") as output:
                    second_open.set()
                    assert first_read.wait(3)
                    output.write(checksum)
            return ""
        staged.append(destination)
        first_read.set()
        packages.wait(timeout=3)
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def run():
        try:
            results.append(updater.download_app_update(update))
        except (OSError, updater.UpdateError, threading.BrokenBarrierError):
            errors.append(traceback.format_exc())
        finally:
            first_read.set()

    threads = [threading.Thread(target=run, name=name)
               for name in ("first-download", "second-download")]
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), mock.patch.object(
            updater, "_download", side_effect=download):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert all(path.read_bytes() == payload for path in results)
    assert len(set(staged)) == 2
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.tmp"))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, _size=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _deno_zip(payload=b"deno-binary"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("deno", (2026, 1, 1, 0, 0, 0)), payload)
    return buffer.getvalue()


def _install_deno(tmp_path, monkeypatch, checksum):
    archive_bytes = _deno_zip()

    def fake_open_url(url, timeout=30):
        if url == updater.DENO_CHANNEL_URL:
            return _FakeResponse(b"v2.1.0\n")
        if url.endswith(".sha256sum"):
            return _FakeResponse(checksum)
        return _FakeResponse(archive_bytes)

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    logged = []
    with mock.patch.object(updater.platform, "system", return_value="Linux"), \
            mock.patch.object(updater.platform, "machine", return_value="x86_64"), \
            mock.patch.object(updater, "_open_url", side_effect=fake_open_url), \
            mock.patch.object(updater, "_tool_available", return_value=True), \
            mock.patch("blinddl.runtime.prepare_runtime_path", lambda: None):
        ok = updater._install_deno_user(logged.append)
    return ok, logged, archive_bytes


def test_deno_user_install_verifies_the_sha256sum_before_extracting(
        tmp_path, monkeypatch):
    # U4: the matching checksum lets the install through.
    archive_bytes = _deno_zip()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    ok, _logged, _bytes = _install_deno(
        tmp_path, monkeypatch, f"{digest}  deno.zip\n".encode())
    assert ok is True
    assert (tmp_path / ".deno" / "bin" / "deno").read_bytes() == b"deno-binary"


def test_deno_user_install_rejects_a_mismatched_checksum(tmp_path, monkeypatch):
    # U4: a substituted archive is never extracted or made executable.
    ok, logged, _bytes = _install_deno(
        tmp_path, monkeypatch, ("0" * 64 + "  deno.zip\n").encode())
    assert ok is False
    assert not (tmp_path / ".deno" / "bin" / "deno").exists()
    assert not (tmp_path / ".deno" / "bin" / "deno-download.zip").exists()
    assert any("SHA-256" in line for line in logged)


def test_version_is_dynamic_and_agrees_with_the_package():
    # H9: pyproject carries no second copy of the version to drift.
    with open(ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)
    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "blinddl.__version__"}


@pytest.mark.parametrize("value,expected", [
    ("1.2", (1, 2, 0)),
    ("1.2.0", (1, 2, 0)),
    ("v0.26.1", (0, 26, 1)),
    ("0.26.1", (0, 26, 1)),
    ("1.2.3.4", (1, 2, 3)),
    ("v2", (2, 0, 0)),
    ("", (0, 0, 0)),
])
def test_version_parsers_share_semantics(value, expected):
    # L17: updater and release publishing must rank versions identically.
    publish = _load_publish_release()
    assert updater._version_tuple(value) == expected
    assert publish.version_key(value) == expected
    assert blinddl.parse_version(value) == expected
    assert publish.version_key("1.2") == publish.version_key("1.2.0")


def test_debian_linux_update_selects_the_deb():
    # L18
    version = _newer_version()
    release = _release(version, [
        f"blinddl_{version}_amd64.deb",
        f"blindDL-v{version}-linux-x64.tar.gz",
        "SHA256SUMS-linux-x64.txt",
    ])
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater.platform, "machine", return_value="x86_64"), \
            mock.patch.object(updater, "_is_debian_family", return_value=True):
        update = updater._select_update(release)
    assert update.package_name == f"blinddl_{version}_amd64.deb"
    assert update.checksum_name == "SHA256SUMS-linux-x64.txt"


def test_debian_linux_without_a_deb_falls_back_to_the_tarball():
    # L18
    version = _newer_version()
    release = _release(version, [
        f"blindDL-v{version}-linux-x64.tar.gz",
        "SHA256SUMS-linux-x64.txt",
    ])
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater.platform, "machine", return_value="x86_64"), \
            mock.patch.object(updater, "_is_debian_family", return_value=True):
        update = updater._select_update(release)
    assert update.package_name.endswith("linux-x64.tar.gz")


@pytest.mark.parametrize("machine,arch", [("arm64", "arm64"), ("x86_64", "x64")])
def test_macos_update_selects_the_dmg(machine, arch):
    # L18
    version = _newer_version()
    release = _release(version, [
        f"blindDL-v{version}-macos-{arch}.dmg",
        f"SHA256SUMS-macos-{arch}.txt",
    ])
    with mock.patch.object(updater.sys, "platform", "darwin"), \
            mock.patch.object(updater.platform, "machine", return_value=machine):
        update = updater._select_update(release)
    assert update.package_name.endswith(f"macos-{arch}.dmg")
    assert update.checksum_name == f"SHA256SUMS-macos-{arch}.txt"
