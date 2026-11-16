#!/usr/bin/env python3
"""Turn a ``get`` export into editable actions, for the README walkthrough.

The helper only **reads** the export file.  It never calls ``explain`` or
``put``; the resulting JSON is meant to be inspected, edited by hand and then
fed to the CLI.

Usage::

    python3 -I tools/make_actions.py view.json --out actions.json \\
        [--rename C1 新甲] [--rename-from-aid a1 新甲] [--label a2 公司] \\
        [--address a1 '新地址'] [--move a1 C2] [--delete-address a1] \\
        [--delete-customer C1] [--add tmp1 C2 仓库 '仓库路 1 号']

``--rename-from-aid`` demonstrates that a rename can be issued from any
exported row of a customer (here: the a1 address row).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cleanwrite.codec import encode_text  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("view")
    parser.add_argument("--out", required=True)
    parser.add_argument("--rename", nargs=2, action="append", default=[],
                        metavar=("CID", "NAME"))
    parser.add_argument("--rename-from-aid", nargs=2, action="append",
                        default=[], metavar=("AID", "NAME"))
    parser.add_argument("--label", nargs=2, action="append", default=[],
                        metavar=("AID", "LABEL"))
    parser.add_argument("--address", nargs=2, action="append", default=[],
                        metavar=("AID", "TEXT_OR_none"))
    parser.add_argument("--move", nargs=2, action="append", default=[],
                        metavar=("AID", "TO_CID"))
    parser.add_argument("--delete-address", action="append", default=[])
    parser.add_argument("--delete-customer", action="append", default=[])
    parser.add_argument("--add", nargs=4, action="append", default=[],
                        metavar=("TEMP", "CID", "LABEL", "TEXT_OR_none"))
    args = parser.parse_args(argv)

    view = json.loads(Path(args.view).read_text(encoding="utf-8"))
    rows = view["rows"]
    first_by_cid = {}
    by_aid = {}
    placeholder_by_cid = {}
    for row in rows:
        first_by_cid.setdefault(row["cid_display"], row)
        if row["aid_display"]:
            by_aid[row["aid_display"]] = row
        else:
            placeholder_by_cid[row["cid_display"]] = row

    actions = []

    def cell(value):
        return "null" if value == "none" else encode_text(value)

    for cid, name in args.rename:
        actions.append({"type": "rename_customer",
                        "row_ref": first_by_cid[cid]["row_ref"],
                        "name": encode_text(name)})
    for aid, name in args.rename_from_aid:
        actions.append({"type": "rename_customer",
                        "row_ref": by_aid[aid]["row_ref"],
                        "name": encode_text(name)})
    for aid, label in args.label:
        actions.append({"type": "set_address_label",
                        "row_ref": by_aid[aid]["row_ref"],
                        "label": encode_text(label)})
    for aid, text in args.address:
        actions.append({"type": "set_address_address",
                        "row_ref": by_aid[aid]["row_ref"],
                        "address": cell(text)})
    for aid, to_cid in args.move:
        actions.append({"type": "move_address",
                        "row_ref": by_aid[aid]["row_ref"], "to_cid": to_cid})
    for aid in args.delete_address:
        actions.append({"type": "delete_address",
                        "row_ref": by_aid[aid]["row_ref"]})
    for cid in args.delete_customer:
        row = placeholder_by_cid.get(cid) or first_by_cid[cid]
        actions.append({"type": "delete_customer",
                        "row_ref": row["row_ref"]})
    for temp, cid, label, text in args.add:
        actions.append({"type": "add_address", "temp_ref": temp, "cid": cid,
                        "label": encode_text(label), "address": cell(text)})

    Path(args.out).write_text(
        json.dumps(actions, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print("wrote %s (%d action(s))" % (args.out, len(actions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
