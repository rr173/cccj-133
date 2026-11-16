"""Compile declarative queries into per-fact SQL plans.

The compiler never executes anything. Given a :class:`~metriclayer.model.Model`
and a query document it produces a :class:`QueryPlan` describing, for every
base metric:

* the single fact table that feeds it (its native row multiplicity is kept —
  no fact-to-fact join ever happens),
* the LEFT JOIN chain of many-to-one references used for dimensions and
  entity filters (each join follows one declared role path),
* every filter's scope, including filters the metric explicitly ignores,
* the intermediate grain: fact rows aligned to the query dimensions.

Existence filters compile to *independent semi-join subqueries* (``f.<fk> IN
(SELECT ...)``). They never join the filter's fact table to another fact, so
filtering cannot create row multiplicity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .errors import CompileError
from .model import (
    Model,
    BaseMetric,
    DerivedMetric,
    Reference,
    Dimension,
)

# Collision-free marker for null dimension members. The bracketed reserved
# prefix cannot be produced by a model text value that round-trips through the
# engine: an int dimension renders as a bare canonical decimal, a text
# dimension carries the raw model string which by convention never starts with
# the reserved namespace, and bools are not dimension attributes here. The
# marker is also returned explicitly in query payloads.
MISSING = "__metriclayer__::missing_member__"


@dataclass
class FilterSpec:
    name: str
    kind: str  # "entity" | "existence"
    entity: str
    predicate: dict
    # existence only: fact table + role path from it to the entity
    fact: str | None = None
    roles: tuple[str, ...] = ()
    # entity only: optional explicit role list from query facts
    role_hint: tuple[str, ...] | None = None
    # existence only: explicit per-fact role chains for applying the selected
    # entity population to each metric fact. Required when a fact has more
    # than one path to the filter's entity; otherwise the unique path is used.
    local_paths: dict[str, tuple[str, ...]] | None = None


@dataclass
class BasePlan:
    metric: str
    kind: str
    fact: str
    measure: str | None
    row_predicate: dict | None
    dimension_paths: list[dict]            # role chains used for dimensions
    applied_filters: list[dict]            # filters actually constraining SQL
    ignored_filters: list[str]
    joins: list[dict]                      # ordered LEFT JOIN edges
    intermediate_grain: str
    sql: str
    params: list[Any]


@dataclass
class QueryPlan:
    query_name: str
    dimensions: list[str]
    dimension_details: list[dict]
    filters: list[dict]
    base_plans: list[BasePlan]
    derived_plans: list[dict]
    ignored: dict[str, list[str]]          # metric -> filter names ignored
    read_view: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


@dataclass
class Query:
    name: str
    metrics: list[str]
    dimensions: list[str]
    filters: list[FilterSpec]
    limit: int | None = None


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

def parse_query(doc: dict) -> Query:
    if not isinstance(doc, dict):
        raise CompileError("query root must be an object")
    name = doc.get("name", "query")
    if not isinstance(name, str):
        raise CompileError("query.name must be a string")
    metrics = doc.get("metrics")
    dims = doc.get("dimensions", [])
    if not isinstance(metrics, list) or not metrics or not all(
        isinstance(m, str) for m in metrics
    ):
        raise CompileError("query.metrics must be a non-empty list of names")
    if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
        raise CompileError("query.dimensions must be a list of dimension names")
    raw_filters = doc.get("filters", [])
    if not isinstance(raw_filters, list):
        raise CompileError("query.filters must be a list")
    filters = [_parse_filter(f) for f in raw_filters]
    names = [f.name for f in filters]
    if len(set(names)) != len(names):
        raise CompileError(f"duplicate filter names: {names}")
    limit = doc.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        raise CompileError("query.limit must be a positive integer")
    return Query(name=name, metrics=list(metrics), dimensions=list(dims),
                 filters=filters, limit=limit)


def load_query(path: str | Path) -> Query:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CompileError(f"query file not found: {path}")
    except json.JSONDecodeError as exc:
        raise CompileError(f"query file is not valid JSON: {exc}") from exc
    return parse_query(doc)


def _parse_filter(raw: Any) -> FilterSpec:
    if not isinstance(raw, dict):
        raise CompileError("each filter must be an object")
    fname = raw.get("name")
    ftype = raw.get("type")
    entity = raw.get("entity")
    predicate = raw.get("predicate")
    if not isinstance(fname, str) or not fname:
        raise CompileError("filter.name must be a non-empty string")
    if not isinstance(entity, str):
        raise CompileError(f"filter '{fname}': entity must be a string")
    if not isinstance(predicate, dict):
        raise CompileError(f"filter '{fname}': predicate must be an object")
    if ftype == "entity":
        hint = raw.get("roles")
        if hint is not None and (
            not isinstance(hint, list) or not all(isinstance(r, str) for r in hint)
        ):
            raise CompileError(f"filter '{fname}': roles must be a list of strings")
        return FilterSpec(
            name=fname, kind="entity", entity=entity, predicate=predicate,
            role_hint=tuple(hint) if hint is not None else None,
        )
    if ftype == "existence":
        f = raw.get("fact")
        roles = raw.get("roles")
        if not isinstance(f, str):
            raise CompileError(
                f"filter '{fname}': existence filter needs a fact table"
            )
        if not isinstance(roles, list) or not roles or not all(
            isinstance(r, str) for r in roles
        ):
            raise CompileError(
                f"filter '{fname}': existence filter needs a non-empty role "
                f"path from its fact to entity '{entity}'"
            )
        local_paths_raw = raw.get("local_paths", {})
        if not isinstance(local_paths_raw, dict):
            raise CompileError(
                f"filter '{fname}': local_paths must be an object of "
                f"fact -> role list"
            )
        local_paths: dict[str, tuple[str, ...]] = {}
        for fact_name, role_list in local_paths_raw.items():
            if not isinstance(role_list, list) or not role_list or not all(
                isinstance(r, str) for r in role_list
            ):
                raise CompileError(
                    f"filter '{fname}': local_paths['{fact_name}'] must be a "
                    f"non-empty list of role names"
                )
            local_paths[fact_name] = tuple(role_list)
        return FilterSpec(
            name=fname, kind="existence", entity=entity, predicate=predicate,
            fact=f, roles=tuple(roles), local_paths=local_paths,
        )
    raise CompileError(
        f"filter '{fname}': type must be 'entity' or 'existence'"
    )


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def compile_query(model: Model, query: Query) -> QueryPlan:
    # Resolve & validate metrics first.
    selected: list[BaseMetric | DerivedMetric] = []
    for mname in query.metrics:
        selected.append(model.metric(mname))

    dims: list[Dimension] = []
    for dname in query.dimensions:
        dims.append(model.dimension(dname))

    filters = list(query.filters)
    _validate_filters_against_model(model, filters)

    needed_bases = _collect_base_metrics(model, selected)
    base_plans: list[BasePlan] = []
    ignored_map: dict[str, list[str]] = {}
    for base in needed_bases:
        base_plans.append(_compile_base(model, base, dims, filters))
        if base.ignore_filters:
            ignored_map[base.name] = list(base.ignore_filters)
    for m in selected:
        if isinstance(m, DerivedMetric) and m.ignore_filters:
            ignored_map.setdefault(m.name, list(m.ignore_filters))

    # A derived metric inherits the union of ignores of its base leaves for
    # documentation; its numbers are computed on the aligned base values.
    derived_plans = []
    for m in selected:
        if isinstance(m, DerivedMetric):
            leaf_ignores = sorted({
                ig for b in m.base_names
                for ig in model.metric(b).ignore_filters
            })
            derived_plans.append({
                "metric": m.name,
                "expression": m.expression,
                "base_metrics": list(m.base_names),
                "declared_ignores": list(m.ignore_filters),
                "effective_ignores": leaf_ignores,
            })
            ignored_map.setdefault(m.name, leaf_ignores)

    filter_docs = [
        {
            "name": f.name,
            "kind": f.kind,
            "entity": f.entity,
            "fact": f.fact,
            "roles": list(f.roles),
            "local_paths": (
                {k: list(v) for k, v in f.local_paths.items()}
                if f.local_paths else {}
            ),
            "predicate": f.predicate,
        }
        for f in filters
    ]
    dim_docs = [
        {
            "name": d.name,
            "entity": d.entity,
            "attribute": d.attribute,
            "paths": {k: list(v) for k, v in d.paths.items()},
        }
        for d in dims
    ]
    return QueryPlan(
        query_name=query.name,
        dimensions=[d.name for d in dims],
        dimension_details=dim_docs,
        filters=filter_docs,
        base_plans=base_plans,
        derived_plans=derived_plans,
        ignored=ignored_map,
        read_view="single SQLite read transaction; one read-only connection "
                  "per query, all per-fact SQL run against the same snapshot",
        notes=[
            "each base metric reads exactly one fact table at its native row "
            "grain; facts are never joined to facts",
            "existence filters compile to independent NOT/IN semi-joins; two "
            "existence filters are evaluated independently and may be satisfied "
            "by different fact rows",
        ],
    )


def _collect_base_metrics(
    model: Model, selected: list[BaseMetric | DerivedMetric]
) -> list[BaseMetric]:
    names: list[str] = []
    for m in selected:
        for b in m.base_names:
            if b not in names:
                names.append(b)
    return [model.metric(n) for n in names]  # type: ignore[return-value]


def _validate_filters_against_model(model: Model, filters: list[FilterSpec]) -> None:
    for f in filters:
        if f.entity not in model.tables:
            raise CompileError(
                f"filter '{f.name}': unknown entity '{f.entity}'"
            )
        ent = model.table(f.entity)
        if ent.kind != "entity":
            raise CompileError(
                f"filter '{f.name}': '{f.entity}' is not an entity table"
            )
        # Predicate columns must come from the whitelist of the table they
        # constrain.
        if f.kind == "existence":
            fact = model.table(f.fact)  # type: ignore[arg-type]
            if fact.kind != "fact":
                raise CompileError(
                    f"filter '{f.name}': '{f.fact}' is not a fact table"
                )
            chain = model.resolve_path(f.fact, f.roles)  # type: ignore[arg-type]
            if chain[-1].target != f.entity:
                raise CompileError(
                    f"filter '{f.name}': path {'.'.join(f.roles)} from fact "
                    f"'{f.fact}' ends at '{chain[-1].target}', not '{f.entity}'"
                )
            from .model import _validate_predicate
            _validate_predicate(f.predicate, fact,
                                where=f"existence filter '{f.name}'")
        else:
            from .model import _validate_predicate
            _validate_predicate(f.predicate, ent,
                                where=f"entity filter '{f.name}'")
            if f.role_hint is not None:
                # Hint validity can only be checked per fact later; verify the
                # first edge exists on at least one table is not possible here,
                # so per-fact compilation does the full check.
                pass


def _dimension_roles(model: Model, fact_name: str, dim: Dimension) -> tuple[str, ...]:
    if fact_name in dim.paths:
        return dim.paths[fact_name]
    # No explicit path declared for this fact: auto-resolution is allowed only
    # when unambiguous.
    return model.find_unique_path(fact_name, dim.entity)


def _compile_base(
    model: Model,
    base: BaseMetric,
    dims: list[Dimension],
    filters: list[FilterSpec],
) -> BasePlan:
    fact = model.table(base.fact)

    # 1) dimension role chains for this fact --------------------------------
    dim_paths: list[dict] = []
    required_chains: list[tuple[Dimension, tuple[str, ...], list[Reference]]] = []
    for dim in dims:
        roles = _dimension_roles(model, base.fact, dim)
        chain = model.resolve_path(base.fact, roles)
        required_chains.append((dim, roles, chain))
        dim_paths.append({
            "dimension": dim.name,
            "roles": list(roles),
            "endpoint": dim.entity,
            "attribute": dim.attribute,
            "nullable_chain": [r.nullable for r in chain],
        })

    # 2) filter applicability ------------------------------------------------
    applied: list[dict] = []
    ignored: list[str] = []
    # existence filter -> (filter's own fact chain, this metric's fact chain
    # to the same entity). The two chains never meet in a join.
    existence_entries: list[dict] = []
    entity_chains: list[tuple[FilterSpec, list[Reference]]] = []
    for f in filters:
        if f.name in base.ignore_filters:
            ignored.append(f.name)
            continue
        if f.kind == "existence":
            filter_chain = model.resolve_path(f.fact, f.roles)  # type: ignore[arg-type]
            # The selected entity population constrains THIS metric through
            # its own declared path to the same entity. An explicit per-fact
            # role chain in local_paths wins; otherwise auto-resolution is
            # allowed only when the path is unique. Ambiguity or no path is a
            # compile error, never a guess.
            if f.local_paths and base.fact in f.local_paths:
                local_roles = f.local_paths[base.fact]
                local_chain = model.resolve_path(base.fact, local_roles)
                if local_chain[-1].target != f.entity:
                    raise CompileError(
                        f"existence filter '{f.name}': local_paths for fact "
                        f"'{base.fact}' ends at '{local_chain[-1].target}', "
                        f"not '{f.entity}'"
                    )
            else:
                local_roles = model.find_unique_path(base.fact, f.entity)
                local_chain = model.resolve_path(base.fact, local_roles)
            existence_entries.append({
                "filter": f,
                "filter_chain": filter_chain,
                "local_chain": local_chain,
                "local_roles": local_roles,
            })
            applied.append({
                "name": f.name,
                "scope": (
                    f"entity population selected by existence on fact "
                    f"'{f.fact}' via roles {'.'.join(f.roles)}; applied to "
                    f"this metric through its own roles "
                    f"{'.'.join(local_roles)} (independent semi-join, no "
                    f"fact-to-fact join)"
                ),
                "predicate": f.predicate,
            })
        else:
            if f.role_hint is not None:
                chain = model.resolve_path(base.fact, f.role_hint)
                if chain[-1].target != f.entity:
                    raise CompileError(
                        f"entity filter '{f.name}': explicit roles "
                        f"{'.'.join(f.role_hint)} from fact '{base.fact}' end "
                        f"at '{chain[-1].target}', not '{f.entity}'"
                    )
            else:
                roles = model.find_unique_path(base.fact, f.entity)
                chain = model.resolve_path(base.fact, roles)
            entity_chains.append((f, chain))
            applied.append({
                "name": f.name,
                "scope": "entity attribute via roles " +
                         ".".join(r.role for r in chain),
                "predicate": f.predicate,
            })

    # 3) merge all role chains into one deduplicated LEFT JOIN tree ----------
    joins = _build_joins(model, base.fact, required_chains, entity_chains,
                         existence_entries)

    # 4) build SQL -----------------------------------------------------------
    sql, params = _build_sql(
        model, base, dims, required_chains, entity_chains, existence_entries
    )
    grain = (
        f"rows of fact '{base.fact}' aligned to dimensions "
        f"{[d.name for d in dims] or ['(scalar)']}; native fact multiplicity "
        f"preserved, LEFT JOINs are many-to-one"
    )
    return BasePlan(
        metric=base.name,
        kind=base.kind,
        fact=base.fact,
        measure=base.measure,
        row_predicate=base.row_predicate,
        dimension_paths=dim_paths,
        applied_filters=applied,
        ignored_filters=ignored,
        joins=joins,
        intermediate_grain=grain,
        sql=sql,
        params=params,
    )


def _build_joins(
    model: Model,
    fact_name: str,
    required_chains: list[tuple[Dimension, tuple[str, ...], list[Reference]]],
    entity_chains: list[tuple[FilterSpec, list[Reference]]],
    existence_entries: list[dict],
) -> list[dict]:
    """Deduplicate role prefixes: every distinct (alias, role) edge is joined
    at most once. The existence filter's own fact is NEVER joined here; only
    this metric's local chain to the selected entity is."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    all_chains = (
        [c for _, _, c in required_chains]
        + [c for _, c in entity_chains]
        + [e["local_chain"] for e in existence_entries]
    )
    for chain in all_chains:
        parent_alias = "f"
        parent_table = fact_name
        for ref in chain:
            key = (parent_alias, ref.role)
            if key not in seen:
                seen.add(key)
                child_alias = _alias_for(parent_alias, ref.role)
                out.append({
                    "role": ref.role,
                    "from_table": parent_table,
                    "from_alias": parent_alias,
                    "fk_column": ref.column,
                    "to_table": ref.target,
                    "to_alias": child_alias,
                    "nullable": ref.nullable,
                    "join": "LEFT JOIN",
                })
            parent_alias = _alias_for(parent_alias, ref.role)
            parent_table = ref.target
    return out


def _alias_for(parent_alias: str, role: str) -> str:
    # Deterministic, double-quoted-safe (identifiers come from the model).
    return f"{parent_alias}__{role}"


# ---------------------------------------------------------------------------
# SQL generation — identifiers only ever come from model whitelists
# ---------------------------------------------------------------------------

def _build_sql(
    model: Model,
    base: BaseMetric,
    dims: list[Dimension],
    required_chains: list[tuple[Dimension, tuple[str, ...], list[Reference]]],
    entity_chains: list[tuple[FilterSpec, list[Reference]]],
    existence_entries: list[dict],
) -> tuple[str, list[Any]]:
    fact = model.table(base.fact)
    params: list[Any] = []
    joins_sql: list[str] = []
    joined_edges: set[tuple[str, str]] = set()

    def emit_chain(chain: list[Reference]) -> None:
        parent_alias = "f"
        for ref in chain:
            key = (parent_alias, ref.role)
            if key in joined_edges:
                parent_alias = _alias_for(parent_alias, ref.role)
                continue
            joined_edges.add(key)
            child_alias = _alias_for(parent_alias, ref.role)
            joins_sql.append(
                f'LEFT JOIN "{ref.target}" {child_alias} ON '
                f'{child_alias}."{ref.target_column}" = '
                f'{parent_alias}."{ref.column}"'
            )
            parent_alias = child_alias

    for _, _, chain in required_chains:
        emit_chain(chain)
    for _, chain in entity_chains:
        emit_chain(chain)
    for entry in existence_entries:
        emit_chain(entry["local_chain"])

    where: list[str] = []
    # metric's own row predicate lives on the fact table
    if base.row_predicate is not None:
        sql_pred, _ = _predicate_sql(base.row_predicate, "f", fact, params)
        where.append(sql_pred)
    # entity attribute filters
    for fspec, chain in entity_chains:
        terminal_alias = _terminal_alias(chain)
        ent = model.table(fspec.entity)
        sql_pred, _ = _predicate_sql(
            fspec.predicate, terminal_alias, ent, params
        )
        where.append(sql_pred)
    # existence filters: each becomes an independent semi-join over the
    # selected ENTITY population. The filter's fact appears only inside the
    # subquery, never joined to this fact.
    for entry in existence_entries:
        sub, sub_params = _existence_subquery(model, entry)
        local_terminal = _terminal_alias(entry["local_chain"])
        ent_pk = model.table(entry["filter"].entity).primary_key
        where.append(f'{local_terminal}."{ent_pk}" IN {sub}')
        params.extend(sub_params)

    select_parts: list[str] = []
    # dimension expressions on terminal aliases; null -> MISSING marker
    for dim, _, chain in required_chains:
        alias = _terminal_alias(chain)
        col_type = model.table(dim.entity).columns[dim.attribute].type
        expr = f'{alias}."{dim.attribute}"'
        select_parts.append(
            f"COALESCE({_dim_cast(expr, col_type)}, '{MISSING}') "
            f'AS "dim__{dim.name}"'
        )
    # the raw values needed for exact aggregation in Python
    if base.kind == "count_rows":
        select_parts.append('1 AS "measure"')
    else:
        col = fact.columns[base.measure]  # type: ignore[arg-type]
        select_parts.append(f'"f"."{base.measure}" AS "measure"')
        select_parts.append(
            f'CASE WHEN "f"."{base.measure}" IS NULL THEN 1 ELSE 0 END '
            f'AS "measure_is_null"'
        )

    sql = f'SELECT {", ".join(select_parts)}\n  FROM "{base.fact}" f'
    if joins_sql:
        sql += "\n  " + "\n  ".join(joins_sql)
    if where:
        sql += "\n  WHERE " + "\n    AND ".join(where)
    return sql, params


def _dim_cast(expr: str, col_type: str) -> str:
    # Int columns are stored BLOB-as-canonical-text; render them as text so
    # COALESCE and grouping use a single representation.
    if col_type == "int":
        return f"CAST({expr} AS TEXT)"
    return expr


def _terminal_alias(chain: list[Reference]) -> str:
    alias = "f"
    for ref in chain:
        alias = _alias_for(alias, ref.role)
    return alias


def _existence_subquery(
    model: Model,
    entry: dict,
) -> tuple[str, list[Any]]:
    """Semi-join returning the primary keys of the selected entities.

    The filter's own fact table (``sf``) walks its declared role path to the
    target entity; the fact-level predicate is applied to ``sf``. The result
    is the set of entity PKs for which at least one fact row matches. Two
    existence filters produce two independent subqueries, so different fact
    rows may satisfy each. Requiring one row to satisfy both predicates is
    expressed with AND inside a single filter instead.
    """
    fspec: FilterSpec = entry["filter"]
    chain: list[Reference] = entry["filter_chain"]
    params: list[Any] = []
    fact = model.table(fspec.fact)  # type: ignore[arg-type]
    terminal_entity = model.table(fspec.entity)
    parent = "sf"
    joins: list[str] = []
    # Walk EVERY edge of the filter path, including the first hop out of the
    # filter's fact. Aliases reuse the same role-based scheme rooted at "sf".
    for ref in chain:
        child = _alias_for(parent, ref.role)
        joins.append(
            f'LEFT JOIN "{ref.target}" {child} ON '
            f'{child}."{ref.target_column}" = {parent}."{ref.column}"'
        )
        parent = child
    terminal_pk_col = f'{parent}."{terminal_entity.primary_key}"'
    pred_sql, _ = _predicate_sql(fspec.predicate, "sf", fact, params)
    lines = [f"SELECT DISTINCT {terminal_pk_col}",
             f'FROM "{fspec.fact}" "sf"']
    lines.extend(joins)
    # terminal PK not null: nullable references are missing members and can
    # never belong to the selected entity population
    lines.append("WHERE " + pred_sql)
    lines.append(f"  AND {terminal_pk_col} IS NOT NULL")
    return "(\n    " + "\n    ".join(lines) + "\n  )", params


# ---------------------------------------------------------------------------
# Predicate SQL — 3-valued logic via the int_cmp UDF for int columns
# ---------------------------------------------------------------------------

def _predicate_sql(
    pred: dict,
    alias: str,
    table: Table,  # noqa: F821 — forward reference is fine at runtime
    params: list[Any],
) -> tuple[str, list[Any]]:
    (op, body), = pred.items()
    if op in ("and", "or"):
        parts = [
            _predicate_sql(sub, alias, table, params)[0] for sub in body
        ]
        joiner = " AND " if op == "and" else " OR "
        return "(" + joiner.join(parts) + ")", params
    col = body["column"]
    colref = f'{alias}."{col}"'
    ctype = table.columns[col].type
    if op == "eq":
        value = body["value"]
        if ctype == "int":
            params.append(_int_param(value))
            return f"int_cmp({colref}, ?) = 0", params
        params.append(value)
        return f"{colref} = ?", params
    if op in ("lt", "lte", "gt", "gte"):
        want = {"lt": -1, "lte": 0, "gt": 1, "gte": 0}[op]
        params.append(_int_param(body["value"]))
        if op in ("lt", "gt"):
            return f"int_cmp({colref}, ?) = {want}", params
        # lte / gte: cmp in {-1,0} or {0,1}; unknown (null) stays excluded
        allowed = "(-1, 0)" if op == "lte" else "(0, 1)"
        return f"int_cmp({colref}, ?) IN {allowed}", params
    if op == "is_null":
        return f"{colref} IS {'NULL' if body['value'] else 'NOT NULL'}", params
    if op == "in":
        placeholders = ", ".join("?" for _ in body["values"])
        if ctype == "int":
            # int columns are BLOB-affinity canonical decimal text, so equality
            # IN matches the same canonical representation; ordering variants
            # of "in" are not part of the predicate language.
            params.extend(_int_param(v) for v in body["values"])
        else:
            params.extend(body["values"])
        return f"{colref} IN ({placeholders})", params
    raise CompileError(f"unsupported predicate operator '{op}'")


def _int_param(value: int) -> str:
    # int values are bound as canonical decimal strings; the column is BLOB
    # affinity so the stored value is the same canonical text.
    return str(int(value))
