#!/usr/bin/env python3
"""Local fixture CT-style log server for the CTM1 mirror convention.

This is a *fixture*, not a production log and not a general RFC 6962/9162
HTTP implementation.  It speaks a small JSON protocol over HTTP on
127.0.0.1 and is intentionally scriptable so tests can inject faults
(equivocation, replay, bad proof, updated head mid-round, ...).

Endpoints
---------
GET  /v1/sources
GET  /v1/source/<source_id>/head
POST /v1/source/<source_id>/consistency
      {"old_size": m, "new_size": n}
      -> {"source_id", "old_size", "new_size", "old_root", "new_root",
          "epoch", "tree_size", "root", "head_b64", "proof":[hex...]}
GET  /v1/source/<source_id>/keys
      -> {"source_id", "generations":[{epoch, public_key,
          activation_size, cert_b64}]}
POST /v1/source/<source_id>/entries
      {"tree_size": n, "start": i, "count": c}
      -> {"source_id", "tree_size", "root", "entries":[
             {leaf_index, leaf_b64, proof:[hex...]}]}
      when the log has advanced the server *may* answer 409 with an updated
      signed head instead of the requested pages (fault injection).

Admin/test endpoints (never used by the client):
GET  /admin/state
GET  /admin/faults
POST /admin/script     {"ops":[...]}
POST /admin/append     {"leaves":[b64...]}  or {"count":k}
POST /admin/rotate     {"activation_size": n}
POST /admin/head       {"source_id","epoch","tree_size"} -> signed frame
POST /admin/clear      {"source_id"}   (drop queued faults for a source)
POST /admin/restart    (clears all scripted faults)

The logs are held per-source in memory and served from an append-only list,
with Merkle hashes computed by ``ctmirror.merkle``; the fixture uses the
same *definition* but the client does not trust these computations.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from ctmirror import merkle, protocol  # noqa: E402


class SourceLog:
    def __init__(self, source_id: str, seed: int):
        self.source_id = source_id
        self.leaves: list[bytes] = []
        self.keys: dict[int, Ed25519PrivateKey] = {}
        self.public: dict[int, bytes] = {}
        self.certs: dict[int, bytes] = {}  # new_epoch -> cert frame
        self.epoch = 0
        self.activation_size: dict[int, int] = {}
        self.timestamp_ms = 1_700_000_000_000
        self._seed = seed
        self.lock = threading.RLock()
        self.gen_key(0, activation_size=0)

    def gen_key(self, epoch: int, activation_size: int) -> bytes:
        key = Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(f"{self._seed}:{self.source_id}:{epoch}"
                           .encode()).digest())
        self.keys[epoch] = key
        self.public[epoch] = protocol.public_key_bytes(key.public_key())
        self.epoch = max(self.epoch, epoch)
        if activation_size:
            self.activation_size[epoch] = activation_size
        return self.public[epoch]

    def rotate(self, activation_size: int) -> dict:
        """Create epoch+1 key and old-key-signed certificate."""
        with self.lock:
            old = self.epoch
            new = old + 1
            self.gen_key(new, activation_size)
            cert = protocol.make_key_cert(
                self.source_id, old, new, self.public[new], activation_size,
                self.keys[old])
            self.certs[new] = cert
            return {"old_epoch": old, "new_epoch": new,
                    "new_public_key": self.public[new].hex(),
                    "activation_size": activation_size,
                    "cert_b64": base64.b64encode(cert).decode()}

    @property
    def latest_epoch(self) -> int:
        """Epoch authorised for heads: highest epoch whose activation size is
        reached.  Older than activation -> previous generation stays active."""
        with self.lock:
            epoch = 0
            for e in sorted(self.keys):
                if e == 0:
                    epoch = 0
                    continue
                if len(self.leaves) > self.activation_size.get(e, 1 << 63):
                    epoch = e
            return epoch

    def head_frame(self, epoch: Optional[int] = None,
                   tree_size: Optional[int] = None) -> bytes:
        with self.lock:
            e = self.latest_epoch if epoch is None else epoch
            n = len(self.leaves) if tree_size is None else tree_size
            if n > len(self.leaves):
                raise ValueError("tree_size beyond leaves")
            root = merkle.tree_hash(self.leaves[:n])
            self.timestamp_ms += 1
            return protocol.make_tree_head(
                self.source_id, e, n, self.timestamp_ms, root, self.keys[e])

    def append(self, leaves: list[bytes]) -> None:
        with self.lock:
            self.leaves.extend(leaves)


class FixtureState:
    def __init__(self, sources: dict[str, SourceLog]):
        self.sources = sources
        self.script: list[dict] = []
        self.head_override: dict[str, bytes] = {}
        self.consistency_fault: dict[str, list[dict]] = {}
        self.entries_fault: dict[str, list[dict]] = {}
        self.keys_fault: dict[str, list[dict]] = {}
        self.request_log: list[dict] = []
        self.lock = threading.RLock()

    def next_entries_fault(self, source_id: str) -> Optional[dict]:
        with self.lock:
            q = self.entries_fault.get(source_id) or []
            return q.pop(0) if q else None

    def next_consistency_fault(self, source_id: str) -> Optional[dict]:
        with self.lock:
            q = self.consistency_fault.get(source_id) or []
            return q.pop(0) if q else None

    def next_keys_fault(self, source_id: str) -> Optional[dict]:
        with self.lock:
            q = self.keys_fault.get(source_id) or []
            return q.pop(0) if q else None

    def log(self, item: dict) -> None:
        with self.lock:
            self.request_log.append(item)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    state: FixtureState = None  # type: ignore
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj: dict, extra_headers=None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8 * 1024 * 1024:
            raise ValueError("bad content length")
        data = self.rfile.read(length)
        obj = json.loads(data.decode())
        if not isinstance(obj, dict):
            raise ValueError("expected object")
        return obj

    # ------------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/admin/state":
                return self._admin_state()
            if path == "/admin/faults":
                with self.state.lock:
                    return self._send(200, {
                        "consistency": {k: list(v) for k, v in
                                        self.state.consistency_fault.items()},
                        "entries": {k: list(v) for k, v in
                                    self.state.entries_fault.items()},
                        "keys": {k: list(v) for k, v in
                                 self.state.keys_fault.items()},
                        "head_override": sorted(self.state.head_override)})
            if path == "/v1/sources":
                return self._send(200, {"sources": [
                    {"source_id": s, "epoch": log.latest_epoch,
                     "tree_size": len(log.leaves),
                     "activation_size": {str(k): v for k, v in
                                         log.activation_size.items()}}
                    for s, log in self.state.sources.items()]})
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "source" \
                    and parts[3] == "head":
                return self._head(parts[2])
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "source" \
                    and parts[3] == "keys":
                return self._keys(parts[2])
            if path == "/v1/requests":
                with self.state.lock:
                    return self._send(200, {"requests":
                                            self.state.request_log[-200:]})
            return self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - fixture, report as 500
            return self._send(500, {"error": repr(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            body = self._read_json()
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "source" \
                    and parts[3] == "consistency":
                return self._consistency(parts[2], body)
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "source" \
                    and parts[3] == "entries":
                return self._entries(parts[2], body)
            if path == "/admin/append":
                return self._admin_append(body)
            if path == "/admin/rotate":
                return self._admin_rotate(body)
            if path == "/admin/head":
                return self._admin_head(body)
            if path == "/admin/script":
                return self._admin_script(body)
            if path == "/admin/clear":
                self.state.entries_fault.pop(
                    protocol.require_str(body.get("source_id"),
                                         "source_id"), None)
                self.state.consistency_fault.pop(
                    protocol.require_str(body.get("source_id"),
                                         "source_id"), None)
                self.state.keys_fault.pop(
                    protocol.require_str(body.get("source_id"),
                                         "source_id"), None)
                return self._send(200, {"ok": True})
            if path == "/admin/restart":
                self.state.script = []
                self.state.entries_fault = {}
                self.state.consistency_fault = {}
                self.state.keys_fault = {}
                self.state.head_override = {}
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": repr(exc)})

    # -- log endpoints ------------------------------------------------------
    def _source(self, source_id: str) -> SourceLog:
        log = self.state.sources.get(source_id)
        if log is None:
            raise KeyError("unknown source")
        return log

    def _head(self, source_id: str):
        try:
            log = self._source(source_id)
        except KeyError:
            return self._send(404, {"error": "unknown source"})
        query = urlparse(self.path).query
        epoch_param = None
        for part in query.split("&"):
            if part.startswith("epoch="):
                try:
                    epoch_param = int(part[6:])
                except ValueError:
                    return self._send(400, {"error": "bad epoch"})
                if epoch_param not in log.keys:
                    return self._send(400, {"error": "unknown epoch"})
        with self.state.lock:
            override = (self.state.head_override.pop(source_id, None)
                        if epoch_param is None else None)
        frame = (override if override is not None else
                 log.head_frame(epoch=epoch_param))
        parsed = protocol.parse_tree_head(frame)
        self.state.log({"kind": "head", "source_id": source_id,
                        "tree_size": parsed.tree_size,
                        "epoch": parsed.epoch})
        return self._send(200, {
            "source_id": source_id, "head_b64": _b64(frame),
            "epoch": parsed.epoch, "tree_size": parsed.tree_size,
            "timestamp_ms": parsed.timestamp_ms,
            "root": parsed.root.hex()})

    def _keys(self, source_id: str):
        try:
            log = self._source(source_id)
        except KeyError:
            return self._send(404, {"error": "unknown source"})
        fault = self.state.next_keys_fault(source_id)
        with log.lock:
            epochs = sorted(log.keys)
            frames = dict(log.certs)
            publics = dict(log.public)
            activation = dict(log.activation_size)
            if fault:
                kind = fault.get("kind")
                if kind == "jump" and len(epochs) >= 2:
                    # drop a middle generation; epochs now skip by two
                    drop = fault.get("epoch", epochs[-1])
                    epochs = [e for e in epochs if e != drop]
                elif kind == "self_signed" and len(epochs) >= 2:
                    target = fault.get("epoch", epochs[-1])
                    prev = max(e for e in epochs if e < target)
                    frames[target] = protocol.make_key_cert(
                        source_id, prev, target, publics[target],
                        activation.get(target, 0), log.keys[target])
                elif kind == "wrong_key" and len(epochs) >= 2:
                    target = fault.get("epoch", epochs[-1])
                    bad = hashlib.sha256(b"wrong-key").digest()
                    new_pub = protocol.public_key_bytes(
                        Ed25519PrivateKey.from_private_bytes(bad)
                        .public_key())
                    prev = max(e for e in epochs if e < target)
                    frames[target] = protocol.make_key_cert(
                        source_id, prev, target, new_pub,
                        activation.get(target, 0), log.keys[prev])
                elif kind == "bad_activation":
                    target = fault.get("epoch", epochs[-1])
                    activation[target] = activation.get(target, 0) ^ 1
            gens = []
            for epoch in epochs:
                gens.append({
                    "epoch": epoch,
                    "public_key": publics[epoch].hex(),
                    "activation_size": activation.get(epoch, 0),
                    "cert_b64": (_b64(frames[epoch])
                                 if epoch in frames else None),
                })
        return self._send(200, {"source_id": source_id,
                                "generations": gens})

    def _consistency(self, source_id: str, body: dict):
        try:
            log = self._source(source_id)
        except KeyError:
            return self._send(404, {"error": "unknown source"})
        old = protocol.require_uint(body.get("old_size"), "old_size")
        new = protocol.require_uint(body.get("new_size"), "new_size")
        if not 0 <= old <= new <= len(log.leaves):
            return self._send(400, {"error": "size outside tree"})
        fault = self.state.next_consistency_fault(source_id)
        with log.lock:
            head_epoch = log.latest_epoch
        frame = log.head_frame(tree_size=new, epoch=head_epoch)
        parsed = protocol.parse_tree_head(frame)
        if old == 0:
            proof: list[bytes] = []
        elif old == new:
            proof = []
        else:
            proof = merkle.consistency_path(log.leaves[:new], old)
        old_root = merkle.tree_hash(log.leaves[:old])
        epoch = parsed.epoch
        if fault:
            kind = fault.get("kind")
            if kind == "bad_proof":
                if proof:
                    proof[0] = hashlib.sha256(proof[0]).digest()
                else:
                    proof = [hashlib.sha256(b"x").digest()]
            elif kind == "short_proof":
                proof = proof[:-1]
            elif kind == "extra_proof":
                proof = proof + [merkle.EMPTY_ROOT]
            elif kind == "wrong_old_root":
                old_root = hashlib.sha256(b"wrong").digest()
            elif kind == "updated_head":
                return self._send(409, {
                    "error": "updated tree head", "source_id": source_id,
                    "head_b64": _b64(log.head_frame()),
                    "updated": True})
            elif kind == "prove_other_size":
                other = fault.get("tree_size", len(log.leaves))
                other = min(max(other, old), len(log.leaves))
                other_frame = log.head_frame(tree_size=other)
                other_parsed = protocol.parse_tree_head(other_frame)
                other_proof = ([] if other == old else
                               merkle.consistency_path(log.leaves[:other],
                                                       old))
                return self._send(200, {
                    "source_id": source_id, "old_size": old,
                    "new_size": other,
                    "old_root": merkle.tree_hash(log.leaves[:old]).hex(),
                    "new_root": other_parsed.root.hex(),
                    "epoch": other_parsed.epoch, "tree_size": other,
                    "root": other_parsed.root.hex(),
                    "head_b64": _b64(other_frame),
                    "proof": [p.hex() for p in other_proof]})
            elif kind == "replay":
                # sign an older size under the current epoch
                old_frame = protocol.make_tree_head(
                    source_id, epoch, old, log.timestamp_ms,
                    old_root, log.keys[epoch])
                return self._send(200, {
                    "source_id": source_id, "replayed": True,
                    "head_b64": _b64(old_frame), "old_size": old,
                    "new_size": old, "old_root": old_root.hex(),
                    "new_root": old_root.hex(), "epoch": epoch,
                    "tree_size": old, "root": old_root.hex(),
                    "proof": []})
        self.state.log({"kind": "consistency", "source_id": source_id,
                        "old_size": old, "new_size": new})
        return self._send(200, {
            "source_id": source_id, "old_size": old, "new_size": new,
            "old_root": old_root.hex(), "new_root": parsed.root.hex(),
            "epoch": epoch, "tree_size": new, "root": parsed.root.hex(),
            "head_b64": _b64(frame),
            "proof": [p.hex() for p in proof]})

    def _entries(self, source_id: str, body: dict):
        try:
            log = self._source(source_id)
        except KeyError:
            return self._send(404, {"error": "unknown source"})
        tree_size = protocol.require_uint(body.get("tree_size"), "tree_size")
        start = protocol.require_uint(body.get("start"), "start")
        count = protocol.require_uint(body.get("count"), "count")
        if count == 0 or count > 100000:
            return self._send(400, {"error": "bad count"})
        if tree_size == 0 or start >= tree_size:
            return self._send(400, {"error": "start outside tree"})
        if tree_size > len(log.leaves):
            return self._send(400, {"error": "tree_size beyond log"})
        fault = self.state.next_entries_fault(source_id)
        if fault and fault.get("only_tree_size") is not None and \
                fault["only_tree_size"] != tree_size:
            # Requeue: the fault is meant for a specific target only.
            with self.state.lock:
                self.state.entries_fault.setdefault(source_id, []).insert(
                    0, fault)
            fault = None
        if fault and fault.get("kind") == "updated_head":
            # The log advanced under the client's feet: append then serve the
            # newer signed head instead of the requested pages.
            extra = fault.get("append", 1)
            if extra:
                log.append([f"{source_id}:late:{len(log.leaves) + k}"
                            .encode() for k in range(extra)])
            frame = log.head_frame()
            self.state.log({"kind": "entries_updated_head",
                            "source_id": source_id,
                            "requested": tree_size})
            return self._send(409, {
                "error": "updated tree head", "source_id": source_id,
                "updated": True, "head_b64": _b64(frame),
                "requested_tree_size": tree_size})
        end = min(start + count, tree_size)
        indices = list(range(start, end))
        # Build the raw page first, then apply faults to it.
        serve_size = tree_size
        entries = []
        for i in indices:
            proof = merkle.inclusion_path(log.leaves[:serve_size], i)
            entries.append({"leaf_index": i, "leaf": log.leaves[i],
                            "proof": proof})
        reported_tree_size = tree_size
        if fault and fault.get("index") is not None and \
                fault.get("kind") in ("inclusion_bad", "bad_proof") and \
                fault["index"] not in [e["leaf_index"] for e in entries]:
            # The fault targets an index not on this page: put it back.
            with self.state.lock:
                self.state.entries_fault.setdefault(source_id, []).insert(
                    0, fault)
            fault = None
        if fault:
            kind = fault.get("kind")
            if kind == "inclusion_bad" and entries:
                target = fault.get("index", entries[0]["leaf_index"])
                for e in entries:
                    if e["leaf_index"] == target:
                        p = list(e["proof"])
                        p = ([hashlib.sha256(b"bad").digest()] if not p
                             else [hashlib.sha256(p[0] + b"!").digest()]
                             + p[1:])
                        e["proof"] = p
            elif kind == "inclusion_short" and entries:
                for e in entries:
                    e["proof"] = e["proof"][:-1]
            elif kind == "inclusion_extra" and entries:
                for e in entries:
                    e["proof"] = e["proof"] + [merkle.EMPTY_ROOT]
            elif kind == "drop_index_always":
                target = fault.get("index")
                entries = [e for e in entries
                           if e["leaf_index"] != target]
                with self.state.lock:
                    self.state.entries_fault.setdefault(source_id, []).append(
                        fault)
            elif kind == "drop_indices":
                drop = set(fault.get("indices", []))
                entries = [e for e in entries
                           if e["leaf_index"] not in drop]
            elif kind == "duplicate_index" and entries:
                entries = entries + [dict(entries[fault.get("at", 0)])]
            elif kind == "conflicting_duplicate" and entries:
                copy = dict(entries[fault.get("at", 0)])
                copy["leaf"] = copy["leaf"] + b"!"
                entries = entries + [copy]
            elif kind == "bad_proof" and entries:
                target = fault.get("index", entries[0]["leaf_index"])
                for e in entries:
                    if e["leaf_index"] == target:
                        p = list(e["proof"])
                        if p:
                            p[0] = hashlib.sha256(p[0] + b"!").digest()
                        else:
                            p = [hashlib.sha256(b"bad").digest()]
                        e["proof"] = p
            elif kind == "short_proof" and entries:
                for e in entries:
                    e["proof"] = e["proof"][:-1]
            elif kind == "extra_proof" and entries:
                for e in entries:
                    e["proof"] = e["proof"] + [merkle.EMPTY_ROOT]
            elif kind == "wrong_tree_size":
                reported_tree_size = tree_size + 1
            elif kind == "older_tree_size":
                reported_tree_size = max(tree_size - 1, 1)
            elif kind == "empty_page":
                entries = []
            elif kind == "out_of_range" and entries:
                entries = entries + [{"leaf_index": tree_size,
                                      "leaf": b"beyond", "proof": []}]
            elif kind == "committed_index" and entries:
                entries = entries + [{"leaf_index": max(start - 1, 0),
                                      "leaf": b"old", "proof": []}]
            elif kind == "foreign_leaf" and entries:
                other = next((l for sid2, l in self.state.sources.items()
                              if sid2 != source_id), None)
                if other is not None and other.leaves:
                    e = entries[0]
                    e["leaf"] = other.leaves[e["leaf_index"] %
                                             len(other.leaves)]
            elif kind == "wrong_root":
                root_override = hashlib.sha256(b"wrong-root").digest()
        out = []
        for e in entries:
            out.append({"leaf_index": e["leaf_index"],
                        "leaf_b64": _b64(e["leaf"]),
                        "proof": [h.hex() for h in e["proof"]]})
        root = (merkle.tree_hash(log.leaves[:tree_size]) if tree_size
                else merkle.EMPTY_ROOT)
        if fault and fault.get("kind") == "wrong_root":
            root = root_override
        reported = (reported_tree_size if fault and
                    fault.get("kind") in ("wrong_tree_size",
                                          "older_tree_size") else tree_size)
        response = {"source_id": source_id, "tree_size": reported,
                    "root": root.hex(), "start": start, "count": len(out),
                    "entries": out}
        self.state.log({"kind": "entries", "source_id": source_id,
                        "tree_size": tree_size, "start": start,
                        "count": len(out)})
        return self._send(200, response)

    # -- admin --------------------------------------------------------------
    def _admin_state(self):
        with self.state.lock:
            out = {}
            for sid, log in self.state.sources.items():
                out[sid] = {
                    "tree_size": len(log.leaves),
                    "latest_epoch": log.latest_epoch,
                    "epochs": sorted(log.keys),
                    "public_keys": {str(e): p.hex()
                                    for e, p in log.public.items()},
                    "certs": {str(e): _b64(c)
                              for e, c in log.certs.items()},
                    "activation_size": {str(k): v for k, v in
                                        log.activation_size.items()},
                }
            return self._send(200, {"sources": out,
                                    "requests": self.state.request_log[-100:]})

    def _admin_append(self, body: dict):
        source_id = protocol.require_str(body.get("source_id"), "source_id")
        log = self._source(source_id)
        if "leaves" in body:
            leaves = [protocol.b64decode_strict(x, "leaf")
                      for x in protocol.require_list(body["leaves"], "leaves")]
        else:
            count = protocol.require_uint(body.get("count"), "count")
            leaves = [f"{source_id}:leaf:{len(log.leaves) + k}"
                      .encode() for k in range(count)]
        log.append(leaves)
        return self._send(200, {"ok": True, "tree_size": len(log.leaves)})

    def _admin_head(self, body: dict):
        source_id = protocol.require_str(body.get("source_id"), "source_id")
        log = self._source(source_id)
        epoch = protocol.require_uint(body.get("epoch"), "epoch")
        size = protocol.require_uint(body.get("tree_size"), "tree_size")
        if epoch not in log.keys:
            return self._send(400, {"error": "unknown epoch"})
        if size > len(log.leaves):
            return self._send(400, {"error": "size beyond log"})
        frame = log.head_frame(epoch=epoch, tree_size=size)
        return self._send(200, {"source_id": source_id,
                                "head_b64": _b64(frame)})

    def _admin_rotate(self, body: dict):
        source_id = protocol.require_str(body.get("source_id"), "source_id")
        log = self._source(source_id)
        activation = protocol.require_uint(body.get("activation_size"),
                                           "activation_size")
        result = log.rotate(activation)
        result["source_id"] = source_id
        return self._send(200, result)

    def _admin_script(self, body: dict):
        source_id = body.get("source_id")
        if source_id is not None:
            protocol.require_str(source_id, "source_id")
            self._source(source_id)
        ops = protocol.require_list(body.get("ops"), "ops")
        for op in ops:
            if not isinstance(op, dict):
                return self._send(400, {"error": "op must be object"})
            kind = op.get("kind")
            sid = op.get("source_id", source_id)
            if kind in ("bad_proof", "short_proof", "extra_proof",
                        "wrong_old_root", "replay", "prove_other_size"):
                self.state.consistency_fault.setdefault(sid, []).append(op)
            elif kind in ("updated_head", "drop_indices",
                          "drop_index_always", "duplicate_index",
                          "inclusion_bad", "inclusion_short",
                          "inclusion_extra", "wrong_tree_size",
                          "older_tree_size",
                          "empty_page", "conflicting_duplicate",
                          "out_of_range", "committed_index", "foreign_leaf",
                          "cross_source", "wrong_root", "updated_head"):
                self.state.entries_fault.setdefault(sid, []).append(op)
            elif kind in ("jump", "self_signed", "wrong_key",
                          "bad_activation"):
                self.state.keys_fault.setdefault(sid, []).append(op)
            elif kind == "bad_head_signature":
                log = self._source(sid)
                frame = bytearray(log.head_frame())
                frame[-1] ^= 0x01
                self.state.head_override[sid] = bytes(frame)
            elif kind == "head_override":
                frame = protocol.b64decode_strict(op.get("head_b64"),
                                                  "head_b64")
                self.state.head_override[sid] = frame
            elif kind == "equivocation":
                # Install a competing head at the same size with a new root.
                log = self._source(sid)
                size = protocol.require_uint(op.get("tree_size"), "tree_size")
                if size > len(log.leaves):
                    return self._send(400, {"error": "size beyond log"})
                fake_root = hashlib.sha256(
                    b"equivocation:" + bytes(str(size), "ascii")).digest()
                frame = protocol.make_tree_head(
                    sid, log.latest_epoch, size, log.timestamp_ms + 7,
                    fake_root, log.keys[log.latest_epoch])
                self.state.head_override[sid] = frame
            else:
                return self._send(400, {"error": f"unknown op {kind}"})
        return self._send(200, {"ok": True})


def make_server(sources: list[tuple[str, int]], port: int = 0) -> tuple[
        ThreadingHTTPServer, dict]:
    state = FixtureState({sid: SourceLog(sid, seed) for sid, seed in sources})
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CTM1 fixture log server")
    ap.add_argument("--source", action="append", default=[],
                    help="source_id[:seed] (repeatable)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--leaves", type=int, default=0,
                    help="initial leaves per source (default synthetic)")
    args = ap.parse_args(argv)
    specs = []
    for spec in args.source or ["alpha:1"]:
        sid, _, seed = spec.partition(":")
        specs.append((sid, int(seed or 1)))
    server, state = make_server(specs, args.port)
    for _, log in state.sources.items():
        if args.leaves:
            log.append([f"{log.source_id}:leaf:{i}".encode()
                        for i in range(args.leaves)])
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                      "sources": [s for s, _ in specs]}))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
