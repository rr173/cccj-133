"""Error types for the clean-up view write-back module."""

from __future__ import annotations


class CleanWriteError(Exception):
    """Base class for all errors raised by this module."""

    code = "error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class EncodingError(CleanWriteError):
    """A text cell is invalid or not in canonical form."""

    code = "invalid_encoding"


class InputError(CleanWriteError):
    """Initialisation / import / action payload is malformed."""

    code = "invalid_input"


class TokenError(CleanWriteError):
    """snapshot_token is malformed, forged, or belongs to another database."""

    code = "invalid_token"


class StaleTokenError(CleanWriteError):
    """The token is authentic but the database has moved on (revision changed)."""

    code = "stale_token"


class RowRefError(CleanWriteError):
    """A row reference is unknown, from another export, or used illegally."""

    code = "invalid_row_ref"


class ConflictError(CleanWriteError):
    """The action batch cannot be derived consistently."""

    code = "conflict"

    def __init__(self, message: str, conflicts=None, **details):
        super().__init__(message, **details)
        self.conflicts = list(conflicts or [])

    def to_dict(self) -> dict:
        out = super().to_dict()
        if self.conflicts:
            out["conflicts"] = self.conflicts
        return out

