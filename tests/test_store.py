"""Storage invariants, promotion atomicity and restart tests."""

import os
import unittest

from ctmirror import merkle
from ctmirror.store import ConflictError, Store, StoreCorrupt
from tests.harness import (admin, leaves_for, make_client, make_manifest,
                           reopen, seed, start_server, stop_server)


class StoreInvariantTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha", "beta"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir,
                               f"store-{self.id().split('.')[-1]}.sqlite")
        self.reset()
        self.manifest = make_manifest(self.server, ["alpha", "beta"])

    def reset(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def tearDown(self):
        stop_server(self.server)
        self.reset()

    def test_committed_count_matches_head_after_every_promotion(self):
        seed(self.server, "alpha", leaves_for("alpha", 7))
        store, client, _ = make_client(self.server, ["alpha", "beta"],
                                       db_path=self.db, page_size=3,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        size = client.status("alpha")["committed"]["tree_size"]
        self.assertEqual(size, 0)
        for _ in range(10):
            res = client.sync("alpha", max_pages=1)
            st = client.status("alpha")
            self.assertEqual(st["committed"]["entry_count"],
                             st["committed"]["tree_size"])
            self.assertEqual(st["committed"]["tree_size"],
                             len(list(client.entries(
                                 "alpha", 0, st["committed"]["tree_size"]))))
            if res.promoted:
                break
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 7)

    def test_no_visible_combination_of_new_head_and_old_leaves(self):
        """A reader must never see head size N with fewer than N rows."""
        seed(self.server, "alpha", leaves_for("alpha", 9))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        # While candidate is incomplete, committed head and rows agree.
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 0)
        self.assertEqual(st["committed"]["entry_count"], 0)
        self.assertEqual(st["candidate"]["tree_size"], 9)
        # Promotion is one transaction: inspect the store directly.
        with store.read_tx() as conn:
            heads = conn.execute(
                "SELECT COUNT(*) FROM heads").fetchone()[0]
            rows = conn.execute(
                "SELECT COUNT(*) FROM committed_leaves").fetchone()[0]
            self.assertEqual((heads, rows), (0, 0))
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        with store.read_tx() as conn:
            head_size = conn.execute(
                "SELECT tree_size FROM heads WHERE source_id='alpha'"
                " ORDER BY tree_size DESC LIMIT 1").fetchone()[0]
            rows = conn.execute(
                "SELECT COUNT(*) FROM committed_leaves"
                " WHERE source_id='alpha'").fetchone()[0]
            staged = conn.execute(
                "SELECT COUNT(*) FROM candidate_leaves").fetchone()[0]
            cands = conn.execute(
                "SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(head_size, 9)
        self.assertEqual(rows, 9)
        self.assertEqual((staged, cands), (0, 0))
        store.close()

    def test_promotion_refuses_from_corrupted_staging(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        self.assertEqual(client.status("alpha")["staged_count"], 2)
        # Corrupt a staged leaf's content (hash column unchanged): the root
        # recomputation must catch it and refuse to promote.
        with store.write_tx() as conn:
            conn.execute(
                "UPDATE candidate_leaves SET leaf=? WHERE source_id='alpha'"
                " AND leaf_index=0", (b"tampered",))
        with self.assertRaises((StoreCorrupt, RuntimeError)):
            client.sync("alpha")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 0)
        store.close()

    def test_promotion_refuses_with_hole_in_staging(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        with store.write_tx() as conn:
            conn.execute(
                "DELETE FROM candidate_leaves WHERE source_id='alpha'"
                " AND leaf_index=1")
        with self.assertRaises(StoreCorrupt):
            store.promote_candidate("alpha")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 0)
        store.close()

    def test_staged_conflict_rolls_back_page(self):
        seed(self.server, "alpha", leaves_for("alpha", 3))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        with self.assertRaises(ConflictError):
            store.stage_page("alpha", [(0, b"different", merkle.leaf_hash(
                b"different"))])
        self.assertEqual(store.staged_count("alpha"), 2)
        store.close()

    def test_status_is_a_consistent_snapshot(self):
        seed(self.server, "alpha", leaves_for("alpha", 6))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha")
        from ctmirror.store import StoreCorrupt as SC
        original = store.committed_snapshot

        def boom(_sid):
            raise SC("injected")

        # The status code path asserts head/count equality itself; a broken
        # store cannot produce a mixed view.
        store.committed_snapshot = boom
        with self.assertRaises(SC):
            client.status("alpha")
        store.committed_snapshot = original
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"],
                         st["committed"]["entry_count"])
        store.close()

    def test_diagnostics_do_not_leak_into_public_entries(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=1)
        diag = client.diagnostics("alpha", with_content=True)
        self.assertTrue(diag["staged"])
        with self.assertRaises(Exception):
            list(client.entries("alpha", 0, 5))
        store.close()


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "restart.sqlite")
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

    def test_staging_survives_restart_and_stays_bound(self):
        seed(self.server, "alpha", leaves_for("alpha", 6))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha", max_pages=2)
        st = client.status("alpha")
        self.assertEqual(st["candidate"]["tree_size"], 6)
        self.assertEqual(st["staged_count"], 4)
        root6 = st["candidate"]["root"]
        store.close()

        # Log grows while we are down.
        admin(self.server, "/admin/append", {"source_id": "alpha",
                                            "count": 1})
        store2, client2 = reopen(self.db, self.server, self.manifest,
                                 page_size=2)
        try:
            st = client2.status("alpha")
            self.assertEqual(st["committed"]["tree_size"], 0)
            self.assertEqual(st["candidate"]["tree_size"], 6)
            self.assertEqual(st["candidate"]["root"], root6)
            self.assertEqual(st["staged_count"], 4)
            self.assertEqual(st["gaps"], [[4, 6]])
            # The newer 7-leaf head cannot replace the bound candidate.
            res = client2.sync("alpha")
            self.assertEqual(res.candidate_size, 6)
            self.assertTrue(res.promoted)
            self.assertEqual(res.committed_size, 6)
            self.assertEqual([leaf for _, leaf in client2.entries(
                "alpha", 0, 6)], leaves_for("alpha", 6))
            # 7 only arrives in a separate, later round.
            res = client2.sync("alpha")
            self.assertEqual(res.committed_size, 7)
        finally:
            store2.close()

    def test_discard_then_restart_from_committed(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        client.sync("alpha", max_pages=1)
        self.assertEqual(client.status("alpha")["staged_count"], 2)
        out = client.discard_candidate("alpha")
        self.assertTrue(out["discarded"])
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertIsNone(st["candidate"])
        self.assertEqual(st["staged_count"], 0)
        store.close()

        store2, client2 = reopen(self.db, self.server, self.manifest,
                                 page_size=2)
        try:
            st = client2.status("alpha")
            self.assertIsNone(st["candidate"])
            self.assertEqual(st["committed"]["tree_size"], 5)
            res = client2.sync("alpha")
            self.assertEqual(res.committed_size, 9)
        finally:
            store2.close()

    def test_corrupt_committed_rows_detected(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client, _ = make_client(self.server, ["alpha"],
                                       db_path=self.db, page_size=2,
                                       manifest=self.manifest)
        client.sync("alpha")
        with store.write_tx() as conn:
            conn.execute("DELETE FROM committed_leaves WHERE source_id='alpha'"
                         " AND leaf_index=2")
        with self.assertRaises(StoreCorrupt):
            client.status("alpha")
        with self.assertRaises(StoreCorrupt):
            list(client.entries("alpha", 0, 4))
        store.close()


if __name__ == "__main__":
    unittest.main()
