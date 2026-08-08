# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821 - PyInstaller injects its build API and SPECPATH.
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
# Side B is vendored in the repo rather than installed, so the collectors
# below have to be able to find it the way an ordinary import would.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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

for package in ("mutagen", "audiobooker", "mediavocab"):
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

# Side B lives in the repo now that its upstream is gone. Collected the same
# way as musicdl -- from the filesystem, without importing it.
sideb_datas, sideb_hidden = collect_package_from_filesystem("sideb")
datas += sideb_datas
hiddenimports += sideb_hidden
hiddenimports.append("sideb")

ADULT_MODULES = (
    "aebn_dl",
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

# libtorrent powers the optional in-app torrent engine. It is one binary
# extension module, so naming it is enough -- and it is only named when the
# build machine actually has it, because it publishes no wheel for the newest
# Python releases and a release build must not fail over an optional feature.
if importlib.util.find_spec("libtorrent") is not None:
    hiddenimports.append("libtorrent")

for distribution in (
    "wxPython",
    "yt-dlp",
    "musicdl",
    "sideb",
    "requests",
    "mutagen",
    "pycryptodome",
    "python-vlc",
    "aebndl",
    "curl_cffi",
    "lxml",
    "rich",
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


def collect_vlc_runtime():
    """Bundle native libVLC and plugins where release builders provide it."""
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("BLINDDL_VLC_ROOT", "")),
            Path(os.environ.get("ProgramFiles", "")) / "VideoLAN" / "VLC",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN" / "VLC",
        ]
        root = next((path for path in candidates
                     if (path / "libvlc.dll").is_file()), None)
        if root is None:
            return
        binaries.extend([
            (str(root / "libvlc.dll"), "."),
            (str(root / "libvlccore.dll"), "."),
        ])
        datas.append((str(root / "plugins"), "plugins"))
        if (root / "COPYING.txt").is_file():
            datas.append((str(root / "COPYING.txt"), "."))
    elif sys.platform == "darwin":
        root = Path(os.environ.get(
            "BLINDDL_VLC_ROOT", "/Applications/VLC.app/Contents/MacOS"))
        lib_dir = root / "lib"
        library = lib_dir / "libvlc.dylib"
        core = lib_dir / "libvlccore.dylib"
        plugins = root / "plugins"
        if not plugins.is_dir():
            plugins = root / "modules"
        if not (library.is_file() and core.is_file() and plugins.is_dir()):
            return
        for dylib in lib_dir.glob("*.dylib"):
            binaries.append((str(dylib), "vlc/lib"))
        datas.append((str(plugins), "vlc/plugins"))


collect_vlc_runtime()

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
if sys.platform == "win32":
    # python-vlc contains macOS ctypes lookup strings. PyInstaller otherwise
    # duplicates the Windows DLLs under misleading .dylib names.
    a.binaries = [
        entry
        for entry in a.binaries
        if entry[0] not in {"libvlc.dylib", "libvlccore.dylib"}
    ]
elif sys.platform.startswith("linux"):
    # Linux packages provide libVLC and its matching plugin tree together.
    # Bundling only the ctypes-discovered shared libraries prevents libVLC
    # from locating those system plugins and causes initialization to fail.
    a.binaries = [
        entry
        for entry in a.binaries
        if not entry[0].startswith(("libvlc.so", "libvlccore.so"))
    ]
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
