"""One TLS context for every Side B HTTP client.

httpx builds a fresh ``ssl.SSLContext`` for each client it is given, and
building one parses the whole certifi CA bundle: about 26 milliseconds of
pure processor time. Side B makes its clients per run rather than per
process -- one on every music search, three or four on every queued track --
so that cost landed on work the user was waiting for. The context does not
change once built, so the whole process shares this one.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx


@lru_cache(maxsize=1)
def default_ssl_context() -> ssl.SSLContext:
    """The context httpx would have built anyway, built once."""
    return httpx.create_ssl_context()
