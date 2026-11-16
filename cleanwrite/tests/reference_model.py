"""Independent relation reference model — deliberately separate from the product.

This module does **not** import ``cleanwrite`` and does not use the product's
derivation, SQLite queries or ``explain``.  It implements the source relations
``C`` and ``A`` as plain Python dicts and evaluates a small, *command-shaped*
action language directly:

    ("rename", cid, name)
    ("label", cid, aid_or_temp, label)
    ("address", cid, aid_or_temp, value_or_None)
    ("move", cid, aid_or_temp, to_cid)
    ("delete_addr", cid, aid_or_temp)
    ("add", temp, cid, label, address, secret_tag)
    ("delete_cust", cid)

Every command carries the *exported* customer of its row, exactly like a row
reference does.  The evaluator iterates in list order, then checks the same
end-state rules (customer deletion, label uniqueness, target survival) with
plain loops.  Its only job is to be an obviously-correct oracle for combined
batches, so the drill and the tests can compare "product result" with
"reference result" row by row.

The reference model also computes the expected flat view from scratch (its
own LEFT JOIN + code-point ordering + its own cell encoder), so a comparison
covers both the source state **and** the re-exported view.
"""

from __future__ import annotations


class RefReject(Exception):
    pass


def cell(value):
    """Independent re-implementation of the canonical cell format."""
    if value is None:
        return "null"
    out = ['s"']
    for ch in value:
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif cp <= 0x1F or cp == 0x7F or cp > 0xFFFF:
            out.append("\\u{%X}" % cp)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class RefModel:
    def __init__(self, customers, addresses):
        # customers: {cid: (name, internal_note)}
        # addresses: {aid: (cid, label, address, secret_tag)}
        self.customers = dict(customers)
        self.addresses = dict(addresses)

    @classmethod
    def from_tables(cls, c_rows, a_rows):
        customers = {r[0]: (r[1], r[2]) for r in c_rows}
        addresses = {r[0]: (r[1], r[2], r[3], r[4]) for r in a_rows}
        return cls(customers, addresses)

    def copy(self):
        return RefModel(self.customers, self.addresses)

    def tables(self):
        c_rows = sorted((cid, name, note)
                        for cid, (name, note) in self.customers.items())
        a_rows = sorted((aid, cid, label, address, secret)
                        for aid, (cid, label, address, secret)
                        in self.addresses.items())
        return c_rows, a_rows

    def view(self):
        by_cid = {}
        for aid, (cid, label, address, _secret) in self.addresses.items():
            by_cid.setdefault(cid, []).append(aid)
        rows = []
        for cid in sorted(self.customers):
            name, _note = self.customers[cid]
            aids = sorted(by_cid.get(cid, []))
            if not aids:
                rows.append({
                    "row_kind": "placeholder_no_address",
                    "cid": cid, "aid": None,
                    "customer_name": cell(name),
                    "address_label": "",
                    "address_address": "",
                })
            for aid in aids:
                cid2, label, address, _secret = self.addresses[aid]
                rows.append({
                    "row_kind": "customer_address",
                    "cid": cid2, "aid": aid,
                    "customer_name": cell(name),
                    "address_label": cell(label),
                    "address_address": cell(address),
                })
        return rows

    # -- command language ---------------------------------------------------

    def apply(self, commands, new_aids, insert_defaults):
        """Return the resulting RefModel; raise RefReject on any rule breach.

        ``new_aids`` maps temp -> the aid the product should allocate (known
        to the reference test from the product's response, but only for
        naming the added rows; the semantic checks below do not depend on
        it).  ``insert_defaults`` supplies new-address hidden defaults.
        """
        exported_customers = set(self.customers)
        exported_aids = set(self.addresses)
        work = self.copy()
        pending = {}          # temp -> [cid, label, address, secret, deleted]
        pending_order = []
        deletes = set()
        targets = []

        def customer_check(cid):
            if cid not in exported_customers:
                raise RefReject("unknown customer %r" % (cid,))

        def addr(cid, ref):
            """Resolve (cid, aid-or-temp) to (kind, key, slot)."""
            if ref in pending:
                if pending[ref][4]:
                    raise RefReject("address %r already deleted" % (ref,))
                return "temp", ref, pending[ref]
            if ref not in exported_aids:
                raise RefReject("unknown address %r" % (ref,))
            if ref not in work.addresses:
                raise RefReject("address %r already deleted" % (ref,))
            return "real", ref, list(work.addresses[ref])

        def store(kind, key, slot):
            if kind == "real":
                work.addresses[key] = tuple(slot)
            else:
                pending[key][:4] = slot[:4]

        rename_votes = {}     # cid -> set of submitted names (all stay on record)
        renamed_cid_checked = set()

        for cmd in commands:
            op = cmd[0]
            if op == "rename":
                _op, cid, name = cmd
                customer_check(cid)
                rename_votes.setdefault(cid, set()).add(name)
                renamed_cid_checked.add(cid)
                own = work.customers[cid]
                # all votes stay on record; disagreement is rejected after
                # the whole list has been interpreted
                work.customers[cid] = (name, own[1])
            elif op == "label":
                _op, cid, ref, label = cmd
                kind, key, slot = addr(cid, ref)
                slot[1] = label
                store(kind, key, slot)
            elif op == "address":
                _op, cid, ref, value = cmd
                kind, key, slot = addr(cid, ref)
                slot[2] = value
                store(kind, key, slot)
            elif op == "move":
                _op, cid, ref, to_cid = cmd
                customer_check(to_cid)
                targets.append(to_cid)
                kind, key, slot = addr(cid, ref)
                slot[0] = to_cid
                store(kind, key, slot)
            elif op == "delete_addr":
                _op, cid, ref = cmd
                kind, key, slot = addr(cid, ref)
                if kind == "real":
                    del work.addresses[key]
                else:
                    pending[key][4] = True
            elif op == "add":
                _op, temp, cid, label, address, secret = cmd
                if temp in pending:
                    raise RefReject("duplicate temp %r" % (temp,))
                customer_check(cid)
                targets.append(cid)
                pending[temp] = [cid, label, address,
                                 insert_defaults["new_address_secret_tag"]
                                 if secret is None else secret, False]
                pending_order.append(temp)
            elif op == "delete_cust":
                _op, cid = cmd
                customer_check(cid)
                deletes.add(cid)
            else:  # pragma: no cover
                raise RefReject("unknown op %r" % (op,))

        # merge rule for customer renames: every vote must agree exactly,
        # and a customer deleted in the same batch cannot also be renamed
        for cid, votes in rename_votes.items():
            if len(votes) > 1:
                raise RefReject("conflicting rename votes for %r: %r"
                                % (cid, sorted(votes)))
            if cid in deletes:
                raise RefReject("rename of deleted customer %r" % (cid,))
        # end-state: customer deletions may not leave addresses
        for cid in deletes:
            if any(a[0] == cid for a in work.addresses.values()):
                raise RefReject("customer %r still referenced" % (cid,))
            if any(not pending[t][4] and pending[t][0] == cid
                   for t in pending_order):
                raise RefReject("customer %r still referenced by a temp row"
                                % (cid,))
        for cid in deletes:
            work.customers.pop(cid)

        for cid in targets:
            if cid not in work.customers:
                raise RefReject("target customer %r was not preserved"
                                % (cid,))

        seen = {}
        for aid, (cid, label, _a, _s) in work.addresses.items():
            if (cid, label) in seen:
                raise RefReject("duplicate final label %r for %r"
                                % (label, cid))
            seen[(cid, label)] = aid
        for temp in pending_order:
            cid, label, _a, _s, deleted = pending[temp]
            if deleted:
                continue
            if (cid, label) in seen:
                raise RefReject("duplicate final label %r for %r"
                                % (label, cid))
            seen[(cid, label)] = temp

        for temp in pending_order:
            cid, label, address, secret, deleted = pending[temp]
            if deleted:
                continue
            work.addresses[new_aids[temp]] = (cid, label, address, secret)
        return work
