"""Canonical, reversible text encoding for the editable clean-up view.

Every database text value is rendered into a printable cell by a single
canonical encoding.  The encoding is *total* (every str can be encoded) and
*injective* (distinct strings get distinct encodings), and it never trims,
case-folds or guesses.  The four shapes are:

===================  ==============================================
source value         exported cell
===================  ==============================================
``None``             ``null``                       (the word, no quotes)
a str                ``s"..."``
===================  ==============================================

Inside the quoted text (everything between ``s"`` and ``"``):

* a backslash always introduces an escape;
* ``\\\\`` = one backslash, ``\\"`` = one double quote,
  ``\\n`` = newline (U+000A), ``\\r`` = carriage return (U+000D),
  ``\\t`` = tab (U+0009);
* ``\\u{XXXX}`` (one or more upper-case hex digits, no leading zero) encodes
  one Unicode scalar value.  It is used for every control character
  (``U+0000``–``U+001F`` and ``U+007F``) and for any code point above the
  Basic Multilingual Plane;

Everything else is emitted literally.  In particular ordinary Chinese text,
spaces (including leading/trailing ones) and the empty string are emitted
literally; the empty string is the cell ``s""`` and is distinct from
``null``.

There is exactly one canonical cell per value.  The decoder is strict and
rejects every non-canonical spelling:

* the ``null`` word is case-sensitive and must not be quoted;
* ``s"..."`` must use lower-case ``s`` and a double quote;
* ``\\/``, ``\\b``, ``\\f``, raw control characters, unescaped quotes and
  unknown escapes are rejected;
* a scalar above U+FFFF must be written as ``\\u{XXXX}`` (two ``\\uXXXX``
  escapes is not canonical), and a code point that has a shorter escape
  (``"``, ``\\``, newline, tab, carriage return, low control characters)
  must use that escape;
* a surrogate code point, a code point above U+10FFFF, ``\\u{0000}``
  (leading zeros), and a missing closing quote are rejected.

The exported cell is JavaScript/JSON-friendly on purpose: it can be stored in
a JSON string without further escaping surprises, yet its canonical form is
defined by this module alone.
"""

from __future__ import annotations

from .errors import EncodingError

__all__ = [
    "NULL_CELL",
    "encode_text",
    "decode_text",
    "encode_text_or_null",
    "decode_text_or_null",
    "is_canonical",
]

NULL_CELL = "null"

_SIMPLE_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_UNESCAPE = {
    "\\\\": "\\",
    '\\"': '"',
    "\\n": "\n",
    "\\r": "\r",
    "\\t": "\t",
}


def _is_control(ch: str) -> bool:
    cp = ord(ch)
    return cp <= 0x1F or cp == 0x7F


def encode_text(value: str) -> str:
    """Return the unique canonical cell for a non-null text value."""
    if not isinstance(value, str):
        raise EncodingError("encode_text() requires str, got %r" % (type(value),))
    out = ['s"']
    for ch in value:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise EncodingError(
                "text contains a lone surrogate U+%04X; it cannot be exported"
                % cp
            )
        esc = _SIMPLE_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif _is_control(ch) or cp > 0xFFFF:
            out.append("\\u{%X}" % cp)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def encode_text_or_null(value) -> str:
    """Return the canonical cell for ``None`` or a str."""
    if value is None:
        return NULL_CELL
    return encode_text(value)


def decode_text(cell) -> str:
    """Strictly decode a canonical text cell; ``null`` is not accepted here."""
    if not isinstance(cell, str):
        raise EncodingError("cell must be a string, got %r" % (type(cell),))
    if cell == NULL_CELL:
        raise EncodingError("'null' is the NULL marker, not a text cell")
    if len(cell) < 3 or not cell.startswith('s"') or not cell.endswith('"'):
        raise EncodingError(
            "text cells must be of the form s\"...\"; got %r" % (cell,)
        )
    body = cell[2:-1]
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            if _is_control(ch):
                raise EncodingError(
                    "raw control character U+%04X must be escaped" % ord(ch)
                )
            out.append(ch)
            i += 1
            continue
        # backslash escape
        if i + 1 >= n:
            raise EncodingError("dangling backslash at end of cell")
        nxt = body[i + 1]
        if nxt in ('\\', '"', "n", "r", "t"):
            out.append(_UNESCAPE["\\" + nxt])
            i += 2
            continue
        if nxt == "u":
            if i + 2 >= n or body[i + 2] != "{":
                raise EncodingError("unicode escape must be \\u{...}")
            close = body.find("}", i + 3)
            if close < 0:
                raise EncodingError("unterminated \\u{...} escape")
            digits = body[i + 3:close]
            if not digits:
                raise EncodingError("empty \\u{} escape")
            if len(digits) > 1 and digits[0] == "0":
                raise EncodingError("\\u{} digits must not have leading zeros")
            if any(c not in "0123456789ABCDEF" for c in digits):
                raise EncodingError(
                    "\\u{} escape must use upper-case hex digits"
                )
            cp = int(digits, 16)
            if cp > 0x10FFFF:
                raise EncodingError("code point U+%X is out of range" % cp)
            if 0xD800 <= cp <= 0xDFFF:
                raise EncodingError("surrogate U+%04X is not a scalar value" % cp)
            ch_ = chr(cp)
            # canonicality: a code point with a short escape must use it
            if ch_ in _SIMPLE_ESCAPES:
                raise EncodingError(
                    "U+%04X must be written as %s, not \\u{...}"
                    % (cp, _SIMPLE_ESCAPES[ch_])
                )
            if not (_is_control(ch_) or cp > 0xFFFF):
                raise EncodingError(
                    "U+%04X has a literal form; \\u{...} is not canonical" % cp
                )
            out.append(ch_)
            i = close + 1
            continue
        raise EncodingError("unknown escape \\%s" % nxt)
    text = "".join(out)
    if encode_text(text) != cell:
        raise EncodingError(
            "cell is not in canonical form (canonical form is %r)"
            % encode_text(text)
        )
    return text


def decode_text_or_null(cell) -> str | None:
    """Strictly decode a canonical cell that may be the ``null`` marker."""
    if cell == NULL_CELL:
        return None
    return decode_text(cell)


def is_canonical(cell) -> bool:
    try:
        decode_text_or_null(cell)
        return True
    except EncodingError:
        return False
