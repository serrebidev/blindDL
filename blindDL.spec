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
BUNDLE_EXTERNAL_TOOLS = os.environ.get("BLINDDL_BUNDLE_EXTERNAL_TOOLS", "0") == "1"

# Keep optional-at-runtime backends and their complete dependency trees in the
# standalone application.  PyInstaller can otherwise miss modules imported by
# pydantic settings or UPnP adapters only after Soulseek is enabled.
for package in (
    # YouTube needs more than yt_dlp's importable Python modules: extractor
    # plugins, the EJS solver's minified JavaScript, and WebSocket support all
    # load dynamically at runtime. Windows obtains the native JS runtimes
    # through the operating system instead of duplicating them in every
    # release archive.
    "yt_dlp",
    "yt_dlp_ejs",
    "websockets",
    "curl_cffi",
    "mutagen",
    "audiobooker",
    "mediavocab",
    "aioslsk",
    "aiofiles",
    "async_timeout",
    "async_upnp_client",
    "multidict",
    "tzdata",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

if BUNDLE_EXTERNAL_TOOLS and importlib.util.find_spec("nodejs_wheel") is not None:
    node_datas, node_binaries, node_hidden = collect_all("nodejs_wheel")
    datas += node_datas
    binaries += node_binaries
    hiddenimports += node_hidden

# accessible-output2 speaks the status bar. Its screen-reader bridges are
# DLLs it loads by name from its own lib folder rather than modules anything
# imports, so the whole package has to come along; the outputs themselves are
# picked at runtime by trying each one, which import analysis cannot follow.
# Windows-only, and blindDL falls back to NVDA's and JAWS' own APIs without
# it, so a build on a platform that has no wheel simply leaves it out.
if importlib.util.find_spec("accessible_output2") is not None:
    ao2_datas, ao2_binaries, ao2_hidden = collect_all("accessible_output2")
    datas += ao2_datas
    binaries += ao2_binaries
    hiddenimports += ao2_hidden

# aioslsk keeps its share index and transfer list in a ``shelve``, and shelve
# resolves its storage backend by importing a name held in a plain string
# list.  Nothing in that is visible to PyInstaller's import analysis, so the
# only source of these modules is its stock hook-shelve -- which still lists
# just the three backends that existed before Python 3.13 added dbm.sqlite3
# and made it the default.  Leaving it out is what stopped Soulseek working
# in released builds while source checkouts were fine.
hiddenimports += [
    backend
    for backend in ("dbm.sqlite3", "dbm.dumb", "dbm.gnu", "dbm.ndbm")
    if importlib.util.find_spec(backend) is not None
]


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

# libtorrent powers the in-app torrent engine. Source checkouts may still run
# without it, but every distributed application must contain it: a frozen app
# cannot add a CPython extension later with pip.
if importlib.util.find_spec("libtorrent") is None:
    raise RuntimeError(
        "libtorrent is required for a complete blindDL release. Install a "
        "compatible official or locally built wheel before packaging."
    )
hiddenimports.append("libtorrent")

# Chromium app-bound cookie decryption (blinddl/app_bound.py) reaches into
# win32crypt/win32security for DPAPI and win32com.shell for the elevated
# helper launcher. Most of those imports are lazy, so collect them explicitly
# rather than trusting bytecode analysis on the Windows build machine.
if sys.platform == "win32":
    hiddenimports += [
        "win32api",
        "win32con",
        "win32crypt",
        "win32security",
        "win32event",
        "win32com.shell.shell",
    ]

for distribution in (
    "wxPython",
    "yt-dlp",
    "sideb",
    "requests",
    "mutagen",
    "aioslsk",
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

tool_names = ["deno"] if BUNDLE_EXTERNAL_TOOLS else []
if BUNDLE_EXTERNAL_TOOLS and os.environ.get("BLINDDL_BUNDLE_FFMPEG", "1") != "0":
    tool_names += ["ffmpeg", "ffprobe"]
def find_tool(tool_name):
    tool_path = shutil.which(tool_name)
    if sys.platform != "win32":
        return tool_path
    explicit = os.environ.get(f"BLINDDL_{tool_name.upper()}_PATH", "")
    if explicit and Path(explicit).is_file():
        return explicit
    if tool_name == "deno":
        deno = Path.home() / ".deno" / "bin" / "deno.exe"
        if deno.is_file():
            return str(deno)
    # Chocolatey's PATH entries are launcher shims.  Copying one of those into
    # the frozen app leaves it without its adjacent .shim configuration, so it
    # cannot locate the actual executable.  Resolve the package payload first.
    chocolatey = Path(
        os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")
    )
    if tool_name in {"ffmpeg", "ffprobe"}:
        matches = [
            path for path in chocolatey.glob(
                f"lib/ffmpeg*/tools/**/{tool_name}.exe"
            ) if path.is_file()
        ]
        if matches:
            return str(max(matches, key=lambda path: path.stat().st_size))
    package_root = Path(ORIGINAL_LOCALAPPDATA) / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(package_root.glob(f"*/**/{tool_name}.exe"))
    if matches:
        return str(max(matches, key=lambda path: path.stat().st_size))
    if tool_path:
        path = Path(tool_path)
        choco_bin = chocolatey / "bin"
        try:
            is_chocolatey_shim = path.parent.resolve() == choco_bin.resolve()
        except OSError:
            is_chocolatey_shim = False
        if not is_chocolatey_shim:
            return tool_path
    return None


for tool_name in tool_names:
    tool_path = find_tool(tool_name)
    if not tool_path:
        raise RuntimeError(
            f"{tool_name} was not found; refusing to make an incomplete release"
        )
    binaries.append((tool_path, "tools"))


def collect_vlc_runtime():
    """Bundle native libVLC and plugins where release builders provide it."""
    if sys.platform == "win32":
        if not BUNDLE_EXTERNAL_TOOLS:
            return
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
        if not BUNDLE_EXTERNAL_TOOLS:
            return
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
if BUNDLE_EXTERNAL_TOOLS and not any(
    Path(source).name.lower() in {"libvlc.dll", "libvlc.dylib"}
    for source, _destination in binaries
):
    raise RuntimeError(
        "The VLC runtime was not found; refusing to make a release without "
        "built-in media playback"
    )

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

# Windows version resource. NVDA (NVDA+Shift+V) and its JAWS equivalent read
# ProductName/ProductVersion straight out of the executable; without this
# block every PyInstaller binary reports "Application unknown, version not
# detected". Passed as a VSVersionInfo object, which PyInstaller embeds
# directly, and ignored automatically on other platforms.
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    def _version_tuple(value):
        parts = [int(part) for part in re.split(r"[^0-9]+", value) if part]
        parts = (parts + [0, 0, 0, 0])[:4]
        return tuple(parts)

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_version_tuple(VERSION),
            prodvers=_version_tuple(VERSION),
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "serrebidev"),
                            StringStruct(
                                "FileDescription",
                                "blindDL - accessible cross-platform media downloader",
                            ),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", "blindDL"),
                            StringStruct(
                                "LegalCopyright",
                                "Copyright (c) serrebidev and contributors",
                            ),
                            StringStruct("OriginalFilename", "blindDL.exe"),
                            StringStruct("ProductName", "blindDL"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    ),
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

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
    version=version_info,
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
