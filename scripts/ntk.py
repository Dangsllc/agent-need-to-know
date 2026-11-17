#!/usr/bin/env python3
"""ntk: profiles and switches for the plugins and connectors each project
folder uses. Entry point; the code lives in the ntk_pkg package next to
this file. Python 3 standard library only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ntk_pkg.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except Exception:
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            sys.exit(0)
        raise
