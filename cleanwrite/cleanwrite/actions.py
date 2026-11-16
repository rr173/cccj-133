"""Action parsing and strict batch validation.

Every action is a JSON object with a ``type``.  The exact formats are listed
below.  **All editable text values are canonical cells** in the sense of
:mod:`cleanwrite.codec`: ``"s\\"...\\""`` for text and ``"null"`` for SQL
NULL.  Names and labels are non-null, so their cells must start with ``s"``.
Addresses may be NULL, so ``"null"`` is accepted (and only it).  Values are
*not* trimmed, case-folded or guessed: ``"null"`` as SQL NULL is the only
spelling, a literal ``null`` string must be written ``s"null"``.

::

    {"type": "rename_customer", "row_ref": ROW, "name": CELL}
    {"type": "set_address_label", "row_ref": ROW, "label": CELL}
    {"type": "set_address_address", "row_ref": ROW, "address": CELL_OR_NULL}
    {"type": "move_address", "row_ref": ROW, "to_cid": CID_TEXT}
    {"type": "delete_address", "row_ref": ROW}
    {"type": "add_address", "temp_ref": TEMP, "cid": CID_TEXT,
     "label": CELL, "address": CELL_OR_NULL (optional)}
    {"type": "delete_customer", "row_ref": ROW}

``ROW`` is a row reference returned by ``get``, or the ``temp_ref`` of an
``add_address`` earlier in the same batch.  ``CID_TEXT`` / ``to_cid`` are the
exact customer identity strings, taken literally (identities are never
encoded and never come from the editable cells).  ``TEMP`` is a non-empty
string chosen by the caller, must not start with the reserved ``rr_`` or
``cw1.`` prefixes, and must not equal a real aid or a row reference.

Addresses are addressed by row reference only; the module never accepts a
``cid``/``aid`` read back from an editable row as authoritative identity.
"""

from __future__ import annotations

from .codec import decode_text, decode_text_or_null
from .errors import EncodingError, InputError

TEMP_REF_PREFIX = "tmp:"
MAX_ACTIONS = 10_000

_SPEC = {
    "rename_customer": {"type", "row_ref", "name"},
    "set_address_label": {"type", "row_ref", "label"},
    "set_address_address": {"type", "row_ref", "address"},
    "move_address": {"type", "row_ref", "to_cid"},
    "delete_address": {"type", "row_ref"},
    "add_address": {"type", "temp_ref", "cid", "label"},
    "delete_customer": {"type", "row_ref"},
}

_STRING_FIELDS = {
    "rename_customer": ("row_ref",),
    "set_address_label": ("row_ref",),
    "set_address_address": ("row_ref",),
    "move_address": ("row_ref", "to_cid"),
    "delete_address": ("row_ref",),
    "add_address": ("temp_ref", "cid"),
    "delete_customer": ("row_ref",),
}


def _is_scalar_text(value) -> bool:
    return isinstance(value, str) and not any(
        0xD800 <= ord(ch) <= 0xDFFF for ch in value
    )


def parse_actions(payload) -> list[dict]:
    """Validate the shape and encodings of an action batch.

    All problems are collected and reported together.
    """
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise InputError("actions must be a JSON array")
    if len(payload) > MAX_ACTIONS:
        raise InputError("too many actions (limit %d)" % MAX_ACTIONS)
    problems: list[str] = []
    out: list[dict] = []
    for idx, raw in enumerate(payload):
        where = "actions[%d]" % idx
        if not isinstance(raw, dict):
            problems.append("%s must be an object" % where)
            continue
        kind = raw.get("type")
        if kind not in _SPEC:
            problems.append("%s has unknown type %r (expected one of %s)"
                            % (where, kind, ", ".join(sorted(_SPEC))))
            continue
        required = _SPEC[kind]
        allowed = set(required)
        if kind == "add_address":
            allowed.add("address")
        extra = set(raw) - allowed
        missing = required - set(raw)
        bad = False
        if extra:
            problems.append("%s unknown keys: %s"
                            % (where, ", ".join(sorted(extra))))
            bad = True
        if missing:
            problems.append("%s missing keys: %s"
                            % (where, ", ".join(sorted(missing))))
            bad = True
        for key in _STRING_FIELDS[kind]:
            if key in raw and not _is_scalar_text(raw[key]):
                problems.append("%s.%s must be a non-surrogate Unicode "
                                "string" % (where, key))
                bad = True
        if kind == "add_address" and "temp_ref" in raw:
            temp = raw["temp_ref"]
            if isinstance(temp, str):
                if not temp:
                    problems.append("%s.temp_ref must not be empty" % where)
                    bad = True
                elif temp.startswith("rr_"):
                    problems.append(
                        "%s.temp_ref must not impersonate a real row "
                        "reference (rr_ prefix is reserved)" % where)
                    bad = True
                elif temp.startswith("cw1."):
                    problems.append(
                        "%s.temp_ref must not look like a snapshot token"
                        % where)
                    bad = True
        if bad:
            continue
        action = {"type": kind, "index": idx}
        for key in required - {"type"}:
            action[key] = raw[key]
        try:
            if kind == "rename_customer":
                action["name"] = decode_text(raw["name"])
                action["name_cell"] = raw["name"]
            elif kind == "set_address_label":
                action["label"] = decode_text(raw["label"])
                action["label_cell"] = raw["label"]
            elif kind == "set_address_address":
                action["address"] = decode_text_or_null(raw["address"])
                action["address_cell"] = raw["address"]
            elif kind == "add_address":
                action["label"] = decode_text(raw["label"])
                action["label_cell"] = raw["label"]
                if "address" in raw:
                    action["address"] = decode_text_or_null(raw["address"])
                    action["address_cell"] = raw["address"]
                    action["address_given"] = True
                else:
                    action["address_given"] = False
        except EncodingError as exc:
            problems.append("%s: %s" % (where, exc.message))
            continue
        out.append(action)
    if problems:
        raise InputError("%d invalid action(s)" % len(problems),
                         problems=problems)
    return out
