# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Run blindDL with ``python -m blinddl``."""

import os
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .runtime import prepare_runtime_path


def _instance_name():
    """A per-user mutex name shared by source and packaged launches."""
    return f"blindDL-{getpass.getuser()}"


def _flush_standard_streams() -> None:
    """Flush attached consoles without failing in a windowed frozen build."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass


def _self_test(output_path: str) -> int:
    """Exercise every packaged feature without creating a GUI."""
    results: dict[str, object] = {}
    failures: list[str] = []

    def check(name, callback):
        try:
            results[name] = callback()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    def frozen_runtime():
        if not getattr(sys, "frozen", False):
            raise RuntimeError("the application is using a system Python")
        return f"embedded Python {sys.version.split()[0]}"

    check("runtime", frozen_runtime)
    check("wx", lambda: __import__("wx").version())
    check("wx_media", lambda: __import__(
        "wx.media", fromlist=["MediaCtrl"]).__name__)
    check("yt_dlp", lambda: __import__("yt_dlp.version", fromlist=["__version__"]).__version__)
    check("yt_dlp_ejs", lambda: __import__("yt_dlp_ejs").__name__)
    check("websockets", lambda: __import__("websockets").__version__)
    check("tzdata", lambda: __import__("tzdata").__version__)
    check("sideb", lambda: __import__("sideb.app.main", fromlist=["Application"]).__name__)
    check("crypto", lambda: __import__("Crypto.Cipher.Blowfish", fromlist=["new"]).__name__)
    check("cryptography", lambda: __import__("cryptography").__version__)
    check("libtorrent", lambda: __import__("libtorrent").__version__)
    check("audiobooker", lambda: __import__("audiobooker").__name__)
    check("curl_cffi", lambda: __import__("curl_cffi").__version__)
    check("lxml", lambda: ".".join(map(
        str, __import__("lxml.etree", fromlist=["LXML_VERSION"]).LXML_VERSION)))
    check("requests", lambda: __import__("requests").__version__)

    def soulseek_runtime():
        from .soulseek_backend import runtime_probe

        return runtime_probe()

    check("soulseek", soulseek_runtime)

    def vlc_runtime():
        from .gui.media_player import vlc

        if vlc is None:
            raise RuntimeError("libVLC was not found")
        instance = vlc.Instance("--quiet", "--no-video-title-show")
        if instance is None:
            raise RuntimeError(
                "libVLC could not initialize; check its plugin runtime"
            )
        try:
            return vlc.libvlc_get_version().decode("utf-8", errors="replace")
        finally:
            instance.release()

    check("vlc", vlc_runtime)

    def musicdl_sources():
        from .musicdl_backend import ALL_SOURCES

        if not ALL_SOURCES:
            raise RuntimeError("musicdl registered no sources")
        return len(ALL_SOURCES)

    check("musicdl_sources", musicdl_sources)

    def adult_providers():
        from .adult_backend import PROVIDERS, _import_aebn, _import_provider

        for provider in PROVIDERS.values():
            if provider.download_style == "aebn":
                _import_aebn()
            elif provider.download_style not in ("creator", "ytdlp"):
                _import_provider(provider)
        return len(PROVIDERS)

    check("adult_providers", adult_providers)

    def yt_dlp_runtime():
        from yt_dlp.extractor import gen_extractor_classes

        names = {extractor.IE_NAME for extractor in gen_extractor_classes()}
        if "youtube" not in names:
            raise RuntimeError("the YouTube extractor was not bundled")
        return f"{len(names)} extractors including YouTube"

    check("yt_dlp_extractors", yt_dlp_runtime)

    def executable_runtime(tool):
        path = shutil.which(tool)
        if not path:
            raise RuntimeError(f"{tool} was not found")
        version_arg = "--version" if tool == "deno" else "-version"
        completed = subprocess.run(
            [path, version_arg], capture_output=True, text=True,
            timeout=15, check=True,
        )
        version = (completed.stdout or completed.stderr).splitlines()
        return f"{path}: {version[0] if version else 'started successfully'}"

    for tool in ("deno", "ffmpeg", "ffprobe"):
        check(tool, lambda tool=tool: executable_runtime(tool))

    def node_runtime():
        from nodejs_wheel.executable import node

        completed = node(
            ["--version"], return_completed_process=True,
            capture_output=True, text=True, timeout=15, check=True,
        )
        return completed.stdout.strip()

    check("embedded_node", node_runtime)

    report = {"ok": not failures, "results": results, "failures": failures}
    Path(output_path).write_text(
        json.dumps(report, indent=2), encoding="utf-8", newline="\n"
    )
    return 0 if not failures else 1


def main() -> int | None:
    prepare_runtime_path()
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        return _self_test(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "--app-bound-export":
        # Elevated helper relaunched from blinddl.app_bound.export_elevated.
        from . import app_bound

        return app_bound.main(["--export", sys.argv[2]])

    import wx

    from .config import app_data_dir
    from .gui.mainframe import MainFrame
    from .single_instance import RestoreServer, notify_existing

    app = wx.App()
    checker = wx.SingleInstanceChecker(_instance_name(), app_data_dir())
    if checker.IsAnotherRunning():
        restored = notify_existing()
        if not restored:
            wx.MessageBox(
                "blindDL is already running. Look for the blue B icon in the "
                "system tray overflow, or press Windows+B to reach it.",
                "blindDL is already running",
                wx.OK | wx.ICON_INFORMATION,
            )
        return 0
    frame = MainFrame()
    try:
        restore_server = RestoreServer(
            lambda: wx.CallAfter(frame.restore_from_tray)
        ).start()
    except OSError as exc:
        restore_server = None
        wx.MessageBox(
            "blindDL could not initialize its relaunch-to-restore service. "
            "Only one instance is still allowed; use the blue B tray icon "
            f"to restore the window.\n\n{exc}",
            "blindDL restore service",
            wx.OK | wx.ICON_WARNING,
        )
    frame.Show()
    frame.Raise()
    try:
        code = app.MainLoop()
    finally:
        if restore_server is not None:
            restore_server.stop()
        # Keep the checker alive until shutdown, then release its mutex before
        # the source process or frozen executable exits.
        del checker
    _flush_standard_streams()
    os._exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
