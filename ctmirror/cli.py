#!/usr/bin/env python3
"""Command-line interface for the CTM1 offline mirror client."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from .client import (BlockedError, HttpTransport, Manifest, MirrorClient,
                     MirrorError, UnknownSource, UntrustedError,
                     register_manifest)
from .store import Store, StoreCorrupt, StoreError


def _emit(obj, raw: bool = False) -> None:
    if raw:
        sys.stdout.buffer.write(obj)
        sys.stdout.buffer.flush()
    else:
        print(json.dumps(obj, indent=2, sort_keys=True))


def _open(args) -> tuple[Store, MirrorClient]:
    manifest = Manifest.load(args.manifest)
    store = Store(args.db)
    register_manifest(store, manifest)
    transport = HttpTransport(args.server)
    client = MirrorClient(store, transport, manifest,
                          page_size=args.page_size)
    return store, client


def cmd_sync(args) -> int:
    store, client = _open(args)
    try:
        results = []
        for sid in (args.source or client.manifest.sources.keys()):
            if sid not in client.manifest.sources:
                err = (f"source {sid!r} is not in the manifest")
                results.append({"source_id": sid, "state": "error",
                                "error": err})
                if args.strict:
                    raise UnknownSource(err)
                continue
            try:
                res = client.sync(sid, max_pages=args.max_pages)
                results.append(res.as_dict())
            except BlockedError as exc:
                results.append({"source_id": sid, "state": "blocked",
                                "reason": exc.reason, "detail": exc.detail})
                if args.strict:
                    raise
            except (UntrustedError, MirrorError) as exc:
                results.append({"source_id": sid, "state": "error",
                                "error": str(exc)})
                if args.strict:
                    raise
        _emit({"results": results})
        return 0
    finally:
        store.close()


def cmd_status(args) -> int:
    store, client = _open(args)
    try:
        ids = args.source or store.list_sources()
        _emit({"sources": [client.status(sid) for sid in ids]})
        return 0
    finally:
        store.close()


def cmd_diagnostics(args) -> int:
    store, client = _open(args)
    try:
        ids = args.source or store.list_sources()
        _emit({"sources": [client.diagnostics(
            sid, with_content=args.with_content, limit=args.limit)
            for sid in ids]})
        return 0
    finally:
        store.close()


def cmd_entries(args) -> int:
    store, client = _open(args)
    try:
        start = args.start
        end = args.end
        if end is None:
            end = client.store.committed_head(args.source).size
        leaves = list(client.entries(args.source, start, end))
        if args.json:
            _emit({"source_id": args.source, "start": start, "end": end,
                   "entries": [
                       {"leaf_index": i, "leaf_b64": base64.b64encode(
                           leaf).decode("ascii")} for i, leaf in leaves]})
        else:
            for i, leaf in leaves:
                sys.stdout.write(f"{i}\t")
                sys.stdout.flush()
                sys.stdout.buffer.write(leaf + b"\n")
                sys.stdout.buffer.flush()
        return 0
    finally:
        store.close()


def cmd_resume(args) -> int:
    store, client = _open(args)
    try:
        _emit(client.resume(args.source))
        return 0
    finally:
        store.close()


def cmd_discard(args) -> int:
    store, client = _open(args)
    try:
        _emit(client.discard_candidate(args.source))
        return 0
    finally:
        store.close()


def cmd_page(args) -> int:
    store, client = _open(args)
    try:
        _emit(client.fetch_page(args.source, args.start, args.count))
        return 0
    finally:
        store.close()


def cmd_head_frame(args) -> int:
    """Print the committed head frame (base64) without trusting the server."""
    store, client = _open(args)
    try:
        snap = client.store.committed_snapshot(args.source)
        _emit({"source_id": args.source,
               "head_b64": snap["frame_b64"],
               "tree_size": snap["tree_size"],
               "root": snap["root"]})
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctmirror",
                                 description="CTM1 offline mirror client")
    ap.add_argument("--db", default="mirror.sqlite",
                    help="SQLite database path")
    ap.add_argument("--manifest", default="manifest.json",
                    help="deployment manifest (anchor keys + certs)")
    ap.add_argument("--server", default="http://127.0.0.1:8765")
    ap.add_argument("--page-size", type=int, default=2)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="fetch a head and gather a candidate")
    p.add_argument("source", nargs="*")
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="committed/candidate state and gaps")
    p.add_argument("source", nargs="*")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("diagnostics", help="staged entries, gaps, evidence")
    p.add_argument("source", nargs="*")
    p.add_argument("--with-content", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_diagnostics)

    p = sub.add_parser("entries", help="read committed entries only")
    p.add_argument("source")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_entries)

    p = sub.add_parser("resume", help="clear a block on the same candidate")
    p.add_argument("source")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("discard-candidate",
                       help="explicitly drop the candidate and unblock")
    p.add_argument("source")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("page", help="fetch one page of the active candidate")
    p.add_argument("source")
    p.add_argument("start", type=int)
    p.add_argument("count", type=int)
    p.set_defaults(func=cmd_page)

    p = sub.add_parser("head", help="committed head frame")
    p.add_argument("source")
    p.set_defaults(func=cmd_head_frame)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except StoreCorrupt as exc:
        _emit({"error": "store_corrupt", "detail": str(exc)})
        return 3
    except BlockedError as exc:
        _emit({"error": "blocked", "source_id": exc.source_id,
               "reason": exc.reason, "detail": exc.detail})
        return 2
    except StoreError as exc:
        _emit({"error": "StoreError", "detail": str(exc)})
        return 1
    except (MirrorError, ValueError) as exc:
        _emit({"error": type(exc).__name__, "detail": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
