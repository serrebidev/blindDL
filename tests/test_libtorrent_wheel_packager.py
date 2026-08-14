# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import importlib.util
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "tools" / "package_libtorrent_wheel.py"
SPEC = importlib.util.spec_from_file_location("package_libtorrent_wheel", SCRIPT)
packager = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(packager)


def test_detect_version_reads_upstream_setup_cfg(tmp_path):
    setup_cfg = tmp_path / "bindings" / "python" / "setup.cfg"
    setup_cfg.parent.mkdir(parents=True)
    setup_cfg.write_text("[metadata]\nversion = 2.1.1\n", encoding="utf-8")
    assert packager.detect_version(tmp_path) == "2.1.1"


def test_base_wheel_contains_extension_metadata_and_record(tmp_path):
    extension = tmp_path / "libtorrent.cp314-test.pyd"
    extension.write_bytes(b"native-extension")

    wheel = packager.build_base_wheel(
        extension,
        "2.1.1+20260814",
        tmp_path / "wheelhouse",
        tag="cp314-cp314-win_amd64",
    )

    assert wheel.name == "libtorrent-2.1.1+20260814-cp314-cp314-win_amd64.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert extension.name in names
        metadata = archive.read(
            "libtorrent-2.1.1+20260814.dist-info/METADATA"
        ).decode()
        assert "Name: libtorrent" in metadata
        assert "Version: 2.1.1+20260814" in metadata
        assert "Requires-Python:" in metadata
        assert "libtorrent-2.1.1+20260814.dist-info/RECORD" in names
