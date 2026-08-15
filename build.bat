@ECHO OFF
@REM Copyright (c) serrebidev and contributors
@REM This file is part of blindDL.
@REM SPDX-License-Identifier: MIT

SETLOCAL
python scripts\check_no_arl.py
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
python -m pytest -q
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
@REM Official libtorrent does not publish CPython 3.14 wheels. The weekly task
@REM builds and clean-venv-tests the wheel used by every local Windows release.
SET "BLINDDL_REQUIRE_LIBTORRENT_WHEEL=1"
python tools\build_release.py %*
EXIT /B %ERRORLEVEL%
