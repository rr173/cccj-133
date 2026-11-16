"""Command line interface: init / import / get / explain / put / source.

Run from the ``cleanwrite`` directory::

    python3 -I cwcli.py init   --db work/demo.db --sample samples/drill.json
    python3 -I cwcli.py get    --db work/demo.db --out work/view.json
    python3 -I cwcli.py explain --db work/demo.db --view work/view.json \\
                                --actions work/actions.json
    python3 -I cwcli.py put    --db work/demo.db --view work/view.json \\
                               --actions work/actions.json
    python3 -I cwcli.py source --db work/demo.db

Exit codes: 0 success, 2 rejected input/conflict/encoding, 3 stale export,
4 token tampering, 5 invalid/foreign row reference.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import api
from .errors import (
    CleanWriteError,
    ConflictError,
    InputError,
    RowRefError,
    StaleTokenError,
    TokenError,
)
from .store import load_json_file

EXIT_STALE = 3
EXIT_TOKEN = 4


def _dump(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))


def _read_view(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    return load_json_file(path)


def _read_actions(path: str):
    if path == "-":
        return json.load(sys.stdin)
    return load_json_file(path)


def cmd_init(args):
    sample = load_json_file(args.sample) if args.sample else None
    config = load_json_file(args.config) if args.config else None
    report = api.init_db(args.db, sample=sample, config=config,
                         force=args.force)
    _dump(report)
    return 0


def cmd_import(args):
    report = api.import_sample(args.db, load_json_file(args.sample),
                               replace=not args.merge)
    _dump(report)
    return 0


def cmd_config(args):
    _dump(api.set_config(args.db, load_json_file(args.config)))
    return 0


def cmd_get(args):
    view = api.get(args.db)
    if args.out:
        Path(args.out).write_text(
            json.dumps(view, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print("wrote %s (revision %d, %d row(s), token %d chars)"
              % (args.out, view["revision"], len(view["rows"]),
                 len(view["snapshot_token"])))
    else:
        _dump(view)
    return 0


def _require_view(payload: dict) -> str:
    if not isinstance(payload, dict) or "snapshot_token" not in payload:
        raise InputError("view file must be the JSON document produced by get "
                         "(missing snapshot_token)")
    return payload["snapshot_token"]


def cmd_explain(args):
    view = _read_view(args.view)
    report = api.explain(args.db, _require_view(view),
                         _read_actions(args.actions))
    _dump(report)
    return 0


def cmd_put(args):
    view = _read_view(args.view)
    result = api.put(args.db, _require_view(view),
                     _read_actions(args.actions))
    _dump(result)
    return 0


def cmd_source(args):
    _dump(api.source_tables(args.db))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanwrite",
        description="editable clean-up view write-back for C LEFT JOIN A")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a database (optionally from a sample)")
    p.add_argument("--db", required=True)
    p.add_argument("--sample")
    p.add_argument("--config")
    p.add_argument("--force", action="store_true",
                   help="reset an already initialised database")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("import", help="import a sample into an existing database")
    p.add_argument("--db", required=True)
    p.add_argument("--sample", required=True)
    p.add_argument("--merge", action="store_true",
                   help="merge instead of replacing the source relations")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("config", help="replace the insert-default config")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("get", help="export a consistent editable view")
    p.add_argument("--db", required=True)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("explain", help="derive a batch, write nothing")
    p.add_argument("--db", required=True)
    p.add_argument("--view", required=True, help="file produced by get ('-')")
    p.add_argument("--actions", required=True,
                   help="JSON action array file ('-')")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("put", help="apply one batch atomically")
    p.add_argument("--db", required=True)
    p.add_argument("--view", required=True)
    p.add_argument("--actions", required=True)
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("source", help="dump both source tables")
    p.add_argument("--db", required=True)
    p.set_defaults(fn=cmd_source)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except StaleTokenError as exc:
        print("stale export: %s" % exc.message, file=sys.stderr)
        return EXIT_STALE
    except TokenError as exc:
        print("token rejected: %s" % exc.message, file=sys.stderr)
        return EXIT_TOKEN
    except RowRefError as exc:
        print("invalid reference: %s" % exc.message, file=sys.stderr)
        for problem in exc.details.get("problems", []):
            print("  - %s" % json.dumps(problem, ensure_ascii=False),
                  file=sys.stderr)
        return 5
    except ConflictError as exc:
        print("batch rejected: %s" % exc.message, file=sys.stderr)
        for conflict in exc.conflicts:
            print("  - %s" % json.dumps(conflict, ensure_ascii=False),
                  file=sys.stderr)
        return 2
    except CleanWriteError as exc:
        print("error: %s" % exc.message, file=sys.stderr)
        for problem in exc.details.get("problems", []):
            print("  - %s" % (problem if isinstance(problem, str)
                              else json.dumps(problem, ensure_ascii=False)),
                  file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
