# CTM1 offline mirror client — design and definitions

This repository is a **drill/offline mirror client** for a CT-style append-only
log using the *CTM1 convention* described below. It is deliberately not a
generic RFC 6962/9162 HTTP client, it does not parse certificate contents, and
it never generates proofs for external consumers.

The trust starting point is the **deployment manifest** (pre-registered
Ed25519 anchor key per source) plus the **local committed checkpoint**. A
single client cannot detect every possible split view; see
[Trust model and limits](#trust-model-and-limits).

---

## 1. Wire formats (strict, big-endian, fixed width)

All integers are unsigned **big-endian**, fixed width. Material is validated
before any signature is considered: magic, source id encoding, field widths,
trailing bytes, signature length, and (for JSON carriers) canonical base64 /
hex.

### 1.1 Signed tree head (`CTM1`)

```
"CTM1"                                  4 bytes ASCII
u32be  len(source_id_utf8)              4 bytes
       source_id_utf8                    len bytes, non-empty, valid UTF-8, no NUL
u64be  epoch                            8 bytes
u64be  tree_size                        8 bytes
u64be  timestamp_ms                     8 bytes
       root                             32 bytes (SHA-256)
       signature                        64 bytes (Ed25519 over everything before it)
```

`root` is the Merkle Tree Hash of the first `tree_size` leaves. The signature
covers exactly `"CTM1" || len || source_id || epoch || tree_size ||
timestamp_ms || root`.

### 1.2 Successor-key certificate (`CTK1`)

```
"CTK1"
u32be  len(source_id_utf8)
       source_id_utf8
u64be  old_epoch
u64be  new_epoch                        must equal old_epoch + 1
       new_public_key                   32 bytes (raw Ed25519)
u64be  activation_size
       signature                        64 bytes, by the OLD epoch key
```

`activation_size` means: the new generation may only be used once the
client's **committed** size is at least `activation_size`, and any head
signed by the new key must have `tree_size >= activation_size`.

### 1.3 Key-material endpoint (fixture transport)

`GET /v1/source/<id>/keys` returns all generations with their raw public
keys, activation sizes and `CTK1` frames. The manifest's anchor key overrides
whatever the wire claims for the anchor epoch. A successor key is accepted
only if the chain from the highest verified local generation is contiguous
(no generation jump) and every `CTK1` verifies under the immediately previous
key. A self-signed successor, a jump (`0 -> 2`), a key mismatch between the
certificate and the served key, or a bad activation size all fail closed and
**nothing is persisted** for that chain.

---

## 2. Merkle tree definitions (RFC 9162, byte-level)

We use the public Certificate Transparency v2 definitions verbatim.

* Leaf hash: `SHA-256(0x00 || leaf_bytes)`, where `leaf_bytes` is the entry's
  original bytes (no parsing, no normalisation).
* Node hash: `SHA-256(0x01 || left_hash || right_hash)`, 32-byte hashes
  concatenated left-to-right in proof order.
* Empty tree: `MTH({}) = SHA-256("")` (the hash of the empty string, not of a
  zero byte).
* For `n > 1`, let `k` be the largest power of two **strictly smaller** than
  `n`. Then `MTH(D_n) = HASH(0x01 || MTH(D[0:k]) || MTH(D[k:n]))`. Leaves are
  never padded and the split is by leaf count, not by a full binary tree.

Hash bytes are opaque in proof lists; "byte order" only matters inside the
signed preimages, which are big-endian as specified above.

### 2.1 Inclusion proof (`PATH`)

`PATH(m, D_n)` is the RFC 9162 Section 2.1.3.1 recursion and is returned
deepest-first:

```
PATH(m, {d0}) = []
n > 1, k = largest power of two < n:
  m <  k : PATH(m, D[0:k]) : MTH(D[k:n])
  m >= k : PATH(m-k, D[k:n]) : MTH(D[0:k])
```

Verification is the Section 2.1.3.2 loop over `fn=leaf_index`,
`sn=tree_size-1`, starting from the leaf hash; `sn` must reach 0 and the
result must equal the signed root. Extra or missing nodes fail.

### 2.2 Consistency proof (`PROOF`/`SUBPROOF`)

`PROOF(m, D_n) = SUBPROOF(m, D_n, true)` exactly as in RFC 9162 Section
2.1.4.1:

```
SUBPROOF(m, D_m, true)  = []
SUBPROOF(m, D_m, false) = [MTH(D_m)]
m < n, k = largest power of two < n:
  m <= k : SUBPROOF(m, D[0:k], b)        : MTH(D[k:n])
  m >  k : SUBPROOF(m-k, D[k:n], false)  : MTH(D[0:k])
```

Verification follows Section 2.1.4.2 (including the `old_size` power-of-two
prepend and the extra right-shift after a double-merge). Two boundary cases
are defined by this convention:

* `old_size == 0`: an empty proof is acceptable and trivially consistent;
  **all new leaves must still be verified individually**.
* `old_size == new_size`: the proof must be empty and the roots must match.

For `0 < old < new`, an empty proof is rejected (per the RFC). Notice that
the *generation* of proofs in the fixture and the *verification* in the
client are separate implementations: the verifier does not call the
`PATH`/`SUBPROOF` generators. Tests compare against direct recursive
computation and the published RFC example (`PROOF(3, D7) = [c, d, g, l]`,
`PROOF(4, D7) = [l]`, `PROOF(6, D7) = [i, j, k]`, and the four inclusion
paths for `d0, d3, d4, d6`).

---

## 3. Client state model

Each source has exactly two public-visible states:

| state | meaning |
|---|---|
| **committed** | a signed head plus every leaf in `[0, size)`, promised to downstream |
| **candidate** | one target head whose signature and consistency vs. committed verified, but whose new leaves are not all collected |

A candidate carries its exact `(tree_size, root, epoch)` and the base
`(base_size, base_root)` it was validated against. Staging is keyed by the
source and index and is only meaningful while that candidate exists.

**Promotion invariant** — a candidate becomes committed only when:

1. every index in `[base_size, tree_size)` has exactly one staged leaf,
2. each staged leaf's inclusion proof verified against the candidate root at
   the moment it was staged (a page is verified entirely before any row is
   written),
3. the Merkle root recomputed from the stored leaves equals the signed root,
4. the candidate's base still equals the current committed head.

Step 3 is repeated inside the promotion transaction from persisted bytes, and
rows are moved + the new head published + staging deleted in **one** SQLite
`BEGIN IMMEDIATE` transaction. Readers therefore never observe "new head,
old leaves".

Gaps are stored implicitly (staged indices) and reported as half-open
intervals, so `status` can say `[[7,9]]` rather than "downloading".

### 3.1 Page handling

* All pages in a round must name the candidate's `tree_size` and `root`;
  otherwise the page is rejected (block).
* An entry's index must lie in `[base_size, tree_size)`.
* Inclusion proofs are verified against the candidate root **before** the
  page's entries are inserted; the insertion of a page is atomic. A bad entry
  rejects the whole page but leaves previously stored pages intact.
* Duplicate index within one page: identical bytes + valid proof folds;
  different bytes is a conflict (`leaf_conflict`).
* A page that returns nothing storable for the requested index is not "end of
  stream": the loop records that index as unfillable this round and continues
  with other gaps. An empty page is never treated as completion.
* Page order is arbitrary; `page <source> <start> <count>` fetches one page of
  the active candidate out of order.

### 3.2 Blocks, resume, discard

Invalid proofs, conflicting content, equivocation and replay set a persistent
`blocked` flag with the reason and the offending response material in
`observations`/`deferred_heads`. While blocked:

* other sources continue normally;
* `resume` only clears the flag and continues the **same** candidate;
* `discard-candidate` drops staging and unblocks, restarting from committed.

### 3.3 New heads during a candidate

A head that arrives while a candidate is in flight (from the head endpoint or
as a 409 "updated head" from the pages endpoint) is verified and persisted in
`deferred_heads`, never mixed into the current candidate. Once the candidate
is promoted (or explicitly discarded) the next round may select the newest
deferred head, subject to a fresh consistency proof against the new
committed checkpoint. Deferred heads are purged when committed catches up.

---

## 4. Failure taxonomy

| reason | trigger |
|---|---|
| `invalid_consistency_proof` | proof does not reproduce roots, wrong/extra/short nodes, response names a different head/size |
| `invalid_page` | bad inclusion proof, out-of-range index, wrong target size/root, wrong source, malformed page |
| `leaf_conflict` | same index with different bytes (in page or against staging) |
| `equivocation` | same size, different root (vs. committed or the active candidate) |
| `replay` | signed head smaller than committed size |
| `promotion_failed` | persisted staging fails the root/contiguity re-check |

Untrusted material (bad signature, unknown generation, generation jump,
self-signed successor, new key before `activation_size`, retired epoch) is
rejected without advancing state; if a candidate is open the head is deferred
instead of aborting the round.

---

## 5. Persistence layout (SQLite)

Separate tables for the two generations of data:

* `sources`, `generations` (registered keys, activation state),
* `heads` + `committed_leaves` (public),
* `candidates` + `candidate_leaves` (private staging),
* `deferred_heads`, `observations`, `events`, `page_stats`, `meta`.

Inclusion paths are **not** persisted: only leaf bytes and their leaf hash
(the hash is used for the promotion root check). Storage therefore grows with
the leaves themselves, not with proof material or page copies (verified by
`tests/test_scale.py`).

---

## 6. Trust model and limits

* Trust anchors: the manifest's pre-registered public keys and the local
  committed checkpoint. A server cannot make the client accept a different
  root at an already-committed size, nor a smaller size.
* One client with one view cannot detect a **split view** where the log
  consistently serves it a self-consistent fork that starts above its
  checkpoint. Detecting that requires gossip/witnesses or a second
  independent view; this client does not claim to.
* No certificate semantics are interpreted; entries are arbitrary bytes.
* Proofs are verified locally; the fixture server's Merkle implementation is
  never trusted.

---

## 7. CLI

```
python3 -m ctmirror --db mirror.sqlite --manifest manifest.json \
                    --server http://127.0.0.1:8765 --page-size 2 <cmd>

  sync [SOURCE ...] [--max-pages N] [--strict]
  status [SOURCE ...]
  diagnostics [SOURCE ...] [--with-content] [--limit N]
  entries SOURCE [--start N] [--end N] [--json]
  page SOURCE START COUNT
  resume SOURCE
  discard-candidate SOURCE
  head SOURCE
```

Exit codes: `0` success, `1` request/validation error, `2` blocked
(`--strict`), `3` store corruption. `entries` and `status` read only the
committed area; staged rows are visible only through `diagnostics`.
