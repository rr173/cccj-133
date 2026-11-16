"""Merkle tree definitions for the CTM1 mirror convention.

This module implements the public RFC 9162 (Certificate Transparency v2)
Merkle tree definitions, byte-for-byte:

  * leaf hash:      SHA-256(0x00 || leaf_bytes)
  * node hash:      SHA-256(0x01 || left_hash || right_hash)
  * empty tree root: SHA-256("")            (the hash of the empty string)
  * MTH(D_n) for n > 1 splits at k, the largest power of two strictly
    smaller than n, without padding leaves.

All hashes are raw 32-byte SHA-256 digests. Byte order only matters inside
the signed tree-head preimage (see ``protocol``); Merkle node hashes are
opaque byte strings concatenated in left-to-right order.

Proof verification in this module is written from the RFC 9162 text
(Sections 2.1.3.2 and 2.1.4.2) and does not share code with the proof
generation helpers used by the test fixtures.  The generation helpers are
included for the independent local test tree only; the mirror client never
generates proofs.
"""

import hashlib

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def leaf_hash(leaf: bytes) -> bytes:
    if not isinstance(leaf, (bytes, bytearray, memoryview)):
        raise TypeError("leaf must be bytes")
    return hashlib.sha256(LEAF_PREFIX + bytes(leaf)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise ValueError("node hashes must be 32 bytes")
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


EMPTY_ROOT = hashlib.sha256(b"").digest()


def largest_power_of_two_below(n: int) -> int:
    """Largest power of two strictly less than n, for n > 1."""
    if n <= 1:
        raise ValueError("n must be > 1")
    return 1 << ((n - 1).bit_length() - 1)


# ---------------------------------------------------------------------------
# Verification (used at runtime by the client)
# ---------------------------------------------------------------------------

def verify_inclusion(leaf: bytes, leaf_index: int, tree_size: int,
                     path: list[bytes], root: bytes) -> tuple[bool, str]:
    """RFC 9162 section 2.1.3.2, with an explicit tree_size check first."""
    if not isinstance(leaf_index, int) or not isinstance(tree_size, int):
        return False, "index/size not integers"
    if tree_size <= 0:
        return False, "tree_size must be positive"
    if leaf_index < 0 or leaf_index >= tree_size:
        return False, "leaf_index outside tree"
    for p in path:
        if not isinstance(p, (bytes, bytearray)) or len(p) != 32:
            return False, "inclusion path node is not 32 bytes"
    if not isinstance(root, (bytes, bytearray)) or len(root) != 32:
        return False, "root is not 32 bytes"

    fn = leaf_index
    sn = tree_size - 1
    r = leaf_hash(leaf)
    for p in path:
        if sn == 0:
            return False, "inclusion path longer than tree height"
        if (fn & 1) or fn == sn:
            r = node_hash(p, r)
            if not (fn & 1):
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    if sn != 0:
        return False, "inclusion path too short (sn != 0)"
    if r != root:
        return False, "inclusion path does not reproduce root"
    return True, "ok"


def verify_consistency(old_size: int, new_size: int, proof: list[bytes],
                       old_root: bytes, new_root: bytes) -> tuple[bool, str]:
    """RFC 9162 section 2.1.4.2.

    Handles the two boundary cases required by the mirror convention:
    old_size == new_size (roots must be equal, path empty) and old_size == 0
    (an empty path is acceptable; nothing to verify).  For 0 < old < new an
    empty path is rejected, per the RFC.
    """
    if not isinstance(old_size, int) or not isinstance(new_size, int):
        return False, "sizes not integers"
    if old_size < 0 or new_size < 0:
        return False, "negative size"
    if old_size > new_size:
        return False, "old_size greater than new_size"
    for p in proof:
        if not isinstance(p, (bytes, bytearray)) or len(p) != 32:
            return False, "consistency proof node is not 32 bytes"
    if len(old_root) != 32 or len(new_root) != 32:
        return False, "roots must be 32 bytes"
    if old_size == new_size:
        if proof:
            return False, "non-empty proof for equal sizes"
        return (old_root == new_root,
                "ok" if old_root == new_root else "equal size, different root")
    if old_size == 0:
        if proof:
            return False, "non-empty proof for old_size 0"
        return True, "ok"
    if not proof:
        return False, "empty consistency proof"
    path = list(proof)
    if old_size & (old_size - 1) == 0:
        path = [old_root] + path
    fn = old_size - 1
    sn = new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    if not path:
        return False, "empty consistency proof"
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False, "consistency path longer than expected"
        if (fn & 1) or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not (fn & 1):
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    if sn != 0:
        return False, "consistency path too short (sn != 0)"
    if fr != old_root:
        return False, "consistency path does not reproduce old root"
    if sr != new_root:
        return False, "consistency path does not reproduce new root"
    return True, "ok"


# ---------------------------------------------------------------------------
# Independent generation helpers (tests / fixture server only)
# ---------------------------------------------------------------------------

def tree_hash(leaves: list[bytes]) -> bytes:
    if not leaves:
        return EMPTY_ROOT
    if len(leaves) == 1:
        return leaf_hash(leaves[0])
    k = largest_power_of_two_below(len(leaves))
    return node_hash(tree_hash(leaves[:k]), tree_hash(leaves[k:]))


def inclusion_path(leaves: list[bytes], index: int) -> list[bytes]:
    if not 0 <= index < len(leaves):
        raise IndexError("index outside tree")
    if len(leaves) == 1:
        return []
    k = largest_power_of_two_below(len(leaves))
    if index < k:
        return inclusion_path(leaves[:k], index) + [tree_hash(leaves[k:])]
    return inclusion_path(leaves[k:], index - k) + [tree_hash(leaves[:k])]


def _subproof(leaves: list[bytes], m: int, known: bool) -> list[bytes]:
    n = len(leaves)
    if m == n:
        return [] if known else [tree_hash(leaves)]
    k = largest_power_of_two_below(n)
    if m <= k:
        return _subproof(leaves[:k], m, known) + [tree_hash(leaves[k:])]
    return _subproof(leaves[k:], m - k, False) + [tree_hash(leaves[:k])]


def consistency_path(leaves: list[bytes], m: int) -> list[bytes]:
    if not 0 < m <= len(leaves):
        raise ValueError("m outside tree")
    return _subproof(leaves, m, True)
