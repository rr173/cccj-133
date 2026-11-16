"""Independent Merkle checks.

The expected roots and paths here are produced by direct recursion over
lists of leaf *bytes* (not from the client's verifier and not from the
fixture's generators), and the RFC 9162 published example proofs are
asserted verbatim.
"""

import hashlib
import random
import unittest

from ctmirror import merkle


def H(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def ref_leaf(leaf: bytes) -> bytes:
    return H(b"\x00" + leaf)


def ref_node(left: bytes, right: bytes) -> bytes:
    return H(b"\x01" + left + right)


def ref_split(n: int) -> int:
    """Largest power of two < n, computed without importing merkle."""
    k, p = 1, 1
    while p * 2 < n:
        p *= 2
    return p


def ref_root(leaves):
    if not leaves:
        return H(b"")
    if len(leaves) == 1:
        return ref_leaf(leaves[0])
    k = ref_split(len(leaves))
    return ref_node(ref_root(leaves[:k]), ref_root(leaves[k:]))


def ref_path(leaves, m):
    """Direct recursive path definition, returning sibling hashes deep-first."""
    if len(leaves) == 1:
        return []
    k = ref_split(len(leaves))
    if m < k:
        return ref_path(leaves[:k], m) + [ref_root(leaves[k:])]
    return ref_path(leaves[k:], m - k) + [ref_root(leaves[:k])]


def ref_consistency(leaves, m, known=True):
    """RFC 9162 SUBPROOF recursion written directly (flag included)."""
    if m == len(leaves):
        return [] if known else [ref_root(leaves)]
    k = ref_split(len(leaves))
    if m <= k:
        return (ref_consistency(leaves[:k], m, known) +
                [ref_root(leaves[k:])])
    return (ref_consistency(leaves[k:], m - k, False) +
            [ref_root(leaves[:k])])


class TestRootAndPaths(unittest.TestCase):
    def test_empty_root(self):
        self.assertEqual(merkle.tree_hash([]), H(b""))
        self.assertEqual(merkle.EMPTY_ROOT, hashlib.sha256(b"").digest())

    def test_known_leaf_hash(self):
        self.assertEqual(merkle.leaf_hash(b"abc"), H(b"\x00abc"))

    def test_non_power_of_two_lengths(self):
        for n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 16, 17, 18, 31,
                  33):
            leaves = [f"leaf-{i}".encode() for i in range(n)]
            self.assertEqual(merkle.tree_hash(leaves), ref_root(leaves), n)

    def test_paths_match_direct_recursion(self):
        rng = random.Random(1234)
        for n in range(1, 40):
            leaves = [rng.randbytes(rng.randrange(0, 12)) for _ in range(n)]
            root = ref_root(leaves)
            for i in range(n):
                expected = ref_path(leaves, i)
                self.assertEqual(merkle.inclusion_path(leaves, i), expected)
                ok, why = merkle.verify_inclusion(
                    leaves[i], i, n, expected, root)
                self.assertTrue(ok, (n, i, why))

    def test_consistency_matches_direct_recursion(self):
        rng = random.Random(4321)
        for n in range(1, 40):
            leaves = [rng.randbytes(rng.randrange(0, 12)) for _ in range(n)]
            root = ref_root(leaves)
            for m in range(1, n + 1):
                expected = ref_consistency(leaves, m)
                # the reference recursion above yields the SUBPROOF list;
                # normalise the m<=k-with-known-root case is handled inside
                self.assertEqual(merkle.consistency_path(leaves, m), expected)
                ok, why = merkle.verify_consistency(
                    m, n, expected, ref_root(leaves[:m]), root)
                self.assertTrue(ok, (n, m, why))

    def test_published_rfc_example(self):
        # RFC 9162 section 2.1.5: the 7-leaf tree.
        leaves = [b"d0", b"d1", b"d2", b"d3", b"d4", b"d5", b"d6"]

        def node(*kids):
            return ref_root(list(kids))

        c = node(leaves[2])
        d = node(leaves[3])
        g = node(*leaves[0:2])
        h = node(*leaves[2:4])
        i = node(*leaves[4:6])
        j = node(leaves[6])
        k = ref_node(g, h)
        el = ref_node(i, j)
        root = ref_node(k, el)
        self.assertEqual(merkle.tree_hash(leaves), root)
        self.assertEqual(merkle.inclusion_path(leaves, 0), [leaves and
                                                             node(leaves[1]),
                                                             h, el])
        self.assertEqual(merkle.inclusion_path(leaves, 3), [c, g, el])
        self.assertEqual(merkle.inclusion_path(leaves, 4), [node(leaves[5]),
                                                             j, k])
        self.assertEqual(merkle.inclusion_path(leaves, 6), [i, k])
        self.assertEqual(merkle.consistency_path(leaves, 3), [c, d, g, el])
        self.assertEqual(merkle.consistency_path(leaves, 4), [el])
        self.assertEqual(merkle.consistency_path(leaves, 6),
                         [i, j, k])


class TestVerifierRejection(unittest.TestCase):
    def setUp(self):
        self.leaves = [f"leaf-{i}".encode() for i in range(9)]
        self.root = ref_root(self.leaves)

    def test_short_and_long_paths(self):
        p = ref_path(self.leaves, 4)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 4, 9, p[:-1],
                                        self.root)
        self.assertFalse(ok)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 4, 9, p + [p[0]],
                                        self.root)
        self.assertFalse(ok)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 4, 9,
                                        p + [b"\x00" * 32], self.root)
        self.assertFalse(ok)

    def test_wrong_leaf_and_wrong_index(self):
        p = ref_path(self.leaves, 4)
        ok, _ = merkle.verify_inclusion(b"other", 4, 9, p, self.root)
        self.assertFalse(ok)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 5, 9, p, self.root)
        self.assertFalse(ok)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 4, 8, p, self.root)
        self.assertFalse(ok)

    def test_wrong_size_and_node_width(self):
        p = ref_path(self.leaves, 4)
        ok, _ = merkle.verify_inclusion(self.leaves[4], 4, 9,
                                        [p[0][:-1]] + p[1:], self.root)
        self.assertFalse(ok)
        ok, why = merkle.verify_inclusion(self.leaves[4], 9, 9, p, self.root)
        self.assertFalse(ok)

    def test_consistency_boundaries(self):
        r5 = ref_root(self.leaves[:5])
        r9 = ref_root(self.leaves)
        ok, _ = merkle.verify_consistency(5, 5, [], r5, r5)
        self.assertTrue(ok)
        ok, _ = merkle.verify_consistency(5, 5, [], r5, r9)
        self.assertFalse(ok)
        ok, _ = merkle.verify_consistency(5, 5, [b"\x00" * 32], r5, r5)
        self.assertFalse(ok)
        ok, why = merkle.verify_consistency(0, 9, [], merkle.EMPTY_ROOT, r9)
        self.assertTrue(ok, why)
        ok, _ = merkle.verify_consistency(0, 9, [b"\x00" * 32],
                                          merkle.EMPTY_ROOT, r9)
        self.assertFalse(ok)
        ok, _ = merkle.verify_consistency(9, 5, [], r9, merkle.EMPTY_ROOT)
        self.assertFalse(ok)

        # Empty path for 0 < old < new is rejected.
        ok, _ = merkle.verify_consistency(5, 9, [], r5, r9)
        self.assertFalse(ok)

    def test_consistency_short_long_and_bad(self):
        p = ref_consistency(self.leaves, 5)
        r5, r9 = ref_root(self.leaves[:5]), ref_root(self.leaves)
        ok, _ = merkle.verify_consistency(5, 9, p[:-1], r5, r9)
        self.assertFalse(ok)
        ok, _ = merkle.verify_consistency(5, 9, p + [p[0]], r5, r9)
        self.assertFalse(ok)
        bad = list(p)
        bad[0] = hashlib.sha256(bad[0]).digest()
        ok, _ = merkle.verify_consistency(5, 9, bad, r5, r9)
        self.assertFalse(ok)
        ok, _ = merkle.verify_consistency(5, 9, p, r9, r9)
        self.assertFalse(ok)
        ok, _ = merkle.verify_consistency(5, 9, p, r5, r5)
        self.assertFalse(ok)

    def test_9_vs_10_do_not_confuse(self):
        """A proof for the 10-leaf tree must not verify as a 9-leaf proof."""
        ten = self.leaves + [b"extra"]
        p10 = ref_consistency(ten, 5)
        r5 = ref_root(self.leaves[:5])
        r9 = ref_root(self.leaves)
        r10 = ref_root(ten)
        self.assertTrue(merkle.verify_consistency(5, 10, p10, r5, r10)[0])
        self.assertFalse(merkle.verify_consistency(5, 9, p10, r5, r9)[0])

    def test_every_prefix_consistency_bruteforce(self):
        rng = random.Random(99)
        leaves = [rng.randbytes(5) for _ in range(24)]
        for n in range(1, 25):
            for m in range(1, n):
                p = ref_consistency(leaves[:n], m)
                self.assertTrue(merkle.verify_consistency(
                    m, n, p, ref_root(leaves[:m]), ref_root(leaves[:n]))[0])


if __name__ == "__main__":
    unittest.main()
