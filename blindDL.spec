# -*- mode: python ; coding: utf-8 -*-
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import importlib.util
import re
import shutil
import sys
import warnings
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


ROOT = Path(SPECPATH)
ORIGINAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
warnings.simplefilter("ignore", SyntaxWarning)
if sys.platform == "win32":
    build_local_appdata = ROOT / "build" / "pyinstaller-localappdata"
    build_local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(build_local_appdata)
VERSION = re.search(
    r'^__version__\s*=\s*["\']([^"\']+)["\']',
    (ROOT / "blinddl" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)
datas = [
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []
hiddenimports = []

for package in ("sideb", "mutagen"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden


def collect_package_from_filesystem(package_name):
    """Collect a package without importing it in PyInstaller's helper process."""
    package_spec = importlib.util.find_spec(package_name)
    if not package_spec or not package_spec.submodule_search_locations:
        raise RuntimeError(f"Cannot find package: {package_name}")
    package_dir = Path(next(iter(package_spec.submodule_search_locations)))
    package_datas = []
    package_hidden = []
    for path in package_dir.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir)
        if "tests" in relative.parts or "test" in relative.parts:
            continue
        if path.suffix == ".py":
            if path.name != "__init__.py":
                module_parts = relative.with_suffix("").parts
                package_hidden.append(".".join((package_name, *module_parts)))
        elif path.suffix not in {".pyc", ".pyo"}:
            package_datas.append((str(path), str(Path(package_name) / relative.parent)))
    return package_datas, package_hidden


musicdl_datas, musicdl_hidden = collect_package_from_filesystem("musicdl")
datas += musicdl_datas
hiddenimports += musicdl_hidden

ADULT_MODULES = (
    "base_api",
    "beeg_api",
    "eporner_api",
    "hqporner_api",
    "missav_api",
    "porngo_api",
    "pornhub_api",
    "porntrex_api",
    "redtube_api",
    "sex_api",
    "spankbang_api",
    "thumbzilla_api",
    "tube8_api",
    "xfreehd_api",
    "xhamster_api",
    "xnxx_api",
    "xvideos_api",
    "youporn_api",
)
for package in ADULT_MODULES:
    package_datas, package_hidden = collect_package_from_filesystem(package)
    datas += package_datas
    hiddenimports += package_hidden
    hiddenimports.append(package)

for distribution in (
    "wxPython",
    "yt-dlp",
    "musicdl",
    "sideb",
    "requests",
    "mutagen",
    "pycryptodome",
    "eaf_base_api",
    "unofficial-api-for-beeg",
    "unofficial-api-for-eporner",
    "unofficial-api-for-hqporner",
    "unofficial-api-for-missav",
    "porngo_api",
    "unofficial-api-for-pornhub",
    "unofficial-api-for-porntrex",
    "unofficial-api-for-redtube",
    "Sex_API",
    "unofficial-api-for-spankbang",
    "unofficial-api-for-thumbzilla",
    "unofficial-api-for-tube8",
    "unofficial-api-for-xfreehd",
    "unofficial-api-for-xhamster",
    "unofficial-api-for-xnxx",
    "unofficial-api-for-xvideos",
    "unofficial-api-for-youporn",
):
    try:
        datas += copy_metadata(distribution, recursive=True)
    except Exception:
        pass

tool_names = ["deno"]
if os.environ.get("BLINDDL_BUNDLE_FFMPEG", "1") != "0":
    tool_names += ["ffmpeg", "ffprobe"]
def find_tool(tool_name):
    tool_path = shutil.which(tool_name)
    if tool_path or sys.platform != "win32":
        return tool_path
    package_root = Path(ORIGINAL_LOCALAPPDATA) / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(package_root.glob(f"*/**/{tool_name}.exe"))
    return str(matches[-1]) if matches else None


for tool_name in tool_names:
    tool_path = find_tool(tool_name)
    if tool_path:
        binaries.append((tool_path, "tools"))

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "Crypto.SelfTest",
        "pydantic.mypy",
        "pydantic.v1.mypy",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="blindDL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="blindDL",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="blindDL.app",
        bundle_identifier="com.serrebi.blinddl",
        info_plist={
            "CFBundleDisplayName": "blindDL",
            "CFBundleName": "blindDL",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
        },
    )
