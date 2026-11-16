"""SQLite persistence for the CTM1 mirror client.

Two disjoint storage areas are kept for every source:

  * committed_leaves / heads  -- data already promised to downstream
  * candidate_leaves / candidates -- one in-progress target per source

They are separated by tables, and promotion moves rows and publishes the new
head inside one IMMEDIATE transaction, so a reader can never observe a new
head with missing leaves (or vice versa).

All reads of public state use a single SQLite read transaction so a status
and its committed size/count come from one snapshot.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import merkle

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    anchor_epoch INTEGER NOT NULL,
    anchor_public_key BLOB NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    blocked_detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    epoch INTEGER NOT NULL,
    public_key BLOB NOT NULL,
    activation_size INTEGER NOT NULL,
    cert_frame BLOB,
    verified INTEGER NOT NULL,
    activated INTEGER NOT NULL,
    activated_at TEXT,
    activation_committed_size INTEGER,
    PRIMARY KEY (source_id, epoch)
);

CREATE TABLE IF NOT EXISTS heads (
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    tree_size INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    root BLOB NOT NULL,
    signature BLOB NOT NULL,
    frame BLOB NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, tree_size)
);

CREATE TABLE IF NOT EXISTS committed_leaves (
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    leaf_index INTEGER NOT NULL,
    leaf BLOB NOT NULL,
    leaf_hash BLOB NOT NULL,
    PRIMARY KEY (source_id, leaf_index)
);

CREATE TABLE IF NOT EXISTS candidates (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id),
    tree_size INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    root BLOB NOT NULL,
    signature BLOB NOT NULL,
    frame BLOB NOT NULL,
    base_size INTEGER NOT NULL,
    base_root BLOB NOT NULL,
    consistency_proof TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_leaves (
    source_id TEXT NOT NULL,
    leaf_index INTEGER NOT NULL,
    leaf BLOB NOT NULL,
    leaf_hash BLOB NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (source_id, leaf_index),
    FOREIGN KEY (source_id) REFERENCES candidates(source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deferred_heads (
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    tree_size INTEGER NOT NULL,
    root BLOB NOT NULL,
    epoch INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    signature BLOB NOT NULL,
    frame BLOB NOT NULL,
    relation TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, tree_size, root)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    epoch INTEGER,
    tree_size INTEGER,
    root BLOB,
    frame BLOB,
    note TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,
    event_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS page_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    tree_size INTEGER NOT NULL,
    request_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    returned_count INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    stored_count INTEGER NOT NULL,
    response_bytes INTEGER NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StoreError(RuntimeError):
    pass


class StoreCorrupt(StoreError):
    pass


class ConflictError(StoreError):
    pass


@dataclass(frozen=True)
class CommittedHead:
    size: int
    root: bytes
    epoch: Optional[int]
    timestamp_ms: Optional[int]
    frame: Optional[bytes]

    @property
    def root_hex(self) -> str:
        return self.root.hex()


@dataclass(frozen=True)
class CandidateHead:
    size: int
    root: bytes
    epoch: int
    timestamp_ms: int
    frame: bytes
    base_size: int
    base_root: bytes

    @property
    def root_hex(self) -> str:
        return self.root.hex()


@dataclass(frozen=True)
class Generation:
    source_id: str
    epoch: int
    public_key: bytes
    activation_size: int
    cert_frame: Optional[bytes]
    verified: bool
    activated: bool


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA cache_size=-2048")
        self.conn.executescript(SCHEMA)

    # -- transactions -------------------------------------------------------
    @contextmanager
    def write_tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @contextmanager
    def read_tx(self):
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def event(self, source_id: Optional[str], kind: str, detail: str = "",
              payload=None) -> None:
        body = None
        if payload is not None:
            text = json.dumps(payload, sort_keys=True)
            body = text[:262144]
        with self.write_tx() as conn:
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail, payload)"
                " VALUES(?,?,?,?,?)",
                (source_id, utcnow(), kind, detail, body))

    # -- sources / generations ---------------------------------------------
    def ensure_source(self, source_id: str, anchor_epoch: int,
                      anchor_public_key: bytes) -> None:
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT anchor_epoch, anchor_public_key FROM sources"
                " WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sources(source_id, anchor_epoch,"
                    " anchor_public_key, blocked, created_at)"
                    " VALUES(?,?,?,0,?)",
                    (source_id, anchor_epoch, anchor_public_key, utcnow()))
                conn.execute(
                    "INSERT INTO generations(source_id, epoch, public_key,"
                    " activation_size, cert_frame, verified, activated,"
                    " activated_at, activation_committed_size)"
                    " VALUES(?,?,?,0,NULL,1,1,?,0)",
                    (source_id, anchor_epoch, anchor_public_key, utcnow()))
            else:
                if (row["anchor_epoch"] != anchor_epoch or
                        bytes(row["anchor_public_key"]) != anchor_public_key):
                    raise StoreError(
                        f"manifest anchor for {source_id} changed; refusing")

    def source_exists(self, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return row is not None

    def list_sources(self) -> list[str]:
        return [r["source_id"] for r in self.conn.execute(
            "SELECT source_id FROM sources ORDER BY source_id")]

    def get_source(self, source_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()

    def set_blocked(self, source_id: str, reason: str, detail: str,
                    payload=None) -> None:
        body = json.dumps(payload, sort_keys=True)[:262144] if payload else None
        with self.write_tx() as conn:
            conn.execute(
                "UPDATE sources SET blocked=1, blocked_reason=?,"
                " blocked_detail=? WHERE source_id=?",
                (reason, detail[:8192], source_id))
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail, payload)"
                " VALUES(?,?,?,?,?)",
                (source_id, utcnow(), "blocked", reason, body))

    def clear_blocked(self, source_id: str, how: str) -> None:
        with self.write_tx() as conn:
            conn.execute(
                "UPDATE sources SET blocked=0, blocked_reason=NULL,"
                " blocked_detail=NULL WHERE source_id=?", (source_id,))
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail)"
                " VALUES(?,?,?,?)",
                (source_id, utcnow(), "unblocked", how))

    def get_generation(self, source_id: str,
                       epoch: int) -> Optional[Generation]:
        row = self.conn.execute(
            "SELECT * FROM generations WHERE source_id=? AND epoch=?",
            (source_id, epoch)).fetchone()
        if row is None:
            return None
        return Generation(
            source_id=row["source_id"], epoch=row["epoch"],
            public_key=bytes(row["public_key"]),
            activation_size=row["activation_size"],
            cert_frame=bytes(row["cert_frame"]) if row["cert_frame"] else None,
            verified=bool(row["verified"]), activated=bool(row["activated"]))

    def list_generations(self, source_id: str) -> list[Generation]:
        return [
            Generation(
                source_id=r["source_id"], epoch=r["epoch"],
                public_key=bytes(r["public_key"]),
                activation_size=r["activation_size"],
                cert_frame=bytes(r["cert_frame"]) if r["cert_frame"] else None,
                verified=bool(r["verified"]), activated=bool(r["activated"]))
            for r in self.conn.execute(
                "SELECT * FROM generations WHERE source_id=? ORDER BY epoch",
                (source_id,))]

    def add_verified_generation(self, source_id: str, epoch: int,
                                public_key: bytes, activation_size: int,
                                cert_frame: bytes) -> str:
        """Insert a cert-verified successor generation (not yet activated).

        Returns "added", "present" or raises ConflictError when the registered
        key material disagrees with an existing registration.
        """
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT public_key, activation_size, cert_frame FROM"
                " generations WHERE source_id=? AND epoch=?",
                (source_id, epoch)).fetchone()
            if row is not None:
                if (bytes(row["public_key"]) != public_key or
                        row["activation_size"] != activation_size or
                        (row["cert_frame"] is not None and
                         bytes(row["cert_frame"]) != cert_frame)):
                    raise ConflictError(
                        f"conflicting registered key for {source_id}"
                        f" epoch {epoch}")
                return "present"
            conn.execute(
                "INSERT INTO generations(source_id, epoch, public_key,"
                " activation_size, cert_frame, verified, activated)"
                " VALUES(?,?,?,?,?,1,0)",
                (source_id, epoch, public_key, activation_size, cert_frame))
            return "added"

    def activate_generation(self, source_id: str, epoch: int,
                            committed_size: int) -> None:
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT verified FROM generations WHERE source_id=?"
                " AND epoch=?", (source_id, epoch)).fetchone()
            if row is None or not row["verified"]:
                raise StoreError("cannot activate unverified generation")
            conn.execute(
                "UPDATE generations SET activated=1, activated_at=?,"
                " activation_committed_size=? WHERE source_id=? AND epoch=?",
                (utcnow(), committed_size, source_id, epoch))

    # -- heads --------------------------------------------------------------
    def committed_head(self, source_id: str) -> CommittedHead:
        row = self.conn.execute(
            "SELECT * FROM heads WHERE source_id=?"
            " ORDER BY tree_size DESC LIMIT 1", (source_id,)).fetchone()
        if row is None:
            return CommittedHead(0, merkle.EMPTY_ROOT, None, None, None)
        return CommittedHead(
            size=row["tree_size"], root=bytes(row["root"]),
            epoch=row["epoch"], timestamp_ms=row["timestamp_ms"],
            frame=bytes(row["frame"]))

    def committed_leaves_count(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(leaf_index), -1) AS m"
            " FROM committed_leaves WHERE source_id=?",
            (source_id,)).fetchone()
        if row["c"] != row["m"] + 1:
            raise StoreCorrupt(
                f"committed leaves for {source_id} are not contiguous")
        return row["c"]

    def heads_history(self, source_id: str) -> list[dict]:
        return [
            {"tree_size": r["tree_size"], "epoch": r["epoch"],
             "timestamp_ms": r["timestamp_ms"],
             "root": bytes(r["root"]).hex(),
             "frame_b64": base64.b64encode(bytes(r["frame"])).decode("ascii")}
            for r in self.conn.execute(
                "SELECT * FROM heads WHERE source_id=?"
                " ORDER BY tree_size", (source_id,))]

    # -- candidates ---------------------------------------------------------
    def candidate(self, source_id: str) -> Optional[CandidateHead]:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE source_id=?",
            (source_id,)).fetchone()
        if row is None:
            return None
        return CandidateHead(
            size=row["tree_size"], root=bytes(row["root"]),
            epoch=row["epoch"], timestamp_ms=row["timestamp_ms"],
            frame=bytes(row["frame"]), base_size=row["base_size"],
            base_root=bytes(row["base_root"]))

    def create_candidate(self, source_id: str, head, base_size: int,
                         base_root: bytes, proof: list[bytes]) -> None:
        """Atomically install a new candidate, clearing any old staging."""
        proof_json = json.dumps([p.hex() for p in proof])
        with self.write_tx() as conn:
            conn.execute(
                "DELETE FROM candidates WHERE source_id=?", (source_id,))
            conn.execute(
                "INSERT INTO candidates(source_id, tree_size, epoch,"
                " timestamp_ms, root, signature, frame, base_size, base_root,"
                " consistency_proof, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, head.tree_size, head.epoch, head.timestamp_ms,
                 head.root, head.signature, head.frame, base_size, base_root,
                 proof_json, utcnow()))
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail)"
                " VALUES(?,?,?,?)",
                (source_id, utcnow(), "candidate_selected",
                 f"size={head.tree_size} epoch={head.epoch}"))

    def staged_indices(self, source_id: str) -> list[int]:
        return [r["leaf_index"] for r in self.conn.execute(
            "SELECT leaf_index FROM candidate_leaves WHERE source_id=?"
            " ORDER BY leaf_index", (source_id,))]

    def has_staged(self, source_id: str, index: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM candidate_leaves WHERE source_id=?"
            " AND leaf_index=?", (source_id, index)).fetchone() is not None

    def staged_count(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM candidate_leaves WHERE source_id=?",
            (source_id,)).fetchone()
        return row["c"]

    def missing_ranges(self, source_id: str, start: int,
                       end: int) -> list[tuple[int, int]]:
        """Half-open [start,end) ranges with no verified staged leaf.

        Streams the sorted index cursor and keeps only the gap intervals, so
        memory is O(number of gaps), not O(number of leaves).
        """
        if end <= start:
            return []
        gaps = []
        expected = start
        cur = self.conn.execute(
            "SELECT leaf_index FROM candidate_leaves WHERE source_id=?"
            " AND leaf_index>=? AND leaf_index<? ORDER BY leaf_index",
            (source_id, start, end))
        for row in cur:
            idx = row["leaf_index"]
            if idx < expected:
                raise StoreCorrupt("duplicate staged index")
            if idx > expected:
                gaps.append((expected, idx))
            expected = idx + 1
        if expected < end:
            gaps.append((expected, end))
        return gaps

    def stage_leaf(self, source_id: str, index: int, leaf: bytes,
                   leaf_hash: bytes) -> str:
        """Stage one leaf; returns 'stored' or 'folded'.

        Raises ConflictError (rolling back the caller's page transaction) if
        an existing staged value at the same index differs.
        """
        with self.write_tx() as conn:
            cand = conn.execute(
                "SELECT tree_size, base_size FROM candidates"
                " WHERE source_id=?", (source_id,)).fetchone()
            if cand is None:
                raise StoreError("no candidate; refusing to stage")
            if not cand["base_size"] <= index < cand["tree_size"]:
                raise StoreError("staged index outside candidate range")
            row = conn.execute(
                "SELECT leaf, leaf_hash FROM candidate_leaves"
                " WHERE source_id=? AND leaf_index=?",
                (source_id, index)).fetchone()
            if row is not None:
                if bytes(row["leaf"]) == leaf and \
                        bytes(row["leaf_hash"]) == leaf_hash:
                    return "folded"
                raise ConflictError(
                    f"staged leaf {index} differs from this page")
            conn.execute(
                "INSERT INTO candidate_leaves(source_id, leaf_index, leaf,"
                " leaf_hash, added_at) VALUES(?,?,?,?,?)",
                (source_id, index, leaf, leaf_hash, utcnow()))
            return "stored"

    def stage_page(self, source_id: str, entries: list[tuple[int, bytes,
                                                              bytes]]) -> int:
        """Atomically stage a whole page.  Any conflict rolls all back."""
        stored = 0
        with self.write_tx() as conn:
            cand = conn.execute(
                "SELECT tree_size, base_size FROM candidates"
                " WHERE source_id=?", (source_id,)).fetchone()
            if cand is None:
                raise StoreError("no candidate; refusing to stage")
            for index, leaf, leaf_hash in entries:
                if not cand["base_size"] <= index < cand["tree_size"]:
                    raise StoreError("staged index outside candidate range")
                row = conn.execute(
                    "SELECT leaf, leaf_hash FROM candidate_leaves"
                    " WHERE source_id=? AND leaf_index=?",
                    (source_id, index)).fetchone()
                if row is not None:
                    if bytes(row["leaf"]) == leaf and \
                            bytes(row["leaf_hash"]) == leaf_hash:
                        continue
                    raise ConflictError(
                        f"staged leaf {index} differs from this page")
                conn.execute(
                    "INSERT INTO candidate_leaves(source_id, leaf_index, leaf,"
                    " leaf_hash, added_at) VALUES(?,?,?,?,?)",
                    (source_id, index, leaf, leaf_hash, utcnow()))
                stored += 1
        return stored

    def page_stat(self, source_id: str, tree_size: int, start: int,
                  count: int, returned: int, accepted: bool, stored: int,
                  response_bytes: int) -> None:
        with self.write_tx() as conn:
            conn.execute(
                "INSERT INTO page_stats(source_id, fetched_at, tree_size,"
                " request_start, request_count, returned_count, accepted,"
                " stored_count, response_bytes) VALUES(?,?,?,?,?,?,?,?,?)",
                (source_id, utcnow(), tree_size, start, count, returned,
                 1 if accepted else 0, stored, response_bytes))

    def page_stats(self, source_id: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM page_stats WHERE source_id=?"
            " ORDER BY id DESC LIMIT ?", (source_id, limit))
        return [dict(r) for r in rows]

    def add_deferred_head(self, source_id: str, head, relation: str) -> None:
        """Persist a verified signed head seen while a candidate was bound."""
        with self.write_tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deferred_heads(source_id, tree_size,"
                " root, epoch, timestamp_ms, signature, frame, relation,"
                " observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (source_id, head.tree_size, head.root, head.epoch,
                 head.timestamp_ms, head.signature, head.frame, relation,
                 utcnow()))

    def deferred_heads(self, source_id: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM deferred_heads WHERE source_id=?"
            " ORDER BY tree_size DESC, observed_at DESC", (source_id,))
        return [{"tree_size": r["tree_size"], "root": bytes(r["root"]),
                 "epoch": r["epoch"], "timestamp_ms": r["timestamp_ms"],
                 "signature": bytes(r["signature"]),
                 "frame": bytes(r["frame"]), "relation": r["relation"],
                 "observed_at": r["observed_at"]} for r in rows]

    def purge_deferred_heads(self, source_id: str, up_to_size: int) -> int:
        with self.write_tx() as conn:
            cur = conn.execute(
                "DELETE FROM deferred_heads WHERE source_id=?"
                " AND tree_size<=?", (source_id, up_to_size))
            return cur.rowcount

    def forget_deferred_head(self, source_id: str, tree_size: int,
                             root: bytes) -> None:
        with self.write_tx() as conn:
            conn.execute(
                "DELETE FROM deferred_heads WHERE source_id=?"
                " AND tree_size=? AND root=?",
                (source_id, tree_size, root))

    def observation(self, source_id: str, kind: str, head=None,
                    note: str = "") -> None:
        with self.write_tx() as conn:
            conn.execute(
                "INSERT INTO observations(source_id, observed_at, kind,"
                " epoch, tree_size, root, frame, note)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (source_id, utcnow(), kind,
                 getattr(head, "epoch", None),
                 getattr(head, "tree_size", None),
                 getattr(head, "root", None),
                 getattr(head, "frame", None), note[:8192]))

    def observations(self, source_id: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE source_id=?"
            " ORDER BY id DESC LIMIT ?", (source_id, limit))
        out = []
        for r in rows:
            out.append({
                "kind": r["kind"], "at": r["observed_at"],
                "epoch": r["epoch"], "tree_size": r["tree_size"],
                "root": bytes(r["root"]).hex() if r["root"] else None,
                "frame_b64": (base64.b64encode(bytes(r["frame"]))
                              .decode("ascii") if r["frame"] else None),
                "note": r["note"]})
        return out

    def events(self, source_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE source_id=? OR source_id IS NULL"
            " ORDER BY id DESC LIMIT ?", (source_id, limit))
        return [dict(r) for r in rows]

    # -- promotion ----------------------------------------------------------
    def _iter_hashes(self, conn, source_id: str, base_size: int,
                     size: int) -> Iterator[bytes]:
        expected = 0
        cur = conn.execute(
            "SELECT leaf_index, leaf_hash FROM committed_leaves"
            " WHERE source_id=? AND leaf_index<? ORDER BY leaf_index",
            (source_id, base_size))
        for row in cur:
            if row["leaf_index"] != expected:
                raise StoreCorrupt("committed leaf sequence hole")
            expected += 1
            yield bytes(row["leaf_hash"])
        if expected != base_size:
            raise StoreCorrupt("committed leaf prefix incomplete")
        cur = conn.execute(
            "SELECT leaf_index, leaf_hash FROM candidate_leaves"
            " WHERE source_id=? AND leaf_index>=? AND leaf_index<?"
            " ORDER BY leaf_index", (source_id, base_size, size))
        for row in cur:
            if row["leaf_index"] != expected:
                raise StoreCorrupt("candidate leaf sequence hole")
            expected += 1
            yield bytes(row["leaf_hash"])
        if expected != size:
            raise StoreCorrupt("candidate leaves incomplete")

    @staticmethod
    def _root_from_hashes(hashes: Iterator[bytes]) -> bytes:
        stack: list[tuple[int, bytes]] = []
        count = 0
        for h in hashes:
            level = 0
            while stack and stack[-1][0] == level:
                _, left = stack.pop()
                h = merkle.node_hash(left, h)
                level += 1
            stack.append((level, h))
            count += 1
        if count == 0:
            return merkle.EMPTY_ROOT
        root = stack[-1][1]
        for _, h in reversed(stack[:-1]):
            root = merkle.node_hash(h, root)
        return root

    def promote_candidate(self, source_id: str) -> CandidateHead:
        """Move a complete, verified candidate to committed atomically."""
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE source_id=?",
                (source_id,)).fetchone()
            if row is None:
                raise StoreError("no candidate to promote")
            head = CandidateHead(
                size=row["tree_size"], root=bytes(row["root"]),
                epoch=row["epoch"], timestamp_ms=row["timestamp_ms"],
                frame=bytes(row["frame"]), base_size=row["base_size"],
                base_root=bytes(row["base_root"]))
            committed = conn.execute(
                "SELECT tree_size, root FROM heads WHERE source_id=?"
                " ORDER BY tree_size DESC LIMIT 1", (source_id,)).fetchone()
            committed_size = committed["tree_size"] if committed else 0
            committed_root = (bytes(committed["root"]) if committed
                              else merkle.EMPTY_ROOT)
            if head.base_size != committed_size or \
                    head.base_root != committed_root:
                raise StoreCorrupt(
                    "candidate base no longer matches committed state")
            count = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(leaf_index), -1) AS m"
                " FROM committed_leaves WHERE source_id=?",
                (source_id,)).fetchone()
            if count["c"] != committed_size or count["m"] != committed_size - 1:
                raise StoreCorrupt("committed leaves do not match head size")
            staged = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MIN(leaf_index), -1) AS lo,"
                " COALESCE(MAX(leaf_index), -1) AS hi FROM candidate_leaves"
                " WHERE source_id=?", (source_id,)).fetchone()
            expected = head.size - committed_size
            if (staged["c"] != expected or staged["lo"] != committed_size or
                    staged["hi"] != head.size - 1):
                raise StoreCorrupt("candidate staging does not cover range")
            computed = self._root_from_hashes(
                self._iter_hashes(conn, source_id, committed_size, head.size))
            if computed != head.root:
                raise StoreCorrupt(
                    "recomputed candidate root does not match signed head")
            conn.execute(
                "INSERT INTO committed_leaves(source_id, leaf_index, leaf,"
                " leaf_hash) SELECT source_id, leaf_index, leaf, leaf_hash"
                " FROM candidate_leaves WHERE source_id=?", (source_id,))
            conn.execute(
                "INSERT INTO heads(source_id, tree_size, epoch, timestamp_ms,"
                " root, signature, frame, committed_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (source_id, head.size, head.epoch, head.timestamp_ms,
                 head.root, row["signature"], head.frame, utcnow()))
            conn.execute("DELETE FROM candidate_leaves WHERE source_id=?",
                         (source_id,))
            conn.execute("DELETE FROM candidates WHERE source_id=?",
                         (source_id,))
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail)"
                " VALUES(?,?,?,?)",
                (source_id, utcnow(), "promoted",
                 f"size={head.size} epoch={head.epoch}"))
            return head

    def discard_candidate(self, source_id: str, how: str) -> int:
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT tree_size FROM candidates WHERE source_id=?",
                (source_id,)).fetchone()
            if row is None:
                raise StoreError("no candidate to discard")
            conn.execute("DELETE FROM candidate_leaves WHERE source_id=?",
                         (source_id,))
            conn.execute("DELETE FROM candidates WHERE source_id=?",
                         (source_id,))
            conn.execute(
                "INSERT INTO events(source_id, event_at, kind, detail)"
                " VALUES(?,?,?,?)",
                (source_id, utcnow(), "candidate_discarded",
                 f"size={row['tree_size']} via={how}"))
            return row["tree_size"]

    # -- committed reads -----------------------------------------------------
    def committed_snapshot(self, source_id: str) -> dict:
        with self.read_tx() as conn:
            head = conn.execute(
                "SELECT * FROM heads WHERE source_id=?"
                " ORDER BY tree_size DESC LIMIT 1",
                (source_id,)).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(leaf_index), -1) AS m"
                " FROM committed_leaves WHERE source_id=?",
                (source_id,)).fetchone()
            size = head["tree_size"] if head else 0
            if count["c"] != size or count["m"] != size - 1:
                raise StoreCorrupt(
                    f"committed entries {count['c']} != head size {size}")
            return {
                "tree_size": size,
                "root": bytes(head["root"]).hex() if head else
                        merkle.EMPTY_ROOT.hex(),
                "epoch": head["epoch"] if head else None,
                "timestamp_ms": head["timestamp_ms"] if head else None,
                "frame_b64": (base64.b64encode(bytes(head["frame"]))
                              .decode("ascii") if head else None),
                "entry_count": count["c"],
            }

    def iter_committed_entries(self, source_id: str, start: int,
                               end: int) -> Iterator[tuple[int, bytes]]:
        """Stream committed leaves in [start,end).

        No long-lived read transaction is held: promotion only ever appends
        above the committed size, so rows in an already-committed range are
        immutable.  Each batch is a single statement, keeping memory bounded
        by the batch size rather than the requested range.
        """
        snapshot = self.conn.execute(
            "SELECT tree_size FROM heads WHERE source_id=?"
            " ORDER BY tree_size DESC LIMIT 1", (source_id,)).fetchone()
        size = snapshot["tree_size"] if snapshot else 0
        if not 0 <= start <= end <= size:
            raise StoreError(
                f"range [{start},{end}) outside committed size {size}")
        expected = start
        while expected < end:
            batch_end = min(expected + 256, end)
            cur = self.conn.execute(
                "SELECT leaf_index, leaf FROM committed_leaves"
                " WHERE source_id=? AND leaf_index>=? AND leaf_index<?"
                " ORDER BY leaf_index", (source_id, expected, batch_end))
            got = 0
            for row in cur:
                if row["leaf_index"] != expected:
                    raise StoreCorrupt("committed read hole")
                expected += 1
                got += 1
                yield row["leaf_index"], bytes(row["leaf"])
            if expected != batch_end:
                raise StoreCorrupt("committed read short")
            if got == 0:
                break

    def committed_entries(self, source_id: str, start: int,
                          end: int) -> list[bytes]:
        return [leaf for _, leaf in
                self.iter_committed_entries(source_id, start, end)]

    def iter_candidate_entries(self, source_id: str, start: int,
                               end: int) -> Iterator[tuple[int, bytes]]:
        """Stream staged candidate leaves in [start,end), verifying hashes.

        Batched statements (no long-lived transaction) so memory stays
        bounded while still checking every leaf hash.
        """
        expected = start
        while expected < end:
            batch_end = min(expected + 256, end)
            cur = self.conn.execute(
                "SELECT leaf_index, leaf, leaf_hash FROM candidate_leaves"
                " WHERE source_id=? AND leaf_index>=? AND leaf_index<?"
                " ORDER BY leaf_index", (source_id, expected, batch_end))
            got = 0
            for row in cur:
                if row["leaf_index"] != expected:
                    raise StoreCorrupt("candidate read hole")
                leaf = bytes(row["leaf"])
                if merkle.leaf_hash(leaf) != bytes(row["leaf_hash"]):
                    raise StoreCorrupt(
                        f"staged leaf {expected} hash does not match content")
                expected += 1
                got += 1
                yield row["leaf_index"], leaf
            if expected != batch_end:
                raise StoreCorrupt("candidate read short")
            if got == 0:
                break

    def staged_details(self, source_id: str, limit: Optional[int] = None,
                       with_content: bool = False) -> list[dict]:
        q = ("SELECT leaf_index, leaf, leaf_hash FROM candidate_leaves"
             " WHERE source_id=? ORDER BY leaf_index")
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        out = []
        for r in self.conn.execute(q, (source_id,)):
            item = {"leaf_index": r["leaf_index"],
                    "leaf_sha256": bytes(r["leaf_hash"]).hex(),
                    "leaf_size": len(r["leaf"])}
            if with_content:
                item["leaf_b64"] = base64.b64encode(
                    bytes(r["leaf"])).decode("ascii")
            out.append(item)
        return out

    def db_stats(self) -> dict:
        page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        freelist = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        return {"page_count": page_count, "page_size": page_size,
                "freelist_count": freelist,
                "approx_bytes": page_count * page_size}
