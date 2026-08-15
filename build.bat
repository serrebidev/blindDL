@ECHO OFF
@REM Copyright (c) serrebidev and contributors
@REM This file is part of blindDL.
@REM SPDX-License-Identifier: MIT

SETLOCAL
python scripts\check_no_arl.py
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
python -m pytest -q
IF ERRORLEVEL 1 EXIT /B %ERRORLEVEL%
python tools\build_release.py %*
EXIT /B %ERRORLEVEL%
