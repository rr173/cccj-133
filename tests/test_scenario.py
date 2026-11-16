"""End-to-end scenario tests against the local fixture log.

These mirror the acceptance script in the task statement:

1. 5-leaf tree committed; grow to 9, download 5 and 6 only.
2. The log switches to 10 leaves; a 10-leaf proof must not complete the
   9-leaf candidate.
3. Bad proof for 7, page for 8 arrives, process restart: committed stays 5,
   candidate stays 9, diagnostics show 7 missing, 8 is not served.
4. Fix 7, commit to 9 in one promotion; content matches an independent
   reconstruction.
5. Rotate keys with activation_size 9: unusable while committed == 5,
   usable once 9 is reached via the old-key-signed certificate.
"""

import base64
import hashlib
import os
import unittest

from ctmirror import merkle
from ctmirror.client import (BlockedError, HttpTransport, MirrorClient,
                             register_manifest)
from ctmirror.store import Store
from tests.harness import (admin, leaves_for, make_client, make_manifest,
                           reopen, seed, start_server, stop_server)


def assert_committed_matches(client, source_id, expected_leaves):
    """Every step asserts committed entry count == committed head size."""
    status = client.status(source_id)
    self_size = status["committed"]["tree_size"]
    count = status["committed"]["entry_count"]
    assert count == self_size, (count, self_size)
    entries = list(client.entries(source_id, 0, self_size))
    assert [leaf for _, leaf in entries] == expected_leaves[:self_size]
    # Recompute the root from committed bytes independently.
    root = merkle.tree_hash([leaf for _, leaf in entries])
    assert root.hex() == status["committed"]["root"], "root mismatch"
    return status


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha", "beta"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, f"{self.id().split('.')[-1]}.sqlite")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass
        self.manifest = make_manifest(self.server, ["alpha", "beta"])

    def tearDown(self):
        stop_server(self.server)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def reset_db(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def client(self, page_size=2):
        store, client, _ = make_client(
            self.server, ["alpha", "beta"], db_path=self.db,
            page_size=page_size, manifest=self.manifest)
        return store, client

    # ------------------------------------------------------------------
    def test_acceptance_flow(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client(page_size=2)
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 5)
        status = assert_committed_matches(client, "alpha",
                                          leaves_for("alpha", 5))
        self.assertEqual(status["committed"]["tree_size"], 5)
        self.assertEqual(status["committed"]["entry_count"], 5)

        # Grow to 9 and stop early: a candidate is selected but not promoted.
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        res = client.sync("alpha", max_pages=1)
        self.assertEqual(res.state, "candidate_pending")
        self.assertEqual(res.committed_size, 5)
        self.assertEqual(res.candidate_size, 9)
        status = client.status("alpha")
        self.assertEqual(status["committed"]["tree_size"], 5)
        self.assertEqual(status["candidate"]["tree_size"], 9)
        self.assertEqual(status["candidate"]["base_size"], 5)
        self.assertEqual(status["candidate"]["staged_count"], 2)
        self.assertEqual(status["gaps"], [[7, 9]])
        # Diagnostics can see staged entries, public entries cannot.
        diag = client.diagnostics("alpha", with_content=True)
        self.assertEqual([s["leaf_index"] for s in diag["staged"]], [5, 6])
        self.assertEqual(diag["staged"][0]["leaf_b64"],
                         base64.b64encode(b"alpha:leaf:5").decode())
        with self.assertRaises(Exception):
            list(client.entries("alpha", 5, 6))

        # Now the log moves on to 10 leaves.  The 9-leaf candidate is bound to
        # its old target; the new head must not be mixed into it.  The next
        # page request makes the log answer with an updated (10-leaf) head.
        seed(self.server, "alpha", leaves_for("alpha", 10)[9:])
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "updated_head", "append": 1}]})
        res = client.sync("alpha", max_pages=1)
        self.assertEqual(res.candidate_size, 9)
        # Two distinct heads were observed and deferred: the 10-leaf head at
        # round start and the 11-leaf head served instead of page 7.
        self.assertEqual(sorted(d["tree_size"]
                                for d in res.deferred_heads), [10, 11])
        self.assertEqual(sorted(d["tree_size"] for d in
                                client.status("alpha")["deferred_heads"]),
                         [10, 11])
        st = client.status("alpha")
        self.assertEqual(st["candidate"]["tree_size"], 9)
        self.assertEqual(st["candidate"]["staged_count"], 2)
        self.assertEqual(st["gaps"], [[7, 9]])

        # Inject a bad inclusion proof for index 7 on the next page; pages
        # are 2 wide starting at 7 (7 and 8 in one page) so index 8 is
        # otherwise valid but must be rejected with the whole page.
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "inclusion_bad", "index": 7}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertEqual(ctx.exception.reason, "invalid_page")
        status = client.status("alpha")
        self.assertTrue(status["blocked"])
        self.assertEqual(status["committed"]["tree_size"], 5)
        self.assertEqual(status["candidate"]["tree_size"], 9)
        # Whole page rejected: neither 7 nor 8 staged, earlier pages kept.
        diag = client.diagnostics("alpha")
        self.assertEqual([s["leaf_index"] for s in diag["staged"]], [5, 6])
        self.assertEqual(diag["missing"], [[7, 9]])
        # Downstream still sees exactly the 5 committed entries.
        self.assertEqual(len(list(client.entries("alpha", 0, 5))), 5)
        with self.assertRaises(Exception):
            list(client.entries("alpha", 0, 9))
        store.close()

        # ---- restart -----------------------------------------------------
        store2, client2 = reopen(self.db, self.server, self.manifest,
                                 page_size=2)
        try:
            status = client2.status("alpha")
            self.assertEqual(status["committed"]["tree_size"], 5)
            self.assertEqual(status["candidate"]["tree_size"], 9)
            self.assertTrue(status["blocked"])
            self.assertEqual(status["gaps"], [[7, 9]])
            self.assertEqual(status["staged_count"], 2)
            self.assertNotIn(9, [s["leaf_index"] for s in
                                 client2.diagnostics("alpha")["staged"]])
            # Resume continues the same candidate, not a new target.
            r = client2.resume("alpha")
            self.assertTrue(r["changed"])
            self.assertEqual(r["candidate_size"], 9)
            self.assertFalse(client2.status("alpha")["blocked"])
            # Even though the log now has 11 leaves, resume still targets 9
            # and fetches only the missing indices, not the newer pages.
            res = client2.sync("alpha", max_pages=0)
            self.assertEqual(res.candidate_size, 9)
            self.assertEqual(res.committed_size, 5)
            self.assertEqual(res.pages_fetched, 0)
            status = client2.status("alpha")
            self.assertEqual(status["committed"]["tree_size"], 5)
            self.assertEqual(status["gaps"], [[7, 9]])
            self.assertEqual(status["staged_count"], 2)
            self.assertEqual(status["candidate"]["root"],
                             merkle.tree_hash(leaves_for("alpha", 9)).hex())
            store2.close()
        except AssertionError:
            store2.close()
            raise

        # ---- commit to 9 -------------------------------------------------
        store3, client3 = reopen(self.db, self.server, self.manifest,
                                 page_size=2)
        try:
            res = client3.sync("alpha")
            self.assertTrue(res.promoted)
            self.assertEqual(res.committed_size, 9)
            status = assert_committed_matches(
                client3, "alpha", leaves_for("alpha", 9))
            self.assertEqual(status["candidate"], None)
            self.assertFalse(status["blocked"])
            # The newer (11-leaf) head was only a deferred observation.
            self.assertEqual(status["committed"]["tree_size"], 9)
            # Independent reconstruction from the fixture's own leaf bytes.
            got = [leaf for _, leaf in client3.entries("alpha", 0, 9)]
            self.assertEqual(got, leaves_for("alpha", 9))
            self.assertEqual(merkle.tree_hash(got).hex(),
                             status["committed"]["root"])
            # Next sync may now adopt the newest (11-leaf) tree.
            res = client3.sync("alpha")
            self.assertEqual(res.committed_size, 11)
            got = [leaf for _, leaf in client3.entries("alpha", 0, 11)]
            self.assertEqual(got[:9], leaves_for("alpha", 9))
            self.assertEqual(got[9:],
                             [b"alpha:leaf:9"] + [b"alpha:late:10"])
            status = client3.status("alpha")
            self.assertEqual(status["committed"]["entry_count"], 11)
            self.assertEqual(merkle.tree_hash(got).hex(),
                             status["committed"]["root"])
        finally:
            store3.close()

    # ------------------------------------------------------------------
    def test_no_advance_on_head_alone(self):
        seed(self.server, "alpha", leaves_for("alpha", 3))
        store, client = self.client(page_size=2)
        res = client.sync("alpha", max_pages=0)
        self.assertEqual(res.state, "candidate_pending")
        status = client.status("alpha")
        self.assertEqual(status["committed"]["tree_size"], 0)
        self.assertEqual(status["candidate"]["tree_size"], 3)
        self.assertEqual(status["gaps"], [[0, 3]])
        self.assertEqual(list(client.entries("alpha", 0, 0)), [])
        # Re-running with no new pages still cannot promote.
        res = client.sync("alpha", max_pages=0)
        self.assertEqual(res.committed_size, 0)
        store.close()

    def test_last_page_alone_does_not_promote(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client(page_size=2)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        client.sync("alpha", max_pages=0)
        # Fetch the final page only; indices 5..6 are still missing.
        page = client.fetch_page("alpha", 8, 2)
        self.assertTrue(page["progressed"])
        self.assertEqual(page["staged_count"], 1)
        self.assertEqual(page["gaps"], [[5, 8]])
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        # One later page alone never promotes; the earlier gap remains.
        self.assertEqual(st["gaps"], [[5, 8]])
        res = client.sync("alpha", max_pages=1)
        self.assertEqual(res.state, "candidate_pending")
        self.assertEqual(res.gaps, [[7, 8]])
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 9)
        self.assertEqual([leaf for _, leaf in client.entries("alpha", 5, 9)],
                         leaves_for("alpha", 9)[5:])
        store.close()

    def test_out_of_order_pages_and_gap_listing(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client = self.client(page_size=1)
        client.sync("alpha", max_pages=0)
        client.fetch_page("alpha", 2, 1)
        client.fetch_page("alpha", 0, 1)
        st = client.status("alpha")
        self.assertEqual(st["gaps"], [[1, 2], [3, 4]])
        self.assertEqual(st["candidate"]["staged_count"], 2)
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 4)
        store.close()

    def test_duplicate_identical_folds_conflicting_conflicts(self):
        seed(self.server, "alpha", leaves_for("alpha", 3))
        seed(self.server, "beta", leaves_for("beta", 3))
        store, client = self.client(page_size=2)
        client.sync("alpha")
        admin(self.server, "/admin/script", {"source_id": "beta", "ops": [
            {"kind": "duplicate_index", "at": 0}]})
        res = client.sync("beta")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 3)
        store.close()

        # fresh db for the conflict half of the test
        self.reset_db()
        store, client = self.client(page_size=2)
        client.sync("alpha")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 3)
        # Take alpha to 6 and stage index 3 only, then re-fetch the same page
        # with a conflicting duplicate for index 3.
        seed(self.server, "alpha", leaves_for("alpha", 6)[3:])
        client.sync("alpha", max_pages=0)
        res = client.sync("alpha", max_pages=1)
        self.assertEqual(res.state, "candidate_pending")
        self.assertEqual(client.status("alpha")["staged_count"], 2)
        # Re-fetch the same page region with a conflicting duplicate for the
        # already staged index 3: a conflict, not a silent fold.
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "conflicting_duplicate", "at": 0}]})
        with self.assertRaises(BlockedError) as ctx:
            client.fetch_page("alpha", 3, 2)
        self.assertEqual(ctx.exception.reason, "leaf_conflict")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 3)
        self.assertEqual(st["candidate"]["tree_size"], 6)
        self.assertEqual(st["gaps"], [[5, 6]])
        self.assertEqual(st["staged_count"], 2)
        store.close()

    def test_missing_indices_do_not_count_as_end(self):
        seed(self.server, "alpha", leaves_for("alpha", 6))
        # page_size 2 so the omitted index 1 does not drag index 3 out of the
        # same page: pages are [0,1], [2,3], [4,5].
        store, client = self.client(page_size=2)
        # The server persistently omits index 1 from whatever page covers it.
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "drop_index_always", "index": 1}]})
        res = client.sync("alpha")
        self.assertEqual(res.state, "candidate_pending", res.as_dict())
        self.assertEqual(res.committed_size, 0)
        # The short page at index 1 is not end-of-stream: the remaining
        # indices 2..5 are still fetched, and only index 1 stays missing.
        # Index 1 is not re-requested in the same round.
        self.assertEqual(res.gaps, [[1, 2]])
        self.assertEqual(res.pages_fetched, 4)
        self.assertEqual(client.status("alpha")["staged_count"], 5)
        # Re-requesting index 1 yields an empty page.  Two empty pages in a
        # row must not be treated as end-of-stream; sync stops with the gap
        # still reported and nothing promoted.
        res = client.sync("alpha")
        self.assertEqual(res.committed_size, 0)
        self.assertEqual(res.candidate_size, 6)
        self.assertEqual(res.gaps, [[1, 2]])
        self.assertEqual(res.state, "candidate_pending")
        self.assertFalse(res.promoted)
        st = client.status("alpha")
        self.assertEqual(st["gaps"], [[1, 2]])
        self.assertEqual(st["committed"]["tree_size"], 0)
        # Every other index arrived: only 1 is missing.
        self.assertEqual(st["staged_count"], 5)
        # Diagnostics list the missing interval explicitly.
        self.assertEqual(client.diagnostics("alpha")["missing"], [[1, 2]])
        # Clear the fault; now the missing index is served and promotes.
        admin(self.server, "/admin/clear", {"source_id": "alpha"})
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 6)
        store.close()

    def test_equivocation_and_replay(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client = self.client()
        client.sync("alpha")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 4)

        # Same size, different root -> equivocation, response preserved.
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "equivocation", "tree_size": 4}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertEqual(ctx.exception.reason, "equivocation")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 4)
        self.assertEqual(st["committed"]["root"],
                         merkle.tree_hash(leaves_for("alpha", 4)).hex())
        obs = client.diagnostics("alpha")["observations"]
        self.assertTrue(any(o["kind"] == "equivocation_head" for o in obs))
        self.assertTrue(any(o["frame_b64"] for o in obs))
        store.close()

        self.reset_db()
        # Replay: a genuinely signed head at a smaller size.  The log now has
        # 10 leaves, so it can sign size 9 as well as 10.
        admin(self.server, "/admin/append", {"source_id": "alpha",
                                            "count": 6})
        store, client = self.client()
        first = client.sync("alpha")
        self.assertEqual(first.committed_size, 10)
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 0, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertEqual(ctx.exception.reason, "replay")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 10)
        # The replay observation keeps the raw frame for later analysis.
        obs = client.diagnostics("alpha")["observations"]
        self.assertTrue(any(o["kind"] == "replay_head" and o["frame_b64"]
                            for o in obs))
        store.close()

    def test_bad_consistency_blocks_then_resume(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client()
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "bad_proof"}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertEqual(ctx.exception.reason, "invalid_consistency_proof")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertTrue(st["blocked"])
        # Resume retries the same target; the fault was one-shot.
        client.resume("alpha")
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        self.assertEqual(res.committed_size, 9)
        store.close()

    def test_short_and_extra_consistency_nodes_are_rejected(self):
        self.reset_db()
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client()
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        for kind in ("short_proof", "extra_proof", "wrong_old_root"):
            server, _ = start_server(["alpha"])
            try:
                seed(server, "alpha", leaves_for("alpha", 5))
                manifest = make_manifest(server, ["alpha"])
                db = self.db + ".cons"
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(db + suffix)
                    except FileNotFoundError:
                        pass
                store, client, _ = make_client(
                    server, ["alpha"], db_path=db, manifest=manifest)
                client.sync("alpha")
                self.assertEqual(
                    client.status("alpha")["committed"]["tree_size"], 5)
                seed(server, "alpha", leaves_for("alpha", 9)[5:])
                admin(server, "/admin/script", {"source_id": "alpha",
                                                "ops": [{"kind": kind}]})
                with self.assertRaises(BlockedError, msg=kind) as ctx:
                    client.sync("alpha")
                self.assertEqual(
                    ctx.exception.reason, "invalid_consistency_proof", kind)
                self.assertEqual(
                    client.status("alpha")["committed"]["tree_size"], 5)
                store.close()
            finally:
                stop_server(server)

    def test_proof_for_other_tree_size_rejected(self):
        """A 10-leaf proof must never complete a 9-leaf candidate."""
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client()
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 10)[5:])
        # Pin a 9-leaf head on the head endpoint while the consistency
        # response proves a 10-leaf tree.  The client must refuse to bind
        # staging to a target that the proof does not match.
        frame9 = admin(self.server, "/admin/head",
                       {"source_id": "alpha", "epoch": 0, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame9["head_b64"]},
            {"kind": "prove_other_size", "tree_size": 10},
        ]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertIn(ctx.exception.reason, ("invalid_consistency_proof",
                                             "untrusted"))
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertIsNone(st["candidate"])
        # Nothing from the 10-leaf tree was staged.
        self.assertEqual(st["staged_count"], 0)
        self.assertEqual(len(list(client.entries("alpha", 0, 5))), 5)
        store.close()

        # A consistency response carrying a different head frame is also a
        # hard failure (checked independently of the head endpoint).
        server, _ = start_server(["alpha"])
        try:
            seed(server, "alpha", leaves_for("alpha", 5))
            manifest = make_manifest(server, ["alpha"])
            db = self.db + ".target"
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db + suffix)
                except FileNotFoundError:
                    pass
            store, client, _ = make_client(
                server, ["alpha"], db_path=db, manifest=manifest)
            client.sync("alpha")
            seed(server, "alpha", leaves_for("alpha", 10)[5:])
            # The head fetch says 10; the consistency answer claims to prove
            # a 6-leaf tree instead.  The sizes disagree, so it is rejected.
            admin(server, "/admin/script", {"source_id": "alpha", "ops": [
                {"kind": "prove_other_size", "tree_size": 6}]})
            with self.assertRaises(BlockedError) as ctx:
                client.sync("alpha")
            self.assertEqual(ctx.exception.reason, "invalid_consistency_proof")
            st = client.status("alpha")
            self.assertEqual(st["committed"]["tree_size"], 5)
            self.assertIsNone(st["candidate"])
            self.assertEqual(st["staged_count"], 0)
            store.close()
        finally:
            stop_server(server)

    def test_stale_page_for_older_target_rejected(self):
        """A page naming an older tree size must not be used for the target."""
        seed(self.server, "alpha", leaves_for("alpha", 4))
        store, client = self.client(page_size=2)
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 8)[4:])
        client.sync("alpha", max_pages=0)
        self.assertEqual(client.status("alpha")["candidate"]["tree_size"], 8)
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "older_tree_size"}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("alpha")
        self.assertEqual(ctx.exception.reason, "invalid_page")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 4)
        self.assertEqual(st["candidate"]["tree_size"], 8)
        self.assertEqual(st["gaps"], [[4, 8]])
        self.assertEqual(st["staged_count"], 0)
        store.close()

    def test_inclusion_faults(self):
        cases = [
            "inclusion_short", "inclusion_extra", "inclusion_bad",
            "wrong_tree_size", "older_tree_size", "wrong_root",
            "out_of_range",
            "committed_index", "foreign_leaf",
        ]
        for kind in cases:
            with self.subTest(kind=kind):
                # Fresh server and store per fault so no state leaks between
                # subtests.
                server, _ = start_server(["alpha", "beta"])
                try:
                    seed(server, "alpha", leaves_for("alpha", 3))
                    seed(server, "beta", leaves_for("beta", 3))
                    manifest = make_manifest(server, ["alpha", "beta"])
                    db = self.db + ".fault"
                    for suffix in ("", "-wal", "-shm"):
                        try:
                            os.unlink(db + suffix)
                        except FileNotFoundError:
                            pass
                    store, client, _ = make_client(
                        server, ["alpha", "beta"], db_path=db, page_size=2,
                        manifest=manifest)
                    client.sync("alpha")
                    admin(server, "/admin/script", {"source_id": "beta",
                                                    "ops": [{"kind": kind}]})
                    with self.assertRaises(BlockedError, msg=kind) as ctx:
                        client.sync("beta")
                    # An out-of-range index or a repeated index with other
                    # bytes can be diagnosed as a conflict; both are page
                    # rejections that stage nothing.
                    self.assertIn(ctx.exception.reason,
                                  ("invalid_page", "leaf_conflict"), kind)
                    st = client.status("beta")
                    self.assertEqual(st["committed"]["tree_size"], 0)
                    self.assertEqual(st["gaps"], [[0, 3]])
                    store.close()
                finally:
                    stop_server(server)
                    for suffix in ("", "-wal", "-shm"):
                        try:
                            os.unlink(db + suffix)
                        except FileNotFoundError:
                            pass

    def test_bad_entry_rejects_whole_page_keeps_prior(self):
        seed(self.server, "alpha", leaves_for("alpha", 6))
        store, client = self.client(page_size=2)
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "inclusion_bad", "index": 3}]})
        with self.assertRaises(BlockedError):
            client.sync("alpha")
        diag = client.diagnostics("alpha")
        self.assertEqual([s["leaf_index"] for s in diag["staged"]], [0, 1])
        self.assertEqual(diag["missing"], [[2, 6]])
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 0)
        store.close()

    def test_same_leaf_bytes_in_two_sources_are_independent(self):
        """Identical leaf bytes in two sources remain separate entries."""
        shared = b"shared-payload"
        seed(self.server, "alpha", [shared, b"a1", b"a2"])
        seed(self.server, "beta", [shared, b"b1"])
        store, client = self.client(page_size=2)
        self.assertTrue(client.sync("alpha").promoted)
        self.assertTrue(client.sync("beta").promoted)
        a = client.status("alpha")
        b = client.status("beta")
        self.assertEqual(a["committed"]["tree_size"], 3)
        self.assertEqual(b["committed"]["tree_size"], 2)
        self.assertNotEqual(a["committed"]["root"], b["committed"]["root"])
        self.assertEqual(list(client.entries("alpha", 0, 1))[0][1], shared)
        self.assertEqual(list(client.entries("beta", 0, 1))[0][1], shared)
        self.assertEqual([leaf for _, leaf in client.entries("alpha", 0, 3)],
                         [shared, b"a1", b"a2"])
        self.assertEqual([leaf for _, leaf in client.entries("beta", 0, 2)],
                         [shared, b"b1"])
        # The same bytes hash to the same leaf hash, but roots differ because
        # the trees differ; a leaf from one source never satisfies the other.
        self.assertEqual(merkle.leaf_hash(shared),
                         merkle.leaf_hash(b"shared-payload"))
        store.close()

    def test_cross_source_leaf_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        seed(self.server, "beta", leaves_for("beta", 4))
        store, client = self.client(page_size=2)
        client.sync("alpha")
        admin(self.server, "/admin/script", {"source_id": "beta", "ops": [
            {"kind": "foreign_leaf"}]})
        with self.assertRaises(BlockedError) as ctx:
            client.sync("beta")
        self.assertEqual(ctx.exception.reason, "invalid_page")
        store.close()

    def test_bad_head_signature_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        store, client = self.client()
        client.sync("alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "bad_head_signature"}]})
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertIn("signature", str(ctx.exception).lower())
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertIsNone(st["candidate"])
        self.assertEqual(st["staged_count"], 0)
        # The invalid head is preserved in diagnostics.
        obs = client.diagnostics("alpha")["observations"]
        self.assertTrue(any(o["kind"] == "untrusted_head" and o["frame_b64"]
                            for o in obs))
        store.close()

    def test_unknown_source_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 2))
        store, client = self.client()
        with self.assertRaises(Exception):
            client.sync("gamma")
        store.close()

    def test_manifest_tamper_is_rejected(self):
        from ctmirror.client import Manifest, SourceConfig
        seed(self.server, "alpha", leaves_for("alpha", 2))
        bad = Manifest({"alpha": SourceConfig(
            "alpha", 0, hashlib.sha256(b"nope").digest(), ())})
        store = Store(self.db)
        register_manifest(store, bad)
        client = MirrorClient(
            store, HttpTransport(f"http://127.0.0.1:{self.server.server_port}"),
            bad)
        with self.assertRaises(Exception):
            client.sync("alpha")
        store.close()


class KeyRotationTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "rotation.sqlite")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass
        self.manifest = make_manifest(self.server, ["alpha"])

    def tearDown(self):
        stop_server(self.server)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def client(self):
        store, client, _ = make_client(
            self.server, ["alpha"], db_path=self.db, page_size=2,
            manifest=self.manifest)
        return store, client

    def test_activation_size_gates_new_key(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        rot = admin(self.server, "/admin/rotate",
                    {"source_id": "alpha", "activation_size": 9})
        self.assertEqual(rot["old_epoch"], 0)
        self.assertEqual(rot["new_epoch"], 1)
        store, client = self.client()
        client.sync("alpha")
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertEqual(st["active_epoch"], 0)
        self.assertEqual(st["generations"][0]["epoch"], 0)
        store.close()

        # A new-epoch head must not advance us while committed < 9, even
        # though its signature is valid under the certified successor key.
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 1, "tree_size": 5})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        store, client = self.client()
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertIn("activation", str(ctx.exception).lower())
        st = client.status("alpha")
        self.assertEqual(st["committed"]["tree_size"], 5)
        self.assertEqual(st["active_epoch"], 0)
        store.close()

        # The client registered and verified the successor key material
        # (cert signed by the old key) from the first run, but it is not yet
        # active.
        store, client = self.client()
        st = client.status("alpha")
        gens = {g["epoch"]: g for g in st["generations"]}
        self.assertIn(1, gens)
        self.assertTrue(gens[1]["verified"])
        self.assertFalse(gens[1]["activated"])
        self.assertEqual(gens[1]["activation_size"], 9)

        # Reaching committed size 9 through the old key activates epoch 1.
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        res = client.sync("alpha")
        self.assertEqual(res.committed_size, 9)
        st = client.status("alpha")
        self.assertEqual(st["active_epoch"], 1)
        self.assertTrue({g["epoch"]: g for g in st["generations"]}[1]
                        ["activated"])

        # Now a new-epoch head can be adopted (size >= activation_size).
        seed(self.server, "alpha", leaves_for("alpha", 10)[9:])
        res = client.sync("alpha")
        self.assertEqual(res.committed_size, 10)
        st = client.status("alpha")
        self.assertEqual(st["committed"]["epoch"], 1)
        self.assertEqual(st["committed"]["entry_count"], 10)
        store.close()

    def test_self_signed_successor_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 9))
        admin(self.server, "/admin/rotate",
              {"source_id": "alpha", "activation_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "self_signed"}]})
        store, client = self.client()
        # Serve the new-epoch head so the client fetches key material.
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 1, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertIn("previous key", str(ctx.exception))
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 0)
        gens = {g["epoch"]: g for g in client.status("alpha")["generations"]}
        self.assertNotIn(1, gens, "rogue key must not be persisted")
        store.close()

    def test_generation_jump_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 9))
        admin(self.server, "/admin/rotate",
              {"source_id": "alpha", "activation_size": 6})
        admin(self.server, "/admin/rotate",
              {"source_id": "alpha", "activation_size": 7})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "jump", "epoch": 1}]})
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 2, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        store, client = self.client()
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertIn("jump", str(ctx.exception).lower())
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 0)
        store.close()

    def test_wrong_successor_key_rejected(self):
        seed(self.server, "alpha", leaves_for("alpha", 9))
        admin(self.server, "/admin/rotate",
              {"source_id": "alpha", "activation_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "wrong_key"}]})
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 1, "tree_size": 9})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        store, client = self.client()
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertEqual(client.status("alpha")["committed"]["tree_size"], 0)
        self.assertNotIn(1, {g["epoch"] for g in
                             client.status("alpha")["generations"]})
        store.close()

    def test_new_key_head_below_activation_rejected(self):
        """A new-epoch head for a tree smaller than activation_size fails."""
        seed(self.server, "alpha", leaves_for("alpha", 9))
        admin(self.server, "/admin/rotate",
              {"source_id": "alpha", "activation_size": 9})
        frame = admin(self.server, "/admin/head",
                      {"source_id": "alpha", "epoch": 1, "tree_size": 8})
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "head_override", "head_b64": frame["head_b64"]}]})
        store, client = self.client()
        with self.assertRaises(Exception) as ctx:
            client.sync("alpha")
        self.assertIn("activation", str(ctx.exception).lower())
        store.close()


if __name__ == "__main__":
    unittest.main()
