#!/usr/bin/env python3
"""Build a deployment manifest from a running fixture log.

The manifest is the trust starting point: it contains, per source, the
anchor epoch and Ed25519 public key, plus any base64 CTK1 successor
certificates that were already distributed out of band.  The client will
accept successor keys only when every cert in the chain verifies from the
anchor and epochs increment by one.

Usage::

    python3 tools/make_manifest.py --server http://127.0.0.1:8765 \
        --source alpha --source beta --out manifest.json \
        --cert-epochs 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8765")
    ap.add_argument("--source", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--anchor-epoch", type=int, default=0)
    ap.add_argument("--cert-epochs", default="",
                    help="comma separated epochs whose CTK1 certs go in the "
                         "manifest (must start at anchor+1)")
    args = ap.parse_args(argv)
    cert_epochs = [int(x) for x in args.cert_epochs.split(",") if x]
    sources = []
    for sid in args.source:
        state = fetch(f"{args.server}/admin/state")
        info = state["sources"][sid]
        key_hex = info["public_keys"][str(args.anchor_epoch)]
        certs = []
        for epoch in cert_epochs:
            frames = info["certs"].get(str(epoch))
            if not frames:
                raise SystemExit(f"no cert for {sid} epoch {epoch}")
            certs.append(frames)
        sources.append({
            "source_id": sid,
            "anchor_epoch": args.anchor_epoch,
            "anchor_public_key": key_hex,
            "certificates": certs,
        })
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"sources": sources}, fh, indent=2)
        fh.write("\n")
    print(json.dumps({"manifest": args.out, "sources": len(sources)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
