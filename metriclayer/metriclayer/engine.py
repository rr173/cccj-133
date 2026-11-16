"""SQLite storage, strict data validation and exact query execution.

Storage conventions
-------------------
* ``int`` columns use BLOB affinity and hold canonical decimal TEXT
  (``str(int(value))``). BLOB affinity prevents SQLite from coercing the
  digits into lossy REAL, and comparisons/joins work on the canonical text
  for equality while ordering goes through the ``int_cmp`` UDF.
* ``bool`` columns use INTEGER affinity and hold only 0/1.
* ``text`` columns use TEXT affinity and hold strings.

Execution
---------
SQLite performs only the relational work (role-path joins, whitelist
predicates, existence semi-joins). Every aggregate is evaluated in Python on
the raw fact values, so sums stay arbitrary-precision integers and derived
metrics use :class:`fractions.Fraction` — no float is ever produced.
"""

from __future__ import annotations

import json
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import DataError
from .model import Model, Table, BaseMetric
from .compile import Query, QueryPlan, MISSING, compile_query

INT_AFFINITY = "BLOB"   # canonical decimal text, no numeric coercion
TEXT_AFFINITY = "TEXT"
BOOL_AFFINITY = "INTEGER"


# ---------------------------------------------------------------------------
# Data loading & validation
# ---------------------------------------------------------------------------

def build_database(model: Model, data_path: str | Path,
                   db_path: str | Path) -> dict:
    """Validate every row against the model and (re)create the SQLite file.

    Returns a small report dictionary. Raises :class:`DataError` on the first
    violation: unknown column, wrong/loose typing, null in a non-nullable
    column, duplicate primary key, duplicate reference target, or a dangling
    non-null foreign key. Nullable FKs are accepted and survive as missing
    members in joins.
    """
    data_path, db_path = Path(data_path), Path(db_path)
    try:
        doc = json.loads(data_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DataError(f"data file not found: {data_path}")
    except json.JSONDecodeError as exc:
        raise DataError(f"data file is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise DataError("data root must be an object of table -> rows")

    rows_by_table: dict[str, list[dict]] = {}
    for table in model.tables.values():
        raw_rows = doc.get(table.name, [])
        if not isinstance(raw_rows, list):
            raise DataError(f"table '{table.name}' data must be a list")
        rows_by_table[table.name] = [
            _validate_row(table, row, i) for i, row in enumerate(raw_rows)
        ]

    _validate_keys_and_references(model, rows_by_table)

    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_ddl(model))
        for table in model.tables.values():
            rows = rows_by_table[table.name]
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in table.columns)
            conn.executemany(
                f'INSERT INTO "{table.name}" VALUES ({placeholders})',
                [[_storage_value(table.columns[c], r[c]) for c in table.columns]
                 for r in rows],
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "database": str(db_path),
        "tables": {t: len(rows_by_table[t]) for t in model.tables},
        "violations": [],
    }


def _validate_row(table: Table, raw: Any, index: int) -> dict:
    where = f"{table.name}[{index}]"
    if not isinstance(raw, dict):
        raise DataError(f"{where}: row must be an object")
    unknown = set(raw) - set(table.columns)
    if unknown:
        raise DataError(
            f"{where}: columns {sorted(unknown)} are not in the model whitelist"
        )
    clean: dict[str, Any] = {}
    for cname, col in table.columns.items():
        if cname not in raw:
            raise DataError(f"{where}: missing column '{cname}'")
        clean[cname] = _validate_value(col, raw[cname], where)
    return clean


def _validate_value(col, value: Any, where: str) -> Any:
    label = f"{where}.{col.name}"
    if value is None:
        if not col.nullable:
            raise DataError(f"{label}: null not allowed on non-nullable column")
        return None
    if col.type == "int":
        if isinstance(value, bool):
            raise DataError(f"{label}: bool is not an int (types are distinct)")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            # canonical decimal text representation of an arbitrarily large int
            if not _is_canonical_int_text(value):
                raise DataError(
                    f"{label}: int column accepts only INTEGER or canonical "
                    f"decimal TEXT; {value!r} is not canonical"
                )
            return int(value)
        raise DataError(f"{label}: expected int, got {type(value).__name__}")
    if col.type == "bool":
        if not isinstance(value, bool):
            raise DataError(
                f"{label}: bool column accepts only true/false (0/1 integers "
                f"are rejected; bool and int are distinct)"
            )
        return value
    # text
    if not isinstance(value, str):
        raise DataError(f"{label}: expected text, got {type(value).__name__}")
    # The missing-member marker namespace is reserved; a model text value that
    # collides with it would be indistinguishable from a null dimension member
    # during grouping, so it is rejected at load time rather than producing a
    # silent collision.
    if value == MISSING or value.startswith(MISSING):
        raise DataError(
            f"{label}: text value {value!r} uses the reserved missing-member "
            f"namespace {MISSING!r}; choose a different value"
        )
    return value


def _is_canonical_int_text(value: str) -> bool:
    if not value:
        return False
    body = value[1:] if value[0] in "+-" else value
    if not body or not body.isdigit():
        return False
    if len(body) > 1 and body[0] == "0":
        return False  # no leading zeros
    return True


def _storage_value(col, value: Any) -> Any:
    if value is None:
        return None
    if col.type == "int":
        return str(int(value))      # canonical decimal text in BLOB column
    if col.type == "bool":
        return 1 if value else 0
    return value


def _validate_keys_and_references(
    model: Model, rows_by_table: dict[str, list[dict]]
) -> None:
    pk_sets: dict[str, set[Any]] = {}
    for tname, table in model.tables.items():
        seen: set[Any] = set()
        for i, row in enumerate(rows_by_table[tname]):
            key = row[table.primary_key]
            if key in seen:
                raise DataError(
                    f"{tname}[{i}]: duplicate primary key "
                    f"'{table.primary_key}'={key!r}"
                )
            seen.add(key)
        pk_sets[tname] = seen

    # Reference target uniqueness: every target must be a primary key (unique
    # by construction above), and non-null FK values must resolve.
    for tname, table in model.tables.items():
        for ref in table.references:
            targets = pk_sets[ref.target]
            for i, row in enumerate(rows_by_table[tname]):
                fk = row[ref.column]
                if fk is None:
                    if not ref.nullable:
                        raise DataError(
                            f"{tname}[{i}].{ref.column}: dangling null on a "
                            f"non-nullable reference"
                        )
                    continue  # nullable missing member is allowed
                if fk not in targets:
                    raise DataError(
                        f"{tname}[{i}].{ref.column}: dangling reference {fk!r} "
                        f"-> {ref.target}.{ref.target_column}"
                    )


def _ddl(model: Model) -> str:
    statements: list[str] = []
    for table in model.tables.values():
        col_sql = []
        for col in table.columns.values():
            affinity = {
                "int": INT_AFFINITY,
                "text": TEXT_AFFINITY,
                "bool": BOOL_AFFINITY,
            }[col.type]
            null_sql = "" if col.nullable else " NOT NULL"
            col_sql.append(f'  "{col.name}" {affinity}{null_sql}')
        col_sql.append(f'  PRIMARY KEY ("{table.primary_key}")')
        statements.append(
            f'CREATE TABLE "{table.name}" (\n' + ",\n".join(col_sql) + "\n)"
        )
    return ";\n".join(statements) + ";\n"


# ---------------------------------------------------------------------------
# Read connection (single snapshot per query)
# ---------------------------------------------------------------------------

def open_read_connection(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.create_function("int_cmp", 2, _int_cmp, deterministic=True)
    conn.execute("BEGIN")  # one read transaction = one database read view
    return conn


def _int_cmp(a: Any, b: Any) -> int | None:
    """Ordering over canonical decimal text / INTEGER mixed storage.

    Returns -1/0/1 like ``cmp``; NULL on either side yields NULL so the
    predicate evaluates to unknown and the row is filtered out.
    """
    if a is None or b is None:
        return None
    try:
        ai, bi = int(a), int(b)
    except (TypeError, ValueError):
        raise DataError(
            f"int_cmp received non-integer value {a!r} / {b!r}; an int column "
            f"contains non-canonical data"
        )
    if ai < bi:
        return -1
    if ai > bi:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Exact execution
# ---------------------------------------------------------------------------

def execute_plan(
    model: Model, plan: QueryPlan, conn: sqlite3.Connection
) -> "ExecutedQuery":
    # metric name -> cell key -> raw aggregate state
    bases: dict[str, dict[tuple, dict]] = {}
    for bp in plan.base_plans:
        metric = model.metric(bp.metric)
        assert isinstance(metric, BaseMetric)
        cells: dict[tuple, dict] = {}
        cur = conn.execute(bp.sql, bp.params)
        dim_cols = [f"dim__{d}" for d in plan.dimensions]
        for row in cur:
            key = tuple(row[c] for c in dim_cols)
            cell = cells.setdefault(key, {"rows": 0, "non_null": 0,
                                          "sum": 0, "sum_seen": False,
                                          "distinct": set()})
            cell["rows"] += 1
            value = row["measure"]
            if bp.kind == "count_rows":
                continue
            is_null = bool(row["measure_is_null"])
            if not is_null:
                cell["non_null"] += 1
                measure_type = (
                    model.table(bp.fact).columns[metric.measure].type
                    if metric.measure else None
                )
                if bp.kind == "sum":
                    cell["sum"] += int(value)
                    cell["sum_seen"] = True
                elif bp.kind == "count_distinct":
                    # exact distinct values: int columns compare on their
                    # integer value, text on the raw string, bool on 0/1
                    cell["distinct"].add(
                        int(value) if measure_type == "int" else value
                    )
                # count only needs non_null
        bases[bp.metric] = cells

    # union of cells: only cells with at least one contributing fact
    all_keys = sorted({k for cells in bases.values() for k in cells},
                      key=_cell_sort_key)

    rows_out: list[dict] = []
    selected = [model.metric(n) for n in
                _selected_metric_names(plan, model)]
    for key in all_keys:
        out_row: dict[str, Any] = {}
        base_values: dict[str, Fraction | int | None] = {}
        for name, cells in bases.items():
            base_values[name] = _finalize_base(
                model.metric(name), cells.get(key)  # type: ignore[arg-type]
            )
        for metric in selected:
            if isinstance(metric, BaseMetric):
                out_row[metric.name] = _encode(base_values[metric.name])
            else:
                out_row[metric.name] = _encode(
                    _eval_derived(metric.expression, base_values)
                )
        rows_out.append({
            "dimensions": [
                {"__missing__": True} if v == MISSING else v for v in key
            ],
            "metrics": out_row,
        })

    return ExecutedQuery(plan=plan, rows=rows_out,
                         dimension_names=list(plan.dimensions))


def _selected_metric_names(plan: QueryPlan, model: Model) -> list[str]:
    # plan stores base + derived docs; reconstruct selection order from the
    # original query kept on the plan's query name is not enough, so callers
    # pass the query through execute() below. Kept for internal use.
    names = [bp.metric for bp in plan.base_plans]
    names.extend(d["metric"] for d in plan.derived_plans)
    return names


def _finalize_base(metric: BaseMetric, cell: dict | None) -> int | Fraction | None:
    if cell is None:
        # no fact of this metric in the cell: count -> 0, sum -> null
        if metric.kind in ("count_rows", "count", "count_distinct"):
            return 0
        return None
    if metric.kind == "count_rows":
        return cell["rows"]
    if metric.kind == "count":
        return cell["non_null"]
    if metric.kind == "count_distinct":
        return len(cell["distinct"])
    # sum: null when no non-null value contributed
    return cell["sum"] if cell["sum_seen"] else None


def _eval_derived(expr: dict,
                  values: dict[str, Fraction | int | None]) -> Fraction | None:
    (op, body), = expr.items()

    def operand(x: Any) -> Fraction | None:
        if isinstance(x, str):
            v = values[x]
            return None if v is None else Fraction(v)
        return _eval_derived(x, values)

    left, right = operand(body[0]), operand(body[1])
    if left is None or right is None:
        return None
    if op == "add":
        return left + right
    if op == "sub":
        return left - right
    if op == "mul":
        return left * right
    if op == "div":
        if right == 0:
            return None
        return left / right
    raise ValueError(f"unknown derived op {op}")  # validated earlier


def _encode(value: int | Fraction | None) -> Any:
    if value is None:
        return None
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return {"type": "fraction", "numerator": str(value.numerator),
                    "denominator": "1"}
        return {"type": "fraction", "numerator": str(value.numerator),
                "denominator": str(value.denominator)}
    # exact integer, including > 64-bit: decimal string output
    return {"type": "int", "value": str(int(value))}


def _cell_sort_key(key: tuple) -> list:
    return [("1", v) if v == MISSING else ("0", v) for v in key]


class ExecutedQuery:
    def __init__(self, plan: QueryPlan, rows: list[dict],
                 dimension_names: list[str]):
        self.plan = plan
        self.rows = rows
        self.dimension_names = dimension_names

    def to_dict(self) -> dict:
        return {
            "query": self.plan.query_name,
            "dimensions": self.dimension_names,
            "missing_marker": MISSING,
            "rows": self.rows,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def execute(model: Model, db_path: str | Path, query: Query) -> ExecutedQuery:
    """Compile and run a query against one read-only snapshot connection."""
    plan = compile_query(model, query)
    conn = open_read_connection(db_path)
    try:
        result = execute_plan(model, plan, conn)
    finally:
        conn.execute("COMMIT")
        conn.close()
    # preserve the user's metric selection order
    order = list(query.metrics)
    result.rows = [
        {"dimensions": r["dimensions"],
         "metrics": {m: r["metrics"][m] for m in order}}
        for r in result.rows
    ]
    if query.limit is not None:
        result.rows = result.rows[: query.limit]
    return result
