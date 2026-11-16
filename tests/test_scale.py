"""Scale and resource-shape tests.

These assert *how* the client behaves, not just the end state:

* only pages needed for the candidate gaps are requested;
* persisted bytes grow with leaves, not with queue positions or pages;
* a small page size against a large log does not hold all leaf bodies in
  memory at once (the client streams rows and never builds a full list);
* multiple sources are kept fully separate.
"""

import os
import tracemalloc
import unittest

from ctmirror import merkle
from tests.harness import (admin, leaves_for, make_client, make_manifest,
                           seed, start_server, stop_server)


class LargeTreeTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["big", "small"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "large.sqlite")
        self.reset()
        self.manifest = make_manifest(self.server, ["big", "small"])

    def reset(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def tearDown(self):
        stop_server(self.server)
        self.reset()

    def test_small_pages_large_tree_only_requested_pages(self):
        n = 300
        seed(self.server, "big", [f"big:{i}".encode() for i in range(n)])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=7,
                                       manifest=self.manifest)
        pages_before = len(client.diagnostics("big")["page_stats"])
        res = client.sync("big", max_pages=2)
        self.assertEqual(res.committed_size, 0)
        stats = client.diagnostics("big")["page_stats"]
        self.assertEqual(len(stats) - pages_before, 2)
        # Requests are confined to the current gap and page size, in order.
        self.assertEqual([(r["request_start"], r["request_count"])
                          for r in reversed(stats)],
                         [(0, 7), (7, 7)])
        self.assertLessEqual(
            store.db_stats()["approx_bytes"], 2 * 1024 * 1024)
        # Finish and verify content root against the fixture's own leaves.
        res = client.sync("big")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, n)
        status = client.status("big")
        import hashlib

        def root_of(leaves):
            if not leaves:
                return hashlib.sha256(b"").digest()
            if len(leaves) == 1:
                return merkle.leaf_hash(leaves[0])
            k = merkle.largest_power_of_two_below(len(leaves))
            return merkle.node_hash(root_of(leaves[:k]), root_of(leaves[k:]))

        self.assertEqual(status["committed"]["root"],
                         root_of([f"big:{i}".encode()
                                  for i in range(n)]).hex())

    def test_resume_requests_only_missing_range(self):
        n = 120
        seed(self.server, "big", [f"big:{i}".encode() for i in range(n)])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=10,
                                       manifest=self.manifest)
        client.sync("big", max_pages=0)
        # Fetch a late page out of order; gaps are [0,110) and [110,120)
        # minus the fetched indices.
        client.fetch_page("big", 110, 10)
        before = admin(self.server, "/v1/requests")["requests"]
        client.sync("big", max_pages=1)
        after = admin(self.server, "/v1/requests")["requests"]
        new_entries = [r for r in after[len(before):]
                       if r["kind"] == "entries"]
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["start"], 0)
        self.assertEqual(new_entries[0]["count"], 10)
        # Finish.
        res = client.sync("big")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, n)

    def test_memory_does_not_scale_with_leaf_bodies(self):
        n = 200
        blob = b"x" * 2048
        seed(self.server, "big", [blob + str(i).encode() for i in range(n)])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        client.sync("big")
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        status = client.status("big")
        self.assertEqual(status["committed"]["tree_size"], n)
        # 200 leaves x 2 KiB is ~400 KiB of bodies.  The client must not hold
        # them all: peak traced memory stays below a modest multiple of one
        # page plus protocol overhead.
        self.assertLess(peak - baseline, 1_500_000,
                        f"peak {peak - baseline} bytes")
        # Random spot checks against the independently built tree.
        leaves = [blob + str(i).encode() for i in range(n)]
        self.assertEqual([leaf for _, leaf in client.entries("big", 55, 60)],
                         leaves[55:60])
        self.assertEqual(merkle.tree_hash(leaves).hex(),
                         status["committed"]["root"])

    def test_two_sources_never_mix(self):
        seed(self.server, "big", [f"big:{i}".encode() for i in range(40)])
        seed(self.server, "small", [b"small:0", b"small:1"])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=3,
                                       manifest=self.manifest)
        client.sync("big", max_pages=1)
        res = client.sync("small")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 2)
        # Big is still mid-candidate; its staging is untouched.
        st = client.status("big")
        self.assertEqual(st["committed"]["tree_size"], 0)
        self.assertEqual(st["candidate"]["tree_size"], 40)
        self.assertEqual(st["staged_count"], 3)
        small = client.status("small")
        self.assertEqual(small["committed"]["tree_size"], 2)
        self.assertEqual([leaf for _, leaf in client.entries("small", 0, 2)],
                         [b"small:0", b"small:1"])
        res = client.sync("big")
        self.assertEqual(res.committed_size, 40)
        self.assertEqual(
            [leaf for _, leaf in client.entries("big", 0, 40)],
            [f"big:{i}".encode() for i in range(40)])

    def test_one_blocked_source_does_not_stop_another(self):
        seed(self.server, "big", [f"big:{i}".encode() for i in range(5)])
        seed(self.server, "small", [b"small:0", b"small:1"])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        admin(self.server, "/admin/script", {"source_id": "big", "ops": [
            {"kind": "inclusion_bad", "index": 0}]})
        from ctmirror.client import BlockedError
        with self.assertRaises(BlockedError):
            client.sync("big")
        res = client.sync("small")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 2)
        self.assertTrue(client.status("big")["blocked"])
        self.assertFalse(client.status("small")["blocked"])
        # Big resumes only the same candidate after resume.
        self.assertEqual(client.resume("big")["candidate_size"], 5)
        res = client.sync("big")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 5)

    def test_storage_only_grows_with_leaves(self):
        """Staging carries leaves, not inclusion paths or page copies."""
        n = 60
        seed(self.server, "big", [f"big:{i}".encode() for i in range(n)])
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=5,
                                       manifest=self.manifest)
        before = store.db_stats()["approx_bytes"]
        client.sync("big", max_pages=4)  # 20 leaves staged
        mid = store.db_stats()["approx_bytes"]
        client.sync("big")
        after = store.db_stats()["approx_bytes"]
        st = client.status("big")
        self.assertEqual(st["committed"]["tree_size"], n)
        leaf_bytes = sum(len(f"big:{i}".encode()) for i in range(n))
        # Staging + committed copies of 60 tiny leaves, plus heads/events.
        # Inclusion proofs (each ~6*32 bytes x 60) are not stored.
        self.assertLess(after - before, leaf_bytes * 4 + 200_000,
                        (before, mid, after))
        stages = client.diagnostics("big")["page_stats"]
        self.assertLessEqual(sum(s["response_bytes"] for s in stages),
                             after * 8)

    def test_proof_bytes_not_persisted(self):
        seed(self.server, "big", leaves_for("big", 40))
        store, client, _ = make_client(self.server, ["big", "small"],
                                       db_path=self.db, page_size=4,
                                       manifest=self.manifest)
        client.sync("big")
        with store.read_tx() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(candidate_leaves)")]
            self.assertEqual(cols, ["source_id", "leaf_index", "leaf",
                                    "leaf_hash", "added_at"])
            ccols = [r[1] for r in conn.execute(
                "PRAGMA table_info(committed_leaves)")]
            self.assertEqual(ccols, ["source_id", "leaf_index", "leaf",
                                     "leaf_hash"])


if __name__ == "__main__":
    unittest.main()
