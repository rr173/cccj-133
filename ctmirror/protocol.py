"""Binary wire formats for the CTM1 mirror convention.

A signed tree head frame is exactly::

    "CTM1" || u32be(len(source_id_utf8)) || source_id_utf8
           || u64be(epoch) || u64be(tree_size) || u64be(timestamp_ms)
           || root[32] || signature[64]

The Ed25519 signature covers everything before the signature; that byte
string is the "签名原文" of the deployment contract.  Note that the source
id length prefix and the three integers are big-endian, fixed width.

A successor-key certificate frame is exactly::

    "CTK1" || u32be(len(source_id_utf8)) || source_id_utf8
           || u64be(old_epoch) || u64be(new_epoch)
           || new_public_key[32] || u64be(activation_size) || signature[64]

It is signed by the old generation's Ed25519 key.  new_epoch must be
old_epoch + 1.  Material decoding is deliberately strict: magic, source
id, field widths, UTF-8, trailing bytes and signature length all fail
closed before a signature is considered.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat)

HEAD_MAGIC = b"CTM1"
CERT_MAGIC = b"CTK1"
PUBLIC_KEY_LEN = 32
SIGNATURE_LEN = 64
ROOT_LEN = 32
MAX_SOURCE_ID_BYTES = 4096
MAX_PROOF_NODES = 64


class FormatError(ValueError):
    """Cryptographic material or wire encoding failed strict validation."""


class _Reader:
    def __init__(self, data: bytes, what: str):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise FormatError(f"{what}: expected bytes")
        self.data = bytes(data)
        self.pos = 0
        self.what = what

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError(
                f"{self.what}: truncated at offset {self.pos}, need {n} bytes")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big", signed=False)

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "big", signed=False)

    def source_id(self) -> str:
        n = self.u32()
        if n == 0:
            raise FormatError(f"{self.what}: empty source_id")
        if n > MAX_SOURCE_ID_BYTES:
            raise FormatError(f"{self.what}: source_id too long")
        raw = self.take(n)
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FormatError(f"{self.what}: source_id not UTF-8") from exc
        if "\x00" in value:
            raise FormatError(f"{self.what}: source_id contains NUL")
        return value

    def finish(self) -> None:
        if self.pos != len(self.data):
            raise FormatError(
                f"{self.what}: {len(self.data) - self.pos} trailing bytes")


def encode_source_id(source_id: str) -> bytes:
    if not isinstance(source_id, str):
        raise FormatError("source_id must be str")
    try:
        raw = source_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FormatError("source_id is not encodable UTF-8") from exc
    if not raw or len(raw) > MAX_SOURCE_ID_BYTES or b"\x00" in raw:
        raise FormatError("invalid source_id")
    return len(raw).to_bytes(4, "big") + raw


def u64(value: int, name: str) -> bytes:
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise FormatError(f"{name} must be an unsigned 64-bit integer")
    return value.to_bytes(8, "big")


@dataclass(frozen=True)
class TreeHead:
    source_id: str
    epoch: int
    tree_size: int
    timestamp_ms: int
    root: bytes
    signature: bytes
    signed_bytes: bytes
    frame: bytes

    def identity(self) -> tuple:
        return (self.source_id, self.epoch, self.tree_size,
                self.timestamp_ms, self.root)


@dataclass(frozen=True)
class KeyCert:
    source_id: str
    old_epoch: int
    new_epoch: int
    new_public_key: bytes
    activation_size: int
    signature: bytes
    signed_bytes: bytes
    frame: bytes


def public_key_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != PUBLIC_KEY_LEN:
        raise FormatError("Ed25519 public key must be 32 bytes")
    try:
        return Ed25519PublicKey.from_public_bytes(bytes(raw))
    except ValueError as exc:
        raise FormatError("invalid Ed25519 public key") from exc


def verify_signature(public_key: bytes, message: bytes,
                     signature: bytes) -> tuple[bool, str]:
    if len(public_key) != PUBLIC_KEY_LEN:
        return False, "public key is not 32 bytes"
    if len(signature) != SIGNATURE_LEN:
        return False, "signature is not 64 bytes"
    try:
        load_public_key(public_key).verify(bytes(signature), bytes(message))
    except (InvalidSignature, ValueError, TypeError):
        return False, "Ed25519 signature does not verify"
    return True, "ok"


def parse_tree_head(frame: bytes,
                    expected_source: Optional[str] = None) -> TreeHead:
    r = _Reader(frame, "tree head")
    magic = r.take(4)
    if magic != HEAD_MAGIC:
        raise FormatError("tree head: bad magic (expected CTM1)")
    sid = r.source_id()
    if expected_source is not None and sid != expected_source:
        raise FormatError(
            f"tree head: source_id {sid!r} does not match {expected_source!r}")
    epoch = r.u64()
    tree_size = r.u64()
    timestamp_ms = r.u64()
    root = r.take(ROOT_LEN)
    signature = r.take(SIGNATURE_LEN)
    r.finish()
    signed = r.data[:len(r.data) - SIGNATURE_LEN]
    return TreeHead(sid, epoch, tree_size, timestamp_ms, root, signature,
                    signed, r.data)


def make_tree_head(source_id: str, epoch: int, tree_size: int,
                   timestamp_ms: int, root: bytes,
                   private_key: Ed25519PrivateKey) -> bytes:
    if len(root) != ROOT_LEN:
        raise FormatError("root must be 32 bytes")
    preimage = (HEAD_MAGIC + encode_source_id(source_id) +
                u64(epoch, "epoch") + u64(tree_size, "tree_size") +
                u64(timestamp_ms, "timestamp_ms") + bytes(root))
    return preimage + private_key.sign(preimage)


def parse_key_cert(frame: bytes,
                   expected_source: Optional[str] = None) -> KeyCert:
    r = _Reader(frame, "key certificate")
    magic = r.take(4)
    if magic != CERT_MAGIC:
        raise FormatError("key certificate: bad magic (expected CTK1)")
    sid = r.source_id()
    if expected_source is not None and sid != expected_source:
        raise FormatError(
            f"key certificate: source_id {sid!r} does not match "
            f"{expected_source!r}")
    old_epoch = r.u64()
    new_epoch = r.u64()
    new_pub = r.take(PUBLIC_KEY_LEN)
    activation_size = r.u64()
    signature = r.take(SIGNATURE_LEN)
    r.finish()
    if new_epoch != old_epoch + 1:
        raise FormatError(
            "key certificate: new_epoch must be exactly old_epoch + 1 "
            "(generation jump rejected)")
    if old_epoch >= 1 << 64:
        raise FormatError("key certificate: epoch out of range")
    signed = r.data[:len(r.data) - SIGNATURE_LEN]
    return KeyCert(sid, old_epoch, new_epoch, new_pub, activation_size,
                   signature, signed, r.data)


def make_key_cert(source_id: str, old_epoch: int, new_epoch: int,
                  new_public_key: bytes, activation_size: int,
                  old_private_key: Ed25519PrivateKey) -> bytes:
    preimage = (CERT_MAGIC + encode_source_id(source_id) +
                u64(old_epoch, "old_epoch") + u64(new_epoch, "new_epoch") +
                bytes(new_public_key) + u64(activation_size, "activation_size"))
    return preimage + old_private_key.sign(preimage)


# ---------------------------------------------------------------------------
# JSON transport helpers (strict, but JSON is only a carrier for binary frames)
# ---------------------------------------------------------------------------

def b64decode_strict(value, what: str) -> bytes:
    if not isinstance(value, str):
        raise FormatError(f"{what}: expected base64 string")
    if len(value) > 64 * 1024 * 1024:
        raise FormatError(f"{what}: base64 string too large")
    try:
        raw = value.encode("ascii")
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise FormatError(f"{what}: invalid base64") from exc
    if base64.b64encode(decoded) != raw:
        raise FormatError(f"{what}: non-canonical base64")
    return decoded


def hexdecode_strict(value, what: str) -> bytes:
    if not isinstance(value, str):
        raise FormatError(f"{what}: expected hex string")
    if len(value) % 2:
        raise FormatError(f"{what}: odd-length hex")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise FormatError(f"{what}: invalid hex") from exc


def require_uint(value, what: str, bits: int = 64) -> int:
    if type(value) is not int:
        raise FormatError(f"{what}: expected integer")
    if not 0 <= value < (1 << bits):
        raise FormatError(f"{what}: integer out of range")
    return value


def require_str(value, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise FormatError(f"{what}: expected non-empty string")
    return value


def require_list(value, what: str) -> list:
    if not isinstance(value, list):
        raise FormatError(f"{what}: expected array")
    return value


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
