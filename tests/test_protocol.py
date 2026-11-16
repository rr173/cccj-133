"""Wire-format, signature and trust-material tests."""

import hashlib
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ctmirror import protocol


def key(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed).digest())


class TestTreeHeadFrame(unittest.TestCase):
    def setUp(self):
        self.sk = key(b"head")
        self.root = bytes(range(32))
        self.frame = protocol.make_tree_head(
            "source-α", 3, 9, 1_700_000_000_123, self.root, self.sk)

    def test_exact_layout(self):
        f = self.frame
        self.assertEqual(f[:4], b"CTM1")
        sid = "source-α".encode()
        self.assertEqual(int.from_bytes(f[4:8], "big"), len(sid))
        self.assertEqual(f[8:8 + len(sid)], sid)
        off = 8 + len(sid)
        self.assertEqual(int.from_bytes(f[off:off + 8], "big"), 3)
        self.assertEqual(int.from_bytes(f[off + 8:off + 16], "big"), 9)
        self.assertEqual(int.from_bytes(f[off + 16:off + 24], "big"),
                         1_700_000_000_123)
        self.assertEqual(f[off + 24:off + 56], self.root)
        self.assertEqual(len(f[off + 56:]), 64)

    def test_signature_covers_prefix(self):
        head = protocol.parse_tree_head(self.frame)
        ok, why = protocol.verify_signature(
            protocol.public_key_bytes(self.sk.public_key()),
            self.frame[:-64], self.frame[-64:])
        self.assertTrue(ok, why)
        self.assertEqual(head.signed_bytes, self.frame[:-64])

    def test_every_bit_matters(self):
        head = protocol.parse_tree_head(self.frame)
        pub = protocol.public_key_bytes(self.sk.public_key())
        signed = head.signed_bytes
        for pos in (0, 5, len(signed) - 1):
            tampered = bytearray(signed)
            tampered[pos] ^= 0x01
            ok, _ = protocol.verify_signature(pub, bytes(tampered),
                                              head.signature)
            self.assertFalse(ok, pos)

    def test_source_id_length_prefix_differs_from_naive(self):
        # A naive implementation concatenating the id as a string would
        # produce a different preimage; assert ours matches the spec exactly.
        sid = "abc"
        pre = (b"CTM1" + (3).to_bytes(4, "big") + b"abc" +
               (0).to_bytes(8, "big") + (0).to_bytes(8, "big") +
               (0).to_bytes(8, "big") + b"\x00" * 32)
        self.assertEqual(protocol.make_tree_head(
            sid, 0, 0, 0, b"\x00" * 32, self.sk)[:-64], pre)

    def test_rejects_bad_frames(self):
        cases = [
            self.frame[:3],
            b"XXXX" + self.frame[4:],
            self.frame + b"\x00",
            self.frame[:-64],
            self.frame[:-1],
            b"",
        ]
        for frame in cases:
            with self.assertRaises(protocol.FormatError, msg=frame[:8]):
                protocol.parse_tree_head(frame)

    def test_rejects_unknown_source(self):
        with self.assertRaises(protocol.FormatError):
            protocol.parse_tree_head(self.frame, expected_source="other")

    def test_rejects_non_utf8_source(self):
        bad = (b"CTM1" + (2).to_bytes(4, "big") + b"\xff\xfe" +
               (0).to_bytes(8, "big") * 3 + b"\x00" * 32 + b"\x00" * 64)
        with self.assertRaises(protocol.FormatError):
            protocol.parse_tree_head(bad)

    def test_rejects_empty_source(self):
        bad = (b"CTM1" + (0).to_bytes(4, "big") +
               (0).to_bytes(8, "big") * 3 + b"\x00" * 32 + b"\x00" * 64)
        with self.assertRaises(protocol.FormatError):
            protocol.parse_tree_head(bad)

    def test_signature_shapes(self):
        pub = protocol.public_key_bytes(self.sk.public_key())
        ok, why = protocol.verify_signature(pub, self.frame[:-64],
                                            self.frame[-64:-1])
        self.assertFalse(ok)
        ok, _ = protocol.verify_signature(pub[:-1], self.frame[:-64],
                                          self.frame[-64:])
        self.assertFalse(ok)
        ok, _ = protocol.verify_signature(b"\x00" * 32, self.frame[:-64],
                                          self.frame[-64:])
        self.assertFalse(ok)


class TestKeyCert(unittest.TestCase):
    def setUp(self):
        self.old = key(b"old")
        self.new = key(b"new")
        self.new_pub = protocol.public_key_bytes(self.new.public_key())
        self.old_pub = protocol.public_key_bytes(self.old.public_key())
        self.frame = protocol.make_key_cert("s1", 0, 1, self.new_pub, 9,
                                            self.old)

    def test_layout_and_verification(self):
        cert = protocol.parse_key_cert(self.frame, expected_source="s1")
        self.assertEqual(cert.frame[:4], b"CTK1")
        self.assertEqual((cert.old_epoch, cert.new_epoch), (0, 1))
        self.assertEqual(cert.new_public_key, self.new_pub)
        self.assertEqual(cert.activation_size, 9)
        ok, why = protocol.verify_signature(self.old_pub, cert.signed_bytes,
                                            cert.signature)
        self.assertTrue(ok, why)
        ok, _ = protocol.verify_signature(self.new_pub, cert.signed_bytes,
                                          cert.signature)
        self.assertFalse(ok, "self-signed successor key must not verify")

    def test_epoch_jump_rejected(self):
        frame = protocol.make_key_cert("s1", 0, 2, self.new_pub, 9, self.old)
        with self.assertRaises(protocol.FormatError):
            protocol.parse_key_cert(frame)

    def test_epoch_rollback_rejected(self):
        frame = protocol.make_key_cert("s1", 3, 2, self.new_pub, 9, self.old)
        with self.assertRaises(protocol.FormatError):
            protocol.parse_key_cert(frame)

    def test_truncation_and_trailing(self):
        for frame in (self.frame[:-1], self.frame + b"\x00",
                      self.frame[:6], self.frame[:-64]):
            with self.assertRaises(protocol.FormatError):
                protocol.parse_key_cert(frame)

    def test_wrong_magic_and_source(self):
        with self.assertRaises(protocol.FormatError):
            protocol.parse_key_cert(b"CTM1" + self.frame[4:])
        with self.assertRaises(protocol.FormatError):
            protocol.parse_key_cert(self.frame, expected_source="s2")

    def test_activation_size_is_signed(self):
        cert = protocol.parse_key_cert(self.frame)
        tampered = bytearray(cert.signed_bytes)
        tampered[-1] ^= 0x01
        ok, _ = protocol.verify_signature(self.old_pub, bytes(tampered),
                                          cert.signature)
        self.assertFalse(ok)


class TestStrictHelpers(unittest.TestCase):
    def test_b64_strict(self):
        self.assertEqual(protocol.b64decode_strict("AAAA", "x"), b"\0\0\0")
        for bad in ("A", "***", 12, None, "AAAA="):
            with self.assertRaises(protocol.FormatError, msg=repr(bad)):
                protocol.b64decode_strict(bad, "x")

    def test_hex_strict(self):
        self.assertEqual(protocol.hexdecode_strict("00ff", "x"), b"\x00\xff")
        for bad in ("0", "zz", 5, None, " 00"):
            with self.assertRaises(protocol.FormatError, msg=repr(bad)):
                protocol.hexdecode_strict(bad, "x")

    def test_uint_strict(self):
        self.assertEqual(protocol.require_uint(5, "x"), 5)
        for bad in (True, 1.0, "1", -1, 1 << 64, None):
            with self.assertRaises(protocol.FormatError, msg=repr(bad)):
                protocol.require_uint(bad, "x")

    def test_u64_encoding(self):
        self.assertEqual(protocol.u64(1, "x"), b"\x00" * 7 + b"\x01")
        for bad in (True, -1, 1 << 64, 2.5):
            with self.assertRaises(protocol.FormatError):
                protocol.u64(bad, "x")


if __name__ == "__main__":
    unittest.main()
