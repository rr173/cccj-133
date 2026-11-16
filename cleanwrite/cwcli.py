#!/usr/bin/env python3
"""Thin launcher so the CLI runs under the isolated interpreter (``-I``).

``-I`` ignores PYTHONPATH, the current directory and user site-packages.
We add this file's directory explicitly after interpreter startup, so a
planted ``json.py`` next to untrusted command files cannot be imported.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cleanwrite.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
