# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Packaged builds bootstrap shared native tools without Python or pip."""

import os
import stat
import zipfile
from unittest import mock

import pytest

from blinddl import __version__, torrent_engine, updater


@pytest.fixture(autouse=True)
def _forget_install_attempts():
    """Each test starts with nothing tried yet.

    The installer remembers what it has already failed to install, so it
    does not spawn a package manager per search for the rest of the session.
    That memory is process-wide, and these tests share a process.
    """
    updater._install_attempted.clear()
    yield
    updater._install_attempted.clear()


def _newer_version():
    """One patch above this build.

    The updater only offers a release that outranks what is running, so a
    fixture pinned to a literal version silently stops testing anything the
    moment that version ships - which is exactly what happened at 0.8.0.
    """
    major, minor, patch = updater._version_tuple(__version__)
    return f"{major}.{minor}.{patch + 1}"


def test_repeated_tool_checks_do_not_lengthen_path(monkeypatch, tmp_path):
    # Every music and YouTube search asks whether Deno and Node are there
    # yet. Each ask used to prepend the same directories again, until the
    # environment handed to yt-dlp and ffmpeg no longer fitted in the
    # 32,767 characters Windows allows.
    from blinddl import runtime

    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setattr(runtime, "_ON_PATH", set())
    monkeypatch.setenv("PATH", "C:\\Windows" if os.name == "nt" else "/usr/bin")
    with mock.patch.object(runtime.Path, "is_dir", return_value=True):
        runtime.prepare_runtime_path()
        after_first = os.environ["PATH"]
        runtime.prepare_runtime_path()
        runtime.prepare_runtime_path()

    assert os.environ["PATH"] == after_first


def test_a_tool_that_will_not_install_is_not_retried_every_search():
    # A package manager that could not install Deno once will not manage it
    # because the user searched again -- and each attempt is a heavy process
    # that resolves a manifest over the network before failing.
    wanted = ("DenoLand.Deno",)
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "missing_external_tools",
                              return_value=list(wanted)), \
            mock.patch.object(updater, "_find_winget",
                              return_value="winget.exe"), \
            mock.patch.object(updater, "_run", return_value=False) as run:
        assert updater.ensure_external_tools(lambda _line: None, wanted) is False
        assert updater.ensure_external_tools(lambda _line: None, wanted) is False

    assert run.call_count == 1


def test_an_update_check_gives_a_failed_install_another_go():
    updater._install_attempted.add("DenoLand.Deno")
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "ensure_external_tools",
                              return_value=True), \
            mock.patch.object(updater, "_find_winget", return_value="winget.exe"), \
            mock.patch.object(updater, "_run"):
        updater.update_winget_packages(lambda _line: None)

    assert not updater._install_attempted


def test_frozen_tool_updates_use_winget():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "ensure_external_tools", return_value=True), \
            mock.patch.object(updater, "_find_winget", return_value="winget.exe"), \
            mock.patch.object(updater, "_run") as run:
        updater.update_winget_packages(lambda _line: None)
    assert run.call_count == len(updater.WINGET_PACKAGES)
    assert all(call.args[0][1] == "upgrade" for call in run.call_args_list)


def test_frozen_missing_deno_is_installed_with_winget():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "_tool_available", return_value=False), \
            mock.patch.object(updater, "ensure_external_tools", return_value=True) as ensure:
        assert updater.ensure_deno(lambda _line: None) is True
    ensure.assert_called_once_with(mock.ANY, ("DenoLand.Deno",))


def test_missing_external_tools_are_installed_silently_with_winget():
    wanted = ("DenoLand.Deno", "Gyan.FFmpeg.Essentials")
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), []]), \
            mock.patch.object(updater, "_find_winget",
                              return_value="winget.exe"), \
            mock.patch.object(updater, "_run", return_value=True) as run:
        assert updater.ensure_external_tools(lambda _line: None, wanted)

    assert run.call_count == 2
    for call, package_id in zip(run.call_args_list, wanted, strict=True):
        command = call.args[0]
        assert command[:2] == ["winget.exe", "install"]
        assert package_id in command
        assert "--silent" in command
        assert "--disable-interactivity" in command


def test_install_progress_names_each_tool_as_it_starts_and_finishes():
    # What the first-run window shows and speaks. WinGet's own output goes
    # to the log instead: it is not something a screen reader can sit through.
    wanted = ("VideoLAN.VLC",)
    spoken = []
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), [], []]), \
            mock.patch.object(updater, "_find_winget",
                              return_value="winget.exe"), \
            mock.patch.object(updater, "_run", return_value=True):
        assert updater.ensure_external_tools(
            lambda _line: None, wanted, progress=spoken.append)

    assert spoken == [
        "Installing VLC media player (audio preview). This can take a few minutes.",
        "VLC media player (audio preview) installed.",
    ]


def test_install_progress_says_when_a_tool_did_not_arrive():
    wanted = ("VideoLAN.VLC",)
    spoken = []
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), list(wanted),
                                           list(wanted)]), \
            mock.patch.object(updater, "_find_winget",
                              return_value="winget.exe"), \
            mock.patch.object(updater, "_run", return_value=False):
        assert updater.ensure_external_tools(
            lambda _line: None, wanted, progress=spoken.append) is False

    assert spoken[-1] == "VLC media player (audio preview) could not be installed."


def test_missing_macos_tools_are_installed_with_homebrew():
    wanted = ("Gyan.FFmpeg.Essentials", "VideoLAN.VLC")
    with mock.patch.object(updater.sys, "platform", "darwin"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), []]), \
            mock.patch.object(updater, "_find_brew",
                              return_value="/opt/homebrew/bin/brew"), \
            mock.patch.object(updater, "_run", return_value=True) as run:
        assert updater.ensure_external_tools(lambda _line: None, wanted)

    assert run.call_args_list[0].args[0] == [
        "/opt/homebrew/bin/brew", "install", "ffmpeg"
    ]
    assert run.call_args_list[1].args[0] == [
        "/opt/homebrew/bin/brew", "install", "--cask", "vlc"
    ]


def test_missing_linux_tools_are_installed_in_one_package_manager_call():
    wanted = ("Gyan.FFmpeg.Essentials", "OpenJS.NodeJS.LTS", "VideoLAN.VLC")
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), []]), \
            mock.patch.object(updater, "_find_linux_package_manager",
                              return_value=("apt-get", "/usr/bin/apt-get")), \
            mock.patch.object(updater, "_linux_elevation",
                              return_value=["/usr/bin/pkexec"]), \
            mock.patch.object(updater, "_run", return_value=True) as run:
        assert updater.ensure_external_tools(lambda _line: None, wanted)

    assert run.call_args_list[0].args[0] == [
        "/usr/bin/pkexec", "/usr/bin/apt-get", "update"
    ]
    install = run.call_args_list[1].args[0]
    assert install[:5] == [
        "/usr/bin/pkexec", "/usr/bin/apt-get", "install", "-y",
        "--no-install-recommends",
    ]
    assert install[5:] == ["ffmpeg", "nodejs", "vlc"]


def test_macos_deno_falls_back_to_a_user_install_without_homebrew():
    wanted = ("DenoLand.Deno",)
    with mock.patch.object(updater.sys, "platform", "darwin"), \
            mock.patch.object(updater, "missing_external_tools",
                              side_effect=[list(wanted), []]), \
            mock.patch.object(updater, "_find_brew", return_value=None), \
            mock.patch.object(updater, "_install_deno_user",
                              return_value=True) as install:
        assert updater.ensure_external_tools(lambda _line: None, wanted)
    install.assert_called_once()


def test_installed_windows_update_uses_a_silent_restart_helper(tmp_path):
    package = tmp_path / "blindDL-Setup-v9.9.9-windows-x64.exe"
    package.write_bytes(b"installer")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    helper = tmp_path / "finish-installed-update.ps1"
    script = helper.read_text(encoding="utf-8")
    assert "/VERYSILENT" in script
    assert "/SUPPRESSMSGBOXES" in script
    assert "Start-Process -FilePath $Target" in script
    assert "powershell.exe" in popen.call_args.args[0][0]


def test_frozen_missing_libtorrent_never_asks_for_python_or_pip():
    with mock.patch.object(torrent_engine.sys, "frozen", True, create=True):
        hint = torrent_engine.install_hint()
    assert "Reinstall or update blindDL" in hint
    assert "pip are not required" in hint
    assert "earlier Python" not in hint


def _windows_release():
    version = _newer_version()
    assets = [
        f"blindDL-Setup-v{version}-windows-x64.exe",
        f"blindDL-v{version}-windows-x64.zip",
        "SHA256SUMS-windows-x64.txt",
    ]
    return {
        "tag_name": f"v{version}",
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
    assert update is not None, "a newer release must be offered as an update"
    assert update.package_name.endswith("windows-x64.zip")
    assert update.checksum_name == "SHA256SUMS-windows-x64.txt"


def test_installed_windows_update_selects_the_installer():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.platform, "machine", return_value="AMD64"), \
            mock.patch.object(updater, "_windows_installed_build", return_value=True):
        update = updater._select_update(_windows_release())
    assert update is not None, "a newer release must be offered as an update"
    assert update.package_name.endswith("windows-x64.exe")


def test_current_release_does_not_offer_a_downgrade():
    release = _windows_release()
    release["tag_name"] = "v0.5.0"
    assert updater._select_update(release) is None


def test_non_debian_linux_update_selects_the_portable_tarball():
    version = _newer_version()
    assets = [
        f"blinddl_{version}_amd64.deb",
        f"blindDL-v{version}-linux-x64.tar.gz",
        "SHA256SUMS-linux-x64.txt",
    ]
    release = {
        "tag_name": f"v{version}",
        "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{name}"}
            for name in assets
        ],
    }
    with mock.patch.object(updater.sys, "platform", "linux"), \
            mock.patch.object(updater.platform, "machine", return_value="x86_64"), \
            mock.patch.object(updater, "_is_debian_family", return_value=False):
        update = updater._select_update(release)
    assert update is not None, "a newer release must be offered as an update"
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

    def fake_download(url, destination, digest=None, on_progress=None):
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


class _FakeResponse:
    """Just enough of an HTTP response for _download to read."""

    def __init__(self, payload, length=True):
        self._payload = payload
        self._offset = 0
        self.headers = {"Content-Length": str(len(payload))} if length else {}

    def read(self, size):
        block = self._payload[self._offset:self._offset + size]
        self._offset += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_a_download_reports_its_percentage_as_it_goes(tmp_path):
    # A download that says nothing cannot be told from one that has stalled.
    payload = b"x" * (updater.DOWNLOAD_BLOCK * 20)
    lines = []
    with mock.patch.object(updater, "_open_url",
                           return_value=_FakeResponse(payload)):
        updater._download(
            "https://example.invalid/package", tmp_path / "package.bin",
            on_progress=updater._progress_reporter("blindDL 9.9.9", lines.append),
        )

    percentages = [int(line.split(":")[1].split()[0]) for line in lines]
    assert percentages == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert lines[-1] == "blindDL 9.9.9: 100 percent of 5.0 MB."


def test_a_download_of_unknown_size_reports_megabytes(tmp_path):
    payload = b"x" * (updater.PROGRESS_BYTES_STEP * 2)
    lines = []
    with mock.patch.object(updater, "_open_url",
                           return_value=_FakeResponse(payload, length=False)):
        updater._download(
            "https://example.invalid/package", tmp_path / "package.bin",
            on_progress=updater._progress_reporter("blindDL 9.9.9", lines.append),
        )

    assert lines == ["blindDL 9.9.9: 16 MB downloaded.",
                     "blindDL 9.9.9: 32 MB downloaded."]


def test_progress_lines_are_spoken_and_the_rest_is_only_logged(tmp_path):
    update = updater.AppUpdate(
        version="9.9.9",
        page_url="https://example.invalid/release",
        package_name="blindDL-v9.9.9-windows-x64.zip",
        package_url="https://example.invalid/package",
        checksum_name="SHA256SUMS-windows-x64.txt",
        checksum_url="https://example.invalid/checksums",
    )
    spoken, logged = [], []

    def fake_download(url, destination, digest=None, on_progress=None):
        if url == update.checksum_url:
            destination.write_text(
                "0" * 64 + f"  {update.package_name}\n", encoding="utf-8")
            return ""
        destination.write_bytes(b"package")
        assert on_progress is not None, "the package download must report progress"
        on_progress(50 * 1024 * 1024, 100 * 1024 * 1024)
        return "0" * 64

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_download", side_effect=fake_download):
        updater.download_app_update(update, logged.append, progress=spoken.append)

    assert spoken == ["blindDL 9.9.9: 50 percent of 100 MB."]
    assert not any("percent" in line for line in logged)


def test_update_transport_rejects_non_https_urls():
    with pytest.raises(updater.UpdateError, match="non-HTTPS"):
        updater._open_url("file:///tmp/blinddl-update.zip")


def test_portable_update_rejects_parent_directory_paths(tmp_path):
    archive = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside.exe", b"unsafe")

    with pytest.raises(updater.UpdateError, match="unsafe path"):
        updater._safe_extract_zip(archive, tmp_path / "destination")

    assert not (tmp_path / "outside.exe").exists()


def test_portable_update_rejects_symbolic_links(tmp_path):
    archive = tmp_path / "link.zip"
    link = zipfile.ZipInfo("blindDL/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(link, "../outside.exe")

    with pytest.raises(updater.UpdateError, match="unsafe link"):
        updater._safe_extract_zip(archive, tmp_path / "destination")
