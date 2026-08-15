#!/bin/sh
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV=${BLINDDL_RELEASE_VENV:-"$ROOT/.release-venv"}
PYTHON=${PYTHON:-python3}

if [ "$(uname -s)" != "Linux" ]; then
    echo "tools/build_linux_release.sh must run on Linux." >&2
    exit 1
fi

for command in "$PYTHON" xvfb-run; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "$command is required for the Linux release build." >&2
        exit 1
    fi
done

SYSTEM_WX=$(
    "$PYTHON" -c 'import pathlib, wx; print(pathlib.Path(wx.__file__).parent)'
) || {
    echo "System wxPython is required; install python3-wxgtk4.0 and python3-wxgtk-media4.0." >&2
    exit 1
}

"$PYTHON" -m venv "$VENV"
VENV_SITE=$(
    "$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
)
if [ ! -e "$VENV_SITE/wx" ]; then
    ln -s "$SYSTEM_WX" "$VENV_SITE/wx"
fi

"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install "pyinstaller>=6.19" pytest -r "$ROOT/requirements.txt"
"$VENV/bin/python" -m pip install --upgrade --pre yt-dlp
"$VENV/bin/python" -m pip check
"$VENV/bin/python" "$ROOT/scripts/check_no_arl.py"
cd "$ROOT"
xvfb-run -a "$VENV/bin/python" -m pytest -q
"$VENV/bin/python" tools/build_release.py
