"""Tests for deferred heads and the candidate-completion policy."""

import os
import unittest

from ctmirror import merkle, protocol
from ctmirror.client import BlockedError
from tests.harness import (admin, leaves_for, make_client, make_manifest,
                           reopen, seed, start_server, stop_server)


class DeferredHeadTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "deferred.sqlite")
        self.reset()
        self.manifest = make_manifest(self.server, ["alpha"])

    def reset(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def tearDown(self):
        stop_server(self.server)
        self.reset()

    def test_deferred_head_survives_restart_and_is_used_next(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        # page_size 1 keeps the 6-leaf candidate partially filled so the
        # 8-leaf head really is observed mid-candidate.
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=1,
                                       manifest=self.manifest)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 6)[4:])
        client.sync("alpha", max_pages=0)
        self.assertEqual(client.status("alpha")["candidate"]["tree_size"], 6)
        # The log advances to 8 while the 6-leaf candidate is still open.
        seed(self.server, "alpha", leaves_for("alpha", 8)[6:])
        res = client.sync("alpha", max_pages=0)
        self.assertEqual(res.candidate_size, 6)
        self.assertEqual(res.deferred_heads[0]["tree_size"], 8)
        deferred = client.status("alpha")["deferred_heads"]
        self.assertEqual([d["tree_size"] for d in deferred], [8])
        store.close()

        store2, client2 = reopen(self.db, self.server, self.manifest,
                                 page_size=1)
        try:
            # Finish the 6-leaf candidate; 8 is still listed as deferred.
            res = client2.sync("alpha")
            self.assertTrue(res.promoted)
            self.assertEqual(res.committed_size, 6)
            status = client2.status("alpha")
            self.assertEqual(status["committed"]["tree_size"], 6)
            self.assertEqual([d["tree_size"]
                              for d in status["deferred_heads"]], [8])
            # Next round adopts the persisted 8-leaf head with a fresh
            # consistency proof.
            res = client2.sync("alpha", max_pages=0)
            self.assertEqual(res.candidate_size, 8)
            self.assertEqual(client2.status("alpha")["candidate"]["root"],
                             merkle.tree_hash(
                                 leaves_for("alpha", 8)).hex())
            res = client2.sync("alpha")
            self.assertTrue(res.promoted)
            self.assertEqual(res.committed_size, 8)
            self.assertEqual(client2.status("alpha")["deferred_heads"], [])
        finally:
            store2.close()

    def test_deferred_equivocation_is_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=1,
                                       manifest=self.manifest)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 6)[4:])
        client.sync("alpha", max_pages=0)
        seed(self.server, "alpha", leaves_for("alpha", 8)[6:])
        client.sync("alpha", max_pages=0)
        self.assertEqual(len(client.status("alpha")["deferred_heads"]), 1)
        # Complete the candidate so the deferred head becomes selectable.
        client.sync("alpha")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 6)
        # Now the live endpoint signs a different root at the deferred size.
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "equivocation", "tree_size": 8}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha", max_pages=0)
        self.assertEqual(ctx.exception.reason, "equivocation")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 6)
        self.assertIsNone(st["candidate"])
        store.close()

    def test_activation_gate_applies_while_candidate_open(self):
        """A successor-key head cannot be used early, and the candidate stays."""
        seed(self.server, "alpha", leaves_for("alpha", 5))
        rot = admin(self.server, "/admin/rotate",
                    {"source_id": "alpha", "activation_size": 9})
        manifest = make_manifest(self.server, ["alpha"])
        # Include the CTK1 cert in the manifest so epoch 1 is verifiable.
        import base64, json
        from ctmirror.client import Manifest, SourceConfig
        cfg = manifest.sources["alpha"]
        payload = {"sources": [{
            "source_id": "alpha", "anchor_epoch": 0,
            "anchor_public_key": cfg.anchor_public_key.hex(),
            "certificates": [rot["cert_b64"]]}]}
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "m.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        manifest = Manifest.load(path)

        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=1,
                                       manifest=manifest)
        # First commit the 5-leaf tree (anchor epoch, zero activation).
        self.assertTrue(client.sync("alpha").promoted)
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 5)
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        client.sync("alpha", max_pages=1)
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertEqual(st["candidate"]["tree_size"], 9)
        self.assertEqual(st["candidate"]["epoch"], 0)
        # Inject a signed epoch-1 head: validly signed but not yet usable.
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 1, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        res = client.sync("alpha", max_pages=0)
        self.assertEqual(res.candidate_size, 9)  # unchanged target
        # The signature verifies but the key is not yet usable; the head is
        # deferred with that explanation instead of being adopted.
        self.assertEqual(len(res.deferred_heads), 1)
        self.assertEqual(res.deferred_heads[0]["relation"], "untrusted")
        self.assertIn("activation_size",
                      res.deferred_heads[0]["note"])
        st = client.status("alpha")
        self.assertEqual(st["candidate"]["epoch"], 0)
        # Complete through the old key; epoch 1 becomes active only then.
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 9)
        self.assertEqual(st["active_epoch"], 1)
        # A subsequent epoch-1 head is adoptable.
        seed(self.server, "alpha", leaves_for("alpha", 10)[9:])
        res = client.sync("alpha")
        self.assertEqual(res.committed_size, 10)
        self.assertEqual(client.status("alpha")["committed"]["epoch"], 1)
        store.close()

    def test_deferred_head_below_committed_is_purged(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=1,
                                       manifest=self.manifest)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 6)[4:])
        client.sync("alpha", max_pages=0)
        seed(self.server, "alpha", leaves_for("alpha", 10)[6:])
        client.sync("alpha", max_pages=0)  # records deferred 10
        self.assertEqual([d["tree_size"] for d in
                          client.status("alpha")["deferred_heads"]], [10])
        # Discard and go straight to 10; the deferred entry is purged.
        client.discard_candidate("alpha")
        res = client.sync("alpha")
        self.assertEqual(res.committed_size, 10)
        self.assertEqual(client.status("alpha")["deferred_heads"], [])
        store.close()

    def test_headers_of_deferred_entries_are_signed(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=1,
                                       manifest=self.manifest)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 6)[4:])
        client.sync("alpha", max_pages=0)
        seed(self.server, "alpha", leaves_for("alpha", 8)[6:])
        client.sync("alpha", max_pages=0)
        diag = client.diagnostics("alpha")
        frames = [bytes.fromhex(d["root"]) for d in diag["deferred_heads"]]
        import base64
        for d in diag["deferred_heads"]:
            frame = base64.b64decode(d["frame_b64"])
            head = protocol.parse_tree_head(frame, expected_source="alpha")
            self.assertEqual(head.root.hex(), d["root"])
            gen = store.get_generation("alpha", head.epoch)
            ok, why = protocol.verify_signature(gen.public_key,
                                                head.signed_bytes,
                                                head.signature)
            self.assertTrue(ok, why)
        store.close()


if __name__ == "__main__":
    unittest.main()
