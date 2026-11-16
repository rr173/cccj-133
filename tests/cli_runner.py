"""Subprocess entry point for CLI tests.

This script puts the project root on sys.path explicitly (so it can be run
from any working directory), then defers to the CLI.  It is project code, not
downloaded material, so it may use the environment's installed packages.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctmirror.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
