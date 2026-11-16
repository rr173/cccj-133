"""Model declarations: entities, facts, references, metrics and dimensions.

A model is a JSON document with three sections::

    {
      "tables":   [ ... ],   # entities and facts, each with typed columns,
                             # a non-null unique primary key and many-to-one refs
      "metrics":  [ ... ],   # base metrics (one fact table) and derived metrics
      "dimensions": [ ... ]  # query dimensions with explicit per-fact role paths
}

The model is parsed into dataclasses and validated eagerly: column types,
primary keys, many-to-one references, the acyclic reference graph, whitelist
usage in predicates/dimensions, and every declared relation path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ModelError, CompileError

VALID_TYPES = ("int", "text", "bool")
BASE_KINDS = ("count_rows", "count", "sum", "count_distinct")
_DERIVED_OPS = ("add", "sub", "mul", "div")


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # "int" | "text" | "bool"
    nullable: bool


@dataclass(frozen=True)
class Reference:
    """A many-to-one reference from one table to another table's primary key.

    ``role`` disambiguates two references that reach the same target. A
    reference without an explicit role gets the implicit role ``"<col>"``.
    """

    column: str          # local FK column
    target: str          # target table name
    target_column: str   # target primary key column
    role: str
    nullable: bool       # copied from the FK column declaration


@dataclass
class Table:
    name: str
    kind: str            # "entity" | "fact"
    columns: dict[str, Column]
    primary_key: str
    references: list[Reference] = field(default_factory=list)

    @property
    def ref_by_role(self) -> dict[str, Reference]:
        return {r.role: r for r in self.references}

    def outgoing_role(self, role: str) -> Reference:
        refs = self.ref_by_role
        if role not in refs:
            raise CompileError(
                f"table '{self.name}' has no reference role '{role}'; "
                f"declared roles: {sorted(refs)}"
            )
        return refs[role]


@dataclass
class BaseMetric:
    name: str
    kind: str                     # count_rows | count | sum | count_distinct
    fact: str
    measure: str | None           # column for count/sum/count_distinct
    row_predicate: dict | None   # optional AND/OR/eq/lt/is_null/in predicate
    ignore_filters: tuple[str, ...]

    @property
    def base_names(self) -> tuple[str, ...]:
        return (self.name,)


@dataclass
class DerivedMetric:
    name: str
    expression: dict              # {op: [operand, operand]}; operands are
                                  # base metric names or nested expressions
    ignore_filters: tuple[str, ...]

    @property
    def base_names(self) -> tuple[str, ...]:
        return tuple(_walk_leaves(self.expression))


@dataclass
class Dimension:
    name: str
    entity: str
    attribute: str
    # fact table name -> list of role names from that fact to ``entity``
    paths: dict[str, tuple[str, ...]]


@dataclass
class Model:
    tables: dict[str, Table]
    metrics: dict[str, BaseMetric | DerivedMetric]
    dimensions: dict[str, Dimension]
    source_path: str | None = None

    # ---- lookups -------------------------------------------------------
    def table(self, name: str) -> Table:
        if name not in self.tables:
            raise CompileError(f"unknown table '{name}'")
        return self.tables[name]

    def metric(self, name: str) -> BaseMetric | DerivedMetric:
        if name not in self.metrics:
            raise CompileError(f"unknown metric '{name}'")
        return self.metrics[name]

    def dimension(self, name: str) -> Dimension:
        if name not in self.dimensions:
            raise CompileError(f"unknown dimension '{name}'")
        return self.dimensions[name]

    def facts(self) -> list[Table]:
        return [t for t in self.tables.values() if t.kind == "fact"]

    # ---- path machinery ------------------------------------------------
    def resolve_path(self, fact_name: str, roles: tuple[str, ...]) -> list[Reference]:
        """Turn an explicit role list into concrete references and verify the
        chain is a valid walk over the reference graph."""
        current = self.table(fact_name)
        chain: list[Reference] = []
        for role in roles:
            ref = current.outgoing_role(role)
            chain.append(ref)
            current = self.table(ref.target)
        return chain

    def path_endpoint(self, fact_name: str, roles: tuple[str, ...]) -> str:
        chain = self.resolve_path(fact_name, roles)
        return chain[-1].target if chain else fact_name

    def find_unique_path(self, fact_name: str, target_entity: str) -> tuple[str, ...]:
        """Find the unique role path from a fact to an entity.

        Raises when there is no path or when more than one path exists —
        ambiguous paths must be named explicitly, never guessed.
        """
        found: list[tuple[str, ...]] = []
        self._collect_paths(fact_name, (), set(), target_entity, found)
        if not found:
            raise CompileError(
                f"no declared relation path from fact '{fact_name}' to "
                f"entity '{target_entity}'"
            )
        if len(found) > 1:
            raise CompileError(
                f"ambiguous path from fact '{fact_name}' to entity "
                f"'{target_entity}': {['.'.join(p) for p in found]}; "
                f"name the roles explicitly"
            )
        return found[0]

    def _collect_paths(
        self,
        table_name: str,
        prefix: tuple[str, ...],
        visiting: set[str],
        target: str,
        out: list[tuple[str, ...]],
    ) -> None:
        if table_name in visiting:  # defensive; graph is validated acyclic
            return
        if table_name == target and prefix:
            out.append(prefix)
            return
        visiting = visiting | {table_name}
        for ref in self.table(table_name).references:
            self._collect_paths(
                ref.target, prefix + (ref.role,), visiting, target, out
            )


# ---------------------------------------------------------------------------
# Parsing & validation
# ---------------------------------------------------------------------------

def load_model(path: str | Path) -> Model:
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModelError(f"model file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ModelError(f"model file is not valid JSON: {exc}") from exc
    return parse_model(doc, source_path=str(path))


def parse_model(doc: dict, source_path: str | None = None) -> Model:
    if not isinstance(doc, dict):
        raise ModelError("model root must be a JSON object")
    tables = _parse_tables(doc.get("tables", []))
    _validate_reference_graph(tables)
    model = Model(tables={}, metrics={}, dimensions={}, source_path=source_path)
    model.tables = tables
    metrics = _parse_metrics(doc.get("metrics", []), model)
    model.metrics = metrics
    model.dimensions = _parse_dimensions(doc.get("dimensions", []), model)
    return model


def _parse_tables(raw: Any) -> dict[str, Table]:
    if not isinstance(raw, list) or not raw:
        raise ModelError("model.tables must be a non-empty list")
    tables: dict[str, Table] = {}
    for item in raw:
        t = _parse_table(item)
        if t.name in tables:
            raise ModelError(f"duplicate table name '{t.name}'")
        tables[t.name] = t
    # cross-table reference checks now that every table is known
    for t in tables.values():
        for ref in t.references:
            if ref.target not in tables:
                raise ModelError(
                    f"table '{t.name}' reference '{ref.column}' targets "
                    f"unknown table '{ref.target}'"
                )
            target = tables[ref.target]
            if target.primary_key != ref.target_column:
                raise ModelError(
                    f"reference '{t.name}.{ref.column}' must target the primary "
                    f"key of '{ref.target}' ('{target.primary_key}'), not "
                    f"'{ref.target_column}'"
                )
            local = t.columns[ref.column]
            pk = target.columns[target.primary_key]
            if local.type != pk.type:
                raise ModelError(
                    f"reference '{t.name}.{ref.column}' type {local.type} does "
                    f"not match primary key type of '{ref.target}.{pk.name}' "
                    f"({pk.type})"
                )
            # facts may reference entities or other facts? Specification says
            # references are entity-to-entity and fact-to-entity.
            if t.kind == "fact" and target.kind == "fact":
                raise ModelError(
                    f"reference '{t.name}.{ref.column}' points at fact table "
                    f"'{ref.target}'; facts reference entities only"
                )
    return tables


def _parse_table(item: Any) -> Table:
    if not isinstance(item, dict):
        raise ModelError("each table must be an object")
    name = item.get("name")
    kind = item.get("kind")
    if not isinstance(name, str) or not name:
        raise ModelError("table.name must be a non-empty string")
    if kind not in ("entity", "fact"):
        raise ModelError(f"table '{name}'.kind must be 'entity' or 'fact'")
    raw_cols = item.get("columns")
    if not isinstance(raw_cols, list) or not raw_cols:
        raise ModelError(f"table '{name}' must declare at least one column")

    columns: dict[str, Column] = {}
    for c in raw_cols:
        if not isinstance(c, dict):
            raise ModelError(f"table '{name}': columns must be objects")
        cname, ctype = c.get("name"), c.get("type")
        if not isinstance(cname, str) or not cname:
            raise ModelError(f"table '{name}': column name must be a string")
        if ctype not in VALID_TYPES:
            raise ModelError(
                f"column '{name}.{cname}' type must be one of {VALID_TYPES}"
            )
        if cname in columns:
            raise ModelError(f"duplicate column '{name}.{cname}'")
        nullable = bool(c.get("nullable", False))
        columns[cname] = Column(cname, ctype, nullable)

    pk = item.get("primary_key")
    if not isinstance(pk, str) or pk not in columns:
        raise ModelError(
            f"table '{name}' primary_key must name a declared column"
        )
    if columns[pk].nullable:
        raise ModelError(f"primary key '{name}.{pk}' must be non-null")
    if columns[pk].type not in ("int", "text"):
        raise ModelError(f"primary key '{name}.{pk}' must be int or text")

    table = Table(name=name, kind=kind, columns=columns, primary_key=pk)
    seen_roles: set[str] = set()
    for r in item.get("references", []) or []:
        ref = _parse_reference(name, r, columns)
        if ref.role in seen_roles:
            raise ModelError(
                f"table '{name}': duplicate reference role '{ref.role}'"
            )
        seen_roles.add(ref.role)
        table.references.append(ref)
    return table


def _parse_reference(
    table_name: str, raw: Any, columns: dict[str, Column]
) -> Reference:
    if not isinstance(raw, dict):
        raise ModelError(f"table '{table_name}': references must be objects")
    col = raw.get("column")
    target = raw.get("target")
    target_col = raw.get("target_column")
    if not isinstance(col, str) or col not in columns:
        raise ModelError(
            f"reference in '{table_name}' must name a declared local column"
        )
    if not isinstance(target, str) or not isinstance(target_col, str):
        raise ModelError(
            f"reference '{table_name}.{col}' needs string target/target_column"
        )
    role = raw.get("role")
    if role is not None and not isinstance(role, str):
        raise ModelError(f"reference '{table_name}.{col}' role must be a string")
    role = role or col
    if not role:
        raise ModelError(f"reference '{table_name}.{col}' has an empty role")
    return Reference(
        column=col,
        target=target,
        target_column=target_col,
        role=role,
        nullable=columns[col].nullable,
    )


def _validate_reference_graph(tables: dict[str, Table]) -> None:
    """Entity references form a DAG. Depth-first search with coloring."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in tables}

    def visit(name: str, stack: list[str]) -> None:
        color[name] = GRAY
        for ref in tables[name].references:
            nxt = ref.target
            if nxt not in tables:
                continue  # reported elsewhere
            if color[nxt] == GRAY:
                cycle = " -> ".join(stack + [nxt])
                raise ModelError(f"reference graph contains a cycle: {cycle}")
            if color[nxt] == WHITE:
                visit(nxt, stack + [nxt])
        color[name] = BLACK

    for n in tables:
        if color[n] == WHITE:
            visit(n, [n])


def _parse_metrics(raw: Any, model: Model) -> dict[str, BaseMetric | DerivedMetric]:
    if not isinstance(raw, list) or not raw:
        raise ModelError("model.metrics must be a non-empty list")
    out: dict[str, BaseMetric | DerivedMetric] = {}
    model.metrics = out  # operand references resolve against earlier entries
    for item in raw:
        if not isinstance(item, dict):
            raise ModelError("each metric must be an object")
        mname = item.get("name")
        if not isinstance(mname, str) or not mname:
            raise ModelError("metric.name must be a non-empty string")
        if mname in out:
            raise ModelError(f"duplicate metric name '{mname}'")
        kind = item.get("kind")
        ignores = tuple(item.get("ignore_filters", []) or [])
        if not all(is_instance_str(x) for x in ignores):
            raise ModelError(f"metric '{mname}': ignore_filters must be strings")
        if kind in BASE_KINDS:
            out[mname] = _parse_base_metric(mname, kind, item, ignores, model)
        elif kind == "derived":
            out[mname] = DerivedMetric(
                name=mname,
                expression=_parse_expression(item.get("expression"), model, mname),
                ignore_filters=ignores,
            )
        else:
            raise ModelError(
                f"metric '{mname}' kind must be one of "
                f"{BASE_KINDS + ('derived',)}"
            )
    return out


def is_instance_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x)


def _parse_base_metric(
    name: str,
    kind: str,
    item: dict,
    ignores: tuple[str, ...],
    model: Model,
) -> BaseMetric:
    fact_name = item.get("fact")
    if not isinstance(fact_name, str) or fact_name not in model.tables:
        raise ModelError(f"metric '{name}' must reference a declared fact table")
    fact = model.table(fact_name)
    if fact.kind != "fact":
        raise ModelError(f"metric '{name}': '{fact_name}' is not a fact table")
    measure = item.get("measure")
    if kind == "count_rows":
        if measure is not None:
            raise ModelError(f"metric '{name}': count_rows takes no measure")
    else:
        if not isinstance(measure, str) or measure not in fact.columns:
            raise ModelError(
                f"metric '{name}': measure must name a column of '{fact_name}'"
            )
        if kind == "sum" and fact.columns[measure].type != "int":
            raise ModelError(
                f"metric '{name}': exact sum requires an int column; "
                f"'{fact_name}.{measure}' is {fact.columns[measure].type}"
            )
    predicate = item.get("row_predicate")
    if predicate is not None:
        _validate_predicate(predicate, fact, where=f"metric '{name}'.row_predicate")
    return BaseMetric(
        name=name,
        kind=kind,
        fact=fact_name,
        measure=measure,
        row_predicate=predicate,
        ignore_filters=ignores,
    )


def _validate_predicate(pred: Any, table: Table, where: str) -> None:
    """Recursively validate a predicate against one table's column whitelist."""
    if not isinstance(pred, dict) or len(pred) != 1:
        raise CompileError(f"{where}: predicate must be a single-key object")
    (op, body), = pred.items()
    if op in ("and", "or"):
        if not isinstance(body, list) or not body:
            raise CompileError(f"{where}: '{op}' needs a non-empty list")
        for sub in body:
            _validate_predicate(sub, table, where)
        return
    if not isinstance(body, dict):
        raise CompileError(f"{where}: '{op}' body must be an object")
    col = body.get("column")
    if not isinstance(col, str) or col not in table.columns:
        raise CompileError(
            f"{where}: column '{col}' is not in the whitelist of table "
            f"'{table.name}' ({sorted(table.columns)})"
        )
    ctype = table.columns[col].type
    if op == "eq":
        _check_literal(body.get("value"), ctype, where, col)
        if ctype == "bool" and not isinstance(body.get("value"), bool):
            raise CompileError(
                f"{where}: bool column '{col}' cannot be compared to an integer; "
                f"bool and int are distinct types"
            )
    elif op in ("lt", "lte", "gt", "gte"):
        if ctype != "int":
            raise CompileError(
                f"{where}: ordering predicate '{op}' is only valid on int "
                f"columns; '{col}' is {ctype}"
            )
        _check_literal(body.get("value"), ctype, where, col)
    elif op == "is_null":
        if not isinstance(body.get("value"), bool):
            raise CompileError(f"{where}: is_null needs boolean 'value'")
    elif op == "in":
        vals = body.get("values")
        if not isinstance(vals, list) or not vals:
            raise CompileError(f"{where}: 'in' needs a non-empty values list")
        for v in vals:
            _check_literal(v, ctype, where, col)
        if ctype == "bool" and any(not isinstance(v, bool) for v in vals):
            raise CompileError(
                f"{where}: bool column '{col}' cannot mix integer values"
            )
    else:
        raise CompileError(f"{where}: unsupported predicate operator '{op}'")


def _check_literal(value: Any, ctype: str, where: str, col: str) -> None:
    # Model-level check mirrors the runtime type rules; JSON has no big-int
    # issue because Python ints are arbitrary precision.
    if value is None:
        raise CompileError(
            f"{where}: use {{\"is_null\": ...}} for null, not a null literal"
        )
    if ctype == "int" and not isinstance(value, int):
        raise CompileError(f"{where}: value for int column '{col}' must be int")
    if ctype == "text" and not isinstance(value, str):
        raise CompileError(f"{where}: value for text column '{col}' must be text")
    if ctype == "bool" and not isinstance(value, bool):
        raise CompileError(
            f"{where}: value for bool column '{col}' must be true/false"
        )
    if isinstance(value, bool) and ctype != "bool":
        raise CompileError(f"{where}: bool literal cannot target {ctype} column")


def _parse_expression(expr: Any, model: Model, metric_name: str) -> dict:
    if not isinstance(expr, dict) or len(expr) != 1:
        raise ModelError(
            f"metric '{metric_name}': expression must be a single-key object"
        )
    (op, body), = expr.items()
    if op in _DERIVED_OPS:
        if not isinstance(body, list) or len(body) != 2:
            raise ModelError(
                f"metric '{metric_name}': '{op}' needs exactly two operands"
            )
        return {op: [_parse_operand(x, model, metric_name) for x in body]}
    raise ModelError(
        f"metric '{metric_name}': expression op must be one of {_DERIVED_OPS}"
    )


def _parse_operand(x: Any, model: Model, metric_name: str) -> Any:
    if isinstance(x, str):
        if x not in model.metrics:
            raise ModelError(
                f"metric '{metric_name}': unknown operand metric '{x}'"
            )
        if not isinstance(model.metrics[x], BaseMetric):
            raise ModelError(
                f"metric '{metric_name}': derived metrics may combine base "
                f"metrics only; '{x}' is itself derived"
            )
        return x
    if isinstance(x, dict):
        return _parse_expression(x, model, metric_name)
    raise ModelError(
        f"metric '{metric_name}': expression operands are metric names or "
        f"nested expressions"
    )


def _walk_leaves(expr: dict) -> list[str]:
    (op, body), = expr.items()
    leaves: list[str] = []
    for operand in body:
        if isinstance(operand, str):
            leaves.append(operand)
        else:
            leaves.extend(_walk_leaves(operand))
    return leaves


def _parse_dimensions(raw: Any, model: Model) -> dict[str, Dimension]:
    if not isinstance(raw, list):
        raise ModelError("model.dimensions must be a list")
    out: dict[str, Dimension] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ModelError("each dimension must be an object")
        dname = item.get("name")
        entity = item.get("entity")
        attr = item.get("attribute")
        if not isinstance(dname, str) or not dname:
            raise ModelError("dimension.name must be a non-empty string")
        if dname in out:
            raise ModelError(f"duplicate dimension name '{dname}'")
        if not isinstance(entity, str) or entity not in model.tables:
            raise ModelError(
                f"dimension '{dname}': unknown entity '{entity}'"
            )
        ent = model.table(entity)
        if ent.kind != "entity":
            raise ModelError(
                f"dimension '{dname}': '{entity}' is not an entity table"
            )
        if not isinstance(attr, str) or attr not in ent.columns:
            raise ModelError(
                f"dimension '{dname}': attribute must be a column of '{entity}'"
            )
        raw_paths = item.get("paths", {})
        if not isinstance(raw_paths, dict):
            raise ModelError(f"dimension '{dname}': paths must be an object")
        paths: dict[str, tuple[str, ...]] = {}
        for fact_name, role_list in raw_paths.items():
            if fact_name not in model.tables:
                raise ModelError(
                    f"dimension '{dname}': path from unknown fact '{fact_name}'"
                )
            if model.table(fact_name).kind != "fact":
                raise ModelError(
                    f"dimension '{dname}': path start '{fact_name}' is not a fact"
                )
            if not isinstance(role_list, list) or not all(
                isinstance(r, str) for r in role_list
            ):
                raise ModelError(
                    f"dimension '{dname}': path for '{fact_name}' must be a "
                    f"list of role names"
                )
            roles = tuple(role_list)
            chain = model.resolve_path(fact_name, roles)
            if chain[-1].target != entity:
                raise ModelError(
                    f"dimension '{dname}': path from '{fact_name}' ends at "
                    f"'{chain[-1].target}', not '{entity}'"
                )
            paths[fact_name] = roles
        out[dname] = Dimension(
            name=dname, entity=entity, attribute=attr, paths=paths
        )
    return out
