"""Read side: an immutable, consistent snapshot and the editable view.

``get`` reads both source relations at one consistent SQLite snapshot (a
read transaction), exports them and then, in a separate write transaction,
persists the export record.  The returned view has one row per
``C LEFT JOIN A`` pair: a customer with three addresses appears three times.

Ordering is by ``cid`` and then ``aid`` in **Unicode code point** order (the
plain Python ``str`` order), across all rows of the whole database.  A
customer without any address still occupies exactly one row, with
``row_kind = "placeholder_no_address"``, ``aid_display = ""``, empty label /
address cells, and a row reference of its own.  That placeholder is a
dedicated kind, so it can never be confused with a real address whose
``address`` is NULL (``address_address`` cell ``"null"``, real aid).

The editable payload contains **no** ``internal_note`` or ``secret_tag``: it
only contains the customer name, the address label, the canonical address
and identity display columns.  Hidden fields never left the database, not
even in the token (which only carries one-way hashes).
"""

from __future__ import annotations

import json
import time

from . import store
from .codec import encode_text, encode_text_or_null
from .errors import InputError, TokenError
from .tokens import (
    JOINED_ROW_KIND,
    hash_token,
    PLACEHOLDER_KIND,
    Export,
    new_export_id,
    row_ref_for,
    sign_token,
    view_definition,
    view_hash,
)


def build_view(state: dict) -> list[tuple[str, str | None]]:
    """Order the ``C LEFT JOIN A`` rows from a loaded source state.

    A customer with addresses contributes one row per address and **no**
    placeholder row; a customer without any address contributes exactly one
    ``(cid, None)`` placeholder.  Ordering is ``cid`` then ``aid`` in Unicode
    code point order (Python ``str`` order).
    """
    addresses_by_cid: dict[str, list[str]] = {}
    for aid, a in state["addresses"].items():
        addresses_by_cid.setdefault(a["cid"], []).append(aid)
    ordered: list[tuple[str, str | None]] = []
    for cid in sorted(state["customers"]):
        aids = sorted(addresses_by_cid.get(cid, []))
        if not aids:
            ordered.append((cid, None))
        for aid in aids:
            ordered.append((cid, aid))
    return ordered


def export_snapshot(db_path) -> dict:
    """Read one consistent version and persist a signed export."""
    conn = store.open_existing(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        revision = store.get_revision(conn)
        config = store.get_config(conn)
        state = store.load_state(conn)
        s_hash = store.state_hash(state)
        v_hash = view_hash(config)
        rows = build_view(state)
        db_identity = store.get_meta(conn, "db_identity")
        secret = store.get_meta(conn, "hmac_secret")
        export_id = new_export_id()
        records: dict[str, dict] = {}
        view_rows = []
        for cid, aid in rows:
            cust = state["customers"][cid]
            if aid is None:
                ref = row_ref_for(export_id, "customer", cid, None)
                kind = PLACEHOLDER_KIND
                label_cell = ""
                address_cell = ""
            else:
                ref = row_ref_for(export_id, "address", cid, aid)
                kind = JOINED_ROW_KIND
                addr = state["addresses"][aid]
                label_cell = encode_text(addr["label"])
                address_cell = encode_text_or_null(addr["address"])
            if ref in records:
                raise InputError("internal error: duplicate row reference")
            records[ref] = {
                "row_kind_ref": "customer_row" if aid is None else "address",
                "kind": "customer" if aid is None else "address",
                "cid": cid,
                "aid": aid,
                "label": None if aid is None else addr["label"],
                "address": None if aid is None else addr["address"],
            }
            view_rows.append({
                "row_kind": kind,
                "row_ref": ref,
                "customer_name": encode_text(cust["name"]),
                "address_label": label_cell,
                "address_address": address_cell,
                "cid_display": cid,
                "aid_display": "" if aid is None else aid,
            })
        payload = {
            "export_id": export_id,
            "db_identity": db_identity,
            "revision": revision,
            "state_hash": s_hash,
            "config_hash": store.config_hash(config),
            "view_hash": v_hash,
            "row_refs": sorted(records),
            "nonce": export_id[len("exp_"):],
        }
        token = sign_token(secret, payload)
        conn.execute(
            "INSERT INTO cw_exports(export_id, token_sha256, db_identity, "
            "revision, state_hash, config_hash, view_name, view_hash, "
            "rows_json, state_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (export_id, hash_token(token),
             db_identity, revision, s_hash, store.config_hash(config),
             view_definition(config)["view_name"], v_hash,
             json.dumps(records, ensure_ascii=False, sort_keys=True),
             json.dumps(state, ensure_ascii=False, sort_keys=True),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "snapshot_token": token,
        "revision": revision,
        "view": view_definition(config),
        "rows": view_rows,
    }


def load_export(conn, token_payload: dict) -> Export:
    """Load the local export record and cross-check it against the token."""
    row = conn.execute(
        "SELECT * FROM cw_exports WHERE export_id = ?",
        (token_payload["export_id"],),
    ).fetchone()
    if row is None:
        raise TokenError("snapshot_token names an export this database does "
                         "not know")
    if row["db_identity"] != token_payload["db_identity"]:
        raise TokenError("snapshot_token belongs to another database")
    for field in ("revision", "state_hash", "config_hash", "view_hash"):
        if str(row[field]) != str(token_payload[field]):
            raise TokenError("snapshot_token %s does not match its export "
                             "record" % field.replace("_", " "))
    records = json.loads(row["rows_json"])
    if sorted(records) != token_payload["row_refs"]:
        raise TokenError("snapshot_token row list does not match its export "
                         "record")
    return Export(
        export_id=row["export_id"],
        db_identity=row["db_identity"],
        revision=int(row["revision"]),
        state_hash=row["state_hash"],
        config_hash=row["config_hash"],
        view_hash=row["view_hash"],
        row_refs=records,
        view_state=json.loads(row["state_json"]),
    )
