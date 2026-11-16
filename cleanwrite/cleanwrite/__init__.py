"""cleanwrite — write-back of an editable cleaning view into two source tables.

Public API (see :mod:`cleanwrite.api`)::

    init_db(db, sample=None, config=None, force=False)
    import_sample(db, sample, replace=True)
    set_config(db, config)
    source_tables(db)
    get(db) -> {"snapshot_token", "revision", "view", "rows"}
    explain(db, token, actions) -> derivation report
    put(db, token, actions)     -> applied result

The editable view is ``C LEFT JOIN A`` ordered by ``cid`` then ``aid`` in
Unicode code point order.  Every row carries an opaque ``row_ref``; the
``snapshot_token`` binds the database identity, revision, view definition,
insert defaults and the row-ref list.  Hidden columns (``internal_note``,
``secret_tag``) never appear in the view or in the token.

Text cells use the canonical encoding of :mod:`cleanwrite.codec`::

    NULL       ->  null
    text       ->  s"..."
    \\\\ -> \\\\   \\" -> \\"   newline -> \\n
    \\r -> \\r     tab -> \\t    other controls -> \\u{XX}
    code points > U+FFFF -> \\u{XXXX}

Action formats are documented in :mod:`cleanwrite.actions` and in the README.
"""

from .api import (
    explain,
    get,
    import_sample,
    init_db,
    put,
    set_config,
    source_tables,
)
from .codec import decode_text, decode_text_or_null, encode_text, encode_text_or_null
from .errors import (
    CleanWriteError,
    ConflictError,
    EncodingError,
    InputError,
    RowRefError,
    StaleTokenError,
    TokenError,
)

__all__ = [
    "init_db", "import_sample", "set_config", "source_tables",
    "get", "explain", "put",
    "encode_text", "encode_text_or_null", "decode_text", "decode_text_or_null",
    "CleanWriteError", "EncodingError", "InputError", "TokenError",
    "StaleTokenError", "RowRefError", "ConflictError",
]
