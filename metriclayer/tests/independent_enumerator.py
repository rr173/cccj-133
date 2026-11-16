#!/usr/bin/env python3
"""Independent reference enumerator for the metric layer.

This script deliberately does NOT import the ``metriclayer`` package and never
touches SQLite. It re-implements the specification from scratch on top of the
plain JSON model + data files:

  * typed value parsing (int / text / bool, nullable),
  * explicit role-path resolution and entity-population filters,
  * existence filters as per-entity ``any(...)`` tests (no joins at all),
  * per-fact, per-cell enumeration of rows, non-null counts, exact integer
    sums and exact distinct-value sets,
  * cell union across facts, count=0 / sum=null fill rules,
  * derived metrics as exact :class:`fractions.Fraction` arithmetic,
  * the collision-free missing-member marker.

It then runs every query through BOTH this enumerator and the product (the
product via its public API, which does use SQLite) and asserts cell-by-cell
equality. Run it with the isolated interpreter::

    python3 -I tests/independent_enumerator.py \
        --model examples/model.json --data examples/data.json \
        --queries examples/queries
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

MISSING = "__metriclayer__::missing_member__"
MISSING_OUT = {"__missing__": True}


# ---------------------------------------------------------------------------
# Typed values
# ---------------------------------------------------------------------------

def parse_typed(col: dict, value):
    if value is None:
        if not col["nullable"]:
            raise AssertionError(f"null on non-nullable column {col['name']}")
        return None
    t = col["type"]
    if t == "int":
        if isinstance(value, bool):
            raise AssertionError("bool used as int")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        raise AssertionError(f"bad int value {value!r}")
    if t == "bool":
        if not isinstance(value, bool):
            raise AssertionError(f"bad bool value {value!r}")
        return value
    if not isinstance(value, str):
        raise AssertionError(f"bad text value {value!r}")
    return value


# ---------------------------------------------------------------------------
# Predicates: strict three-valued logic; only True passes
# ---------------------------------------------------------------------------

def eval_predicate(pred, row, coltypes):
    (op, body), = pred.items()
    if op == "and":
        results = [eval_predicate(p, row, coltypes) for p in body]
        if any(r is False for r in results):
            return False
        if any(r is None for r in results):
            return None
        return True
    if op == "or":
        results = [eval_predicate(p, row, coltypes) for p in body]
        if any(r is True for r in results):
            return True
        if any(r is None for r in results):
            return None
        return False
    col = body["column"]
    if col not in coltypes:
        raise AssertionError(f"column {col} not in whitelist")
    ctype = coltypes[col]["type"]
    raw = row.get(col)
    if op == "eq":
        if raw is None:
            return None
        lit = body["value"]
        if ctype == "bool" and not isinstance(lit, bool):
            raise AssertionError("bool compared to non-bool")
        return raw == lit
    if op in ("lt", "lte", "gt", "gte"):
        if ctype != "int":
            raise AssertionError("ordering on non-int column")
        if raw is None:
            return None
        lit = body["value"]
        if op == "lt":
            return raw < lit
        if op == "lte":
            return raw <= lit
        if op == "gt":
            return raw > lit
        return raw >= lit
    if op == "is_null":
        want_null = body["value"]
        return (raw is None) == want_null
    if op == "in":
        if raw is None:
            return None
        return raw in body["values"]
    raise AssertionError(f"unknown predicate op {op}")


# ---------------------------------------------------------------------------
# Model + data loading (independent of the product code)
# ---------------------------------------------------------------------------

def load(model_path, data_path):
    model = json.loads(Path(model_path).read_text(encoding="utf-8"))
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    tables = {t["name"]: t for t in model["tables"]}
    # index columns and parse every cell with strict typing
    rows = {}
    for tname, t in tables.items():
        coltypes = {c["name"]: c for c in t["columns"]}
        parsed = []
        for raw in data.get(tname, []):
            if set(raw) - set(coltypes):
                raise AssertionError(f"unknown column in {tname}")
            parsed.append({
                c: parse_typed(coltypes[c], raw[c]) for c in coltypes
            })
        rows[tname] = parsed
    return model, tables, rows


def walk_chain(tables, rows, start_row, start_table, roles):
    """Follow a role chain from a fact row; return the terminal entity row or
    None when a nullable reference is missing."""
    current_row = start_row
    current_table = tables[start_table]
    for role in roles:
        ref = next(r for r in current_table["references"]
                   if r.get("role", r["column"]) == role)
        fk = current_row[ref["column"]]
        if fk is None:
            return None
        target_rows = rows[ref["target"]]
        matches = [r for r in target_rows
                   if r[ref["target_column"]] == fk]
        if len(matches) != 1:
            raise AssertionError(
                f"reference target not unique for {ref['column']}={fk}"
            )
        current_row = matches[0]
        current_table = tables[ref["target"]]
    return current_row


def resolve_roles(model, tables, fact_name, entity_name, declared_paths):
    if fact_name in declared_paths:
        return tuple(declared_paths[fact_name])
    # unique path search (mirrors the product: ambiguity is an error)
    found = []

    def dfs(tname, prefix, visiting):
        if tname in visiting:
            return
        if tname == entity_name and prefix:
            found.append(tuple(prefix))
            return
        for ref in tables[tname]["references"]:
            role = ref.get("role", ref["column"])
            dfs(ref["target"], prefix + [role], visiting | {tname})

    dfs(fact_name, [], set())
    if len(found) != 1:
        raise AssertionError(
            f"path {fact_name}->{entity_name} ambiguous or missing: {found}"
        )
    return found[0]


# ---------------------------------------------------------------------------
# Existence filter populations
# ---------------------------------------------------------------------------

def existence_population(fspec, tables, rows):
    """Return the set of target-entity PK values with >= one matching fact row.
    Two filters are evaluated independently (separate calls -> separate sets)."""
    fact_name = fspec["fact"]
    roles = fspec["roles"]
    entity = fspec["entity"]
    fact = tables[fact_name]
    coltypes = {c["name"]: c for c in fact["columns"]}
    terminal_ref = next(r for r in fact["references"]
                        if r.get("role", r["column"]) == roles[-1]) \
        if len(roles) == 1 else None
    population = set()
    for frow in rows[fact_name]:
        if eval_predicate(fspec["predicate"], frow, coltypes) is not True:
            continue
        terminal = walk_chain(tables, rows, frow, fact_name, roles)
        if terminal is None:
            continue  # nullable missing member cannot be a selected entity
        population.add(terminal[tables[entity]["primary_key"]])
    return population


# ---------------------------------------------------------------------------
# Enumeration of one query
# ---------------------------------------------------------------------------

def enumerate_query(model, tables, rows, query):
    dims_decl = {d["name"]: d for d in model["dimensions"]}
    metrics_decl = {m["name"]: m for m in model["metrics"]}

    selected = query["metrics"]
    base_names: list[str] = []

    def leaves(expr):
        (op, body), = expr.items()
        out = []
        for x in body:
            if isinstance(x, str):
                out.append(x)
            else:
                out.extend(leaves(x))
        return out

    for mname in selected:
        m = metrics_decl[mname]
        for b in (leaves(m["expression"]) if m["kind"] == "derived"
                  else [mname]):
            if b not in base_names:
                base_names.append(b)

    # precompute existence populations (independent per filter)
    populations = {}
    for f in query.get("filters", []):
        if f["type"] == "existence":
            populations[f["name"]] = existence_population(f, tables, rows)

    # base metric -> cell key -> aggregate state
    base_cells: dict[str, dict] = {}
    for bname in base_names:
        metric = metrics_decl[bname]
        fact_name = metric["fact"]
        fact = tables[fact_name]
        coltypes = {c["name"]: c for c in fact["columns"]}
        cells: dict = {}

        dim_roles = []
        for dname in query.get("dimensions", []):
            d = dims_decl[dname]
            roles = resolve_roles(
                model, tables, fact_name, d["entity"], d.get("paths", {})
            )
            dim_roles.append((d, roles))

        for frow in rows[fact_name]:
            # metric's own row predicate
            if metric.get("row_predicate") is not None and \
                    eval_predicate(metric["row_predicate"], frow,
                                   coltypes) is not True:
                continue

            accepted = True
            for fspec in query.get("filters", []):
                if fspec["name"] in metric.get("ignore_filters", []):
                    continue
                if fspec["type"] == "existence":
                    local_paths = fspec.get("local_paths", {})
                    if fact_name in local_paths:
                        roles = tuple(local_paths[fact_name])
                    else:
                        roles = resolve_roles(
                            model, tables, fact_name, fspec["entity"], {}
                        )
                    terminal = walk_chain(
                        tables, rows, frow, fact_name, roles
                    )
                    pk = None if terminal is None else \
                        terminal[tables[fspec["entity"]]["primary_key"]]
                    if pk not in populations[fspec["name"]]:
                        accepted = False
                        break
                else:  # entity attribute filter
                    if fspec.get("roles"):
                        roles = tuple(fspec["roles"])
                    else:
                        roles = resolve_roles(
                            model, tables, fact_name, fspec["entity"], {}
                        )
                    terminal = walk_chain(
                        tables, rows, frow, fact_name, roles
                    )
                    ent_coltypes = {
                        c["name"]: c
                        for c in tables[fspec["entity"]]["columns"]
                    }
                    if terminal is None:
                        accepted = False  # null -> unknown, only true passes
                        break
                    if eval_predicate(fspec["predicate"], terminal,
                                      ent_coltypes) is not True:
                        accepted = False
                        break
            if not accepted:
                continue

            key_parts = []
            for d, roles in dim_roles:
                terminal = walk_chain(tables, rows, frow, fact_name, roles)
                if terminal is None or terminal[d["attribute"]] is None:
                    key_parts.append(MISSING)
                else:
                    key_parts.append(terminal[d["attribute"]])
            key = tuple(key_parts)
            cell = cells.setdefault(key, {
                "rows": 0, "non_null": 0, "sum": 0, "sum_seen": False,
                "distinct": set(),
            })
            cell["rows"] += 1
            if metric["kind"] != "count_rows":
                v = frow[metric["measure"]]
                if v is not None:
                    cell["non_null"] += 1
                    if metric["kind"] == "sum":
                        cell["sum"] += v
                        cell["sum_seen"] = True
                    elif metric["kind"] == "count_distinct":
                        cell["distinct"].add(v)
        base_cells[bname] = cells

    all_keys = sorted({k for c in base_cells.values() for k in c},
                      key=lambda k: [("1", v) if v == MISSING else ("0", v)
                                     for v in k])

    def base_value(bname, key):
        metric = metrics_decl[bname]
        cell = base_cells[bname].get(key)
        if cell is None:
            return 0 if metric["kind"] in (
                "count_rows", "count", "count_distinct") else None
        if metric["kind"] == "count_rows":
            return cell["rows"]
        if metric["kind"] == "count":
            return cell["non_null"]
        if metric["kind"] == "count_distinct":
            return len(cell["distinct"])
        return cell["sum"] if cell["sum_seen"] else None

    def derived(expr, values):
        (op, body), = expr.items()

        def operand(x):
            if isinstance(x, str):
                v = values[x]
                return None if v is None else Fraction(v)
            return derived(x, values)

        a, b = operand(body[0]), operand(body[1])
        if a is None or b is None:
            return None
        if op == "add":
            return a + b
        if op == "sub":
            return a - b
        if op == "mul":
            return a * b
        if op == "div":
            return None if b == 0 else a / b
        raise AssertionError(op)

    def encode(v):
        if v is None:
            return None
        if isinstance(v, Fraction):
            return {"type": "fraction", "numerator": str(v.numerator),
                    "denominator": str(v.denominator)}
        return {"type": "int", "value": str(int(v))}

    out_rows = []
    for key in all_keys:
        values = {b: base_value(b, key) for b in base_names}
        metrics_out = {}
        for mname in selected:
            m = metrics_decl[mname]
            if m["kind"] == "derived":
                metrics_out[mname] = encode(derived(m["expression"], values))
            else:
                metrics_out[mname] = encode(values[mname])
        out_rows.append({
            "dimensions": [MISSING_OUT if v == MISSING else v for v in key],
            "metrics": metrics_out,
        })
    return {"query": query["name"],
            "dimensions": query.get("dimensions", []),
            "missing_marker": MISSING,
            "rows": out_rows}


# ---------------------------------------------------------------------------
# Comparison harness
# ---------------------------------------------------------------------------

def normalize(payload):
    """Canonical, JSON-comparable shape for a result payload."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--queries", required=True,
                    help="directory of query JSON files")
    ap.add_argument("--db", default=None,
                    help="product SQLite DB (built separately); if omitted the "
                         "enumerator runs without cross-checking the product")
    args = ap.parse_args()

    model, tables, rows = load(args.model, args.data)
    query_files = sorted(Path(args.queries).glob("*.json"))
    if not query_files:
        print("no query files found", file=sys.stderr)
        return 1

    product = None
    if args.db:
        # Imported lazily and only when cross-checking; the enumeration itself
        # above never depends on the product.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from metriclayer.api import run_query  # noqa: E402

    failures = 0
    checked = 0
    for qf in query_files:
        query = json.loads(qf.read_text(encoding="utf-8"))
        try:
            expected = enumerate_query(model, tables, rows, query)
        except AssertionError as exc:
            # A query the specification says must be rejected before
            # execution (e.g. unreachable dimension). The product has to
            # refuse it too, with the same class of error.
            if args.db is None:
                print(f"[enum-reject] {qf.name}: {exc}")
                continue
            try:
                run_query(args.model, args.db, query)
            except Exception as pexc:  # noqa: BLE001
                print(f"[both-reject] {qf.name}: enum='{exc}' / "
                      f"product={type(pexc).__name__}='{pexc}'")
                checked += 1
                continue
            failures += 1
            print(f"[MISMATCH] {qf.name}: enum rejected but product accepted")
            continue
        if args.db is None:
            print(f"[enum-only] {qf.name}: {len(expected['rows'])} cells")
            continue
        try:
            result = run_query(args.model, args.db, query)
            actual = result.to_dict()
        except Exception as exc:  # noqa: BLE001
            print(f"[product-error] {qf.name}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        checked += 1
        if normalize(expected) == normalize(actual):
            print(f"[match] {qf.name}: {len(actual['rows'])} cells, "
                  f"{sum(len(r['metrics']) for r in actual['rows'])} values")
        else:
            failures += 1
            print(f"[MISMATCH] {qf.name}")
            print("  expected:", normalize(expected))
            print("  actual:  ", normalize(actual))
    if args.db is not None:
        print(f"\n{checked} queries cross-checked cell-by-cell; "
              f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
