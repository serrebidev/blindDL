# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

from types import SimpleNamespace
from unittest import mock

import pytest

from tools import build_release


def test_release_build_uses_an_importable_libtorrent_without_pip():
    module = SimpleNamespace(__version__="2.1.1.0")
    with mock.patch.object(
        build_release.importlib, "import_module", return_value=module
    ), mock.patch.object(build_release, "run") as run:
        assert build_release.ensure_libtorrent() == "2.1.1.0"
    run.assert_not_called()


def test_windows_release_policy_reinstalls_the_maintained_wheel(
    tmp_path, monkeypatch
):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    monkeypatch.setenv("BLINDDL_REQUIRE_LIBTORRENT_WHEEL", "1")
    monkeypatch.setenv("BLINDDL_LIBTORRENT_WHEELHOUSE", str(wheelhouse))
    module = SimpleNamespace(__version__="2.1.1.0")
    with mock.patch.object(
        build_release.importlib, "import_module", return_value=module
    ) as import_module, mock.patch.object(
        build_release.importlib, "invalidate_caches"
    ), mock.patch.object(build_release, "run") as run:
        assert build_release.ensure_libtorrent() == "2.1.1.0"

    run.assert_called_once_with(
        build_release.sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--force-reinstall",
        "libtorrent>=2.1.1",
    )
    import_module.assert_called_once_with("libtorrent")


def test_linux_frozen_verification_cannot_find_a_system_python(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("PYTHONHOME", "/developer/python")
    monkeypatch.setenv("PYTHONPATH", "/developer/packages")
    with mock.patch.object(build_release.sys, "platform", "linux"):
        environment = build_release.isolated_runtime_environment()

    assert environment["PATH"] == ""
    assert "PYTHONHOME" not in environment
    assert "PYTHONPATH" not in environment


def test_a_busy_disk_image_is_retried_instead_of_failing_the_release(tmp_path):
    # "Resource busy" from hdiutil is Spotlight holding the bundle, not a
    # broken build. Failing on it threw away every other platform's packages.
    app = tmp_path / "blindDL.app"
    dmg = tmp_path / "blindDL.dmg"
    results = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
    with mock.patch.object(build_release.subprocess, "run",
                           side_effect=results) as run, \
            mock.patch.object(build_release, "detach_stale_volume") as detach, \
            mock.patch.object(build_release.time, "sleep") as sleep:
        build_release.create_dmg(app, dmg)

    assert run.call_count == 2
    assert run.call_args_list[0].args[0][:2] == ("hdiutil", "create")
    detach.assert_called_once_with()
    sleep.assert_called_once_with(15)


def test_a_disk_image_that_never_builds_still_fails_the_release(tmp_path):
    app = tmp_path / "blindDL.app"
    dmg = tmp_path / "blindDL.dmg"
    with mock.patch.object(build_release.subprocess, "run",
                           return_value=SimpleNamespace(returncode=1)) as run, \
            mock.patch.object(build_release, "detach_stale_volume"), \
            mock.patch.object(build_release.time, "sleep"), \
            pytest.raises(RuntimeError, match="in 4 attempts"):
        build_release.create_dmg(app, dmg)

    assert run.call_count == 4


def test_release_build_explains_a_missing_python314_wheelhouse(
    tmp_path, monkeypatch
):
    wheelhouse = tmp_path / "missing"
    monkeypatch.setenv("BLINDDL_LIBTORRENT_WHEELHOUSE", str(wheelhouse))
    with mock.patch.object(
        build_release.importlib,
        "import_module",
        side_effect=ImportError("missing"),
    ), pytest.raises(RuntimeError, match="Run the platform libtorrent updater"):
        build_release.ensure_libtorrent()


def test_release_build_installs_and_rechecks_the_local_wheelhouse(
    tmp_path, monkeypatch
):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    monkeypatch.setenv("BLINDDL_LIBTORRENT_WHEELHOUSE", str(wheelhouse))
    module = SimpleNamespace(__version__="2.1.1.0")
    with mock.patch.object(
        build_release.importlib,
        "import_module",
        side_effect=[ImportError("missing"), module],
    ), mock.patch.object(build_release.importlib, "invalidate_caches"), \
            mock.patch.object(build_release, "run") as run:
        assert build_release.ensure_libtorrent() == "2.1.1.0"

    run.assert_called_once_with(
        build_release.sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--force-reinstall",
        "libtorrent>=2.1.1",
    )
