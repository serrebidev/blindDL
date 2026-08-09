# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Packaged builds must never depend on user-installed developer tools."""

from unittest import mock

import pytest

from blinddl import torrent_engine, updater


def test_frozen_tool_updates_do_not_invoke_a_system_package_manager():
    lines = []
    with mock.patch.object(updater.sys, "frozen", True, create=True), \
            mock.patch.object(updater.subprocess, "run") as run:
        updater.update_winget_packages(lines.append)
    run.assert_not_called()
    assert "built into blindDL" in lines[-1]


def test_frozen_missing_deno_requests_reinstall_not_winget():
    lines = []
    with mock.patch.object(updater.sys, "frozen", True, create=True), \
            mock.patch.object(updater.shutil, "which", return_value=None), \
            mock.patch.object(updater, "_run") as run:
        assert updater.ensure_deno(lines.append) is False
    run.assert_not_called()
    assert "Reinstall or update blindDL" in lines[-1]


def test_frozen_missing_libtorrent_never_asks_for_python_or_pip():
    with mock.patch.object(torrent_engine.sys, "frozen", True, create=True):
        hint = torrent_engine.install_hint()
    assert "Reinstall or update blindDL" in hint
    assert "pip are not required" in hint
    assert "earlier Python" not in hint


def _windows_release():
    assets = [
        "blindDL-Setup-v0.8.0-windows-x64.exe",
        "blindDL-v0.8.0-windows-x64.zip",
        "SHA256SUMS-windows-x64.txt",
    ]
    return {
        "tag_name": "v0.8.0",
        "html_url": "https://example.invalid/release",
        "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{name}"}
            for name in assets
        ],
    }


def test_portable_windows_update_selects_the_zip():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.platform, "machine", return_value="AMD64"), \
            mock.patch.object(updater, "_windows_installed_build", return_value=False):
        update = updater._select_update(_windows_release())
    assert update.package_name.endswith("windows-x64.zip")
    assert update.checksum_name == "SHA256SUMS-windows-x64.txt"


def test_installed_windows_update_selects_the_installer():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.platform, "machine", return_value="AMD64"), \
            mock.patch.object(updater, "_windows_installed_build", return_value=True):
        update = updater._select_update(_windows_release())
    assert update.package_name.endswith("windows-x64.exe")


def test_current_release_does_not_offer_a_downgrade():
    release = _windows_release()
    release["tag_name"] = "v0.5.0"
    assert updater._select_update(release) is None


def test_non_debian_linux_update_selects_the_portable_tarball():
    assets = [
        "blinddl_0.8.0_amd64.deb",
        "blindDL-v0.8.0-linux-x64.tar.gz",
        "SHA256SUMS-linux-x64.txt",
    ]
    release = {
        "tag_name": "v0.8.0",
        "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{name}"}
            for name in assets
        ],
    }
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater.platform, "machine", return_value="x86_64"), \
            mock.patch.object(updater, "_is_debian_family", return_value=False):
        update = updater._select_update(release)
    assert update.package_name.endswith("linux-x64.tar.gz")


def test_download_with_a_bad_checksum_is_deleted(tmp_path):
    update = updater.AppUpdate(
        version="0.7.0",
        page_url="https://example.invalid/release",
        package_name="blindDL-v0.7.0-windows-x64.zip",
        package_url="https://example.invalid/package",
        checksum_name="SHA256SUMS-windows-x64.txt",
        checksum_url="https://example.invalid/checksums",
    )

    def fake_download(url, destination, digest=None):
        if url == update.checksum_url:
            destination.write_text(
                "0" * 64 + f"  {update.package_name}\n", encoding="utf-8"
            )
            return ""
        destination.write_bytes(b"tampered")
        return "1" * 64

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_download", side_effect=fake_download):
        with pytest.raises(updater.UpdateError, match="SHA-256"):
            updater.download_app_update(update)

    assert not list(tmp_path.rglob("*.part"))
