"""metriclayer — a typed, path-explicit semantic metric layer over SQLite.

Public API (see :mod:`metriclayer.api`):

    load_model(path) -> Model
    build_database(model, data_path, db_path) -> dict  (validation report)
    run_query(model, db_path, query) -> QueryResult
    explain_query(model, query) -> QueryPlan
"""

from .errors import MetricLayerError, ModelError, DataError, CompileError
from .model import (
    Model,
    Table,
    Column,
    Reference,
    BaseMetric,
    DerivedMetric,
    Dimension,
    load_model,
)
from .api import build_database, run_query, explain_query, QueryResult

__all__ = [
    "MetricLayerError",
    "ModelError",
    "DataError",
    "CompileError",
    "Model",
    "Table",
    "Column",
    "Reference",
    "BaseMetric",
    "DerivedMetric",
    "Dimension",
    "load_model",
    "build_database",
    "run_query",
    "explain_query",
    "QueryResult",
]

__version__ = "1.0.0"
