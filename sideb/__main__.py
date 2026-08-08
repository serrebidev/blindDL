"""Entry point: `python -m sideb` or the installed `sideb` console script.

With no arguments, launches the interactive questionary-driven flow. With
arguments, delegates to the non-interactive argparse-based CLI. See
ARCHITECTURE.md section 8 ("Two Modes").
"""

from __future__ import annotations

import sys

# Conventional shell exit code for "terminated by SIGINT" (128 + signal 2).
_SIGINT_EXIT_CODE = 130


def main() -> int:
    try:
        if len(sys.argv) == 1:
            from sideb.cli.interactive import main as interactive_main

            return interactive_main()
        from sideb.cli.noninteractive import main as noninteractive_main

        return noninteractive_main(sys.argv[1:])
    except KeyboardInterrupt:
        # Catches Ctrl+C anywhere control wasn't already inside a questionary
        # prompt (which handles it and returns None on its own) — most
        # importantly, during an in-progress download/pipeline run. Without
        # this, that Ctrl+C raises straight through asyncio.run() and dumps
        # a raw traceback instead of a clean, expected exit.
        print("\nCancelled.", file=sys.stderr)
        return _SIGINT_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
