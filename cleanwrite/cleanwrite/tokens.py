"""Snapshot tokens, view definition and unforgeable row references.

A ``get`` export binds, in one signed token:

* a random ``db_identity`` of the local database (stored in ``cw_meta``);
* the ``revision`` and a one-way ``state_hash`` of both source relations
  (hidden fields included) at export time;
* the active insert-default ``config_hash``;
* the exact **view definition**: view name, source relation names, the
  column list, the canonical cell encoding name, the ordering rule, the row
  placeholder kind, and the set of row-reference keys (one per exported row).

The token is ``cw1.<base64url(payload)>.<base64url(HMAC-SHA256)>``.  The
HMAC key is a random 32-byte secret stored only in the local database.  The
payload is JSON, but it is *authenticated*, not trusted: any edit invalidates
the MAC.  The token contains no ``internal_note`` / ``secret_tag`` values and
no reversible encoding of them — only one-way digests of the whole state.

A **row reference** (``row_ref``) is an opaque string.  It is returned by
``get`` next to each exported row and is bound to:

* the export (export id, db identity, revision, view definition),
* the source identity *as exported* — the customer ``cid`` and, for a real
  address row, the address ``aid``;
* the ``row_ref`` of the customers of the export (for an address row).

``get`` persists the mapping in ``cw_exports.rows_json``; the module never
reads an address/name identity from the editable rows the caller sends back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from .errors import RowRefError, TokenError
TOKEN_VERSION = "cw1"
VIEW_NAME = "customer_address_clean_view"
VIEW_VERSION = "1"
PEER_RELATIONS = ("C", "A")
VIEW_COLUMNS = (
    "row_kind",          # customer | address
    "row_ref",
    "customer_name",     # canonical text cell (blank for a placeholder)
    "address_label",     # canonical text cell, "" for no address
    "address_address",   # canonical text-or-null cell, "" for no address
    "cid_display",       # plain identity display (export only)
    "aid_display",       # plain identity display, "" when absent
)
PLACEHOLDER_KIND = "placeholder_no_address"
CUSTOMER_KIND = "customer"
ADDRESS_KIND = "address"
JOINED_ROW_KIND = "customer_address"
TEMP_FORBIDDEN_PREFIXES = ("rr_", "cw1.")


def _b64e(blob: bytes) -> str:
    return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:  # noqa: BLE001 - any decoding error is fatal
        raise TokenError("token segment is not valid base64url") from exc


def view_definition(config: dict) -> dict:
    """The exact view definition that a token and its row refs are bound to."""
    return {
        "view_name": VIEW_NAME,
        "version": VIEW_VERSION,
        "relations": list(PEER_RELATIONS),
        "join": "C LEFT JOIN A ON A.cid = C.cid",
        "order": ["cid (Unicode code points)", "aid (Unicode code points; "
                  "placeholder sorts first)"],
        "columns": list(VIEW_COLUMNS),
        "encoding": "cleanwrite.codec/1 (s\"...\" text, null)",
        "placeholder_kind": PLACEHOLDER_KIND,
        "insert_defaults_keys": sorted(config),
    }


def view_hash(config: dict) -> str:
    blob = json.dumps(view_definition(config), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def row_ref_for(export_id: str, kind: str, cid: str, aid: str | None = None) -> str:
    """Opaque, export-bound row reference.

    The reference embeds only the export id and the source identity as
    exported (customer ``cid`` plus address ``aid``, or no aid for a
    placeholder row) and a keyed digest.  It is never trusted: the caller's
    copy must match the persisted export record, and the complete row-ref
    list is covered by the token MAC.
    """
    payload = {
        "e": export_id,
        "k": kind,
        "c": cid,
        "a": aid,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    digest = hmac.new(export_id.encode("utf-8"), blob,
                      hashlib.sha256).hexdigest()[:24]
    return "rr_" + digest + "_" + _b64e(blob)


def split_row_ref(ref: str) -> tuple[str, dict]:
    """Return ``(export_id, payload)`` of a syntactically valid ref."""
    if not isinstance(ref, str) or not ref.startswith("rr_"):
        raise RowRefError("row reference must be a string starting with "
                          "'rr_'")
    rest = ref[3:]
    if len(rest) < 26 or rest[24] != "_":
        raise RowRefError("row reference has an invalid shape")
    try:
        payload = json.loads(_b64d(rest[25:]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RowRefError("row reference payload is not decodable") from exc
    if not isinstance(payload, dict) or set(payload) != {"e", "k", "c", "a"}:
        raise RowRefError("row reference payload is invalid")
    expected = row_ref_for(payload["e"], payload["k"], payload["c"],
                           payload["a"])
    if not hmac.compare_digest(expected, ref):
        raise RowRefError("row reference digest is invalid (tampered)")
    return payload["e"], payload


def new_export_id() -> str:
    return "exp_" + secrets.token_hex(12)


def sign_token(secret: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    body64 = _b64e(body)
    mac = hmac.new(secret.encode("utf-8"), body64.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return "%s.%s.%s" % (TOKEN_VERSION, body64, mac)


def verify_token(secret: str, token: str) -> dict:
    if not isinstance(token, str):
        raise TokenError("snapshot_token must be a string")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise TokenError("snapshot_token is not a cw1 token")
    body64, mac = parts[1], parts[2]
    expected = hmac.new(secret.encode("utf-8"), body64.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise TokenError("snapshot_token authentication failed (forged or "
                         "edited token)")
    try:
        payload = json.loads(_b64d(body64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("snapshot_token payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise TokenError("snapshot_token payload is not an object")
    required = {"export_id", "db_identity", "revision", "state_hash",
                "config_hash", "view_hash", "row_refs", "nonce"}
    if set(payload) != required:
        raise TokenError("snapshot_token payload has unexpected fields")
    return payload


@dataclass(frozen=True)
class Export:
    export_id: str
    db_identity: str
    revision: int
    state_hash: str
    config_hash: str
    view_hash: str
    row_refs: dict = None          # row_ref -> export row record
    view_state: dict = None        # in-memory source snapshot (explain/put)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "ADDRESS_KIND", "CUSTOMER_KIND", "JOINED_ROW_KIND", "PEER_RELATIONS",
    "PLACEHOLDER_KIND", "TEMP_FORBIDDEN_PREFIXES", "TOKEN_VERSION",
    "VIEW_COLUMNS", "VIEW_NAME", "VIEW_VERSION", "Export", "hash_token",
    "new_export_id", "row_ref_for", "sign_token", "split_row_ref",
    "verify_token", "view_definition", "view_hash",
]
