# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Build the native blindDL package for the current operating system."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = ROOT / "release"
VERSION_FILE = ROOT / "blinddl" / "__init__.py"


def version() -> str:
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        VERSION_FILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError("Unable to find blindDL version")
    return match.group(1)


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def architecture() -> str:
    machine = platform.machine().lower()
    return {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)


def build_application() -> None:
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "blindDL.spec",
    )


def verify_application() -> None:
    if sys.platform == "win32":
        executable = DIST / "blindDL" / "blindDL.exe"
    elif sys.platform == "darwin":
        executable = DIST / "blindDL.app" / "Contents" / "MacOS" / "blindDL"
    else:
        executable = DIST / "blindDL" / "blindDL"
    report_path = BUILD / "frozen-self-test.json"
    self_test_data = BUILD / "self-test-data"
    self_test_data.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if sys.platform == "win32":
        environment["APPDATA"] = str(self_test_data / "roaming")
        environment["LOCALAPPDATA"] = str(self_test_data / "local")
    else:
        environment["XDG_CONFIG_HOME"] = str(self_test_data / "config")
        environment["XDG_CACHE_HOME"] = str(self_test_data / "cache")
        environment["XDG_STATE_HOME"] = str(self_test_data / "state")
    print("+", executable, "--self-test", report_path, flush=True)
    subprocess.run(
        [str(executable), "--self-test", str(report_path)],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("ok"):
        raise RuntimeError("Frozen application self-test failed: " + repr(report))
    print("Frozen application self-test passed:")
    for name, value in sorted(report["results"].items()):
        print(f"- {name}: {value}")


def package_windows(app_version: str, arch: str) -> list[Path]:
    archive_base = RELEASE / f"blindDL-v{app_version}-windows-{arch}"
    archive = Path(
        shutil.make_archive(
            str(archive_base), "zip", root_dir=DIST, base_dir="blindDL"
        )
    )
    artifacts = [archive]

    candidates = [
        shutil.which("ISCC.exe"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", ""),
            "Inno Setup 6",
            "ISCC.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Programs",
            "Inno Setup 6",
            "ISCC.exe",
        ),
    ]
    winget_packages = (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    )
    candidates.extend(str(path) for path in winget_packages.glob("*/**/ISCC.exe"))
    compiler = next((path for path in candidates if path and Path(path).is_file()), None)
    if not compiler:
        raise RuntimeError("Inno Setup 6 (ISCC.exe) was not found")
    run(
        compiler,
        f"/DMyAppVersion={app_version}",
        f"/DMyAppArch={arch}",
        "packaging/windows/blindDL.iss",
    )
    installer = RELEASE / f"blindDL-Setup-v{app_version}-windows-{arch}.exe"
    if not installer.is_file():
        raise RuntimeError(f"Installer was not produced: {installer}")
    artifacts.append(installer)
    return artifacts


def package_macos(app_version: str, arch: str) -> list[Path]:
    app = DIST / "blindDL.app"
    if not app.is_dir():
        raise RuntimeError(f"macOS application bundle was not produced: {app}")
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    dmg = RELEASE / f"blindDL-v{app_version}-macos-{arch}.dmg"
    run(
        "hdiutil",
        "create",
        "-volname",
        "blindDL",
        "-srcfolder",
        str(app),
        "-ov",
        "-format",
        "UDZO",
        str(dmg),
    )
    return [dmg]


def package_linux_tar(app_version: str, arch: str) -> Path:
    source = DIST / "blindDL"
    stage = BUILD / f"blindDL-v{app_version}-linux-{arch}"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(source, stage)
    shutil.copy2(ROOT / "packaging" / "linux" / "install.sh", stage / "install.sh")
    (stage / "install.sh").chmod(0o755)
    archive = RELEASE / f"blindDL-v{app_version}-linux-{arch}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(stage, arcname=stage.name)
    return archive


def deb_architecture() -> str:
    result = subprocess.run(
        ["dpkg", "--print-architecture"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def package_deb(app_version: str) -> Path:
    deb_arch = deb_architecture()
    stage = BUILD / f"blinddl_{app_version}_{deb_arch}"
    if stage.exists():
        shutil.rmtree(stage)

    app_target = stage / "opt" / "blinddl"
    shutil.copytree(DIST / "blindDL", app_target)

    bin_dir = stage / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "blinddl"
    launcher.write_text(
        "#!/bin/sh\n"
        "# Copyright (c) serrebidev and contributors\n"
        "# SPDX-License-Identifier: MIT\n"
        "exec /opt/blinddl/blindDL \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    applications = stage / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    shutil.copy2(
        ROOT / "packaging" / "linux" / "blinddl.desktop",
        applications / "blinddl.desktop",
    )

    docs = stage / "usr" / "share" / "doc" / "blinddl"
    docs.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", docs / "copyright")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", docs / "THIRD_PARTY_NOTICES.md")

    control_dir = stage / "DEBIAN"
    control_dir.mkdir(parents=True)
    installed_kib = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) // 1024
    (control_dir / "control").write_text(
        f"Package: blinddl\n"
        f"Version: {app_version}\n"
        f"Section: sound\n"
        f"Priority: optional\n"
        f"Architecture: {deb_arch}\n"
        f"Installed-Size: {installed_kib}\n"
        f"Maintainer: serrebidev\n"
        f"Depends: ffmpeg, libvlc5, vlc-plugin-base, libgtk-3-0 | libgtk-3-0t64, libnotify4 | libnotify4t64\n"
        f"Homepage: https://github.com/serrebidev/blindDL\n"
        f"Description: Accessible cross-platform media downloader\n"
        f" blindDL provides keyboard and screen-reader accessible media search,\n"
        f" downloading, queues, and subscriptions.\n",
        encoding="utf-8",
    )

    output = RELEASE / f"blinddl_{app_version}_{deb_arch}.deb"
    run("dpkg-deb", "--build", "--root-owner-group", str(stage), str(output))
    return output


def write_checksums(artifacts: list[Path], arch: str) -> Path:
    import hashlib

    platform_name = {"win32": "windows", "darwin": "macos"}.get(
        sys.platform, "linux"
    )
    output = RELEASE / f"SHA256SUMS-{platform_name}-{arch}.txt"
    lines = []
    for artifact in sorted(artifacts):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    RELEASE.mkdir(exist_ok=True)
    if not args.skip_build:
        build_application()
        verify_application()

    app_version = version()
    arch = architecture()
    if sys.platform == "win32":
        artifacts = package_windows(app_version, arch)
    elif sys.platform == "darwin":
        artifacts = package_macos(app_version, arch)
    elif sys.platform.startswith("linux"):
        artifacts = [
            package_linux_tar(app_version, arch),
            package_deb(app_version),
        ]
    else:
        raise RuntimeError(f"Unsupported build platform: {sys.platform}")

    checksum = write_checksums(artifacts, arch)
    print("Release artifacts:")
    for artifact in [*artifacts, checksum]:
        print(f"- {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
