# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Packaged builds bootstrap shared native tools without Python or pip."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import zipfile
from pathlib import Path
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


def _helper_arguments(popen):
    """The helper's own arguments, past the cmd.exe that runs it."""
    command = popen.call_args.args[0]
    assert command[1:3] == ["/d", "/c"]
    return command[3:]


def _staged_helper(popen):
    return Path(_helper_arguments(popen)[0])


def test_installed_windows_update_uses_a_silent_restart_helper(tmp_path):
    package = tmp_path / "blindDL-Setup-v9.9.9-windows-x64.exe"
    package.write_bytes(b"installer")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")
    installed = tmp_path / "Program Files" / "blindDL"
    installed.mkdir(parents=True)

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "executable",
                              str(installed / "blindDL.exe")), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    helper = _staged_helper(popen)
    try:
        script = helper.read_text(encoding="ascii")
    finally:
        helper.unlink(missing_ok=True)
    assert "/VERYSILENT" in script
    assert "/SUPPRESSMSGBOXES" in script
    assert "start \"\" /d" in script
    # An installer that exits non-zero, or leaves the old version in place,
    # has to reach the user: it happens after blindDL itself has closed.
    assert "call :save 0" in script
    assert ":verify_version" in script

    mode, pid, install_dir, source, result, version, log, powershell = (
        _helper_arguments(popen)[1:])
    assert mode == "installed"
    assert int(pid) == os.getpid()
    # The folder, not the executable: it is what the installer is pointed at
    # and what the helper watches for processes still running out of it.
    assert Path(install_dir) == installed
    assert Path(source) == package
    assert version == "9.9.9"
    assert Path(result) == tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    assert Path(log).name == updater.WINDOWS_UPDATE_LOG_NAME
    assert powershell == "powershell.exe"


def _portable_update_zip(path, version="9.9.9"):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("blindDL/blindDL.exe", b"the new blindDL")
        package.writestr("blindDL/_internal/python314.dll", b"the new runtime")
    return path


def test_the_portable_update_moves_the_files_and_not_the_folder(tmp_path):
    # Copying the new files over the old ones leaves every file the release
    # dropped behind it -- after a Python upgrade, two runtimes in one folder
    # and a blindDL that was replaced but still starts as it was. Renaming the
    # folder does not have that problem and has a worse one: a sync client or
    # an open Explorer window holds a directory open without holding any file
    # in it, and the rename then fails every single time. So the contents
    # move and the folder stays where it is.
    package = _portable_update_zip(tmp_path / "blindDL-v9.9.9-windows-x64.zip")
    installed = tmp_path / "app"
    installed.mkdir()
    (installed / "blindDL.exe").write_bytes(b"the old blindDL")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "executable",
                              str(installed / "blindDL.exe")), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    helper = _staged_helper(popen)
    try:
        script = helper.read_text(encoding="ascii")
    finally:
        helper.unlink(missing_ok=True)
    assert 'robocopy "%INSTALL_DIR%" "%BACKUP_DIR%" /E /MOVE' in script
    assert 'robocopy "%SOURCE%" "%INSTALL_DIR%" /E /MOVE' in script
    # Nothing renames the folder itself.
    assert "Directory]::Move" not in script
    assert ":rollback" in script

    mode, _pid, install_dir, source, result, version, _log, _ps = (
        _helper_arguments(popen)[1:])
    assert mode == "portable"
    assert Path(install_dir) == installed
    assert Path(source) == package.parent / "portable" / "blindDL"
    assert version == "9.9.9"
    assert Path(result) == tmp_path / "updates" / updater.UPDATE_RESULT_NAME


def test_a_protected_portable_folder_uses_a_uac_helper(tmp_path):
    package = _portable_update_zip(tmp_path / "blindDL-v9.9.9-windows-x64.zip")
    installed = tmp_path / "protected" / "blindDL"
    installed.mkdir(parents=True)
    (installed / "blindDL.exe").write_bytes(b"the old blindDL")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "executable",
                              str(installed / "blindDL.exe")), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_portable_update_needs_elevation",
                              return_value=True), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater, "_start_elevated_windows_helper") as elevated, \
            mock.patch.object(updater.subprocess, "Popen") as ordinary:
        assert updater.install_app_update(update, package)

    elevated.assert_called_once()
    ordinary.assert_not_called()
    shell, arguments = elevated.call_args.args
    assert shell == "cmd.exe"
    assert arguments[:2] == ["/d", "/c"]
    Path(arguments[2]).unlink(missing_ok=True)


def test_the_helper_outlives_the_blinddl_that_started_it(tmp_path):
    # The helper is started as blindDL is closing and does its work after
    # blindDL has gone. A job object that kills its processes on close would
    # take a plain child down with the parent, which is an update that does
    # nothing at all and leaves nothing to say why.
    helper = tmp_path / "helper.bat"
    helper.write_text("@echo off\n", encoding="ascii")

    with mock.patch.object(updater.os, "name", "nt"), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        updater._start_windows_helper(["cmd.exe", "/d", "/c", str(helper)])

    flags = popen.call_args.kwargs["creationflags"]
    assert flags & updater.CREATE_BREAKAWAY_FROM_JOB
    assert flags & updater.CREATE_NEW_PROCESS_GROUP
    assert flags & updater.CREATE_NO_WINDOW


def test_a_job_that_forbids_breakaway_still_gets_a_helper(tmp_path):
    helper = tmp_path / "helper.bat"
    helper.write_text("@echo off\n", encoding="ascii")
    refused = [OSError("breakaway not allowed"), mock.DEFAULT]

    with mock.patch.object(updater.os, "name", "nt"), \
            mock.patch.object(updater.subprocess, "Popen",
                              side_effect=refused) as popen:
        updater._start_windows_helper(["cmd.exe", "/d", "/c", str(helper)])

    assert popen.call_count == 2
    assert not (popen.call_args.kwargs["creationflags"]
                & updater.CREATE_BREAKAWAY_FROM_JOB)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows filesystem semantics")
def test_portable_helper_replaces_a_real_windows_folder(tmp_path):
    """Execute the boundary that text-only updater tests used to miss."""
    powershell = updater._find_windows_powershell()
    shell = updater._find_windows_shell()
    system_exe = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    assert system_exe.is_file()
    version = subprocess.run(
        [
            powershell, "-NoProfile", "-NonInteractive", "-Command",
            "$path = [Console]::In.ReadToEnd(); "
            "(Get-Item -LiteralPath $path).VersionInfo.FileVersion",
        ],
        input=str(system_exe),
        check=True, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    ).stdout.strip()
    assert version

    target = tmp_path / "Portable BlindDL O'Brien"
    source = tmp_path / "stage" / "blindDL"
    (target / "_internal").mkdir(parents=True)
    (source / "_internal").mkdir(parents=True)
    shutil.copy2(system_exe, target / "blindDL.exe")
    shutil.copy2(system_exe, source / "blindDL.exe")
    (target / "_internal" / "old-runtime.dll").write_bytes(b"old")
    (source / "_internal" / "new-runtime.dll").write_bytes(b"new")
    (target / "my portable note.txt").write_text("keep me", encoding="utf-8")
    unrelated = target.with_name(target.name + ".previous")
    unrelated.mkdir()
    (unrelated / "not-an-update.txt").write_text("leave me alone", encoding="utf-8")

    helper = updater._stage_windows_helper()
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "helper.log"
    completed = subprocess.run(
        [
            shell, "/d", "/c", str(helper), "portable", "2147483647",
            str(target), str(source), str(result_path), version,
            str(log_path), powershell,
        ],
        cwd=updater._helper_cwd(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert result["ok"] is True
    assert (target / "_internal" / "new-runtime.dll").read_bytes() == b"new"
    # The swap is a swap: the file the new release does not ship is gone,
    # rather than left behind beside its replacement.
    assert not (target / "_internal" / "old-runtime.dll").exists()
    assert (target / "my portable note.txt").read_text(encoding="utf-8") == "keep me"
    assert (unrelated / "not-an-update.txt").read_text(encoding="utf-8") == (
        "leave me alone"
    )
    assert not list(tmp_path.glob("*.blinddl-update-backup-*"))
    # A helper that finished has nothing left to explain, so it takes its log
    # and itself away.
    assert not log_path.exists()
    assert not helper.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows filesystem semantics")
def test_the_portable_helper_puts_the_old_version_back_when_it_cannot_finish(tmp_path):
    # The half-replaced folder is the one outcome worse than no update: it is
    # a blindDL that will not start, on a machine whose owner asked only for
    # a newer one.
    powershell = updater._find_windows_powershell()
    shell = updater._find_windows_shell()
    system_exe = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    target = tmp_path / "app"
    source = tmp_path / "stage" / "blindDL"
    (target / "_internal").mkdir(parents=True)
    (source / "_internal").mkdir(parents=True)
    shutil.copy2(system_exe, target / "blindDL.exe")
    shutil.copy2(system_exe, source / "blindDL.exe")
    (target / "_internal" / "old-runtime.dll").write_bytes(b"old")

    helper = updater._stage_windows_helper()
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "helper.log"
    completed = subprocess.run(
        [
            shell, "/d", "/c", str(helper), "portable", "2147483647",
            str(target), str(source), str(result_path),
            # A version the staged blindDL.exe cannot possibly be.
            "9999.0.0", str(log_path), powershell,
        ],
        cwd=updater._helper_cwd(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )

    assert completed.returncode == 1
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert result["ok"] is False
    assert "9999.0.0" in result["detail"]
    # Everything that was there is there again.
    assert (target / "blindDL.exe").is_file()
    assert (target / "_internal" / "old-runtime.dll").read_bytes() == b"old"
    # And the log is kept, because this time there is something in it.
    assert log_path.is_file()
    assert log_path.read_text(encoding="utf-8", errors="replace").strip()
    helper.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_installed_helper_executes_and_verifies_the_target(tmp_path):
    powershell = updater._find_windows_powershell()
    shell = updater._find_windows_shell()
    system_exe = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    install_dir = tmp_path / "Installed BlindDL"
    install_dir.mkdir()
    target = install_dir / "blindDL.exe"
    shutil.copy2(system_exe, target)
    version = subprocess.run(
        [
            powershell, "-NoProfile", "-NonInteractive", "-Command",
            "$path = [Console]::In.ReadToEnd(); "
            "(Get-Item -LiteralPath $path).VersionInfo.FileVersion",
        ],
        input=str(target), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    ).stdout.strip()
    installer = tmp_path / "successful installer.cmd"
    installer.write_text("@echo off\nexit /b 0\n", encoding="ascii")
    helper = updater._stage_windows_helper()
    result_path = tmp_path / "installed-result.json"
    log_path = tmp_path / "installed.log"

    completed = subprocess.run(
        [
            shell, "/d", "/c", str(helper), "installed", "2147483647",
            str(install_dir), str(installer), str(result_path), version,
            str(log_path), powershell,
        ],
        cwd=updater._helper_cwd(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert result["ok"] is True
    assert result["version"] == version
    helper.unlink(missing_ok=True)


def test_the_update_helper_does_not_stand_in_its_own_way(tmp_path):
    # A child process inherits blindDL's working directory, and a portable
    # blindDL runs from the folder an update has to empty. Windows holds a
    # directory open for whichever process has it as its current one, so the
    # helper used to arrive already blocking the only thing it was there to
    # do -- on every machine, every time, with nothing else wrong.
    package = _portable_update_zip(tmp_path / "blindDL-v9.9.9-windows-x64.zip")
    installed = tmp_path / "app"
    installed.mkdir()
    (installed / "blindDL.exe").write_bytes(b"the old blindDL")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "executable",
                              str(installed / "blindDL.exe")), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    started_in = Path(popen.call_args.kwargs["cwd"]).resolve()
    assert started_in != installed.resolve()
    assert installed.resolve() not in started_in.parents
    assert started_in.is_dir()
    helper = _staged_helper(popen)
    try:
        script = helper.read_text(encoding="ascii")
    finally:
        helper.unlink(missing_ok=True)
    # And again from inside, for a helper that was started some other way.
    assert 'pushd "%TEMP%"' in script
    # The helper is not in the folder it empties either: a batch file cannot
    # be deleted out from under itself.
    assert installed.resolve() not in helper.resolve().parents


def test_the_installed_helper_also_runs_clear_of_the_install(tmp_path):
    package = tmp_path / "blindDL-Setup-v9.9.9-windows-x64.exe"
    package.write_bytes(b"installer")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")
    installed = tmp_path / "Program Files" / "blindDL"
    installed.mkdir(parents=True)

    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "executable",
                              str(installed / "blindDL.exe")), \
            mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    started_in = Path(popen.call_args.kwargs["cwd"]).resolve()
    assert installed.resolve() not in (started_in, *started_in.parents)
    _staged_helper(popen).unlink(missing_ok=True)


def test_a_read_only_install_folder_is_not_mistaken_for_a_running_blinddl(tmp_path):
    # The helper waits for blindDL to let go of its own executable by opening
    # it for writing. An installed blindDL lives under Program Files, where an
    # unelevated helper may not open anything that way -- and the answer
    # "access denied" is not the answer "still running", which is what it was
    # taken for: five minutes of waiting and then a failure, every time.
    assert "catch [System.UnauthorizedAccessException] { $ok=$true }" in (
        updater._WINDOWS_HELPER)


def test_a_local_deb_is_installed_by_a_path_pkexec_cannot_lose(tmp_path):
    # pkexec runs its program in the target user's home directory, so a
    # package named "./blindDL.deb" is looked for in root's home, where it is
    # not -- and apt-get answers "Unable to locate package" instead.
    package = tmp_path / "blinddl_9.9.9_amd64.deb"
    package.write_bytes(b"package")
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "linux"),             mock.patch.object(updater.shutil, "which",
                              return_value="/usr/bin/pkexec"),             mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    command = popen.call_args.args[0]
    assert command[-1] == str(package.resolve())
    assert os.path.isabs(command[-1])


def test_the_linux_installer_is_told_to_bring_blinddl_back(tmp_path):
    # blindDL closes as soon as the installer starts, so the installer is the
    # only thing left that can start it again.
    package = tmp_path / "blindDL-v9.9.9-linux-x64.tar.gz"
    tree = tmp_path / "blindDL-9.9.9"
    tree.mkdir()
    (tree / "install.sh").write_text("#!/bin/sh" + chr(10))
    with tarfile.open(package, "w:gz") as archive:
        archive.add(tree, arcname=tree.name)
    update = updater.AppUpdate("9.9.9", "", package.name, "", "", "")

    with mock.patch.object(updater.sys, "platform", "linux"),             mock.patch.object(updater.shutil, "which", return_value=None),             mock.patch.object(updater.subprocess, "Popen") as popen:
        assert updater.install_app_update(update, package)

    assert popen.call_args.kwargs["env"]["BLINDDL_RESTART"] == "1"


def test_an_update_that_failed_after_blinddl_closed_is_read_out_once(tmp_path):
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.parent.mkdir(parents=True)
    # PowerShell writes UTF-8 with a byte order mark.
    result.write_text(
        json.dumps({"ok": False, "version": "9.9.9",
                    "detail": "The old blindDL folder is in use."}),
        encoding="utf-8-sig",
    )

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)):
        spoken = updater.last_update_failure()
        assert spoken == ("blindDL 9.9.9 did not install: "
                          "The old blindDL folder is in use.")
        # Said once, on the next start. Not every twelve hours after that.
        assert updater.last_update_failure() is None
    assert not result.exists()


def test_a_helper_that_disappears_before_finishing_is_reported(tmp_path):
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps({"status": "pending", "ok": False, "version": "9.9.9"}),
        encoding="utf-8",
    )

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)):
        spoken = updater.last_update_failure()

    assert "did not report finishing" in spoken
    assert "still downloaded" in spoken


def test_automatic_checks_keep_a_failure_until_a_manual_retry(tmp_path):
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps({
            "status": "complete", "ok": False, "version": "9.9.9",
            "detail": "folder is in use",
        }),
        encoding="utf-8",
    )

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)):
        assert "folder is in use" in updater.last_update_failure(forget=False)
        assert result.is_file(), "automatic checks must remain blocked"
        assert "folder is in use" in updater.last_update_failure()
        assert updater.last_update_failure() is None


def test_a_helper_that_cannot_start_leaves_a_specific_failure(tmp_path):
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_windows_update_hosts",
                              return_value=("cmd.exe", "powershell.exe")), \
            mock.patch.object(updater.subprocess, "Popen",
                              side_effect=OSError("blocked by policy")):
        with pytest.raises(updater.UpdateError, match="could not start"):
            updater._launch_windows_helper(
                "portable", tmp_path / "app", tmp_path / "stage", "9.9.9")
        spoken = updater.last_update_failure()

    assert "blocked by policy" in spoken
    for leftover in Path(tempfile.gettempdir()).glob(
            "blindDL-update-helper-*.bat"):
        leftover.unlink(missing_ok=True)


def test_an_update_that_worked_says_nothing(tmp_path):
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"ok": True, "version": "9.9.9"}),
                      encoding="utf-8")

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)):
        assert updater.last_update_failure() is None


def test_an_update_that_took_gives_its_package_back(tmp_path):
    # The staging folder holds the release package and the tree unpacked from
    # it. Both did their job the moment the update took, and nothing used to
    # clear them until some later release came along to displace them.
    staged = tmp_path / "updates" / "v9.9.9"
    (staged / "portable").mkdir(parents=True)
    (staged / "blindDL-v9.9.9-windows-x64.zip").write_bytes(b"the package")
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.write_text(json.dumps({"ok": True, "version": "9.9.9"}),
                      encoding="utf-8")

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)),             mock.patch.object(updater, "__version__", "9.9.9"):
        assert updater.last_update_failure() is None
    assert not staged.exists()


def test_a_package_for_some_other_version_is_left_alone(tmp_path):
    # An "ok" naming a version this blindDL is not running did not come from
    # this install, and the package it names may still be wanted.
    staged = tmp_path / "updates" / "v9.9.9"
    staged.mkdir(parents=True)
    (staged / "blindDL-v9.9.9-windows-x64.zip").write_bytes(b"the package")
    result = tmp_path / "updates" / updater.UPDATE_RESULT_NAME
    result.write_text(json.dumps({"ok": True, "version": "9.9.9"}),
                      encoding="utf-8")

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)),             mock.patch.object(updater, "__version__", "1.0.0"):
        assert updater.last_update_failure() is None
    assert (staged / "blindDL-v9.9.9-windows-x64.zip").is_file()


def test_a_staged_package_is_not_downloaded_a_second_time(tmp_path):
    # A staged update that never installed leaves its package on disk. Fetching
    # the same hundred-odd megabytes again on every check is the whole cost of
    # an update that keeps not taking.
    payload = b"the release package"
    expected = hashlib.sha256(payload).hexdigest()
    update = updater.AppUpdate(
        version="9.9.9", page_url="", package_name="blindDL-v9.9.9-windows-x64.zip",
        package_url="https://example.invalid/package",
        checksum_name="SHA256SUMS-windows-x64.txt",
        checksum_url="https://example.invalid/checksums",
    )
    staged = tmp_path / "updates" / "v9.9.9"
    staged.mkdir(parents=True)
    (staged / update.package_name).write_bytes(payload)
    fetched = []

    def fake_download(url, destination, digest=None, on_progress=None):
        fetched.append(url)
        destination.write_text(f"{expected}  {update.package_name}\n",
                               encoding="utf-8")
        return ""

    logged = []
    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_download", side_effect=fake_download):
        package = updater.download_app_update(update, logged.append)

    assert package.read_bytes() == payload
    assert fetched == [update.checksum_url], "the package must not be fetched again"
    assert any("already downloaded" in line for line in logged)
    assert expected == hashlib.sha256(package.read_bytes()).hexdigest()


def test_downloading_an_update_clears_out_the_versions_before_it(tmp_path):
    # Every staged version keeps its package and the tree unpacked from it.
    # A machine that updates often was giving up gigabytes to versions it had
    # long since moved past.
    updates = tmp_path / "updates"
    for old_version in ("v9.9.7", "v9.9.8"):
        stale = updates / old_version / "portable" / "blindDL"
        stale.mkdir(parents=True)
        (stale / "blindDL.exe").write_bytes(b"an old release")
    payload = b"the release package"
    update = updater.AppUpdate(
        version="9.9.9", page_url="", package_name="blindDL-v9.9.9-windows-x64.zip",
        package_url="https://example.invalid/package",
        checksum_name="SHA256SUMS-windows-x64.txt",
        checksum_url="https://example.invalid/checksums",
    )

    def fake_download(url, destination, digest=None, on_progress=None):
        if url == update.checksum_url:
            destination.write_text(
                f"{hashlib.sha256(payload).hexdigest()}  {update.package_name}\n",
                encoding="utf-8")
            return ""
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    with mock.patch.object(updater, "app_data_dir", return_value=str(tmp_path)), \
            mock.patch.object(updater, "_download", side_effect=fake_download):
        updater.download_app_update(update)

    assert sorted(p.name for p in updates.glob("v*")) == ["v9.9.9"]


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


def _fake_winreg(install_location):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    module = types.SimpleNamespace(
        HKEY_CURRENT_USER=1,
        HKEY_LOCAL_MACHINE=2,
        KEY_READ=4,
        KEY_WOW64_32KEY=8,
        KEY_WOW64_64KEY=16,
    )

    def open_key(hive, _subkey, _reserved, access):
        if hive == module.HKEY_LOCAL_MACHINE and access & module.KEY_WOW64_64KEY:
            return Key()
        raise FileNotFoundError

    module.OpenKey = open_key
    module.QueryValueEx = lambda _key, _name: (str(install_location), 1)
    return module


def test_inno_registration_selects_the_installer_only_at_its_real_location(tmp_path):
    installed = tmp_path / "Program Files" / "blindDL"
    installed.mkdir(parents=True)
    fake_winreg = _fake_winreg(installed)
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "frozen", True, create=True), \
            mock.patch.object(updater.sys, "executable", str(installed / "blindDL.exe")), \
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}):
        assert updater._windows_installed_build() is True

    portable = tmp_path / "Downloads" / "blindDL"
    portable.mkdir(parents=True)
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.sys, "frozen", True, create=True), \
            mock.patch.object(updater.sys, "executable", str(portable / "blindDL.exe")), \
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}):
        assert updater._windows_installed_build() is False


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


def test_windows_arm_uses_the_published_x64_compatibility_build():
    with mock.patch.object(updater.sys, "platform", "win32"), \
            mock.patch.object(updater.platform, "machine", return_value="ARM64"), \
            mock.patch.object(updater, "_windows_installed_build", return_value=False):
        update = updater._select_update(_windows_release())
    assert update is not None
    assert update.package_name.endswith("windows-x64.zip")
    assert update.checksum_name == "SHA256SUMS-windows-x64.txt"


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


def test_update_transport_rejects_a_redirect_from_https_to_http():
    response = mock.Mock()
    response.geturl.return_value = "http://example.invalid/update.zip"
    with mock.patch.object(updater, "urlopen", return_value=response):
        with pytest.raises(updater.UpdateError, match="redirected"):
            updater._open_url("https://example.invalid/update.zip")
    response.close.assert_called_once_with()


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
