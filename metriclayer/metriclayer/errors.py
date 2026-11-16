"""Exception hierarchy. Every error raised deliberately by this package is a
:class:`MetricLayerError`, so callers can catch a single type."""

from __future__ import annotations


class MetricLayerError(Exception):
    """Base error for model declaration, data validation and query compilation."""


class ModelError(MetricLayerError):
    """The declared model itself is inconsistent (bad types, cyclic graph, ...)."""


class DataError(MetricLayerError):
    """The data file violates the model (duplicate key, dangling FK, bad value)."""


class CompileError(MetricLayerError):
    """A query cannot be compiled against the model (unreachable dimension, ...)."""
