#!/usr/bin/env python3
"""Thin launcher so the CLI runs under the isolated interpreter (``-I``).

Usage::

    python3 -I mlcli.py build --model ... --data ... --db ...
    python3 -I mlcli.py query --model ... --db ... --query ...

``-I`` ignores PYTHONPATH/cwd site customisations; we add the package
directory explicitly here, after interpreter startup, so no planted module
next to an untrusted data file can be imported.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from metriclayer.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
