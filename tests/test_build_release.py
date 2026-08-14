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
