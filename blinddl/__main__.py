# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Run blindDL with ``python -m blinddl``."""

import os
import json
import shutil
import sys
from pathlib import Path

from .runtime import prepare_runtime_path


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
    check("sideb", lambda: __import__("sideb.app.main", fromlist=["Application"]).__name__)
    check("crypto", lambda: __import__("Crypto.Cipher.Blowfish", fromlist=["new"]).__name__)
    check("libtorrent", lambda: __import__("libtorrent").__version__)
    check("audiobooker", lambda: __import__("audiobooker").__name__)
    check("curl_cffi", lambda: __import__("curl_cffi").__version__)
    check("lxml", lambda: ".".join(map(
        str, __import__("lxml.etree", fromlist=["LXML_VERSION"]).LXML_VERSION)))
    check("requests", lambda: __import__("requests").__version__)

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

    for tool in ("deno", "ffmpeg", "ffprobe"):
        check(tool, lambda tool=tool: shutil.which(tool) or (_ for _ in ()).throw(
            RuntimeError(f"{tool} was not found")
        ))

    report = {"ok": not failures, "results": results, "failures": failures}
    Path(output_path).write_text(
        json.dumps(report, indent=2), encoding="utf-8", newline="\n"
    )
    return 0 if not failures else 1


def main() -> int | None:
    prepare_runtime_path()
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        return _self_test(sys.argv[2])

    import wx

    from .gui.mainframe import MainFrame

    app = wx.App()
    frame = MainFrame()
    frame.Show()
    frame.Raise()
    code = app.MainLoop()
    _flush_standard_streams()
    os._exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
