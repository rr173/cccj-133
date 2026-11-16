"""SQLite storage, strict sample import and integrity helpers.

Only the two source relations described in the task are managed here::

    C(cid PRIMARY KEY, name NOT NULL, internal_note NULL)
    A(aid PRIMARY KEY, cid NOT NULL REFERENCES C(cid),
      label NOT NULL, address NULL, secret_tag NULL)

``cid``/``aid``/``name``/``label`` are non-null strings and are compared
exactly (no trimming, no case folding).  ``address``, ``internal_note`` and
``secret_tag`` are text or NULL.  An empty string is a legal value: "non-null"
is a SQL constraint, not an "non-empty" restriction, because empty text must
round-trip losslessly.

Two additional invariants are checked by this module (and re-checked at the
end of every write-back batch):

* every address references an existing customer;
* ``label`` is unique per customer.

There is deliberately **no unique index on ``(cid, label)``**: label
uniqueness is a *final state* rule, so a batch may swap two labels or move an
address into a customer that still carries the old label during the
intermediate steps of the transaction.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from .errors import InputError

SCHEMA_VERSION = "1"

DEFAULT_CONFIG = {
    "new_address_address": None,
    "new_address_secret_tag": None,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cw_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cw_customers (
    cid           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    internal_note TEXT
);

CREATE TABLE IF NOT EXISTS cw_addresses (
    aid        TEXT PRIMARY KEY,
    cid        TEXT NOT NULL REFERENCES cw_customers(cid),
    label      TEXT NOT NULL,
    address    TEXT,
    secret_tag TEXT
);

CREATE INDEX IF NOT EXISTS cw_addresses_cid ON cw_addresses(cid);

-- Every aid that ever existed in this database.  Auto ids are drawn from
-- this set so an aid is never reused, even after the address was deleted.
CREATE TABLE IF NOT EXISTS cw_used_aids (
    aid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS cw_exports (
    export_id     TEXT PRIMARY KEY,
    token_sha256  TEXT NOT NULL UNIQUE,
    db_identity   TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    state_hash    TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    view_name     TEXT NOT NULL,
    view_hash     TEXT NOT NULL,
    rows_json     TEXT NOT NULL,
    state_json    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cw_batches (
    batch_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    export_id       TEXT NOT NULL,
    revision_before INTEGER NOT NULL,
    revision_after  INTEGER NOT NULL,
    actions_json    TEXT NOT NULL,
    result_json     TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def open_existing(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM cw_meta WHERE key = 'db_identity'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise InputError("not a cleanwrite database: %s" % exc) from exc
    if row is None:
        conn.close()
        raise InputError("%s is not an initialised cleanwrite database"
                         % (db_path,))
    return conn


def _new_identity() -> str:
    return secrets.token_hex(16)


def _new_secret() -> str:
    return secrets.token_hex(32)


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM cw_meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cw_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_revision(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "revision", "0"))


def set_revision(conn: sqlite3.Connection, revision: int) -> None:
    set_meta(conn, "revision", str(revision))


def get_config(conn: sqlite3.Connection) -> dict:
    return json.loads(get_meta(conn, "config"))


def config_hash(config: dict) -> str:
    import hashlib

    blob = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_state(conn: sqlite3.Connection) -> dict:
    """Read both source relations inside the caller's transaction."""
    customers = {}
    for row in conn.execute(
        "SELECT cid, name, internal_note FROM cw_customers ORDER BY cid"
    ):
        customers[row["cid"]] = {
            "name": row["name"],
            "internal_note": row["internal_note"],
        }
    addresses = {}
    for row in conn.execute(
        "SELECT aid, cid, label, address, secret_tag FROM cw_addresses "
        "ORDER BY aid"
    ):
        addresses[row["aid"]] = {
            "cid": row["cid"],
            "label": row["label"],
            "address": row["address"],
            "secret_tag": row["secret_tag"],
        }
    return {"customers": customers, "addresses": addresses}


def state_hash(state: dict) -> str:
    """One-way digest of the *whole* source state, hidden fields included.

    The digest never leaves the local database/token and cannot be inverted;
    it lets an export detect that the source changed without a revision bump
    (e.g. manual SQL), even though intercepting external SQL is not required.
    """
    import hashlib

    payload = {
        "customers": [
            [cid, c["name"], c["internal_note"]]
            for cid, c in sorted(state["customers"].items())
        ],
        "addresses": [
            [aid, a["cid"], a["label"], a["address"], a["secret_tag"]]
            for aid, a in sorted(state["addresses"].items())
        ],
    }
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def used_aids(conn: sqlite3.Connection) -> set[str]:
    return {r["aid"] for r in conn.execute("SELECT aid FROM cw_used_aids")}


# ---------------------------------------------------------------------------
# Strict JSON loading and sample validation
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise InputError("duplicate JSON object key %r" % (key,))
        out[key] = value
    return out


def load_json_file(path: str | Path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read %s as UTF-8: %s" % (path, exc)) from exc
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise InputError("invalid JSON in %s: %s" % (path, exc)) from exc


def is_scalar_text(value) -> bool:
    if not isinstance(value, str):
        return False
    return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _check_text(value, where: str, problems: list, nullable=False):
    if value is None:
        if nullable:
            return
        problems.append("%s must be a string (NULL is not allowed)" % where)
        return
    if not isinstance(value, str):
        problems.append("%s must be a string, got %s"
                        % (where, type(value).__name__))
        return
    if not is_scalar_text(value):
        problems.append("%s contains a lone surrogate and is not valid "
                        "Unicode scalar text" % where)


def validate_sample(sample) -> dict:
    """Validate a sample document completely, without touching SQLite.

    Shape::

        {"customers": [{"cid", "name", "internal_note"}, ...],
         "addresses": [{"aid", "cid", "label", "address", "secret_tag"}, ...],
         "config":   {...}                      # optional, init only
        }

    Every identity is checked for duplicates, every address for a live
    foreign key, every value for its exact type.  All problems are collected
    and reported together.
    """
    problems: list[str] = []
    if not isinstance(sample, dict):
        raise InputError("sample must be a JSON object")
    extras = set(sample) - {"customers", "addresses", "config", "description"}
    if extras:
        problems.append("unknown top-level keys: %s"
                        % ", ".join(sorted(extras)))
    customers_raw = sample.get("customers", [])
    addresses_raw = sample.get("addresses", [])
    if not isinstance(customers_raw, list):
        problems.append("customers must be a list")
        customers_raw = []
    if not isinstance(addresses_raw, list):
        problems.append("addresses must be a list")
        addresses_raw = []

    customers: dict[str, dict] = {}
    for idx, item in enumerate(customers_raw):
        where = "customers[%d]" % idx
        if not isinstance(item, dict):
            problems.append("%s must be an object" % where)
            continue
        expected = {"cid", "name", "internal_note"}
        if set(item) != expected:
            missing = expected - set(item)
            extra = set(item) - expected
            if missing:
                problems.append("%s missing keys: %s"
                                % (where, ", ".join(sorted(missing))))
            if extra:
                problems.append("%s unknown keys: %s"
                                % (where, ", ".join(sorted(extra))))
            if missing:
                continue
        cid = item.get("cid")
        _check_text(cid, where + ".cid", problems)
        _check_text(item.get("name"), where + ".name", problems)
        _check_text(item.get("internal_note"), where + ".internal_note",
                    problems, nullable=True)
        if not isinstance(cid, str):
            continue
        if cid == "":
            problems.append("%s.cid must be a non-empty identity" % where)
            continue
        if cid in customers:
            problems.append("duplicate customer identity cid=%r" % (cid,))
            continue
        customers[cid] = {
            "name": item.get("name"),
            "internal_note": item.get("internal_note"),
        }

    addresses: dict[str, dict] = {}
    labels: dict[str, list] = {}
    for idx, item in enumerate(addresses_raw):
        where = "addresses[%d]" % idx
        if not isinstance(item, dict):
            problems.append("%s must be an object" % where)
            continue
        expected = {"aid", "cid", "label", "address", "secret_tag"}
        if set(item) != expected:
            missing = expected - set(item)
            extra = set(item) - expected
            if missing:
                problems.append("%s missing keys: %s"
                                % (where, ", ".join(sorted(missing))))
            if extra:
                problems.append("%s unknown keys: %s"
                                % (where, ", ".join(sorted(extra))))
            if missing:
                continue
        aid = item.get("aid")
        cid = item.get("cid")
        _check_text(aid, where + ".aid", problems)
        _check_text(cid, where + ".cid", problems)
        _check_text(item.get("label"), where + ".label", problems)
        _check_text(item.get("address"), where + ".address", problems,
                    nullable=True)
        _check_text(item.get("secret_tag"), where + ".secret_tag", problems,
                    nullable=True)
        if not isinstance(aid, str) or not isinstance(cid, str):
            continue
        if aid == "":
            problems.append("%s.aid must be a non-empty identity" % where)
            continue
        if aid in addresses:
            problems.append("duplicate address identity aid=%r" % (aid,))
            continue
        if cid not in customers:
            problems.append("%s.aid=%r references unknown customer cid=%r"
                            % (where, aid, cid))
            continue
        addresses[aid] = {
            "cid": cid,
            "label": item.get("label"),
            "address": item.get("address"),
            "secret_tag": item.get("secret_tag"),
        }
        if isinstance(item.get("label"), str):
            labels.setdefault(cid, []).append((item["label"], aid))

    for cid, pairs in labels.items():
        seen: dict[str, str] = {}
        for label, aid in pairs:
            if label in seen:
                problems.append(
                    "duplicate label %r for cid=%r (aid=%r and aid=%r)"
                    % (label, cid, seen[label], aid)
                )
            else:
                seen[label] = aid

    config = sample.get("config")
    if config is not None:
        try:
            config = validate_config(config)
        except InputError as exc:
            problems.extend(exc.details.get("problems", [str(exc)]))

    if problems:
        raise InputError("sample rejected: %d problem(s)" % len(problems),
                         problems=problems)
    return {"customers": customers, "addresses": addresses, "config": config}


def validate_config(config) -> dict:
    problems: list[str] = []
    if not isinstance(config, dict):
        raise InputError("config must be a JSON object")
    allowed = {"new_address_address", "new_address_secret_tag"}
    extra = set(config) - allowed
    if extra:
        problems.append("unknown config keys: %s" % ", ".join(sorted(extra)))
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in config.items() if k in allowed})
    _check_text(merged["new_address_address"],
                "config.new_address_address", problems, nullable=True)
    _check_text(merged["new_address_secret_tag"],
                "config.new_address_secret_tag", problems, nullable=True)
    if problems:
        raise InputError("config rejected", problems=problems)
    return merged


# ---------------------------------------------------------------------------
# Initialisation and import
# ---------------------------------------------------------------------------


def _insert_state(conn: sqlite3.Connection, validated: dict) -> None:
    for cid, c in validated["customers"].items():
        conn.execute(
            "INSERT INTO cw_customers(cid, name, internal_note) "
            "VALUES(?, ?, ?)",
            (cid, c["name"], c["internal_note"]),
        )
    for aid, a in validated["addresses"].items():
        conn.execute(
            "INSERT INTO cw_addresses(aid, cid, label, address, secret_tag) "
            "VALUES(?, ?, ?, ?, ?)",
            (aid, a["cid"], a["label"], a["address"], a["secret_tag"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO cw_used_aids(aid) VALUES(?)", (aid,)
        )


def init_database(db_path: str | Path, sample=None, config=None,
                  force: bool = False) -> dict:
    """Create (or, with ``force``, reset) a cleanwrite database.

    A fresh database starts at revision 0.  When ``sample`` is supplied it is
    fully validated before anything is written and becomes revision 0 too:
    it is the initial state, not a modification.
    """
    path = Path(db_path)
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        row = conn.execute(
            "SELECT value FROM cw_meta WHERE key = 'db_identity'"
        ).fetchone()
        if row is not None and not force:
            conn.rollback()
            raise InputError(
                "%s is already initialised (use force to reset)" % (path,))
        if row is not None and force:
            for table in ("cw_batches", "cw_exports", "cw_addresses",
                          "cw_customers", "cw_used_aids", "cw_meta"):
                conn.execute("DELETE FROM %s" % table)

        validated = None
        if sample is not None:
            validated = validate_sample(
                load_json_file(sample) if isinstance(sample, (str, Path))
                else sample
            )
        init_config = validate_config(config) if config is not None else (
            validated["config"] if validated and validated.get("config")
            else dict(DEFAULT_CONFIG)
        )
        if validated and validated.get("config") and config is not None:
            # explicit CLI/API config wins; sample config only a default
            init_config = validate_config(config)
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        set_meta(conn, "db_identity", _new_identity())
        set_meta(conn, "hmac_secret", _new_secret())
        set_meta(conn, "revision", "0")
        set_meta(conn, "config", json.dumps(init_config, ensure_ascii=False,
                                            sort_keys=True))
        set_meta(conn, "aid_counter", "0")
        if validated:
            _insert_state(conn, validated)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "db": str(path),
        "revision": 0,
        "customers": len(validated["customers"]) if validated else 0,
        "addresses": len(validated["addresses"]) if validated else 0,
    }


def import_sample(db_path: str | Path, sample, replace: bool = True) -> dict:
    """Replace or merge a validated sample, bumping the revision atomically.

    ``replace=True`` (the default) replaces both source relations; this is
    how a drill is reset.  ``replace=False`` merges: customer identities may
    not already exist and address identities may not already exist.
    """
    validated = validate_sample(
        load_json_file(sample) if isinstance(sample, (str, Path)) else sample
    )
    conn = open_existing(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = get_revision(conn)
        if replace:
            conn.execute("DELETE FROM cw_addresses")
            conn.execute("DELETE FROM cw_customers")
        else:
            for cid in validated["customers"]:
                if conn.execute("SELECT 1 FROM cw_customers WHERE cid=?",
                                (cid,)).fetchone():
                    raise InputError("customer identity already exists: %r"
                                     % (cid,))
            for aid in validated["addresses"]:
                if conn.execute("SELECT 1 FROM cw_addresses WHERE aid=?",
                                (aid,)).fetchone():
                    raise InputError("address identity already exists: %r"
                                     % (aid,))
        _insert_state(conn, validated)
        after = before + 1
        set_revision(conn, after)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "revision": after,
        "customers": len(validated["customers"]),
        "addresses": len(validated["addresses"]),
    }


def set_config(db_path: str | Path, config) -> dict:
    """Replace the insert-default config and bump the revision."""
    merged = validate_config(config)
    conn = open_existing(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = get_revision(conn)
        set_meta(conn, "config", json.dumps(merged, ensure_ascii=False,
                                           sort_keys=True))
        set_revision(conn, before + 1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"revision": before + 1, "config": merged}


def allocate_aids(conn: sqlite3.Connection, count: int) -> list[str]:
    """Allocate ``count`` never-used aid strings (inside a write transaction)."""
    if count <= 0:
        return []
    known = used_aids(conn)
    counter = int(get_meta(conn, "aid_counter", "0"))
    out: list[str] = []
    while len(out) < count:
        counter += 1
        candidate = "auto-aid-%08d" % counter
        if candidate in known:
            continue
        if conn.execute("SELECT 1 FROM cw_addresses WHERE aid=?",
                        (candidate,)).fetchone():
            continue
        known.add(candidate)
        out.append(candidate)
    set_meta(conn, "aid_counter", str(counter))
    for aid in out:
        conn.execute("INSERT INTO cw_used_aids(aid) VALUES(?)", (aid,))
    return out


def source_tables(conn: sqlite3.Connection) -> dict:
    """Full source dump (including hidden fields) for local inspection/drills."""
    customers = [
        {"cid": r["cid"], "name": r["name"],
         "internal_note": r["internal_note"]}
        for r in conn.execute(
            "SELECT cid, name, internal_note FROM cw_customers ORDER BY cid")
    ]
    addresses = [
        {"aid": r["aid"], "cid": r["cid"], "label": r["label"],
         "address": r["address"], "secret_tag": r["secret_tag"]}
        for r in conn.execute(
            "SELECT aid, cid, label, address, secret_tag FROM cw_addresses "
            "ORDER BY aid")
    ]
    return {"revision": get_revision(conn), "customers": customers,
            "addresses": addresses, "config": get_config(conn)}
