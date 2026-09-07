@ECHO OFF
@REM Copyright (c) serrebidev and contributors
@REM This file is part of blindDL.
@REM SPDX-License-Identifier: MIT

SETLOCAL
python scripts\check_no_arl.py
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
@REM Several dependencies follow a git branch and publish new code under an
@REM unchanged version, which pip reads as "already satisfied". A machine that
@REM keeps its environment drifts unevenly and ships older libraries than the
@REM hosted runners do. Tests then run against what will actually be frozen.
python tools\refresh_git_requirements.py
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
python -m pytest -q
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
@REM Official libtorrent does not publish CPython 3.14 wheels. The weekly task
@REM builds and clean-venv-tests the wheel used by every local Windows release.
SET "BLINDDL_REQUIRE_LIBTORRENT_WHEEL=1"
python tools\build_release.py %*
EXIT /B %ERRORLEVEL%
