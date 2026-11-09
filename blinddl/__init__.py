# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import re as _re

__version__ = "0.26.4"
APP_NAME = "blindDL"


def parse_version(value):
    """Canonical version key: first three numeric components, zero-padded.

    Shared by the updater, release publishing and build tooling so every
    place that compares versions agrees on cases such as ``1.2`` versus
    ``1.2.0`` and on extra trailing components.
    """
    parts = [int(part) for part in _re.findall(r"\d+", str(value))[:3]]
    return tuple((parts + [0, 0, 0])[:3])
