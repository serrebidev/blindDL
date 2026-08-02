@ECHO OFF
@REM Copyright (c) serrebidev and contributors
@REM This file is part of blindDL.
@REM SPDX-License-Identifier: MIT

SETLOCAL
python tools\build_release.py %*
EXIT /B %ERRORLEVEL%
