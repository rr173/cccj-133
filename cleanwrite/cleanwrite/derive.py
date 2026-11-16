"""Derivation and application of a write-back batch (``explain`` / ``put``).

The batch is interpreted with **two deliberately different merge rules**.

Customers
    A rename is an explicit intent on a shared source field.  All rename
    actions in the batch, from any exported row belonging to the same
    customer, must agree exactly.  A conflicting rename stays on record: a
    later action that restores the old name does not erase the earlier vote.
    Rows that are not submitted do not vote at all.  The customer of a row
    reference is the customer it was exported for; moving an address later in
    the batch never changes the customer a rename on that row refers to.  A
    placeholder row may be renamed (or deleted) but is never a real address.
    A customer that is explicitly deleted in the same batch and also renamed
    is a conflict, not a silent drop.

Addresses
    Address actions are applied strictly in list order.  Repeated edits to
    the same address take effect one after another.  A move keeps the ``aid``
    and the ``secret_tag``.  After an address was deleted in the batch, any
    further edit / move / delete of it is rejected.  A temp address created
    by ``add_address`` obeys the same rules; it is not written to the source
    and gets no official ``aid`` if it is deleted by the end of the batch.
    An existing address keeps its ``secret_tag`` and any other unchanged
    field; a new address takes its hidden defaults from the stored config.

Final-state rules
    * a customer delete is judged at the end of the batch: an address may be
      moved away or deleted first, but if any address still references the
      customer at the end, the whole batch is rejected;
    * deleting the last address never deletes its customer (the placeholder
      appears on the next ``get``);
    * move targets and add targets must be customers that existed at export
      time *and* still exist at the end of the batch;
    * ``label`` uniqueness is checked only on the final state, so swapping
      two labels, or moving an address into a customer while another address
      gives up the old label, both succeed.

Derivation runs entirely in memory on a copy of the exported source state and
never touches SQLite, so ``explain`` cannot consume an aid or advance the
revision.  ``put`` runs the same derivation and then applies the derived
writes in one transaction, after atomically comparing the export revision.
"""

from __future__ import annotations

from .errors import ConflictError
from .tokens import TEMP_FORBIDDEN_PREFIXES

PENDING_PREFIX = "\x00pending\x00"


def _copy_state(state: dict) -> dict:
    return {
        "customers": {cid: dict(c) for cid, c in state["customers"].items()},
        "addresses": {aid: dict(a) for aid, a in state["addresses"].items()},
    }


class _Conflicts:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, reason: str, **info):
        entry = {"reason": reason}
        entry.update(info)
        self.items.append(entry)

    def __bool__(self) -> bool:
        return bool(self.items)



def derive_batch(base_state: dict, export, actions: list[dict],
                 insert_defaults: dict, known_aids=None) -> dict:
    """Derive a batch against the exported source state.

    Pure function: no SQLite, no aid allocation.  Returns the derived
    per-identity writes, preserved field ranges, additions / deletions,
    conflicts, the final source state and the temp-ref bookkeeping.
    """
    conflicts = _Conflicts()
    structural = _Conflicts()
    final = _copy_state(base_state)
    row_refs = export.row_refs
    exported_customers = set(base_state["customers"])
    exported_aids = set(base_state["addresses"])
    # every aid that ever existed in this database (deleted ones included):
    # a temp ref must never be able to impersonate any of them
    reserved_aids = set(known_aids or ()) | exported_aids

    # temp_ref -> pending address record
    pending: dict[str, dict] = {}
    pending_order: list[str] = []

    rename_votes: dict[str, list[dict]] = {}
    delete_customers: dict[str, dict] = {}
    address_changes: dict[str, dict] = {}
    address_deleted: dict[str, dict] = {}
    target_cids: list[dict] = []
    action_records: list[dict] = []

    def add_record(action):
        rec = {"index": action["index"], "type": action["type"]}
        if "row_ref" in action:
            rec["row_ref"] = action["row_ref"]
        if "temp_ref" in action:
            rec["temp_ref"] = action["temp_ref"]
        action_records.append(rec)
        return rec

    def lookup(action):
        """Resolve a row_ref-or-temp_ref.  Returns ("real", record) or
        ("temp", pending-record) or None (conflict recorded)."""
        ref = action["row_ref"]
        if ref in pending:
            return ("temp", pending[ref])
        info = row_refs.get(ref)
        if info is None:
            structural.add("unknown_row_ref", index=action["index"],
                           row_ref=ref,
                           detail="row reference is not part of this export "
                                  "(foreign, reseeded, or tampered reference)")
            return None
        return ("real", info)

    def customer_of(action):
        rec = add_record(action)
        got = lookup(action)
        if got is None:
            return None
        kind, info = got
        if kind == "temp":
            conflicts.add("temp_ref_is_not_customer_row",
                          index=action["index"], row_ref=action["row_ref"],
                          detail="a temporary address ref cannot rename or "
                                 "delete a customer")
            return None
        rec["cid"] = info["cid"]
        return info["cid"]

    def address_of(action):
        """Return (pending-or-aid key, mutable state dict) or None."""
        rec = add_record(action)
        got = lookup(action)
        if got is None:
            return None
        kind, info = got
        if kind == "temp":
            rec["temp_ref_target"] = info["temp_ref"]
            rec["cid"] = info["state"]["cid"]
            if info["deleted"]:
                conflicts.add("address_already_deleted",
                              index=action["index"],
                              row_ref=action["row_ref"],
                              detail="temporary address was deleted earlier "
                                     "in this batch")
                return None
            return ("temp", info["temp_ref"]), info["state"], info
        if info["kind"] != "address":
            conflicts.add("placeholder_is_not_address",
                          index=action["index"], row_ref=action["row_ref"],
                          cid=info["cid"],
                          detail="a placeholder row has no real aid and cannot "
                                 "be edited, moved or deleted as an address")
            return None
        aid = info["aid"]
        rec["aid"] = aid
        rec["cid"] = info["cid"]
        if aid in address_deleted:
            conflicts.add("address_already_deleted",
                          index=action["index"], row_ref=action["row_ref"],
                          aid=aid,
                          detail="address was already deleted earlier in this "
                                 "batch")
            return None
        if aid not in final["addresses"]:
            conflicts.add("address_missing", index=action["index"],
                          row_ref=action["row_ref"], aid=aid)
            return None
        return ("real", aid), final["addresses"][aid], info

    for action in actions:
        kind = action["type"]
        idx = action["index"]

        if kind == "rename_customer":
            cid = customer_of(action)
            if cid is None:
                continue
            rename_votes.setdefault(cid, []).append({
                "index": idx,
                "row_ref": action["row_ref"],
                "name": action["name"],
            })

        elif kind == "delete_customer":
            cid = customer_of(action)
            if cid is None:
                continue
            entry = delete_customers.setdefault(cid, {
                "cid": cid,
                "indexes": [],
                "row_refs": [],
                "before": dict(base_state["customers"][cid]),
            })
            entry["indexes"].append(idx)
            entry["row_refs"].append(action["row_ref"])

        elif kind in ("set_address_label", "set_address_address"):
            got = address_of(action)
            if got is None:
                continue
            key, st, info = got
            field = "label" if kind == "set_address_label" else "address"
            st[field] = action[field]
            if key[0] == "real":
                aid = key[1]
                entry = address_changes.setdefault(aid, {
                    "aid": aid,
                    "before": dict(base_state["addresses"][aid]),
                    "indexes": [],
                    "row_refs": [],
                    "fields": [],
                })
                entry["indexes"].append(idx)
                entry["row_refs"].append(action["row_ref"])
                if field not in entry["fields"]:
                    entry["fields"].append(field)

        elif kind == "move_address":
            got = address_of(action)
            if got is None:
                continue
            key, st, info = got
            to_cid = action["to_cid"]
            target_cids.append({"cid": to_cid, "index": idx,
                                "row_ref": action["row_ref"]})
            if to_cid not in exported_customers:
                conflicts.add("unknown_target_customer", index=idx,
                              cid=to_cid, row_ref=action["row_ref"],
                              detail="move target did not exist at export time")
                continue
            st["cid"] = to_cid
            if key[0] == "real":
                aid = key[1]
                entry = address_changes.setdefault(aid, {
                    "aid": aid,
                    "before": dict(base_state["addresses"][aid]),
                    "indexes": [],
                    "row_refs": [],
                    "fields": [],
                })
                entry["indexes"].append(idx)
                entry["row_refs"].append(action["row_ref"])
                if "cid" not in entry["fields"]:
                    entry["fields"].append("cid")

        elif kind == "delete_address":
            got = address_of(action)
            if got is None:
                continue
            key, st, info = got
            if key[0] == "real":
                aid = key[1]
                address_deleted[aid] = {
                    "aid": aid,
                    "before": dict(base_state["addresses"][aid]),
                    "indexes": [idx],
                    "row_refs": [action["row_ref"]],
                }
                address_changes.pop(aid, None)
                del final["addresses"][aid]
            else:
                pending[key[1]]["deleted"] = True
                pending[key[1]]["delete_indexes"].append(idx)

        elif kind == "add_address":
            temp = action["temp_ref"]
            add_record(action)
            if temp in pending:
                structural.add("duplicate_temp_ref", index=idx, temp_ref=temp,
                              detail="temporary reference was already declared "
                                     "in this batch")
                continue
            if temp in row_refs:
                structural.add("temp_ref_collides_with_row_ref", index=idx,
                              temp_ref=temp,
                              detail="temporary reference must not reuse an "
                                     "exported row reference")
                continue
            if temp in reserved_aids:
                structural.add("temp_ref_impersonates_real_address", index=idx,
                              temp_ref=temp,
                              detail="temporary reference must not reuse a real "
                                     "address identity")
                continue
            if not isinstance(temp, str) or not temp:
                structural.add("illegal_temp_ref", index=idx, temp_ref=temp,
                               detail="temp ref must be a non-empty string")
                continue
            if any(temp.startswith(p) for p in TEMP_FORBIDDEN_PREFIXES):
                structural.add("illegal_temp_ref", index=idx, temp_ref=temp,
                              detail="temporary reference uses a reserved "
                                     "prefix")
                continue
            cid = action["cid"]
            target_cids.append({"cid": cid, "index": idx, "temp_ref": temp})
            if cid not in exported_customers:
                conflicts.add("unknown_target_customer", index=idx, cid=cid,
                              temp_ref=temp,
                              detail="add target did not exist at export time")
                continue
            address = (action["address"] if action["address_given"]
                       else insert_defaults["new_address_address"])
            pending[temp] = {
                "temp_ref": temp,
                "state": {
                    "cid": cid,
                    "label": action["label"],
                    "address": address,
                    "secret_tag": insert_defaults["new_address_secret_tag"],
                },
                "deleted": False,
                "declared_index": idx,
                "indexes": [idx],
                "delete_indexes": [],
            }
            pending_order.append(temp)

    # ---- merge customer renames -----------------------------------------
    for cid, votes in rename_votes.items():
        names = [v["name"] for v in votes]
        if len(set(names)) > 1:
            conflicts.add(
                "rename_conflict", cid=cid,
                votes=[{"name": v["name"], "index": v["index"],
                        "row_ref": v["row_ref"]} for v in votes],
                detail="all renames of one customer must be exactly equal; a "
                       "later vote restoring the old name does not cancel an "
                       "earlier conflicting vote",
            )
            continue
        if cid in delete_customers:
            conflicts.add(
                "rename_on_deleted_customer", cid=cid,
                indexes=[v["index"] for v in votes],
                row_refs=[v["row_ref"] for v in votes],
                delete_indexes=delete_customers[cid]["indexes"],
                detail="a customer explicitly deleted in this batch cannot "
                       "also be renamed",
            )
            continue
        new_name = names[0]
        if new_name != final["customers"][cid]["name"]:
            final["customers"][cid]["name"] = new_name

    # ---- judge customer deletions on the end state ----------------------
    for cid, entry in delete_customers.items():
        still = sorted(aid for aid, a in final["addresses"].items()
                       if a["cid"] == cid)
        still += sorted(t for t in pending_order
                        if not pending[t]["deleted"]
                        and pending[t]["state"]["cid"] == cid)
        if still:
            conflicts.add("customer_still_referenced", cid=cid,
                          indexes=entry["indexes"],
                          row_refs=entry["row_refs"],
                          addresses=still,
                          detail="move away or delete every address before "
                                 "deleting the customer; deleting an address "
                                 "never deletes a customer")
            continue
        del final["customers"][cid]

    # ---- every move / add target must survive the batch ------------------
    for tgt in target_cids:
        if tgt["cid"] not in final["customers"]:
            conflicts.add("target_customer_not_preserved",
                          index=tgt["index"], cid=tgt["cid"],
                          detail="move/add target must still exist at the end "
                                 "of the batch")

    # ---- final label uniqueness (end state only) -------------------------
    # Moving an address into a customer can transiently collide with its old
    # label, and a delete may remove a colliding row later in the same batch;
    # therefore uniqueness is judged only on the finished batch state.
    owners: dict[tuple, list[str]] = {}
    for aid, a in final["addresses"].items():
        owners.setdefault((a["cid"], a["label"]), []).append(aid)
    for temp in pending_order:
        if pending[temp]["deleted"]:
            continue
        st = pending[temp]["state"]
        owners.setdefault((st["cid"], st["label"]), []).append(temp)
    colliding: set[str] = set()
    for (cid, label), group in sorted(owners.items()):
        if len(group) < 2:
            continue
        conflicts.add("duplicate_label_final", cid=cid, label=label,
                      addresses=sorted(group),
                      detail="label must be unique per customer in the final "
                             "state")
        colliding.update(group)

    # ---- every final address references a live customer -----------------
    for aid, a in final["addresses"].items():
        if a["cid"] not in final["customers"]:
            conflicts.add("address_references_missing_customer", aid=aid,
                          cid=a["cid"])

    # Conflicts are reported, not raised here: ``explain`` must return the
    # derivation with conflicts, ``put`` raises before touching the source.
    # The write report only lists identities whose final value is derived
    # cleanly: rows in a label collision are not advertised as writable.
    # ---- derived per-identity writes ------------------------------------
    writes: list[dict] = []
    customers_changed: dict[str, dict] = {}
    for cid, change in sorted(final["customers"].items()):
        before = base_state["customers"][cid]
        if change["name"] != before["name"]:
            votes = rename_votes[cid]
            customers_changed[cid] = {
                "cid": cid,
                "before": dict(before),
                "after": dict(change),
                "changed_fields": ["name"],
                "preserved_fields": ["internal_note"],
                "action_indexes": [v["index"] for v in votes],
                "row_refs": [v["row_ref"] for v in votes],
            }
            writes.append({"op": "rename_customer", **customers_changed[cid]})

    addresses_changed: dict[str, dict] = {}
    for aid, change in sorted(final["addresses"].items()):
        before = base_state["addresses"][aid]
        fields = [f for f in ("cid", "label", "address")
                  if before[f] != change[f]]
        if not fields:
            continue
        entry = address_changes.get(aid, {"indexes": [], "row_refs": []})
        addresses_changed[aid] = {
            "aid": aid,
            "before": dict(before),
            "after": dict(change),
            "changed_fields": fields,
            "preserved_fields": [f for f in ("aid", "label", "address",
                                             "secret_tag")
                                 if f not in fields],
            "action_indexes": list(entry["indexes"]),
            "row_refs": list(entry["row_refs"]),
            "writable": aid not in colliding,
        }
        writes.append({"op": "update_address", **addresses_changed[aid]})
    addresses_deleted: dict[str, dict] = {}
    for aid in sorted(set(base_state["addresses"]) - set(final["addresses"])):
        entry = address_deleted[aid]
        addresses_deleted[aid] = {
            "aid": aid,
            "before": dict(entry["before"]),
            "changed_fields": [],
            "preserved_fields": [],
            "action_indexes": list(entry["indexes"]),
            "row_refs": list(entry["row_refs"]),
        }
        writes.append({"op": "delete_address", **addresses_deleted[aid]})

    added: list[dict] = []
    for temp in pending_order:
        trec = pending[temp]
        if trec["deleted"]:
            continue
        added.append({
            "temp_ref": temp,
            "cid": trec["state"]["cid"],
            "label": trec["state"]["label"],
            "address": trec["state"]["address"],
            "secret_tag": trec["state"]["secret_tag"],
            "action_indexes": list(trec["indexes"]),
            "status": "pending_new_aid",
        })
        writes.append({
            "op": "add_address",
            "temp_ref": temp,
            "cid": trec["state"]["cid"],
            "before": None,
            "after": dict(trec["state"]),
            "changed_fields": [],
            "preserved_fields": [],
            "action_indexes": list(trec["indexes"]),
            "row_refs": [],
            "status": "pending_new_aid",
        })

    customers_deleted: dict[str, dict] = {}
    for cid, entry in sorted(delete_customers.items()):
        customers_deleted[cid] = {
            "cid": cid,
            "before": dict(entry["before"]),
            "changed_fields": [],
            "preserved_fields": [],
            "action_indexes": list(entry["indexes"]),
            "row_refs": list(entry["row_refs"]),
        }
        writes.append({"op": "delete_customer", **customers_deleted[cid]})

    return {
        "conflicts": conflicts.items,
        "structural_conflicts": structural.items,
        "ok": not conflicts.items and not structural.items,
        "writes": writes,
        "customers_changed": customers_changed,
        "addresses_changed": addresses_changed,
        "addresses_added": added,
        "addresses_deleted": {"items": addresses_deleted,
                              "aids": sorted(addresses_deleted)},
        "customers_deleted": customers_deleted,
        "final_state": final,
        "pending": {
            temp: {
                "temp_ref": temp,
                "deleted": pending[temp]["deleted"],
                "cid": pending[temp]["state"]["cid"],
                "label": pending[temp]["state"]["label"],
                "address": pending[temp]["state"]["address"],
                "secret_tag": pending[temp]["state"]["secret_tag"],
                "action_indexes": list(pending[temp]["indexes"]),
            }
            for temp in pending_order
        },
        "insert_defaults": dict(insert_defaults),
        "actions": action_records,
    }


def apply_derived(conn, derived: dict, new_aids: dict[str, str]) -> dict:
    """Apply the derived writes inside the caller's transaction.

    ``new_aids`` maps every surviving temp ref to the officially allocated
    aid.  The caller has already compared the export revision and state hash
    and holds ``BEGIN IMMEDIATE``.
    """
    for aid in derived["addresses_deleted"]["aids"]:
        cur = conn.execute("DELETE FROM cw_addresses WHERE aid = ?", (aid,))
        if cur.rowcount != 1:
            raise ConflictError("address %r disappeared before write-back"
                                % (aid,))
    for cid, change in sorted(derived["customers_changed"].items()):
        cur = conn.execute(
            "UPDATE cw_customers SET name = ? WHERE cid = ? AND name = ?",
            (change["after"]["name"], cid, change["before"]["name"]),
        )
        if cur.rowcount != 1:
            raise ConflictError("customer %r changed before write-back"
                                % (cid,))
    for aid, change in sorted(derived["addresses_changed"].items()):
        before = change["before"]
        after = change["after"]
        fields = [f for f in ("cid", "label", "address")
                  if before[f] != after[f]]
        if not fields:
            continue
        sets = ", ".join("%s = ?" % f for f in fields)
        params = [after[f] for f in fields] + [aid]
        cur = conn.execute(
            "UPDATE cw_addresses SET %s WHERE aid = ?" % sets, params)
        if cur.rowcount != 1:
            raise ConflictError("address %r changed before write-back"
                                % (aid,))
    for cid in sorted(derived["customers_deleted"]):
        cur = conn.execute("DELETE FROM cw_customers WHERE cid = ?", (cid,))
        if cur.rowcount != 1:
            raise ConflictError("customer %r disappeared before write-back"
                                % (cid,))
    mapped: dict[str, str] = {}
    for entry in derived["addresses_added"]:
        temp = entry["temp_ref"]
        aid = new_aids[temp]
        conn.execute(
            "INSERT INTO cw_addresses(aid, cid, label, address, secret_tag) "
            "VALUES(?, ?, ?, ?, ?)",
            (aid, entry["cid"], entry["label"], entry["address"],
             entry["secret_tag"]),
        )
        mapped[temp] = aid
    return mapped
