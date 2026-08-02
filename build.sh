#!/bin/sh
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

set -eu
python3 tools/build_release.py "$@"
