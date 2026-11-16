"""Randomized drills with the invariant checked after *every* step.

The invariant is: for each source, the committed head size equals the number
of committed entries, the entries are exactly a prefix of the log's leaf
sequence, and the committed root equals a from-scratch Merkle computation.
This catches "head advanced but leaves missing" regressions anywhere in a
long, mixed sequence of appends, partial syncs, out-of-order pages and
restarts.
"""

import os
import random
import unittest

from ctmirror import merkle
from ctmirror.client import BlockedError, MirrorError
from tests.harness import (make_client, make_manifest, reopen, seed,
                           start_server, stop_server)


def check_invariant(client, source_id, expected):
    status = client.status(source_id)
    size = status["committed"]["tree_size"]
    count = status["committed"]["entry_count"]
    assert count == size, (source_id, count, size)
    entries = [leaf for _, leaf in client.entries(source_id, 0, size)]
    assert len(entries) == size, (source_id, len(entries), size)
    assert entries == expected[:size], (source_id, "content mismatch")
    assert merkle.tree_hash(entries).hex() == status["committed"]["root"], \
        (source_id, "root mismatch")
    if status["candidate"] is not None:
        cand = status["candidate"]
        assert cand["base_size"] == size
        assert cand["base_root"] == status["committed"]["root"]
        assert 0 <= cand["staged_count"] <= cand["tree_size"] - size
        gaps = status["gaps"]
        # Gaps + staged must cover exactly [size, candidate.size).
        covered = sum(b - a for a, b in gaps)
        assert covered + cand["staged_count"] == \
            cand["tree_size"] - size, (source_id, gaps, cand)
    else:
        assert status["gaps"] == []
        assert status["staged_count"] == 0
    return status


class RandomizedDrillTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha", "beta"])
        self.dir = os.path.join(
            os.path.dirname(__file__), "..", "testdata-tmp")
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "fuzz.sqlite")
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

    def test_random_sequence_keeps_invariant(self):
        rng = random.Random(20261116)
        logs = {"alpha": [], "beta": []}
        store, client, _ = make_client(
            self.server, ["alpha", "beta"], db_path=self.db, page_size=1,
            manifest=self.manifest)
        steps = 0
        while steps < 120:
            steps += 1
            sid = rng.choice(["alpha", "beta"])
            action = rng.choices(
                ["append", "sync", "page", "restart", "status"],
                weights=[3, 4, 2, 1, 1])[0]
            try:
                if action == "append":
                    count = rng.randint(1, 4)
                    new = [f"{sid}:leaf:{len(logs[sid]) + k}".encode()
                           for k in range(count)]
                    seed(self.server, sid, new)
                    logs[sid].extend(new)
                elif action == "sync":
                    client.sync(sid, max_pages=rng.choice([0, 1, 2, None]))
                elif action == "page":
                    st = client.status(sid)
                    if st["candidate"] is not None and st["gaps"]:
                        a, b = rng.choice(st["gaps"])
                        count = min(rng.randint(1, 3), b - a)
                        client.fetch_page(sid, a, count)
                elif action == "restart":
                    store.close()
                    store, client = reopen(self.db, self.server, self.manifest,
                                           page_size=1)
                elif action == "status":
                    pass
            except (BlockedError, MirrorError):
                # Faults are not injected in this drill; a rejection only
                # means transient transport/ordering issues.
                pass
            for source in ("alpha", "beta"):
                check_invariant(client, source, logs[source])
        # Drain everything and verify final equality.
        for _ in range(20):
            try:
                client.sync("alpha")
                client.sync("beta")
            except (BlockedError, MirrorError):
                break
            if (client.status("alpha")["committed"]["tree_size"] ==
                    len(logs["alpha"]) and
                    client.status("beta")["committed"]["tree_size"] ==
                    len(logs["beta"])):
                break
        for source in ("alpha", "beta"):
            st = check_invariant(client, source, logs[source])
            self.assertEqual(st["committed"]["tree_size"], len(logs[source]))
        store.close()

    def test_restart_at_every_point_preserves_binding(self):
        rng = random.Random(7)
        logs = {"alpha": []}
        store, client, _ = make_client(
            self.server, ["alpha"], db_path=self.db, page_size=1,
            manifest=self.manifest)
        for round_no in range(6):
            new = [f"alpha:leaf:{len(logs['alpha']) + k}".encode()
                   for k in range(rng.randint(1, 3))]
            seed(self.server, "alpha", new)
            logs["alpha"].extend(new)
            client.sync("alpha", max_pages=1)
            check_invariant(client, "alpha", logs["alpha"])
            store.close()
            store, client = reopen(self.db, self.server, self.manifest,
                                   page_size=1)
            st = check_invariant(client, "alpha", logs["alpha"])
            # Nothing may have arrived except via re-sync.
            self.assertEqual(st["committed"]["tree_size"] <=
                             len(logs["alpha"]), True)
        for _ in range(30):
            try:
                client.sync("alpha")
            except (BlockedError, MirrorError):
                break
            if client.status("alpha")["committed"]["tree_size"] == \
                    len(logs["alpha"]):
                break
        st = check_invariant(client, "alpha", logs["alpha"])
        self.assertEqual(st["committed"]["tree_size"], len(logs["alpha"]))
        store.close()

    def test_out_of_order_random_pages(self):
        rng = random.Random(99)
        leaves = [f"alpha:leaf:{i}".encode() for i in range(41)]
        seed(self.server, "alpha", leaves)
        store, client, _ = make_client(
            self.server, ["alpha"], db_path=self.db, page_size=3,
            manifest=self.manifest)
        client.sync("alpha", max_pages=0)
        # Fetch pages in a random order with random page sizes.
        for _ in range(200):
            st = client.status("alpha")
            if st["candidate"] is None:
                break
            gaps = st["gaps"]
            if not gaps:
                break
            a, b = rng.choice(gaps)
            count = min(rng.randint(1, 5), b - a)
            client.fetch_page("alpha", a, count)
            check_invariant(client, "alpha", leaves)
        res = client.sync("alpha")
        self.assertTrue(res.promoted)
        st = check_invariant(client, "alpha", leaves)
        self.assertEqual(st["committed"]["tree_size"], len(leaves))
        store.close()


if __name__ == "__main__":
    unittest.main()
