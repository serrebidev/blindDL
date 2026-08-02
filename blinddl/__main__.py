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
    """Exercise imports and bundled tools without creating a GUI."""
    results: dict[str, object] = {}
    failures: list[str] = []

    def check(name, callback):
        try:
            results[name] = callback()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    check("wx", lambda: __import__("wx").version())
    check("yt_dlp", lambda: __import__("yt_dlp.version", fromlist=["__version__"]).__version__)
    check("sideb", lambda: __import__("sideb.app.main", fromlist=["Application"]).__name__)
    check("crypto", lambda: __import__("Crypto.Cipher.Blowfish", fromlist=["new"]).__name__)

    def musicdl_sources():
        from .musicdl_backend import ALL_SOURCES

        if not ALL_SOURCES:
            raise RuntimeError("musicdl registered no sources")
        return len(ALL_SOURCES)

    check("musicdl_sources", musicdl_sources)

    for tool in ("deno", "ffmpeg"):
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
