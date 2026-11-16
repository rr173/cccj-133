# CTM1 offline mirror client

An offline drill client for a CT-style append-only log using the **CTM1**
mirror convention. It verifies Ed25519 signed tree heads, RFC 9162 Merkle
consistency and inclusion proofs, key-generation certificates, and exposes a
continuous, verified entry stream to downstream consumers.

* definitions and byte formats: [`docs/DESIGN.md`](docs/DESIGN.md)
* `ctmirror/` — client, strict wire protocol, SQLite state, CLI
* `fixture/server.py` — local scriptable fake log (not a production log)
* `tests/` — 87 unit/integration tests
* `tools/acceptance_drill.py` — reproduces the required end-to-end scenario

## Quick start

```bash
python3 -m pip install --user cryptography     # Ed25519 only dependency
# (on PEP 668 systems add --break-system-packages, or use a venv)

# 1. start the fixture log with 5 leaves for source "alpha"
python3 fixture/server.py --source alpha:11 --leaves 5 --port 8765

# 2. build the deployment manifest from the fixture's anchor key
python3 tools/make_manifest.py --server http://127.0.0.1:8765 \
        --source alpha --out manifest.json

# 3. commit 5 leaves, then check state
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
        --server http://127.0.0.1:8765 sync alpha
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
        --server http://127.0.0.1:8765 status alpha
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
        --server http://127.0.0.1:8765 entries alpha --start 0 --end 5

# 4. grow the log, select a candidate, watch gaps
curl -s -X POST http://127.0.0.1:8765/admin/append \
     -d '{"source_id":"alpha","count":4}' -H 'Content-Type: application/json'
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
        --server http://127.0.0.1:8765 sync alpha --max-pages 1
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
        --server http://127.0.0.1:8765 diagnostics alpha
```

`sync` without `--max-pages` continues until the candidate is complete or an
error blocks the source; a completed candidate is promoted atomically.

## Commands

| command | purpose |
|---|---|
| `sync [SRC...] [--max-pages N] [--strict]` | select/finish a candidate |
| `status [SRC...]` | committed head/count and candidate gaps |
| `diagnostics [SRC...] [--with-content]` | staged indices, gaps, evidence |
| `entries SRC [--start N] [--end N] [--json]` | committed entries only |
| `page SRC START COUNT` | fetch one out-of-order page |
| `resume SRC` | unblock and continue the **same** candidate |
| `discard-candidate SRC` | drop staging, restart from committed |
| `head SRC` | committed signed head frame (base64) |

Exit codes: `0` ok, `1` error, `2` blocked with `--strict`, `3` store
corruption.

## Guarantees (and how they are tested)

* Signed head frames are parsed strictly (`CTM1`, big-endian fields, UTF-8
  source id, 64-byte signature) and only verified against registered Ed25519 keys — `tests/test_protocol.py`.
* Merkle roots, inclusion paths and consistency proofs follow RFC 9162;
  verification is a separate implementation from proof generation and is
  checked against direct recursive construction, non-power-of-two lengths,
  and the published RFC example — `tests/test_merkle.py`.
* A candidate is promoted only when all new indices are staged with valid
  proofs *and* the recomputed root matches the signed head; promotion is one
  SQLite transaction, so no reader sees a new head with missing leaves —
  `tests/test_store.py`.
* Same-size conflicting roots block as equivocation, smaller signed heads
  block as replay, with raw responses kept for later analysis —
  `tests/test_scenario.py`.
* A 10-leaf proof cannot complete a 9-leaf candidate; new heads observed
  mid-candidate are persisted as deferred heads and never mixed in —
  `tests/test_scenario.py`, `tests/test_deferred.py`.
* Restart keeps committed at 5 / candidate at 9 with index 7 missing and
  index 8 not exposed downstream — `tests/test_store.py`, and the drill.
* Key rotation with `activation_size` 9: the successor key verifies from the
  old-key certificate but cannot sign usability until committed size reaches
  9; then it is adopted — `tests/test_scenario.py` (`KeyRotationTest`).
* Only requested pages are fetched, inclusion paths are not persisted, and
  memory does not scale with total leaf bodies — `tests/test_scale.py`.
* Long randomized sequences of appends, partial syncs, out-of-order pages and
  restarts keep the head/count/content/root invariant after every step —
  `tests/test_fuzz.py`.

## Full verification

```bash
python3 -m unittest discover -s tests -t .
python3 tools/acceptance_drill.py
```

The drill prints the committed/candidate state, staged indices, gap
intervals, roots recomputed from independently constructed leaf lists, and
fails loudly (assertions) if any step disagrees.

## Trust model limits

Trust starts at the manifest's pre-registered keys and the local committed
checkpoint. This single client detects inconsistency between what it has
already committed and what it is now served, but cannot detect a split view
that is self-consistent from its checkpoint onward; that needs gossip or
independent witnesses. It does not interpret certificate semantics, and it
never generates proofs for other consumers.
