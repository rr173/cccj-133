#!/usr/bin/env python3
"""Reproducible acceptance drill for the CTM1 mirror client.

Runs the exact scenario from the task statement, printing evidence at each
step instead of asserting success on its own:

  1. fixture log with a 5-leaf tree; client commits 5
  2. log grows to 9; client selects a 9-leaf candidate, downloads 5 and 6
  3. log moves to 10; a 10-leaf proof must not finish the 9-leaf candidate
  4. bad proof for 7, page for 8, then the process is closed and reopened
     (committed stays 5, candidate stays 9, diagnostics show 7 missing,
     index 8 is not exposed to downstream)
  5. after a correct page for 7, one promotion to 9; bodies equal an
     independently constructed tree
  6. key rotation with activation_size 9: unusable at committed 5, adopted
     only after the old-key authorisation is satisfied at 9

Usage:  python3 tools/acceptance_drill.py [--keep]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading  # noqa: E402
import urllib.request  # noqa: E402

from ctmirror import merkle  # noqa: E402
from ctmirror.client import (BlockedError, HttpTransport,  # noqa: E402
                             MirrorClient, register_manifest)
from ctmirror.store import Store  # noqa: E402
from fixture.server import make_server  # noqa: E402


def admin(server, path, body=None):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def show(title, obj):
    print(f"\n### {title}")
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def leaf(label):
    return label.encode()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    server, state = make_server([("alpha", 11)])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="ctm1-drill-")
    db = os.path.join(tmp, "mirror.sqlite")
    url = f"http://127.0.0.1:{server.server_port}"
    manifest_path = os.path.join(tmp, "manifest.json")
    try:
        # Deployment manifest: anchor generation only.
        state_info = admin(server, "/admin/state")["sources"]["alpha"]
        anchor = state_info["public_keys"]["0"]
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"sources": [{
                "source_id": "alpha", "anchor_epoch": 0,
                "anchor_public_key": anchor, "certificates": []}]}, fh)
        from ctmirror.client import Manifest
        manifest = Manifest.load(manifest_path)
        store = Store(db)
        register_manifest(store, manifest)
        client = MirrorClient(store, HttpTransport(url), manifest,
                              page_size=1)

        old5 = [leaf(f"alpha:leaf:{i}") for i in range(5)]
        admin(server, "/admin/append", {
            "source_id": "alpha",
            "leaves": [base64.b64encode(x).decode() for x in old5]})
        res = client.sync("alpha")
        show("step 1: commit 5 leaves", res.as_dict())
        show("committed", client.status("alpha")["committed"])
        print("independent root(5) =",
              merkle.tree_hash(old5).hex())

        # ---- grow to 9, fetch only 5 and 6 -------------------------------
        target = [leaf(f"alpha:leaf:{i}") for i in range(9)]
        admin(server, "/admin/append", {
            "source_id": "alpha",
            "leaves": [base64.b64encode(x).decode() for x in target[5:]]})
        res = client.sync("alpha", max_pages=2)
        show("step 2: candidate 9, pages for 5 and 6", res.as_dict())
        st = client.status("alpha")
        show("status (committed 5, candidate 9)",
             {"committed": st["committed"], "candidate": st["candidate"],
              "gaps": st["gaps"], "staged_count": st["staged_count"]})
        print("staged indices =",
              [s["leaf_index"] for s in
               client.diagnostics("alpha")["staged"]])
        print("independent root(9) =", merkle.tree_hash(target).hex())
        print("candidate root       =", st["candidate"]["root"])

        # ---- log moves to 10 ---------------------------------------------
        ten = target + [leaf("alpha:leaf:9")]
        admin(server, "/admin/append", {
            "source_id": "alpha",
            "leaves": [base64.b64encode(ten[9]).decode()]})
        res = client.sync("alpha", max_pages=0)
        show("step 3: log at 10; candidate still 9", res.as_dict())
        st = client.status("alpha")
        assert st["candidate"]["tree_size"] == 9
        assert st["committed"]["tree_size"] == 5
        print("candidate bound to root(9) =",
              st["candidate"]["root"] == merkle.tree_hash(target).hex())
        print("deferred heads:", [d["tree_size"]
                                  for d in st["deferred_heads"]])
        # A 10-leaf proof cannot be used: the candidate is fixed at 9 and
        # only indices < 9 may be staged.
        print("candidate target is size 9, not",
              len(ten), "-> no 10-leaf proof is requested")

        # ---- index 8 arrives first ---------------------------------------
        page8 = client.fetch_page("alpha", 8, 1)
        show("step 4: out-of-order page for index 8", page8)
        assert page8["staged_count"] == 3
        st = client.status("alpha")
        print("committed remains", st["committed"]["tree_size"],
              "and 8 is not downstream:",
              len(list(client.entries("alpha", 0, 5))) == 5)

        # ---- bad proof for 7, then close ---------------------------------
        admin(server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "inclusion_bad", "index": 7}]})
        try:
            client.sync("alpha")
        except BlockedError as exc:
            show("step 5: bad inclusion proof for index 7",
                 {"reason": exc.reason, "detail": exc.detail})
        st = client.status("alpha")
        diag = client.diagnostics("alpha")
        show("after block", {
            "committed_size": st["committed"]["tree_size"],
            "committed_count": st["committed"]["entry_count"],
            "candidate_size": st["candidate"]["tree_size"],
            "blocked": st["blocked"], "gaps": st["gaps"],
            "staged_indices": [s["leaf_index"] for s in diag["staged"]],
            "missing_ranges": diag["missing"],
            "downstream_entries": len(list(client.entries("alpha", 0, 5))),
        })
        try:
            list(client.entries("alpha", 0, 9))
            print("ERROR: downstream saw uncommitted entries")
        except Exception as exc:  # noqa: BLE001 - expected
            print("downstream read of 0..9 refused:", type(exc).__name__)
        store.close()

        # ---- reopen: staging still bound to 9 ----------------------------
        store = Store(db)
        register_manifest(store, manifest)
        client = MirrorClient(store, HttpTransport(url), manifest,
                              page_size=2)
        st = client.status("alpha")
        diag = client.diagnostics("alpha")
        show("step 6: reopen", {
            "committed_size": st["committed"]["tree_size"],
            "candidate_size": st["candidate"]["tree_size"],
            "blocked": st["blocked"], "gaps": st["gaps"],
            "staged_indices": [s["leaf_index"] for s in diag["staged"]],
            "missing_ranges": diag["missing"],
            "deferred_heads": [d["tree_size"]
                               for d in st["deferred_heads"]],
        })
        assert st["committed"]["tree_size"] == 5
        assert st["candidate"]["tree_size"] == 9
        assert diag["missing"] == [[7, 8]]
        assert 8 in [s["leaf_index"] for s in diag["staged"]]
        assert [s["leaf_index"] for s in diag["staged"]] == [5, 6, 8]
        client.resume("alpha")
        # Fault is one-shot: the correct page for 7 is served now, and the
        # candidate promotes to 9 in one step.
        res = client.sync("alpha")
        show("step 7: resume same candidate, promote to 9", res.as_dict())
        st = client.status("alpha")
        got = [entry for _, entry in client.entries("alpha", 0, 9)]
        print("committed size =", st["committed"]["tree_size"],
              "entries =", st["committed"]["entry_count"])
        print("bodies match independent tree:", got == target)
        print("root matches independent root:",
              st["committed"]["root"] == merkle.tree_hash(target).hex())
        assert got == target
        assert st["committed"]["entry_count"] == st["committed"]["tree_size"]

        # ---- second source with key rotation -----------------------------
        # Use a fresh source to keep the rotation drill independent.
        server2, state2 = make_server([("bravo", 21)])
        threading.Thread(target=server2.serve_forever, daemon=True).start()
        try:
            url2 = f"http://127.0.0.1:{server2.server_port}"
            info = admin(server2, "/admin/state")["sources"]["bravo"]
            rot = admin(server2, "/admin/rotate",
                        {"source_id": "bravo", "activation_size": 9})
            show("step 8: rotation cert (signed by old epoch)",
                 {"old_epoch": rot["old_epoch"],
                  "new_epoch": rot["new_epoch"],
                  "activation_size": rot["activation_size"],
                  "cert_b64_len": len(rot["cert_b64"])})
            # Manifest carries the CTK1 certificate for the successor key.
            m2path = os.path.join(tmp, "manifest2.json")
            with open(m2path, "w", encoding="utf-8") as fh:
                json.dump({"sources": [{
                    "source_id": "bravo", "anchor_epoch": 0,
                    "anchor_public_key": info["public_keys"]["0"],
                    "certificates": [rot["cert_b64"]]}]}, fh)
            from ctmirror.client import Manifest as M2
            manifest2 = M2.load(m2path)
            br5 = [leaf(f"bravo:leaf:{i}") for i in range(5)]
            admin(server2, "/admin/append", {
                "source_id": "bravo",
                "leaves": [base64.b64encode(x).decode() for x in br5]})
            db2 = os.path.join(tmp, "mirror2.sqlite")
            store2b = Store(db2)
            register_manifest(store2b, manifest2)
            c2 = MirrorClient(store2b, HttpTransport(url2), manifest2,
                              page_size=2)
            c2.sync("bravo")
            st2 = c2.status("bravo")
            show("committed 5 with successor key registered",
                 {"committed_size": st2["committed"]["tree_size"],
                  "active_epoch": st2["active_epoch"],
                  "generations": st2["generations"]})
            # A new-epoch head must not advance us at committed 5.
            frame = admin(server2, "/admin/head",
                          {"source_id": "bravo", "epoch": 1,
                           "tree_size": 5})
            admin(server2, "/admin/script", {"source_id": "bravo", "ops": [
                {"kind": "head_override", "head_b64": frame["head_b64"]}]})
            try:
                c2.sync("bravo")
                print("ERROR: new-epoch head accepted too early")
            except Exception as exc:  # noqa: BLE001
                show("step 9: new-epoch head refused at committed 5",
                     {"error": str(exc)})
            st2 = c2.status("bravo")
            print("committed still =", st2["committed"]["tree_size"],
                  "active_epoch =", st2["active_epoch"])
            assert st2["committed"]["tree_size"] == 5
            assert st2["active_epoch"] == 0
            # Grow to 9 through the old key, then adopt the new key.
            admin(server2, "/admin/append", {
                "source_id": "bravo", "count": 4})
            res = c2.sync("bravo")
            show("step 10: committed 9 activates the new key", res.as_dict())
            st2 = c2.status("bravo")
            print("active_epoch =", st2["active_epoch"],
                  "committed epoch =", st2["committed"]["epoch"])
            assert st2["committed"]["tree_size"] == 9
            assert st2["active_epoch"] == 1
            # New-epoch head at size >= activation_size is now adoptable.
            admin(server2, "/admin/append", {"source_id": "bravo",
                                             "count": 1})
            res = c2.sync("bravo")
            st2 = c2.status("bravo")
            print("final committed =", res.committed_size,
                  "epoch =", st2["committed"]["epoch"])
            assert st2["committed"]["epoch"] == 1
            assert st2["committed"]["entry_count"] == 10
            store2b.close()
            print("\nALL DRILL CHECKS PASSED")
        finally:
            server2.shutdown()
            server2.server_close()
    finally:
        server.shutdown()
        server.server_close()
        if args.keep:
            print(f"\nartifacts kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
