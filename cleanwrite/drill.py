#!/usr/bin/env python3
"""End-to-end drill for the editable clean-up view write-back.

Run from the ``cleanwrite`` directory::

    python3 -I drill.py                     # full scenario, fresh DB
    python3 -I drill.py --db work/drill.db  # keep the database

The drill prints, at every step, **both source tables including the hidden
columns**, the re-exported flat view, ``explain`` output, ``put`` results and
a comparison against the independent reference model in
``tests/reference_model.py`` (which does not import the product package,
does not call ``explain`` and does not use SQLite).

Scenario
--------
1.  Init: C1 "甲" (internal note "内部一") with a1 (label 家) and a2 (label
    公司), C2 "乙" with no address.  Different secret tags per address.
2.  Rename from the a1 row only -> both address rows show "新甲", hidden
    fields unchanged.
3.  Two different names from the two address rows -> whole batch fails, the
    revision and both source tables stay unchanged.
4.  Swap a1/a2 labels and move a1 to C2 in one batch (two-phase merge; the
    final-state uniqueness check must pass).
5.  Another batch declares C1 deleted, then deletes its remaining address ->
    success; C2 and a1 untouched.
6.  Add a temp address, edit it twice, move it, then move it back; then add
    another temp address and delete it in the same batch.
7.  Delete the last address -> placeholder appears; delete/re-add behaviour.
8.  A real NULL address next to a placeholder; invalid escapes; edit after
    delete; final label conflict; deleting a still-referenced customer;
    two old exports competing; foreign row refs / other DB tokens; a last
    action that fails after earlier valid actions -> full rollback.
9.  Empty action batch and a second empty batch after write-back: no hidden
    change (revision, tables, aid counter unchanged).
10. After a failure, re-export and get confirmation that the corrected batch
    works.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import cleanwrite as cw                      # noqa: E402
from cleanwrite.errors import CleanWriteError  # noqa: E402
from reference_model import RefModel, RefReject, cell  # noqa: E402


# ---------------------------------------------------------------------------
# printing helpers
# ---------------------------------------------------------------------------

W = 24


def hr(title=""):
    print()
    print("=" * 78)
    if title:
        print(title)
        print("=" * 78)


def show_cell(value):
    if value is None:
        return "NULL"
    return repr(value)


def print_tables(db):
    dump = cw.source_tables(db)
    print("  revision = %d" % dump["revision"])
    print("  C(cid, name, internal_note)              [内部备注 hidden]")
    for row in dump["customers"]:
        print("    %-6s %-10s %s" % (
            show_cell(row["cid"]), show_cell(row["name"]),
            show_cell(row["internal_note"])))
    print("  A(aid, cid, label, address, secret_tag)  [secret_tag hidden]")
    for row in dump["addresses"]:
        print("    %-6s %-5s %-8s %-16s %s" % (
            show_cell(row["aid"]), show_cell(row["cid"]),
            show_cell(row["label"]), show_cell(row["address"]),
            show_cell(row["secret_tag"])))
    return dump


def print_view(view, title="view (C LEFT JOIN A)"):
    print("  %s — revision %d" % (title, view["revision"]))
    print("    %-22s %-6s %-5s %-12s %-12s %s" % (
        "row_kind", "cid", "aid", "customer_name", "address_label",
        "address_address"))
    for row in view["rows"]:
        print("    %-22s %-6s %-5s %-12s %-12s %s" % (
            row["row_kind"], row["cid_display"], row["aid_display"] or "-",
            row["customer_name"], row["address_label"] or "-",
            row["address_address"] or "-"))


def rows_by_key(view):
    out = {}
    for row in view["rows"]:
        out[(row["cid_display"], row["aid_display"])] = row
    return out


def print_explain(report, limit=6):
    print("  explain: ok=%s wrote=%s export_rev=%d current_rev=%d stale=%s"
          % (report["ok"], report["wrote"], report["export_revision"],
             report["current_revision"], report["stale"]))
    for conflict in report["conflicts"]:
        print("    conflict: %s" % json.dumps(conflict, ensure_ascii=False))
    for write in report["writes"][:limit]:
        print("    write: %s" % json.dumps(write, ensure_ascii=False,
                                           sort_keys=True))
    if len(report["writes"]) > limit:
        print("    ... %d more write(s)" % (len(report["writes"]) - limit))


def db_snapshot(db):
    """Everything an 'empty batch' must not change: source, revision, counter."""
    conn = sqlite3.connect(str(db))
    try:
        meta = {k: v for k, v in conn.execute(
            "SELECT key, value FROM cw_meta WHERE key IN "
            "('revision', 'aid_counter')")}
        used = sorted(r[0] for r in conn.execute("SELECT aid FROM cw_used_aids"))
        sqlite_master = sorted(
            (r[0], r[1]) for r in conn.execute(
                "SELECT type, name FROM sqlite_master"))
    finally:
        conn.close()
    dump = cw.source_tables(db)
    return {
        "revision": int(meta["revision"]),
        "aid_counter": meta.get("aid_counter"),
        "used_aids": used,
        "customers": dump["customers"],
        "addresses": dump["addresses"],
        "schema": sqlite_master,
    }


def assert_equal(actual, expected, what):
    if actual != expected:
        print("  ASSERTION FAILED: %s" % what)
        print("    expected: %r" % (expected,))
        print("    actual:   %r" % (actual,))
        raise SystemExit(1)
    print("  ok: %s" % what)


def expect_reject(fn, what, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except (CleanWriteError, ValueError, RefReject) as exc:
        print("  ok: %s rejected: %s" % (what, exc))
        return exc
    print("  ASSERTION FAILED: %s was not rejected" % what)
    raise SystemExit(1)


def reference_check(db, view, model, label):
    """Independent oracle: source tables and view rows must match."""
    c_rows = sorted((r["cid"], r["name"], r["internal_note"])
                    for r in cw.source_tables(db)["customers"])
    a_rows = sorted((r["aid"], r["cid"], r["label"], r["address"],
                     r["secret_tag"])
                    for r in cw.source_tables(db)["addresses"])
    ref_c, ref_a = model.tables()
    assert_equal(c_rows, ref_c, "%s: C matches reference model" % label)
    assert_equal(a_rows, ref_a, "%s: A matches reference model" % label)
    ref_view = model.view()
    actual = []
    for row in view["rows"]:
        actual.append({
            "row_kind": row["row_kind"],
            "cid": row["cid_display"], "aid": row["aid_display"] or None,
            "customer_name": row["customer_name"],
            "address_label": row["address_label"],
            "address_address": row["address_address"],
        })
    ref_view_norm = [
        {"row_kind": r["row_kind"], "cid": r["cid"], "aid": r["aid"],
         "customer_name": r["customer_name"],
         "address_label": r["address_label"],
         "address_address": r["address_address"]}
        for r in ref_view
    ]
    assert_equal(actual, ref_view_norm,
                 "%s: get view matches reference model" % label)


# ---------------------------------------------------------------------------
# the drill
# ---------------------------------------------------------------------------

def run(db_path: Path):
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- step 0
    hr("0. init + sample import (C1 两个地址, C2 无地址)")
    report = cw.init_db(db_path, sample=str(ROOT / "samples" / "drill.json"))
    print(json.dumps(report, ensure_ascii=False))
    model = RefModel.from_tables(
        [("C1", "甲", "内部一"), ("C2", "乙", None)],
        [("a1", "C1", "家", "中山路1号", "tag-a1"),
         ("a2", "C1", "公司", "解放路9号", "tag-a2")],
    )
    print_tables(db_path)
    view0 = cw.get(db_path)
    print_view(view0)
    reference_check(db_path, view0, model, "step 0")
    assert_equal(view0["revision"], 0, "initial revision is 0")
    assert_equal([r["row_kind"] for r in view0["rows"]],
                 ["customer_address", "customer_address",
                  "placeholder_no_address"],
                 "placeholder only for the customer without addresses")
    raw = json.dumps(view0, ensure_ascii=False)
    assert "内部一" not in raw and "tag-a1" not in raw and "tag-a2" not in raw, \
        "hidden fields never appear in the editable payload"
    print("  ok: editable payload and token contain no hidden values")

    # ---------------------------------------------------------------- step 1
    hr("1. rename via the a1 row only -> both rows show 新甲, hidden intact")
    rows = rows_by_key(view0)
    actions = [{"type": "rename_customer",
                "row_ref": rows[("C1", "a1")]["row_ref"],
                "name": cell("新甲")}]
    exp = cw.explain(db_path, view0["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(len(exp["writes"]), 1, "explain derives one customer write")
    assert_equal(exp["writes"][0]["cid"], "C1", "derived write targets C1")
    assert_equal(exp["writes"][0]["preserved_fields"], ["internal_note"],
                 "rename preserves internal_note")
    assert_equal(cw.source_tables(db_path)["revision"], 0,
                 "explain did not advance the revision")
    ref1 = model.apply(
        [("rename", "C1", "新甲")], {},
        {"new_address_address": None})
    put = cw.put(db_path, view0["snapshot_token"], actions)
    print("  put: applied=%s revision %d -> %d new_aids=%r"
          % (put["applied"], put["revision_before"], put["revision_after"],
             put["new_address_aids"]))
    assert_equal(put["revision_after"], 1, "non-empty batch bumps revision")
    dump = print_tables(db_path)
    assert_equal(dump["customers"][0]["name"], "新甲", "C1 renamed")
    assert_equal(dump["customers"][0]["internal_note"], "内部一",
                 "C1 internal_note unchanged")
    assert_equal([a["secret_tag"] for a in dump["addresses"]],
                 ["tag-a1", "tag-a2"], "address secret tags unchanged")
    view1 = cw.get(db_path)
    print_view(view1, "view after rename")
    assert_equal([r["customer_name"] for r in view1["rows"]],
                 [cell("新甲"), cell("新甲"), cell("乙")],
                 "both C1 address rows show the new name")
    reference_check(db_path, view1, ref1, "step 1")

    # ---------------------------------------------------------------- step 2
    hr("2. conflicting names from both address rows -> whole batch fails")
    rows = rows_by_key(view1)
    conflict_actions = [
        {"type": "rename_customer", "row_ref": rows[("C1", "a1")]["row_ref"],
         "name": cell("名字A")},
        {"type": "rename_customer", "row_ref": rows[("C1", "a2")]["row_ref"],
         "name": cell("名字B")},
    ]
    exp = cw.explain(db_path, view1["snapshot_token"], conflict_actions)
    print_explain(exp)
    assert_equal(exp["ok"], False, "explain reports the rename conflict")
    before = db_snapshot(db_path)
    expect_reject(cw.put, "conflicting rename batch", db_path,
                  view1["snapshot_token"], conflict_actions)
    after = db_snapshot(db_path)
    assert_equal(after["revision"], before["revision"],
                 "revision unchanged after the failed batch")
    assert_equal(after["customers"], before["customers"],
                 "C unchanged after the failed batch")
    # same conflict, then a vote restoring the old name: still a conflict
    three = conflict_actions + [
        {"type": "rename_customer", "row_ref": rows[("C1", "a1")]["row_ref"],
         "name": cell("新甲")}]
    exp = cw.explain(db_path, view1["snapshot_token"], three)
    assert_equal(exp["ok"], False,
                 "a later vote restoring the old name does not erase the "
                 "conflict")
    # a single non-matching vote is fine: missing rows do not vote
    one = [{"type": "rename_customer", "row_ref": rows[("C2", "")]["row_ref"],
            "name": cell("新乙")}]
    exp = cw.explain(db_path, view1["snapshot_token"], one)
    assert_equal(exp["ok"], True, "unsubmitted rows do not vote")
    print("  ok: one vote from a placeholder row is accepted by explain")

    # ---------------------------------------------------------------- step 3
    hr("3. swap a1/a2 labels and move a1 to C2 in one batch")
    rows = rows_by_key(view1)
    actions = [
        {"type": "set_address_label", "row_ref": rows[("C1", "a1")]["row_ref"],
         "label": cell("公司")},
        {"type": "set_address_label", "row_ref": rows[("C1", "a2")]["row_ref"],
         "label": cell("家")},
        {"type": "move_address", "row_ref": rows[("C1", "a1")]["row_ref"],
         "to_cid": "C2"},
    ]
    exp = cw.explain(db_path, view1["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], True,
                 "final-state uniqueness allows the swapeven though the "
                 "intermediate state had duplicates (per-action checks would "
                 "have failed)")
    ref2 = ref1.copy()
    ref2 = ref2.apply(
        [("label", "C1", "a1", "公司"),
         ("label", "C1", "a2", "家"),
         ("move", "C1", "a1", "C2")],
        {}, {"new_address_address": None})
    put = cw.put(db_path, view1["snapshot_token"], actions)
    print("  put: revision %d -> %d"
          % (put["revision_before"], put["revision_after"]))
    dump = print_tables(db_path)
    assert_equal([(a["aid"], a["cid"], a["label"]) for a in dump["addresses"]],
                 [("a1", "C2", "公司"), ("a2", "C1", "家")],
                 "source moved a1 to C2 and swapped labels")
    assert_equal([a["secret_tag"] for a in dump["addresses"]],
                 ["tag-a1", "tag-a2"], "move keeps secret_tag")
    view2 = cw.get(db_path)
    print_view(view2, "view after swap+move")
    reference_check(db_path, view2, ref2, "step 3")
    # explain and put must agree: explain on the same export/revision
    # (done above) derived exactly what put applied
    assert_equal(len(exp["writes"]), len(put["writes"]),
                 "explain and put returned the same write count")

    # ---------------------------------------------------------------- step 4
    hr("4. one batch: declare C1 deleted, then delete its remaining address")
    rows = rows_by_key(view2)
    assert ("C1", "a2") in rows and ("C2", "a1") in rows
    actions = [
        {"type": "delete_customer", "row_ref": rows[("C1", "a2")]["row_ref"]},
        {"type": "delete_address", "row_ref": rows[("C1", "a2")]["row_ref"]},
    ]
    exp = cw.explain(db_path, view2["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], True, "delete customer then its address is legal")
    ref3 = ref2.copy().apply(
        [("delete_cust", "C1"), ("delete_addr", "C1", "a2")],
        {}, {"new_address_address": None})
    put = cw.put(db_path, view2["snapshot_token"], actions)
    print("  put: revision %d -> %d"
          % (put["revision_before"], put["revision_after"]))
    dump = print_tables(db_path)
    assert_equal([c["cid"] for c in dump["customers"]], ["C2"],
                 "C1 gone, C2 untouched")
    assert_equal([(a["aid"], a["cid"], a["secret_tag"])
                  for a in dump["addresses"]],
                 [("a1", "C2", "tag-a1")], "a1 unaffected")
    view3 = cw.get(db_path)
    print_view(view3, "view after C1 delete")
    reference_check(db_path, view3, ref3, "step 4")

    # ---------------------------------------------------------------- step 5
    hr("5. add temp address, edit twice in a row, move, then move back")
    rows = rows_by_key(view3)
    c2_ref = rows[("C2", "a1")]["row_ref"]
    actions = [
        {"type": "add_address", "temp_ref": "T-new", "cid": "C2",
         "label": cell("仓库"), "address": cell("旧仓库")},
        {"type": "set_address_address", "row_ref": "T-new",
         "address": cell("新仓库 1 号")},
        {"type": "set_address_label", "row_ref": "T-new", "label": cell("仓")},
        {"type": "move_address", "row_ref": "T-new", "to_cid": "C2"},
        {"type": "set_address_address", "row_ref": "T-new",
         "address": "null"},
        {"type": "add_address", "temp_ref": "T-gone", "cid": "C2",
         "label": cell("临时"), "address": cell("x")},
        {"type": "delete_address", "row_ref": "T-gone"},
    ]
    exp = cw.explain(db_path, view3["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], True, "add + edit + move + temp delete derives")
    assert_equal([a["temp_ref"] for a in exp["addresses_added"]], ["T-new"],
                 "only T-new survives")
    assert_equal(exp["addresses_added"][0]["address"], None,
                 "the last edit (NULL) wins for the temp address")
    assert_equal(exp["addresses_added"][0]["label"], "仓",
                 "the last label edit wins")
    assert_equal(exp["addresses_added"][0]["secret_tag"], "cfg-default-secret",
                 "new address takes the configured hidden default")
    assert_equal(exp["insert_defaults"]["new_address_address"], None,
                 "default address for new rows is the configured NULL")
    dump_before = cw.source_tables(db_path)
    put = cw.put(db_path, view3["snapshot_token"], actions)
    print("  put: new_address_aids=%r revision %d -> %d"
          % (put["new_address_aids"], put["revision_before"],
             put["revision_after"]))
    assert_equal(sorted(put["new_address_aids"]), ["T-new"],
                 "only the surviving temp ref gets an official aid")
    assert_equal(put["temp_refs"]["T-gone"], None,
                 "a temp ref deleted in the batch maps to null (never stored)")
    new_aid = put["new_address_aids"]["T-new"]
    assert new_aid not in {"a1", "a2"}, "new aid is never a used aid"
    dump = print_tables(db_path)
    added = [a for a in dump["addresses"] if a["aid"] == new_aid]
    assert_equal(added, [{"aid": new_aid, "cid": "C2", "label": "仓",
                          "address": None, "secret_tag": "cfg-default-secret"}],
                 "new address landed with the derived values")
    ref4 = ref3.copy()
    ref4.addresses[new_aid] = ("C2", "仓", None, "cfg-default-secret")
    view4 = cw.get(db_path)
    print_view(view4, "view after add/edit/move")
    reference_check(db_path, view4, ref4, "step 5")
    assert_equal(len(view4["rows"]), 2, "C2 has a1 + the new address")

    # ---------------------------------------------------------------- step 6
    hr("6. a real NULL address next to a placeholder; delete/re-add")
    # add a third customer with a NULL address, and a customer with no address
    insert_ok = cw.import_sample(
        db_path,
        {"customers": [
            {"cid": "C3", "name": "丙", "internal_note": "内部三"},
            {"cid": "C4", "name": "丁", "internal_note": None},
        ],
         "addresses": [
            {"aid": "a3", "cid": "C3", "label": "空址", "address": None,
             "secret_tag": "tag-a3"},
        ]},
        replace=False)
    print("  merged sample: %r" % (insert_ok,))
    ref4.customers["C3"] = ("丙", "内部三")
    ref4.customers["C4"] = ("丁", None)
    ref4.addresses["a3"] = ("C3", "空址", None, "tag-a3")
    view5 = cw.get(db_path)
    print_view(view5, "view with a real NULL address + placeholders")
    rows = rows_by_key(view5)
    assert rows[("C3", "a3")]["row_kind"] == "customer_address"
    assert rows[("C3", "a3")]["address_address"] == "null"
    assert ("C3", "") not in rows, "a customer with an address has no placeholder"
    assert rows[("C4", "")]["row_kind"] == "placeholder_no_address"
    reference_check(db_path, view5, ref4, "step 6a")

    # delete C3's only address -> placeholder for C3 appears
    actions = [{"type": "delete_address",
                "row_ref": rows[("C3", "a3")]["row_ref"]}]
    exp = cw.explain(db_path, view5["snapshot_token"], actions)
    print_explain(exp)
    put = cw.put(db_path, view5["snapshot_token"], actions)
    ref5 = ref4.copy().apply([("delete_addr", "C3", "a3")], {},
                             {"new_address_address": None})
    view6 = cw.get(db_path)
    print_view(view6, "view after deleting the last address of C3")
    rows6 = rows_by_key(view6)
    assert rows6[("C3", "")]["row_kind"] == "placeholder_no_address", \
        "last address deletion creates a placeholder, not a customer delete"
    dump = print_tables(db_path)
    assert any(c["cid"] == "C3" for c in dump["customers"]), \
        "customer C3 was not deleted"
    reference_check(db_path, view6, ref5, "step 6b")

    # re-add an address from the placeholder row's customer -> placeholder goes
    actions = [{"type": "add_address", "temp_ref": "T-back", "cid": "C3",
                "label": cell("新址"), "address": cell("新地址 1 号")}]
    put = cw.put(db_path, view6["snapshot_token"], actions)
    back = put["new_address_aids"]["T-back"]
    ref6 = ref5.copy()
    ref6.addresses[back] = ("C3", "新址", "新地址 1 号", "cfg-default-secret")
    view7 = cw.get(db_path)
    print_view(view7, "view after re-adding an address to C3")
    rows7 = rows_by_key(view7)
    assert ("C3", "") not in rows7, "placeholder disappears after a re-add"
    assert rows7[("C3", back)]["address_address"] == cell("新地址 1 号")
    reference_check(db_path, view7, ref6, "step 6c")

    # ---------------------------------------------------------------- step 7
    hr("7. invalid escape / non-canonical cell is rejected (no write)")
    rows = rows_by_key(view7)
    bad = [{"type": "rename_customer", "row_ref": rows[("C4", "")]["row_ref"],
            "name": 's"bad \\x escape"'}]
    expect_reject(cw.explain, "non-canonical escape in explain", db_path,
                  view7["snapshot_token"], bad)
    before = db_snapshot(db_path)
    expect_reject(cw.put, "non-canonical escape in put", db_path,
                  view7["snapshot_token"], bad)
    assert_equal(db_snapshot(db_path), before, "nothing changed after bad cell")
    # 'null' can never be a name
    expect_reject(cw.explain, "null name", db_path, view7["snapshot_token"],
                  [{"type": "rename_customer",
                    "row_ref": rows[("C4", "")]["row_ref"], "name": "null"}])
    # a literal s"null" is a *text* value and must be accepted
    ok = cw.explain(db_path, view7["snapshot_token"],
                    [{"type": "rename_customer",
                      "row_ref": rows[("C4", "")]["row_ref"],
                      "name": cell("null")}])
    assert_equal(ok["ok"], True, 's"null" is the literal string, not NULL')

    # ---------------------------------------------------------------- step 8
    hr("8. edit after delete / final label conflict / referenced customer")
    rows = rows_by_key(view7)
    actions = [
        {"type": "delete_address", "row_ref": rows[("C3", back)]["row_ref"]},
        {"type": "set_address_label", "row_ref": rows[("C3", back)]["row_ref"],
         "label": cell("又改")},
    ]
    exp = cw.explain(db_path, view7["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], False, "edit after delete is rejected")
    # final label conflict
    actions = [
        {"type": "set_address_label", "row_ref": rows[("C2", "a1")]["row_ref"],
         "label": cell("冲突")},
        {"type": "set_address_label",
         "row_ref": rows[("C2", new_aid)]["row_ref"], "label": cell("冲突")},
    ]
    exp = cw.explain(db_path, view7["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], False, "duplicate final label is rejected")
    # deleting a still-referenced customer
    actions = [{"type": "delete_customer",
                "row_ref": rows[("C2", "a1")]["row_ref"]}]
    exp = cw.explain(db_path, view7["snapshot_token"], actions)
    print_explain(exp)
    assert_equal(exp["ok"], False, "deleting a referenced customer is rejected")
    before = db_snapshot(db_path)
    expect_reject(cw.put, "referenced customer delete", db_path,
                  view7["snapshot_token"], actions)
    assert_equal(db_snapshot(db_path), before,
                 "failed batch left the database byte-for-byte equivalent")
    # placeholder cannot be edited as an address
    actions = [{"type": "delete_address",
                "row_ref": rows[("C4", "")]["row_ref"]}]
    exp = cw.explain(db_path, view7["snapshot_token"], actions)
    assert_equal(exp["ok"], False, "placeholder cannot be an address action")

    # ---------------------------------------------------------------- step 9
    hr("9. vector: last action fails -> earlier actions roll back")
    rows = rows_by_key(view7)
    before = db_snapshot(db_path)
    rollback_actions = [
        {"type": "rename_customer", "row_ref": rows[("C4", "")]["row_ref"],
         "name": cell("丁改")},
        {"type": "set_address_label", "row_ref": rows[("C2", "a1")]["row_ref"],
         "label": cell("改过")},
        {"type": "add_address", "temp_ref": "T-rb", "cid": "C4",
         "label": cell("回滚址")},
        {"type": "delete_customer",
         "row_ref": rows[("C2", "a1")]["row_ref"]},   # fails: still referenced
    ]
    expect_reject(cw.put, "batch whose last action fails", db_path,
                  view7["snapshot_token"], rollback_actions)
    assert_equal(db_snapshot(db_path), before,
                 "rename/label/add from the failed batch left no trace")
    # and a get afterwards still shows the original data
    view8 = cw.get(db_path)
    rows8 = rows_by_key(view8)
    assert_equal(rows8[("C4", "")]["customer_name"], cell("丁"),
                 "C4 name unchanged after rollback")

    # ------------------------------------------------------ step 10 -------
    hr("10. two stale exports race; only the first one wins")
    v_a = cw.get(db_path)
    v_b = cw.get(db_path)
    assert_equal(v_a["revision"], v_b["revision"], "both exports at one revision")
    rows_a = rows_by_key(v_a)
    put_a = cw.put(db_path, v_a["snapshot_token"],
                   [{"type": "rename_customer",
                     "row_ref": rows_a[("C4", "")]["row_ref"],
                     "name": cell("先改")}])
    print("  first put: revision %d -> %d"
          % (put_a["revision_before"], put_a["revision_after"]))
    rows_b = rows_by_key(v_b)
    expect_reject(cw.put, "second export of the same revision", db_path,
                  v_b["snapshot_token"],
                  [{"type": "rename_customer",
                    "row_ref": rows_b[("C2", "a1")]["row_ref"],
                    "name": cell("后改")}])
    stale_explain = cw.explain(db_path, v_b["snapshot_token"], [])
    print("  explain on the stale export: stale=%s warnings=%r"
          % (stale_explain["stale"], stale_explain.get("warnings")))
    dump = print_tables(db_path)
    assert_equal([c["name"] for c in dump["customers"] if c["cid"] == "C4"],
                 ["先改"], "only the first batch applied")
    # explain after a write must not resurrect anything either
    assert_equal(cw.source_tables(db_path)["revision"], put_a["revision_after"],
                 "stale explain did not bump the revision")

    # ------------------------------------------------------ step 11 -------
    hr("11. cross-export / cross-database / tampered references are rejected")
    view_c = cw.get(db_path)
    rows_c = rows_by_key(view_c)
    foreign_actions = [
        {"type": "rename_customer", "row_ref": rows_a[("C4", "")]["row_ref"],
         "name": cell("混用")}]
    expect_reject(cw.explain, "row ref from another export", db_path,
                  view_c["snapshot_token"], foreign_actions)
    # tamper with a token
    token = view_c["snapshot_token"]
    parts = token.split(".")
    body = parts[1]
    tampered = parts[0] + "." + ("A" if body[0] != "A" else "B") + body[1:] \
        + "." + parts[2]
    expect_reject(cw.explain, "tampered token", db_path, tampered, [])
    # second database, token from the first
    other = db_path.parent / "other.db"
    if other.exists():
        other.unlink()
    cw.init_db(other, sample=str(ROOT / "samples" / "drill.json"))
    view_other = cw.get(other)
    expect_reject(cw.explain, "token from another database", other,
                  view_c["snapshot_token"], [])
    expect_reject(cw.put, "row ref from another database", db_path,
                  view_c["snapshot_token"],
                  [{"type": "rename_customer",
                    "row_ref": rows_by_key(view_other)[("C1", "a1")]["row_ref"],
                    "name": cell("跨库")}])
    # tampered row ref itself
    ref = rows_c[("C2", "a1")]["row_ref"]
    broken = ref[:-2] + ("a" if ref[-2] != "a" else "b")
    expect_reject(cw.explain, "tampered row ref", db_path,
                  view_c["snapshot_token"],
                  [{"type": "set_address_label", "row_ref": broken,
                    "label": cell("x")}])
    # replace the view definition: changing the config invalidates old tokens
    cw.set_config(db_path, {"new_address_address": "改变后的默认值",
                            "new_address_secret_tag": "changed"})
    expect_reject(cw.explain, "token from the previous view definition",
                  db_path, view_c["snapshot_token"], [])
    # put the config back for a clean continuation
    cw.set_config(db_path, {"new_address_address": None,
                            "new_address_secret_tag": "cfg-default-secret"})
    view_d = cw.get(db_path)
    print("  new export after config round-trip: revision %d"
          % view_d["revision"])

    # ------------------------------------------------------ step 12 -------
    hr("12. empty batches change nothing (before and after write-back)")
    before = db_snapshot(db_path)
    empty1 = cw.put(db_path, view_d["snapshot_token"], [])
    print("  empty put: %r" % {k: empty1[k] for k in
                              ("applied", "empty_batch", "revision_before",
                               "revision_after", "new_address_aids")})
    assert_equal(db_snapshot(db_path), before,
                 "empty batch left revision, tables and aid counter unchanged")
    # a write-back with content, then another empty batch
    rows_d = rows_by_key(view_d)
    cw.put(db_path, view_d["snapshot_token"],
           [{"type": "rename_customer",
             "row_ref": rows_d[("C2", "a1")]["row_ref"],
             "name": cell("再改乙")}])
    view_e = cw.get(db_path)
    mid = db_snapshot(db_path)
    empty2 = cw.put(db_path, view_e["snapshot_token"], [])
    assert_equal(empty2["revision_after"], mid["revision"],
                 "second empty batch keeps the revision")
    assert_equal(db_snapshot(db_path)["used_aids"], mid["used_aids"],
                 "second empty batch consumes no aid")
    assert_equal(db_snapshot(db_path)["customers"], mid["customers"],
                 "second empty batch changes no source field")
    print("  ok: empty batch after write-back is a no-op too")

    # ------------------------------------------------------ step 13 -------
    hr("13. failed batch -> re-export -> corrected batch continues")
    view_f = cw.get(db_path)
    rows_f = rows_by_key(view_f)
    bad_actions = [
        {"type": "rename_customer", "row_ref": rows_f[("C2", "a1")]["row_ref"],
         "name": cell("坏名")},
        {"type": "delete_address",
         "row_ref": rows_f[("C2", "a1")]["row_ref"]},
        {"type": "set_address_label",
         "row_ref": rows_f[("C2", "a1")]["row_ref"], "label": cell("晚")},
    ]
    expect_reject(cw.put, "batch editing a deleted address", db_path,
                  view_f["snapshot_token"], bad_actions)
    view_g = cw.get(db_path)
    rows_g = rows_by_key(view_g)
    good = cw.put(db_path, view_g["snapshot_token"],
                  [{"type": "rename_customer",
                    "row_ref": rows_g[("C2", "a1")]["row_ref"],
                    "name": cell("恢复后改名")}])
    print("  corrected put: revision %d -> %d"
          % (good["revision_before"], good["revision_after"]))
    view_h = cw.get(db_path)
    print_view(view_h, "final view")
    dump = print_tables(db_path)
    assert_equal([c["name"] for c in dump["customers"] if c["cid"] == "C2"],
                 ["恢复后改名"], "corrected batch applied")

    # ------------------------------------------------ reference model -----
    hr("reference model: independent recomputation of the final combination")
    model2 = RefModel.from_tables(
        [("C2", "恢复后改名", None), ("C3", "丙", "内部三"),
         ("C4", "先改", None)],
        [("a1", "C2", "公司", "中山路1号", "tag-a1"),
         (new_aid, "C2", "仓", None, "cfg-default-secret"),
         (back, "C3", "新址", "新地址 1 号", "cfg-default-secret")],
    )
    reference_check(db_path, view_h, model2, "final")
    print("  ok: source rows and the get view match the reference model")

    hr("done")
    print("Database kept at %s" % db_path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "work" / "drill.db"))
    args = parser.parse_args(argv)
    return run(Path(args.db))


if __name__ == "__main__":
    raise SystemExit(main())
