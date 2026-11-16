"""Stable public API for the metric layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import Model, load_model
from .compile import Query, parse_query, load_query, compile_query, QueryPlan
from .engine import build_database, execute, ExecutedQuery


class QueryResult:
    """Convenience wrapper around the executed query with typed accessors."""

    def __init__(self, executed: ExecutedQuery, query: Query):
        self._executed = executed
        self._query = query

    @property
    def plan(self) -> QueryPlan:
        return self._executed.plan

    @property
    def dimension_names(self) -> list[str]:
        return self._executed.dimension_names

    @property
    def rows(self) -> list[dict]:
        """Each row: ``{"dimensions": [...], "metrics": {name: value}}``."""
        return self._executed.rows

    def metric_map(self) -> dict[tuple, dict]:
        """Dimension tuple -> metric values, for programmatic assertions.

        A missing dimension member is represented by the hashable sentinel
        :data:`MISSING` rather than its output dict form.
        """
        from .compile import MISSING

        def key_of(dim_values):
            return tuple(
                MISSING if isinstance(v, dict) and v.get("__missing__") else v
                for v in dim_values
            )

        return {key_of(r["dimensions"]): r["metrics"] for r in self.rows}

    def to_dict(self) -> dict:
        return self._executed.to_dict()

    def to_json(self, indent: int = 2) -> str:
        return self._executed.to_json(indent)


def explain(model: Model, query: Query) -> QueryPlan:
    return compile_query(model, query)


def explain_query(
    model: Model | str | Path, query: dict | str | Path
) -> QueryPlan:
    """Compile only; no database access. Accepts paths or in-memory objects."""
    m = load_model(model) if isinstance(model, (str, Path)) else model
    if isinstance(query, (str, Path)):
        q = load_query(query)
    else:
        q = parse_query(query)
    return compile_query(m, q)


def run_query(
    model: Model | str | Path,
    db_path: str | Path,
    query: dict | str | Path,
) -> QueryResult:
    """Compile and execute a query, returning exact typed values."""
    m = load_model(model) if isinstance(model, (str, Path)) else model
    if isinstance(query, (str, Path)):
        q = load_query(query)
    else:
        q = parse_query(query)
    executed = execute(m, db_path, q)
    return QueryResult(executed, q)


__all__ = [
    "load_model",
    "build_database",
    "run_query",
    "explain_query",
    "explain",
    "parse_query",
    "Query",
    "QueryPlan",
    "QueryResult",
]
