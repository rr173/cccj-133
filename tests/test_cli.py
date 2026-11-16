"""CLI end-to-end tests: the same binary a user would run.

Each test spawns the fixture server in-process, writes a manifest to a temp
directory, and invokes ``python -m ctmirror`` as a subprocess.  Exit codes
are part of the contract: 0 ok, 1 error, 2 blocked, 3 store corruption.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from ctmirror import merkle
from tests.harness import (admin, leaves_for, make_manifest, seed,
                           start_server, stop_server)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.server, self.state = start_server(["alpha", "beta"])
        self.tmp = tempfile.mkdtemp(prefix="ctmirror-cli-")
        self.db = os.path.join(self.tmp, "mirror.sqlite")
        self.manifest_path = os.path.join(self.tmp, "manifest.json")
        self.manifest = make_manifest(self.server, ["alpha", "beta"])
        payload = {"sources": [
            {"source_id": sid, "anchor_epoch": 0,
             "anchor_public_key": cfg.anchor_public_key.hex(),
             "certificates": []}
            for sid, cfg in self.manifest.sources.items()]}
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        stop_server(self.server)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, expect=0):
        runner = os.path.join(ROOT, "tests", "cli_runner.py")
        cmd = [sys.executable, runner,
               "--db", self.db, "--manifest", self.manifest_path,
               "--server", self.url, "--page-size", "2", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=self.tmp, timeout=120)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def json_out(self, *args, expect=0):
        proc = self.run_cli(*args, expect=expect)
        return json.loads(proc.stdout)

    def test_sync_status_entries_diagnostics(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        out = self.json_out("sync", "alpha")
        self.assertEqual(out["results"][0]["committed_size"], 5)

        status = self.json_out("status", "alpha")["sources"][0]
        self.assertEqual(status["committed"]["tree_size"], 5)
        self.assertEqual(status["committed"]["entry_count"], 5)
        self.assertIsNone(status["candidate"])

        proc = self.run_cli("entries", "alpha", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual([e["leaf_index"] for e in data["entries"]],
                         [0, 1, 2, 3, 4])
        self.assertEqual(
            [bytes.fromhex(__import__("base64").b64decode(
                e["leaf_b64"]).hex()) for e in data["entries"]],
            leaves_for("alpha", 5))

        # Raw mode writes index TAB leaf.
        proc = self.run_cli("entries", "alpha")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0].split("\t")[0], "0")

        diag = self.json_out("diagnostics", "alpha")["sources"][0]
        self.assertEqual(diag["staged"], [])
        self.assertIn("db", diag)
        self.assertGreater(diag["db"]["approx_bytes"], 0)

    def test_gap_and_recovery_via_cli(self):
        seed(self.server, "alpha", leaves_for("alpha", 9))
        self.json_out("sync", "alpha", "--max-pages", "0")
        status = self.json_out("status", "alpha")["sources"][0]
        self.assertEqual(status["committed"]["tree_size"], 0)
        self.assertEqual(status["candidate"]["tree_size"], 9)
        self.assertEqual(status["gaps"], [[0, 9]])
        # Page out of order through the CLI, then ask for gaps again.
        page = self.json_out("page", "alpha", "6", "2")
        self.assertEqual(page["gaps"], [[0, 6], [8, 9]])
        self.assertEqual(page["staged_count"], 2)
        # Public entries remain empty.
        proc = self.run_cli("entries", "alpha", "--json")
        self.assertEqual(json.loads(proc.stdout)["entries"], [])
        out = self.json_out("sync", "alpha")
        self.assertTrue(out["results"][0]["promoted"])
        self.assertEqual(out["results"][0]["committed_size"], 9)

    def test_blocked_exit_codes_and_resume(self):
        seed(self.server, "alpha", leaves_for("alpha", 5))
        self.json_out("sync", "alpha")
        seed(self.server, "alpha", leaves_for("alpha", 9)[5:])
        admin(self.server, "/admin/script", {"source_id": "alpha", "ops": [
            {"kind": "bad_proof"}]})
        proc = self.run_cli("sync", "alpha", "--strict", expect=2)
        err = json.loads(proc.stdout)
        self.assertEqual(err["error"], "blocked")
        self.assertEqual(err["reason"], "invalid_consistency_proof")
        status = self.json_out("status", "alpha")["sources"][0]
        self.assertTrue(status["blocked"])
        self.assertEqual(status["committed"]["tree_size"], 5)
        # Non-strict sync reports per-source state but exits 0.
        proc = self.run_cli("sync", "alpha")
        results = json.loads(proc.stdout)["results"]
        self.assertEqual(results[0]["state"], "blocked")
        # Resume then finish.
        self.json_out("resume", "alpha")
        out = self.json_out("sync", "alpha")
        self.assertEqual(out["results"][0]["committed_size"], 9)

    def test_discard_candidate_via_cli(self):
        seed(self.server, "alpha", leaves_for("alpha", 3))
        self.json_out("sync", "alpha")
        seed(self.server, "alpha", leaves_for("alpha", 6)[3:])
        self.json_out("sync", "alpha", "--max-pages", "1")
        status = self.json_out("status", "alpha")["sources"][0]
        self.assertEqual(status["staged_count"], 2)
        out = self.json_out("discard-candidate", "alpha")
        self.assertTrue(out["discarded"])
        status = self.json_out("status", "alpha")["sources"][0]
        self.assertIsNone(status["candidate"])
        self.assertEqual(status["committed"]["tree_size"], 3)
        out = self.json_out("sync", "alpha")
        self.assertEqual(out["results"][0]["committed_size"], 6)

    def test_multi_source_sync(self):
        seed(self.server, "alpha", leaves_for("alpha", 4))
        seed(self.server, "beta", leaves_for("beta", 6))
        out = self.json_out("sync")
        sizes = {r["source_id"]: r["committed_size"] for r in out["results"]}
        self.assertEqual(sizes, {"alpha": 4, "beta": 6})
        statuses = self.json_out("status")["sources"]
        by_id = {s["source_id"]: s for s in statuses}
        self.assertEqual(by_id["alpha"]["committed"]["tree_size"], 4)
        self.assertEqual(by_id["beta"]["committed"]["tree_size"], 6)
        self.assertNotEqual(by_id["alpha"]["committed"]["root"],
                            by_id["beta"]["committed"]["root"])

    def test_head_command_matches_committed_entries(self):
        seed(self.server, "alpha", leaves_for("alpha", 3))
        self.json_out("sync", "alpha")
        head = self.json_out("head", "alpha")
        import base64
        frame = base64.b64decode(head["head_b64"])
        from ctmirror import protocol
        parsed = protocol.parse_tree_head(frame, expected_source="alpha")
        self.assertEqual(parsed.tree_size, 3)
        self.assertEqual(parsed.root.hex(), head["root"])
        proc = self.run_cli("entries", "alpha", "--json")
        entries = json.loads(proc.stdout)["entries"]
        leaves = [base64.b64decode(e["leaf_b64"]) for e in entries]
        self.assertEqual(merkle.tree_hash(leaves), parsed.root)

    def test_unknown_source_and_bad_args(self):
        seed(self.server, "alpha", leaves_for("alpha", 2))
        self.run_cli("sync", "gamma", "--strict", expect=1)
        out = self.json_out("sync", "gamma", expect=0)
        self.assertEqual(out["results"][0]["state"], "error")
        # Unsynced source: a committed range check fails cleanly.
        proc = self.run_cli("entries", "alpha", "--start", "0",
                            "--end", "5", expect=1)
        self.assertEqual(json.loads(proc.stdout)["error"], "StoreError")
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
