"""Public API: init / import / get / explain / put.

``explain`` and ``put`` share the same contract: both authenticate the token,
load the export record, re-derive the whole batch against the exported source
state and return the *same* derivation (``explain`` stops there).  ``put``
then, inside one ``BEGIN IMMEDIATE`` transaction:

1. atomically compares the export revision (and a one-way hash of the whole
   source state) with the current database — an old export is rejected
   wholesale and the user is asked to ``get`` again; two edits of the same or
   of different old exports are never merged;
2. allocates never-used aids for surviving temp addresses;
3. applies deletes, field updates, renames and inserts in dependency order;
4. re-reads the persisted rows inside the same transaction and re-checks the
   final invariants (label uniqueness, live foreign keys, exact identity
   sets, no resurrected row);
5. records the batch and increments the revision — only for a non-empty
   action batch.

Any failure — including a deliberately failing last action — rolls back the
whole transaction: no derived write, no aid consumption, no revision bump.
An empty action batch is a no-op on the revision, the source fields and the
allocator, but still validates the token and refuses a stale export.
"""

from __future__ import annotations

import json
import secrets
import time

from . import store
from .actions import parse_actions
from .derive import apply_derived, derive_batch
from .errors import (
    ConflictError,
    RowRefError,
    StaleTokenError,
    TokenError,
)
from .tokens import verify_token, view_hash
from .view import export_snapshot, load_export

__all__ = [
    "init_db",
    "import_sample",
    "set_config",
    "source_tables",
    "get",
    "explain",
    "put",
]


def init_db(db_path, sample=None, config=None, force: bool = False) -> dict:
    return store.init_database(db_path, sample=sample, config=config,
                               force=force)


def import_sample(db_path, sample, replace: bool = True) -> dict:
    return store.import_sample(db_path, sample, replace=replace)


def set_config(db_path, config) -> dict:
    return store.set_config(db_path, config)


def source_tables(db_path) -> dict:
    """Dump both source relations (hidden fields included), for local use."""
    conn = store.open_existing(db_path)
    try:
        return store.source_tables(conn)
    finally:
        conn.close()


def get(db_path) -> dict:
    """Export a consistent, editable view plus snapshot_token and row refs."""
    return export_snapshot(db_path)


def _all_conflicts(derived: dict) -> list[dict]:
    return list(derived["structural_conflicts"]) + list(derived["conflicts"])


def _prepare(db_path, token, actions_payload):
    """Authenticate the token and derive the batch without writing anything.

    Returns ``(conn, export, derived, actions)``; the caller owns the open
    connection and must close it.
    """
    conn = store.open_existing(db_path)
    try:
        secret = store.get_meta(conn, "hmac_secret")
        payload = verify_token(secret, token)
        export = load_export(conn, payload)
        db_identity = store.get_meta(conn, "db_identity")
        if payload["db_identity"] != db_identity:
            raise TokenError("snapshot_token belongs to another database")
        config = store.get_config(conn)
        if store.config_hash(config) != export.config_hash:
            raise TokenError("snapshot_token was produced under another "
                             "insert-default config")
        if view_hash(config) != export.view_hash:
            raise TokenError("snapshot_token was produced under another view "
                             "definition")
        actions = parse_actions(actions_payload)
        derived = derive_batch(export.view_state, export, actions,
                               json.loads(json.dumps(config)),
                               known_aids=store.used_aids(conn))
        if derived["structural_conflicts"]:
            # a foreign / tampered / impersonating reference is not a batch
            # conflict but an invalid reference: fail closed, write nothing
            raise RowRefError(
                "invalid action reference(s): %d problem(s)"
                % len(derived["structural_conflicts"]),
                problems=derived["structural_conflicts"],
            )
        return conn, export, derived, actions
    except Exception:
        conn.close()
        raise


def _public_derivation(derived: dict) -> dict:
    return {
        "conflicts": _all_conflicts(derived),
        "writes": derived["writes"],
        "customers_changed": derived["customers_changed"],
        "addresses_changed": derived["addresses_changed"],
        "addresses_added": derived["addresses_added"],
        "addresses_deleted": derived["addresses_deleted"]["items"],
        "customers_deleted": derived["customers_deleted"],
        "pending_temp_refs": derived["pending"],
        "insert_defaults": derived["insert_defaults"],
        "actions": derived["actions"],
    }


def explain(db_path, token, actions=None) -> dict:
    """Derive only: no write, no aid consumption, no revision advance."""
    conn, export, derived, parsed = _prepare(db_path, token, actions)
    try:
        current = store.get_revision(conn)
        report = {
            "ok": derived["ok"],
            "mode": "explain",
            "wrote": False,
            "export_id": export.export_id,
            "export_revision": export.revision,
            "current_revision": current,
            "stale": current != export.revision,
            "action_count": len(parsed),
        }
        report.update(_public_derivation(derived))
        if current != export.revision:
            report["warnings"] = [
                "export revision %d is older than current revision %d; put "
                "will refuse this batch and require a fresh get"
                % (export.revision, current)
            ]
        return report
    finally:
        conn.close()


def put(db_path, token, actions=None) -> dict:
    """Apply exactly one action batch atomically and return its result."""
    conn, export, derived, parsed = _prepare(db_path, token, actions)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = store.get_revision(conn)
            if current != export.revision:
                raise StaleTokenError(
                    "token revision %d, current revision %d; call get again "
                    "and resubmit — batches are never merged automatically"
                    % (export.revision, current),
                    export_revision=export.revision,
                    current_revision=current,
                )
            live_state = store.load_state(conn)
            if store.state_hash(live_state) != export.state_hash:
                raise StaleTokenError(
                    "the source state changed after this export (revision "
                    "unchanged); call get again")
            if derived["conflicts"] or derived["structural_conflicts"]:
                raise ConflictError(
                    "batch rejected: %d conflict(s)"
                    % len(_all_conflicts(derived)),
                    conflicts=_all_conflicts(derived),
                )
            if not parsed:
                conn.commit()
                return {
                    "ok": True,
                    "mode": "put",
                    "applied": False,
                    "empty_batch": True,
                    "revision_before": current,
                    "revision_after": current,
                    "new_address_aids": {},
                    "writes": [],
                    "action_count": 0,
                }

            conn.execute("SAVEPOINT apply_batch")
            try:
                new_aids: dict[str, str] = {}
                if derived["addresses_added"]:
                    allocated = store.allocate_aids(
                        conn, len(derived["addresses_added"]))
                    new_aids = dict(zip(
                        [e["temp_ref"] for e in derived["addresses_added"]],
                        allocated))
                mapped = apply_derived(conn, derived, new_aids)
                _verify_final_state(conn, derived, mapped)
                after = current + 1
                store.set_revision(conn, after)
                result = {
                    "ok": True,
                    "mode": "put",
                    "applied": True,
                    "empty_batch": False,
                    "export_id": export.export_id,
                    "revision_before": current,
                    "revision_after": after,
                    "new_address_aids": mapped,
                    "temp_refs": {
                        temp: (None if rec["deleted"] else mapped.get(temp))
                        for temp, rec in derived["pending"].items()
                    },
                    "action_count": len(parsed),
                }
                result.update(_public_derivation(derived))
                result["addresses_added"] = [
                    {**entry, "aid": mapped[entry["temp_ref"]]}
                    for entry in derived["addresses_added"]
                ]
                batch_id = "bat_" + secrets.token_hex(10)
                conn.execute(
                    "INSERT INTO cw_batches(batch_id, created_at, export_id, "
                    "revision_before, revision_after, actions_json, "
                    "result_json) VALUES(?,?,?,?,?,?,?)",
                    (batch_id,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     export.export_id, current, after,
                     json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                     json.dumps(result, ensure_ascii=False,
                                sort_keys=True)))
                conn.execute("RELEASE apply_batch")
                conn.commit()
                result["batch_id"] = batch_id
                return result
            except Exception:
                conn.execute("ROLLBACK TO apply_batch")
                conn.execute("RELEASE apply_batch")
                raise
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _verify_final_state(conn, derived: dict, mapped: dict) -> None:
    """Re-read the persisted source and re-check every final invariant."""
    actual = store.load_state(conn)
    expected = derived["final_state"]

    expected_customers = {
        cid: {"name": c["name"], "internal_note": c["internal_note"]}
        for cid, c in expected["customers"].items()
    }
    if actual["customers"] != expected_customers:
        raise ConflictError(
            "post-write check failed: persisted customers differ from the "
            "derived final state")
    expected_addresses = {
        aid: dict(a) for aid, a in expected["addresses"].items()
    }
    for entry in derived["addresses_added"]:
        expected_addresses[mapped[entry["temp_ref"]]] = {
            "cid": entry["cid"],
            "label": entry["label"],
            "address": entry["address"],
            "secret_tag": entry["secret_tag"],
        }
    if actual["addresses"] != expected_addresses:
        raise ConflictError(
            "post-write check failed: persisted addresses differ from the "
            "derived final state")
    seen: dict[tuple, str] = {}
    for aid, a in actual["addresses"].items():
        if a["cid"] not in actual["customers"]:
            raise ConflictError(
                "post-write check failed: address %r references missing "
                "customer %r" % (aid, a["cid"]))
        key = (a["cid"], a["label"])
        if key in seen:
            raise ConflictError(
                "post-write check failed: duplicate label %r for customer %r"
                % (a["label"], a["cid"]))
        seen[key] = aid
