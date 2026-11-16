"""Tests for the clean-up view write-back module (stdlib unittest only).

Run from the ``cleanwrite`` directory::

    python3 -I tests/test_cleanwrite.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import cleanwrite as cw  # noqa: E402
from cleanwrite.actions import parse_actions  # noqa: E402
from cleanwrite.codec import (  # noqa: E402
    decode_text,
    decode_text_or_null,
    encode_text,
    encode_text_or_null,
)
from cleanwrite.errors import (  # noqa: E402
    ConflictError,
    EncodingError,
    InputError,
    RowRefError,
    StaleTokenError,
    TokenError,
)
from reference_model import RefModel, RefReject  # noqa: E402

SAMPLE = ROOT / "samples" / "drill.json"


def cell(value):
    return encode_text_or_null(value)


def view_map(view):
    return {(r["cid_display"], r["aid_display"]): r for r in view["rows"]}


def ref(row, name=None, label=None, address=None):
    """Build an action targeting a row of the exported view."""
    if name is not None:
        return {"type": "rename_customer", "row_ref": row["row_ref"],
                "name": cell(name)}
    if label is not None:
        return {"type": "set_address_label", "row_ref": row["row_ref"],
                "label": cell(label)}
    if address is not None:
        return {"type": "set_address_address", "row_ref": row["row_ref"],
                "address": cell(address)}
    raise AssertionError("ref() needs a value")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "db.sqlite"
        cw.init_db(self.db, sample=str(SAMPLE))
        self.view = cw.get(self.db)
        self.rows = view_map(self.view)

    def tearDown(self):
        self.tmp.cleanup()

    def tables(self):
        dump = cw.source_tables(self.db)
        return (
            [(c["cid"], c["name"], c["internal_note"])
             for c in dump["customers"]],
            [(a["aid"], a["cid"], a["label"], a["address"], a["secret_tag"])
             for a in dump["addresses"]],
        )

    def rev(self):
        return cw.source_tables(self.db)["revision"]

    def put_ok(self, actions, view=None):
        view = view or self.view
        return cw.put(self.db, view["snapshot_token"], actions)


class EncodingTests(unittest.TestCase):
    def test_roundtrip(self):
        cases = ["", "甲", "a\nb", "\\", '"', "\t\r\n", "\u0000\u001f\u007f",
                 "中文 空格 ", "😀", "line\nbreak\\slash", "null"]
        for value in cases:
            exported = encode_text(value)
            self.assertIsInstance(exported, str)
            self.assertEqual(decode_text(exported), value)

    def test_null_and_empty_are_distinct(self):
        self.assertEqual(encode_text_or_null(None), "null")
        self.assertEqual(encode_text_or_null(""), 's""')
        self.assertIsNone(decode_text_or_null("null"))
        self.assertEqual(decode_text_or_null('s""'), "")
        self.assertNotEqual(encode_text_or_null(None), encode_text_or_null(""))

    def test_literal_null_string_is_quoted(self):
        self.assertEqual(encode_text_or_null("null"), 's"null"')
        self.assertEqual(decode_text('s"null"'), "null")

    def test_non_canonical_forms_rejected(self):
        bad = [
            "NULL", "Null", " null", "null ", "", "s", 's""x', 'S""',
            's"\\u{41}"',            # A has a literal form
            's"\\u{0000}"',          # leading zero
            's"\\u{7f}"',            # must be \u{7F} ... also non-canonical
            's"\\u{41"',             # unterminated
            's"\\u{}"',
            's"\\u{}"',
            's"\\/"',                # unknown escape
            's"\\x41"',
            's"\\b"',
            's"a\tb"',               # raw control character
            's"a"b"',
            's"\\U0001F600"',
            's"\n"',
        ]
        for bad_cell in bad:
            with self.assertRaises((EncodingError,), msg=bad_cell):
                decode_text_or_null(bad_cell)
        # valid control escapes / astral escapes are accpted
        self.assertEqual(decode_text('s"\\u{1F600}"'), "😀")
        self.assertEqual(decode_text('s"\\u{0}"'), "\x00")
        self.assertEqual(decode_text('s"\\u{7F}"'), "\x7f")
        self.assertEqual(decode_text('s"\\n"'), "\n")
        self.assertEqual(decode_text('s"\\\""'), '"')
        self.assertEqual(decode_text('s"\\\\"'), "\\")

    def test_trailing_text_after_closing_quote_rejected(self):
        with self.assertRaises(EncodingError):
            decode_text('s"a" "b"')
        with self.assertRaises(EncodingError):
            decode_text('s"a""')

    def test_surrogates_rejected(self):
        with self.assertRaises(EncodingError):
            encode_text("\ud800")
        with self.assertRaises(EncodingError):
            decode_text('s"\\u{D800}"')

    def test_out_of_range(self):
        with self.assertRaises(EncodingError):
            decode_text('s"\\u{110000}"')

    def test_bmp_astral_must_use_escape(self):
        # astral chars are always escaped, so they cannot appear literally
        cell = encode_text("😀")
        self.assertEqual(cell, 's"\\u{1F600}"')
        self.assertEqual(decode_text(cell), "😀")

    def test_unicode_roundtrip_in_database(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "x.db"
            cw.init_db(db)
            cw.import_sample(db, {"customers": [
                {"cid": "c", "name": "名字",
                 "internal_note": "备\n注\\"},
            ], "addresses": [
                {"aid": "a", "cid": "c", "label": "",
                 "address": None, "secret_tag": "秘"},
            ]})
            view = cw.get(db)
            row = view["rows"][0]
            self.assertEqual(row["customer_name"], cell("名字"))
            self.assertEqual(row["address_label"], 's""')
            self.assertEqual(row["address_address"], "null")
            self.assertEqual(decode_text(row["customer_name"]), "名字")
        finally:
            tmp.cleanup()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, customers, addresses):
        return {"customers": customers, "addresses": addresses}

    def test_reject_duplicate_identity(self):
        with self.assertRaises(InputError) as ctx:
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None},
                 {"cid": "C", "name": "n2", "internal_note": None}], []))
        self.assertTrue(any("duplicate customer" in p for p in
                            ctx.exception.details["problems"]))
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None}],
                [{"aid": "a", "cid": "C", "label": "l", "address": None,
                  "secret_tag": None},
                 {"aid": "a", "cid": "C", "label": "l2", "address": None,
                  "secret_tag": None}]))

    def test_reject_bad_reference(self):
        with self.assertRaises(InputError) as ctx:
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None}],
                [{"aid": "a", "cid": "X", "label": "l", "address": None,
                  "secret_tag": None}]))
        self.assertTrue(any("unknown customer" in p for p in
                            ctx.exception.details["problems"]))

    def test_reject_bad_types(self):
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": 5, "internal_note": None}], []))
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": None, "name": "n", "internal_note": None}], []))
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": 3}], []))
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None}],
                [{"aid": "a", "cid": "C", "label": 1, "address": None,
                  "secret_tag": None}]))

    def test_reject_duplicate_label_per_customer(self):
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None}],
                [{"aid": "a", "cid": "C", "label": "家", "address": None,
                  "secret_tag": None},
                 {"aid": "b", "cid": "C", "label": "家", "address": None,
                  "secret_tag": None}]))

    def test_same_label_for_different_customers_ok(self):
        report = cw.init_db(self.db, sample=self.sample(
            [{"cid": "C1", "name": "n1", "internal_note": None},
             {"cid": "C2", "name": "n2", "internal_note": None}],
            [{"aid": "a", "cid": "C1", "label": "家", "address": None,
              "secret_tag": None},
             {"aid": "b", "cid": "C2", "label": "家", "address": None,
              "secret_tag": None}]))
        self.assertEqual(report["customers"], 2)

    def test_reject_empty_identity_and_unknown_keys(self):
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "", "name": "n", "internal_note": None}], []))
        with self.assertRaises(InputError):
            cw.init_db(self.db, sample=self.sample(
                [{"cid": "C", "name": "n", "internal_note": None,
                  "extra": 1}], []))
        # empty name / label are *legal* non-null strings
        cw.init_db(self.db, sample=self.sample(
            [{"cid": "C", "name": "", "internal_note": None}],
            [{"aid": "a", "cid": "C", "label": "", "address": "",
              "secret_tag": ""}]))

    def test_json_duplicate_object_keys_rejected(self):
        from cleanwrite.store import load_json_file

        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "s.json"
            path.write_text('{"customers": [], "customers": []}',
                            encoding="utf-8")
            with self.assertRaises(InputError):
                load_json_file(path)
        finally:
            tmp.cleanup()

    def test_init_twice_requires_force(self):
        cw.init_db(self.db)
        with self.assertRaises(InputError):
            cw.init_db(self.db)
        cw.init_db(self.db, force=True)


class ViewTests(Base):
    def test_order_and_placeholder(self):
        view = self.view
        kinds = [(r["cid_display"], r["aid_display"], r["row_kind"])
                 for r in view["rows"]]
        self.assertEqual(kinds, [
            ("C1", "a1", "customer_address"),
            ("C1", "a2", "customer_address"),
            ("C2", "", "placeholder_no_address"),
        ])

    def test_codepoint_order(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "o.db"
            cw.init_db(db, sample={"customers": [
                {"cid": "Z", "name": "z", "internal_note": None},
                {"cid": "a", "name": "a", "internal_note": None},
                {"cid": "ä", "name": "ae", "internal_note": None},
            ], "addresses": [
                {"aid": "b", "cid": "Z", "label": "2", "address": None,
                 "secret_tag": None},
                {"aid": "a", "cid": "Z", "label": "1", "address": None,
                 "secret_tag": None},
            ]})
            view = cw.get(db)
            got = [(r["cid_display"], r["aid_display"]) for r in view["rows"]]
            self.assertEqual(got, [("Z", "a"), ("Z", "b"),
                                   ("a", ""), ("ä", "")])
        finally:
            tmp.cleanup()

    def test_placeholder_has_no_aid_and_differs_from_null_address(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "n.db"
            cw.init_db(db, sample={"customers": [
                {"cid": "C1", "name": "n1", "internal_note": None},
                {"cid": "C2", "name": "n2", "internal_note": None},
            ], "addresses": [
                {"aid": "a", "cid": "C1", "label": "l", "address": None,
                 "secret_tag": None},
            ]})
            view = cw.get(db)
            m = view_map(view)
            null_row = m[("C1", "a")]
            placeholder = m[("C2", "")]
            self.assertEqual(null_row["row_kind"], "customer_address")
            self.assertEqual(null_row["address_address"], "null")
            self.assertEqual(placeholder["row_kind"], "placeholder_no_address")
            self.assertEqual(placeholder["aid_display"], "")
            self.assertEqual(placeholder["address_address"], "")
        finally:
            tmp.cleanup()

    def test_hidden_fields_never_exported(self):
        blob = json.dumps(self.view, ensure_ascii=False)
        self.assertNotIn("内部一", blob)
        self.assertNotIn("tag-a1", blob)
        self.assertNotIn("secret", blob.lower().replace(
            '"new_address_secret_tag"', ""))
        # the token must not contain a reversible encoding of hidden values
        import base64
        token = self.view["snapshot_token"]
        body = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        self.assertNotIn("内部一".encode(), decoded)
        self.assertNotIn(b"tag-a1", decoded)

    def test_get_is_empty_change(self):
        before = self.rev()
        cw.get(self.db)
        cw.get(self.db)
        self.assertEqual(self.rev(), before)


class RenameTests(Base):
    def test_rename_from_one_row_updates_shared_field(self):
        rows = self.rows
        cw.put(self.db, self.view["snapshot_token"],
               [ref(rows[("C1", "a1")], name="新甲")])
        customers, addresses = self.tables()
        self.assertEqual(customers[0], ("C1", "新甲", "内部一"))
        self.assertEqual([a[4] for a in addresses], ["tag-a1", "tag-a2"])
        view = cw.get(self.db)
        self.assertEqual([r["customer_name"] for r in view["rows"]],
                         [cell("新甲"), cell("新甲"), cell("乙")])
        self.assertEqual(self.rev(), 1)

    def test_conflicting_rename_rejected(self):
        rows = self.rows
        actions = [
            ref(rows[("C1", "a1")], name="A"),
            ref(rows[("C1", "a2")], name="B"),
        ]
        with self.assertRaises(ConflictError) as ctx:
            self.put_ok(actions)
        self.assertTrue(any(c["reason"] == "rename_conflict"
                            for c in ctx.exception.conflicts))
        self.assertEqual(self.rev(), 0)
        self.assertEqual(self.tables()[0][0], ("C1", "甲", "内部一"))

    def test_restore_old_name_does_not_hide_conflict(self):
        rows = self.rows
        actions = [
            ref(rows[("C1", "a1")], name="A"),
            ref(rows[("C1", "a2")], name="B"),
            ref(rows[("C1", "a1")], name="甲"),
        ]
        report = cw.explain(self.db, self.view["snapshot_token"], actions)
        self.assertFalse(report["ok"])
        self.assertTrue(any(c["reason"] == "rename_conflict"
                            for c in report["conflicts"]))
        # agreement including an unchanged vote is fine
        actions2 = [
            ref(rows[("C1", "a1")], name="A"),
            ref(rows[("C1", "a2")], name="A"),
        ]
        report = cw.explain(self.db, self.view["snapshot_token"], actions2)
        self.assertTrue(report["ok"])

    def test_rows_not_submitted_do_not_vote(self):
        rows = self.rows
        report = cw.explain(self.db, self.view["snapshot_token"],
                            [ref(rows[("C2", "")], name="乙2")])
        self.assertTrue(report["ok"])

    def test_rename_of_deleted_customer_conflicts(self):
        rows = self.rows
        actions = [
            {"type": "delete_customer",
             "row_ref": rows[("C2", "")]["row_ref"]},
            ref(rows[("C2", "")], name="乙2"),
        ]
        with self.assertRaises(ConflictError) as ctx:
            self.put_ok(actions)
        reasons = [c["reason"] for c in ctx.exception.conflicts]
        self.assertIn("rename_on_deleted_customer", reasons)

    def test_rename_from_placeholder_legal(self):
        rows = self.rows
        cw.put(self.db, self.view["snapshot_token"],
               [ref(rows[("C2", "")], name="新乙")])
        self.assertEqual(self.tables()[0][1], ("C2", "新乙", None))

    def test_row_ref_customer_is_export_time_customer(self):
        # moving a1 to C2 and renaming via the a1 row must rename C1, not C2
        rows = self.rows
        actions = [
            {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
             "to_cid": "C2"},
            ref(rows[("C1", "a1")], name="迁后名"),
        ]
        cw.put(self.db, self.view["snapshot_token"], actions)
        customers = {c[0]: c[1] for c in self.tables()[0]}
        self.assertEqual(customers["C1"], "迁后名")
        self.assertEqual(customers["C2"], "乙")


class AddressTests(Base):
    def test_repeated_edits_last_wins(self):
        row = self.rows[("C1", "a1")]
        actions = [
            ref(row, label="L1"),
            ref(row, label="L2"),
            ref(row, address="addr1"),
            ref(row, address="addr2"),
        ]
        self.put_ok(actions)
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses["a1"][2], "L2")
        self.assertEqual(addresses["a1"][3], "addr2")
        self.assertEqual(addresses["a1"][4], "tag-a1")

    def test_move_keeps_aid_and_secret(self):
        rows = self.rows
        self.put_ok([{"type": "move_address",
                      "row_ref": rows[("C1", "a1")]["row_ref"],
                      "to_cid": "C2"}])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses["a1"][1], "C2")
        self.assertEqual(addresses["a1"][4], "tag-a1")

    def test_move_to_unknown_customer_rejected(self):
        rows = self.rows
        with self.assertRaises(ConflictError):
            self.put_ok([{"type": "move_address",
                          "row_ref": rows[("C1", "a1")]["row_ref"],
                          "to_cid": "NOPE"}])

    def test_delete_then_edit_rejected(self):
        row = self.rows[("C1", "a1")]
        actions = [{"type": "delete_address", "row_ref": row["row_ref"]},
                   ref(row, label="again")]
        with self.assertRaises(ConflictError) as ctx:
            self.put_ok(actions)
        self.assertTrue(any(c["reason"] == "address_already_deleted"
                            for c in ctx.exception.conflicts))

    def test_edit_then_delete_is_fine(self):
        row = self.rows[("C1", "a1")]
        self.put_ok([ref(row, label="先改"),
                     {"type": "delete_address", "row_ref": row["row_ref"]}])
        self.assertNotIn("a1", {a[0] for a in self.tables()[1]})

    def test_swap_labels(self):
        rows = self.rows
        self.put_ok([
            ref(rows[("C1", "a1")], label="公司"),
            ref(rows[("C1", "a2")], label="家"),
        ])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses["a1"][2], "公司")
        self.assertEqual(addresses["a2"][2], "家")

    def test_swap_and_move_ok(self):
        rows = self.rows
        self.put_ok([
            ref(rows[("C1", "a1")], label="公司"),
            ref(rows[("C1", "a2")], label="家"),
            {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
             "to_cid": "C2"},
        ])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual((addresses["a1"][1], addresses["a1"][2]),
                         ("C2", "公司"))
        self.assertEqual((addresses["a2"][1], addresses["a2"][2]),
                         ("C1", "家"))

    def test_final_label_conflict_rejected(self):
        rows = self.rows
        with self.assertRaises(ConflictError) as ctx:
            self.put_ok([
                ref(rows[("C1", "a1")], label="X"),
                ref(rows[("C1", "a2")], label="X"),
            ])
        self.assertTrue(any(c["reason"] == "duplicate_label_final"
                            for c in ctx.exception.conflicts))

    def test_placeholder_cannot_be_address(self):
        rows = self.rows
        for action in (
            ref(rows[("C2", "")], label="x"),
            ref(rows[("C2", "")], address="x"),
            {"type": "move_address",
             "row_ref": rows[("C2", "")]["row_ref"], "to_cid": "C1"},
            {"type": "delete_address",
             "row_ref": rows[("C2", "")]["row_ref"]},
        ):
            with self.assertRaises(ConflictError):
                self.put_ok([action])


class TempTests(Base):
    def test_add_then_edit_move_assigns_aid(self):
        rows = self.rows
        actions = [
            {"type": "add_address", "temp_ref": "T1", "cid": "C2",
             "label": cell("仓库")},
            ref({"row_ref": "T1"}, address="路 1 号"),
            {"type": "move_address", "row_ref": "T1", "to_cid": "C2"},
        ]
        result = self.put_ok(actions)
        self.assertEqual(sorted(result["new_address_aids"]), ["T1"])
        aid = result["new_address_aids"]["T1"]
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses[aid][1], "C2")
        self.assertEqual(addresses[aid][2], "仓库")
        self.assertEqual(addresses[aid][3], "路 1 号")
        self.assertEqual(addresses[aid][4], "cfg-default-secret")
        self.assertNotIn(aid, {"a1", "a2"})

    def test_add_then_delete_never_stores_or_allocates(self):
        before = cw.source_tables(self.db)
        result = self.put_ok([
            {"type": "add_address", "temp_ref": "T", "cid": "C2",
             "label": cell("临时")},
            {"type": "delete_address", "row_ref": "T"},
        ])
        self.assertEqual(result["new_address_aids"], {})
        self.assertIsNone(result["temp_refs"]["T"])
        addresses = {a[0] for a in self.tables()[1]}
        self.assertEqual(addresses, {"a1", "a2"})   # no official aid used
        conn = sqlite3.connect(str(self.db))
        used = {r[0] for r in conn.execute("SELECT aid FROM cw_used_aids")}
        conn.close()
        self.assertFalse(any(a.startswith("auto-aid-") for a in used))

    def test_add_then_edit_after_delete_rejected(self):
        with self.assertRaises(ConflictError):
            self.put_ok([
                {"type": "add_address", "temp_ref": "T", "cid": "C2",
                 "label": cell("x")},
                {"type": "delete_address", "row_ref": "T"},
                ref({"row_ref": "T"}, label="y"),
            ])

    def test_duplicate_temp_ref_rejected(self):
        with self.assertRaises(RowRefError):
            self.put_ok([
                {"type": "add_address", "temp_ref": "T", "cid": "C2",
                 "label": cell("x")},
                {"type": "add_address", "temp_ref": "T", "cid": "C2",
                 "label": cell("y")},
            ])

    def test_temp_ref_cannot_impersonate(self):
        rows = self.rows
        real_ref = rows[("C1", "a1")]["row_ref"]
        # the rr_ prefix is refused already by the action parser
        with self.assertRaises(InputError):
            self.put_ok([
                {"type": "add_address", "temp_ref": real_ref, "cid": "C2",
                 "label": cell("x")},
            ])
        with self.assertRaises(RowRefError):
            self.put_ok([
                {"type": "add_address", "temp_ref": "a1", "cid": "C2",
                 "label": cell("x")},
            ])
        with self.assertRaises(InputError):
            self.put_ok([
                {"type": "add_address", "temp_ref": "rr_fake", "cid": "C2",
                 "label": cell("x")},
            ])
        # an aid that only *existed in the past* must not be reusable either
        cw.put(self.db, self.view["snapshot_token"],
               [{"type": "delete_address",
                 "row_ref": rows[("C1", "a1")]["row_ref"]},
                {"type": "set_address_label",
                 "row_ref": rows[("C1", "a2")]["row_ref"],
                 "label": cell("公司二")}])
        view = cw.get(self.db)
        with self.assertRaises(RowRefError):
            cw.put(self.db, view["snapshot_token"],
                   [{"type": "add_address", "temp_ref": "a1", "cid": "C2",
                     "label": cell("x")}])

    def test_aids_never_reused_after_delete(self):
        rows = self.rows
        result = self.put_ok([
            {"type": "delete_address", "row_ref": rows[("C1", "a1")]["row_ref"]},
            {"type": "add_address", "temp_ref": "T", "cid": "C2",
             "label": cell("新")},
        ])
        new_aid = result["new_address_aids"]["T"]
        self.assertNotEqual(new_aid, "a1")
        self.assertNotEqual(new_aid, "a2")

    def test_add_target_must_exist_and_survive(self):
        with self.assertRaises(ConflictError):
            self.put_ok([
                {"type": "add_address", "temp_ref": "T", "cid": "C2",
                 "label": cell("x")},
                {"type": "delete_customer",
                 "row_ref": self.rows[("C2", "")]["row_ref"]},
            ])


class DeleteTests(Base):
    def test_delete_customer_after_moving_addresses(self):
        rows = self.rows
        actions = [
            {"type": "delete_customer",
             "row_ref": rows[("C1", "a1")]["row_ref"]},
            {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
             "to_cid": "C2"},
            {"type": "delete_address",
             "row_ref": rows[("C1", "a2")]["row_ref"]},
        ]
        cw.put(self.db, self.view["snapshot_token"], actions)
        customers, addresses = self.tables()
        self.assertEqual([c[0] for c in customers], ["C2"])
        self.assertEqual([(a[0], a[1]) for a in addresses], [("a1", "C2")])

    def test_declare_delete_first(self):
        rows = self.rows
        actions = [
            {"type": "delete_customer",
             "row_ref": rows[("C1", "a1")]["row_ref"]},
            {"type": "delete_address",
             "row_ref": rows[("C1", "a2")]["row_ref"]},
            {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
             "to_cid": "C2"},
        ]
        cw.put(self.db, self.view["snapshot_token"], actions)
        self.assertEqual([c[0] for c in self.tables()[0]], ["C2"])

    def test_delete_referenced_customer_rejected(self):
        with self.assertRaises(ConflictError) as ctx:
            self.put_ok([{"type": "delete_customer",
                          "row_ref": self.rows[("C1", "a1")]["row_ref"]}])
        self.assertTrue(any(c["reason"] == "customer_still_referenced"
                            for c in ctx.exception.conflicts))

    def test_last_address_delete_leaves_placeholder(self):
        rows = self.rows
        self.put_ok([
            {"type": "delete_address",
             "row_ref": rows[("C1", "a1")]["row_ref"]},
            {"type": "delete_address",
             "row_ref": rows[("C1", "a2")]["row_ref"]},
        ])
        customers = [c[0] for c in self.tables()[0]]
        self.assertIn("C1", customers, "customer is never deleted as a side "
                      "effect")
        view = cw.get(self.db)
        m = view_map(view)
        self.assertEqual(m[("C1", "")]["row_kind"], "placeholder_no_address")
        # adding an address removes the placeholder row
        rows2 = m
        cw.put(self.db, view["snapshot_token"], [
            {"type": "add_address", "temp_ref": "T", "cid": "C1",
             "label": cell("回")},
        ])
        view2 = cw.get(self.db)
        m2 = view_map(view2)
        self.assertNotIn(("C1", ""), m2)
        self.assertEqual(len([k for k in m2 if k[0] == "C1"]), 1)


class TokenTests(Base):
    def test_tampered_token_rejected(self):
        token = self.view["snapshot_token"]
        parts = token.split(".")
        mutated = parts[0] + "." + ("A" if parts[1][0] != "A" else "B") \
            + parts[1][1:] + "." + parts[2]
        with self.assertRaises(TokenError):
            cw.explain(self.db, mutated, [])
        with self.assertRaises(TokenError):
            cw.put(self.db, mutated, [])

    def test_tampered_row_ref_rejected(self):
        row = self.rows[("C1", "a1")]
        broken = row["row_ref"][:-3] + ("aaa" if not row["row_ref"].endswith("aaa")
                                        else "bbb")
        with self.assertRaises(RowRefError):
            cw.explain(self.db, self.view["snapshot_token"],
                       [{"type": "set_address_label", "row_ref": broken,
                         "label": cell("x")}])

    def test_foreign_export_reference_rejected(self):
        other = cw.get(self.db)
        rows_other = view_map(other)
        with self.assertRaises(RowRefError):
            cw.explain(self.db, self.view["snapshot_token"],
                       [ref(rows_other[("C1", "a1")], label="x")])

    def test_other_database_token_rejected(self):
        other_db = self.dir / "other.sqlite"
        cw.init_db(other_db, sample=str(SAMPLE))
        other = cw.get(other_db)
        with self.assertRaises(TokenError):
            cw.explain(self.db, other["snapshot_token"], [])

    def test_other_database_row_ref_rejected(self):
        other_db = self.dir / "other.sqlite"
        cw.init_db(other_db, sample=str(SAMPLE))
        other = cw.get(other_db)
        with self.assertRaises(RowRefError):
            cw.explain(self.db, self.view["snapshot_token"],
                       [ref(view_map(other)[("C1", "a1")], label="x")])

    def test_view_definition_change_invalidates(self):
        cw.set_config(self.db, {"new_address_address": "changed",
                                "new_address_secret_tag": "changed"})
        with self.assertRaises(TokenError):
            cw.explain(self.db, self.view["snapshot_token"], [])

    def test_missing_token_field(self):
        token = self.view["snapshot_token"]
        with self.assertRaises(TokenError):
            cw.explain(self.db, token + "x", [])


class StaleTests(Base):
    def test_stale_export_rejected(self):
        v1 = cw.get(self.db)
        v2 = cw.get(self.db)
        rows1 = view_map(v1)
        cw.put(self.db, v1["snapshot_token"],
               [ref(rows1[("C2", "")], name="第一次")])
        rows2 = view_map(v2)
        with self.assertRaises(StaleTokenError):
            cw.put(self.db, v2["snapshot_token"],
                   [ref(rows2[("C2", "")], name="第二次")])
        self.assertEqual(self.tables()[0][1][1], "第一次")
        report = cw.explain(self.db, v2["snapshot_token"], [])
        self.assertTrue(report["stale"])
        self.assertEqual(self.rev(), 1)

    def test_explain_does_not_advance_or_allocate(self):
        row = self.rows[("C2", "")]
        actions = [{"type": "add_address", "temp_ref": "T", "cid": "C2",
                    "label": cell("x")}]
        report = cw.explain(self.db, self.view["snapshot_token"], actions)
        self.assertTrue(report["ok"])
        conn = sqlite3.connect(str(self.db))
        used = {r[0] for r in conn.execute("SELECT aid FROM cw_used_aids")}
        conn.close()
        self.assertEqual(used, {"a1", "a2"})
        self.assertEqual(self.rev(), 0)
        result = self.put_ok(actions)
        self.assertEqual(result["new_address_aids"]["T"], "auto-aid-00000001")


class EmptyBatchTests(Base):
    def test_empty_batch_is_no_op(self):
        before = cw.source_tables(self.db)
        result = self.put_ok([])
        self.assertTrue(result["empty_batch"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["revision_after"], result["revision_before"])
        after = cw.source_tables(self.db)
        self.assertEqual(before, after)

    def test_empty_after_write_back(self):
        cw.put(self.db, self.view["snapshot_token"],
               [ref(self.rows[("C2", "")], name="改")])
        view = cw.get(self.db)
        before = cw.source_tables(self.db)
        result = cw.put(self.db, view["snapshot_token"], [])
        self.assertTrue(result["empty_batch"])
        self.assertEqual(cw.source_tables(self.db), before)

    def test_empty_stale_batch_still_rejected(self):
        v1 = cw.get(self.db)
        cw.put(self.db, v1["snapshot_token"],
               [ref(view_map(v1)[("C2", "")], name="x")])
        with self.assertRaises(StaleTokenError):
            cw.put(self.db, v1["snapshot_token"], [])


class AtomicityTests(Base):
    def test_failing_last_action_rolls_back_everything(self):
        rows = self.rows
        before = cw.source_tables(self.db)
        actions = [
            ref(rows[("C2", "")], name="前三步"),
            {"type": "add_address", "temp_ref": "T", "cid": "C2",
             "label": cell("新")},
            ref(rows[("C1", "a1")], label="改标签"),
            {"type": "delete_customer",
             "row_ref": rows[("C1", "a1")]["row_ref"]},   # invalid
        ]
        with self.assertRaises(ConflictError):
            self.put_ok(actions)
        self.assertEqual(cw.source_tables(self.db), before)
        conn = sqlite3.connect(str(self.db))
        used = {r[0] for r in conn.execute("SELECT aid FROM cw_used_aids")}
        conn.close()
        self.assertFalse(any(a.startswith("auto-aid-") for a in used))

    def test_structural_failure_rolls_back(self):
        rows = self.rows
        before = cw.source_tables(self.db)
        actions = [
            ref(rows[("C2", "")], name="前三步"),
            {"type": "set_address_label",
             "row_ref": "rr_000000000000000000000000_bad", "label": cell("x")},
        ]
        with self.assertRaises(RowRefError):
            self.put_ok(actions)
        self.assertEqual(cw.source_tables(self.db), before)

    def test_after_failure_corrected_batch_works(self):
        rows = self.rows
        with self.assertRaises(ConflictError):
            self.put_ok([
                ref(rows[("C1", "a1")], name="A"),
                ref(rows[("C1", "a2")], name="B"),
            ])
        view = cw.get(self.db)
        cw.put(self.db, view["snapshot_token"],
               [ref(view_map(view)[("C1", "a1")], name="最终名")])
        self.assertEqual(self.tables()[0][0], ("C1", "最终名", "内部一"))


class ActionFormatTests(Base):
    def test_unknown_type_and_keys(self):
        with self.assertRaises(InputError):
            parse_actions([{"type": "nope"}])
        with self.assertRaises(InputError):
            parse_actions([{"type": "delete_customer",
                            "row_ref": "rr_x", "extra": 1}])
        with self.assertRaises(InputError):
            parse_actions([{"type": "delete_customer"}])

    def test_name_must_be_text_cell(self):
        row = self.rows[("C1", "a1")]
        with self.assertRaises(InputError):
            self.put_ok([{"type": "rename_customer",
                          "row_ref": row["row_ref"], "name": "甲"}])
        with self.assertRaises(InputError):
            self.put_ok([{"type": "rename_customer",
                          "row_ref": row["row_ref"], "name": "null"}])

    def test_address_cell_null_vs_literal(self):
        row = self.rows[("C1", "a1")]
        self.put_ok([{"type": "set_address_address",
                      "row_ref": row["row_ref"], "address": "null"}])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertIsNone(addresses["a1"][3])
        view = cw.get(self.db)
        row2 = view_map(view)[("C1", "a1")]
        cw.put(self.db, view["snapshot_token"],
               [{"type": "set_address_address",
                 "row_ref": row2["row_ref"], "address": cell("null")}])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses["a1"][3], "null")
        # no trimming / no case folding
        view = cw.get(self.db)
        row3 = view_map(view)[("C1", "a1")]
        cw.put(self.db, view["snapshot_token"],
               [{"type": "set_address_address",
                 "row_ref": row3["row_ref"], "address": cell("  null  ")}])
        addresses = {a[0]: a for a in self.tables()[1]}
        self.assertEqual(addresses["a1"][3], "  null  ")

    def test_unicode_roundtrip_through_actions(self):
        row = self.rows[("C1", "a1")]
        value = "中\\文\n\t\"引号\" 😀 \u0001"
        self.put_ok([
            ref(row, name=value),
            ref(row, label=value),
            ref(row, address=value),
        ])
        customers, addresses = self.tables()
        self.assertEqual(customers[0][1], value)
        self.assertEqual(addresses[0][2], value)
        self.assertEqual(addresses[0][3], value)
        view = cw.get(self.db)
        rows = view_map(view)
        self.assertEqual(decode_text(rows[("C1", "a1")]["customer_name"]),
                         value)
        self.assertEqual(decode_text(rows[("C1", "a1")]["address_address"]),
                         value)


class ReferenceModelTests(Base):
    def _ref_check(self, model, view):
        dump = cw.source_tables(self.db)
        got_c = sorted((c["cid"], c["name"], c["internal_note"])
                       for c in dump["customers"])
        got_a = sorted((a["aid"], a["cid"], a["label"], a["address"],
                        a["secret_tag"]) for a in dump["addresses"])
        ref_c, ref_a = model.tables()
        self.assertEqual(got_c, ref_c)
        self.assertEqual(got_a, ref_a)
        got_view = [{
            "row_kind": r["row_kind"], "cid": r["cid_display"],
            "aid": r["aid_display"] or None,
            "customer_name": r["customer_name"],
            "address_label": r["address_label"],
            "address_address": r["address_address"],
        } for r in view["rows"]]
        expected = [{
            "row_kind": r["row_kind"], "cid": r["cid"], "aid": r["aid"],
            "customer_name": r["customer_name"],
            "address_label": r["address_label"],
            "address_address": r["address_address"],
        } for r in model.view()]
        self.assertEqual(got_view, expected)

    def test_combined_batch_matches_reference_model(self):
        rows = self.rows
        actions = [
            ref(rows[("C1", "a1")], name="新甲"),
            ref(rows[("C1", "a1")], label="公司"),
            ref(rows[("C1", "a2")], label="家"),
            {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
             "to_cid": "C2"},
            {"type": "add_address", "temp_ref": "T", "cid": "C2",
             "label": cell("仓库"), "address": cell("路 1 号")},
            ref(rows[("C1", "a2")], address="无\n地址"),
        ]
        result = self.put_ok(actions)
        new_aid = result["new_address_aids"]["T"]
        # the reference model interprets the *same* commands independently;
        # the product's newly allocated aid is only used as a row label
        model = RefModel.from_tables(
            [("C1", "甲", "内部一"), ("C2", "乙", None)],
            [("a1", "C1", "家", "中山路1号", "tag-a1"),
             ("a2", "C1", "公司", "解放路9号", "tag-a2")],
        ).apply(
            [("rename", "C1", "新甲"),
             ("label", "C1", "a1", "公司"),
             ("label", "C1", "a2", "家"),
             ("move", "C1", "a1", "C2"),
             ("add", "T", "C2", "仓库", "路 1 号", None),
             ("address", "C1", "a2", "无\n地址")],
            {"T": new_aid},
            {"new_address_address": None,
             "new_address_secret_tag": "cfg-default-secret"},
        )
        self._ref_check(model, cw.get(self.db))

    def test_reference_model_checks_agree_with_product_verdicts(self):
        """Same combined batch, product and reference both accept or reject."""
        rows = self.rows
        batches = [
            ("ok rename", [ref(rows[("C2", "")], name="乙二")]),
            ("bad rename", [ref(rows[("C1", "a1")], name="A"),
                            ref(rows[("C1", "a2")], name="B")]),
            ("bad delete", [{"type": "delete_customer",
                             "row_ref": rows[("C1", "a1")]["row_ref"]}]),
            ("bad final label", [ref(rows[("C1", "a1")], label="L"),
                                 ref(rows[("C1", "a2")], label="L")]),
        ]
        commands_for = {
            "ok rename": [("rename", "C2", "乙二")],
            "bad rename": [("rename", "C1", "A"), ("rename", "C1", "B")],
            "bad delete": [("delete_cust", "C1")],
            "bad final label": [("label", "C1", "a1", "L"),
                                ("label", "C1", "a2", "L")],
        }
        base = RefModel.from_tables(
            [("C1", "甲", "内部一"), ("C2", "乙", None)],
            [("a1", "C1", "家", "中山路1号", "tag-a1"),
             ("a2", "C1", "公司", "解放路9号", "tag-a2")],
        )
        for name, actions in batches:
            report = cw.explain(self.db, self.view["snapshot_token"], actions)
            product_ok = report["ok"]
            reference_ok = True
            try:
                base.apply(commands_for[name], {},
                           {"new_address_address": None})
            except RefReject:
                reference_ok = False
            self.assertEqual(product_ok, reference_ok,
                             "verdict mismatch for %r" % (name,))

    def test_reference_rejects_what_product_rejects(self):
        model = RefModel.from_tables(
            [("C1", "甲", "内部一"), ("C2", "乙", None)],
            [("a1", "C1", "家", "中山路1号", "tag-a1"),
             ("a2", "C1", "公司", "解放路9号", "tag-a2")],
        )
        with self.assertRaises(RefReject):
            model.apply([("delete_addr", "C1", "a1"),
                         ("delete_addr", "C1", "a1")],
                        {}, {"new_address_address": None})
        with self.assertRaises(RefReject):
            model.apply([("delete_cust", "C1")],
                        {}, {"new_address_address": None})
        with self.assertRaises(RefReject):
            model.apply([("label", "C1", "a1", "公司")],
                        {}, {"new_address_address": None})


class CliTests(unittest.TestCase):
    def test_cli_flow(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        try:
            d = Path(tmp.name)
            db = d / "cli.db"
            out = d / "view.json"
            acts = d / "actions.json"
            env = {"PATH": "/usr/bin:/bin:/usr/local/bin",
                   "PYTHONPATH": "", "HOME": str(d)}

            def run(*args, expect=0):
                proc = subprocess.run(
                    [sys.executable, "-I", str(ROOT / "cwcli.py"), *args],
                    capture_output=True, text=True, env=env, cwd=str(ROOT))
                if proc.returncode != expect:
                    raise AssertionError(
                        "cli %r exited %d\nstdout=%s\nstderr=%s"
                        % (args, proc.returncode, proc.stdout, proc.stderr))
                return proc

            run("init", "--db", str(db), "--sample", str(SAMPLE))
            run("get", "--db", str(db), "--out", str(out))
            view = json.loads(out.read_text(encoding="utf-8"))
            row = [r for r in view["rows"] if r["aid_display"] == "a1"][0]
            acts.write_text(json.dumps([
                {"type": "rename_customer", "row_ref": row["row_ref"],
                 "name": cell("命令甲")}], ensure_ascii=False),
                encoding="utf-8")
            proc = run("explain", "--db", str(db), "--view", str(out),
                       "--actions", str(acts))
            report = json.loads(proc.stdout)
            self.assertTrue(report["ok"])
            self.assertFalse(report["wrote"])
            proc = run("put", "--db", str(db), "--view", str(out),
                       "--actions", str(acts))
            result = json.loads(proc.stdout)
            self.assertEqual(result["revision_after"], 1)
            proc = run("source", "--db", str(db))
            dump = json.loads(proc.stdout)
            self.assertEqual(dump["customers"][0]["name"], "命令甲")
            self.assertEqual(dump["customers"][0]["internal_note"], "内部一")
            # a stale put exits 3
            run("put", "--db", str(db), "--view", str(out),
                "--actions", str(acts), expect=3)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
