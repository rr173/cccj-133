"""Offline CT-style mirror client (CTM1 convention).

The client fetches a signed tree head, checks it against the locally
registered key generations and the local committed checkpoint, verifies a
consistency proof, then gathers every new leaf with inclusion proofs into a
candidate.  Only when *every* index in ``[committed.size, candidate.size)``
is present with a verified inclusion proof and the recomputed Merkle root
matches the signed candidate head is the candidate promoted atomically.

State transitions are recorded in SQLite; staging for a candidate is bound
to that candidate's exact (tree_size, root, epoch).  A page is never used
across candidate targets.  See ``docs/DESIGN.md`` for the exact definitions
and the trust model.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import quote

from . import merkle, protocol
from .store import (CandidateHead, ConflictError, Store, StoreCorrupt,
                    StoreError)

# Block reasons are persistent and only cleared by resume/discard.
BLOCK_INVALID_CONSISTENCY = "invalid_consistency_proof"
BLOCK_INVALID_PAGE = "invalid_page"
BLOCK_EQUIVOCATION = "equivocation"
BLOCK_REPLAY = "replay"
BLOCK_CONFLICT = "leaf_conflict"

MAX_PAGE_ENTRIES = 1_000_000
DEFAULT_PAGE_SIZE = 2


class MirrorError(RuntimeError):
    pass


class UnknownSource(MirrorError):
    pass


class UntrustedError(MirrorError):
    """Signed material failed validation or a key is not yet usable."""


class BlockedError(MirrorError):
    def __init__(self, source_id: str, reason: str, detail: str):
        super().__init__(f"{source_id} is blocked ({reason}): {detail}")
        self.source_id = source_id
        self.reason = reason
        self.detail = detail


class TransportError(MirrorError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    anchor_epoch: int
    anchor_public_key: bytes
    cert_frames: tuple[bytes, ...] = ()


@dataclass
class Manifest:
    sources: dict[str, SourceConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "Manifest":
        with open(path, "rb") as fh:
            raw = fh.read(4 * 1024 * 1024)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise protocol.FormatError("manifest: not valid JSON") from exc
        if not isinstance(obj, dict) or not isinstance(obj.get("sources"),
                                                       list):
            raise protocol.FormatError("manifest: expected {'sources': [...]}")
        manifest = cls()
        for item in obj["sources"]:
            if not isinstance(item, dict):
                raise protocol.FormatError("manifest: source entry not object")
            sid = protocol.require_str(item.get("source_id"), "source_id")
            if sid in manifest.sources:
                raise protocol.FormatError(f"manifest: duplicate source {sid}")
            epoch = protocol.require_uint(item.get("anchor_epoch"),
                                          "anchor_epoch")
            key = protocol.hexdecode_strict(item.get("anchor_public_key"),
                                            "anchor_public_key")
            if len(key) != protocol.PUBLIC_KEY_LEN:
                raise protocol.FormatError(
                    f"manifest: {sid} anchor key is not 32 bytes")
            protocol.load_public_key(key)
            certs = []
            raw_certs = item.get("certificates", [])
            if not isinstance(raw_certs, list):
                raise protocol.FormatError("manifest: certificates not array")
            for c in raw_certs:
                certs.append(protocol.b64decode_strict(c, "certificate"))
            manifest.sources[sid] = SourceConfig(sid, epoch, key,
                                                 tuple(certs))
        return manifest


class HttpTransport:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body=None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(64 * 1024 * 1024)
                status = resp.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(64 * 1024 * 1024)
            except Exception:  # noqa: BLE001
                raw = b""
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method} {path}: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(
                f"{method} {path}: invalid JSON (status {status})") from exc
        if not isinstance(parsed, dict):
            raise TransportError(f"{method} {path}: JSON is not an object")
        return status, parsed

    def get_json(self, path: str):
        return self._request("GET", path)

    def post_json(self, path: str, body: dict):
        return self._request("POST", path, body)


def _source_path(source_id: str, tail: str) -> str:
    return f"/v1/source/{quote(source_id, safe='')}/{tail}"


def register_manifest(store: Store, manifest: Manifest) -> None:
    """Create sources/generations from the deployment manifest.

    Manifest certificates are verified from the anchor key here; a malformed
    or non-contiguous manifest fails closed.
    """
    for cfg in manifest.sources.values():
        store.ensure_source(cfg.source_id, cfg.anchor_epoch,
                            cfg.anchor_public_key)
        prev_epoch = cfg.anchor_epoch
        prev_key = cfg.anchor_public_key
        for frame in cfg.cert_frames:
            cert = protocol.parse_key_cert(frame, expected_source=cfg.source_id)
            if cert.old_epoch != prev_epoch:
                raise protocol.FormatError(
                    f"manifest: cert for {cfg.source_id} starts at epoch "
                    f"{cert.old_epoch}, expected {prev_epoch}")
            ok, why = protocol.verify_signature(prev_key, cert.signed_bytes,
                                                cert.signature)
            if not ok:
                raise protocol.FormatError(
                    f"manifest: cert for {cfg.source_id} epoch "
                    f"{cert.new_epoch} is not signed by previous key: {why}")
            try:
                store.add_verified_generation(
                    cfg.source_id, cert.new_epoch, cert.new_public_key,
                    cert.activation_size, frame)
            except ConflictError as exc:
                raise protocol.FormatError(
                    f"manifest: {exc}") from exc
            prev_epoch = cert.new_epoch
            prev_key = cert.new_public_key


@dataclass
class SyncResult:
    source_id: str
    state: str  # "empty" | "synced" | "candidate_pending" | "blocked"
    committed_size: int
    candidate_size: Optional[int]
    pages_fetched: int = 0
    promoted: bool = False
    gaps: list[list[int]] = field(default_factory=list)
    deferred_heads: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id, "state": self.state,
            "committed_size": self.committed_size,
            "candidate_size": self.candidate_size,
            "pages_fetched": self.pages_fetched,
            "promoted": self.promoted,
            "gaps": [[a, b] for a, b in self.gaps],
            "deferred_heads": self.deferred_heads,
        }


class MirrorClient:
    def __init__(self, store: Store, transport, manifest: Manifest,
                 page_size: int = DEFAULT_PAGE_SIZE):
        if not 1 <= page_size <= MAX_PAGE_ENTRIES:
            raise ValueError("page_size out of range")
        self.store = store
        self.transport = transport
        self.manifest = manifest
        self.page_size = page_size
        self.last_deferred_heads: list[dict] = []
        self.deferred_during_pages = False

    # -- configuration helpers ---------------------------------------------
    def _config(self, source_id: str) -> SourceConfig:
        cfg = self.manifest.sources.get(source_id)
        if cfg is None:
            raise UnknownSource(f"source {source_id!r} is not in the manifest")
        if not self.store.source_exists(source_id):
            register_manifest(self.store, self.manifest)
        return cfg

    def _ensure_generations(self, source_id: str, target_epoch: int) -> None:
        """Ensure the cert chain from the highest known epoch to target.

        Certificates are fetched from the log but are only accepted when each
        is signed by the immediately previous *registered* key and the epoch
        increments by exactly one.  A jump or a self-signed new key fails.
        Nothing is persisted unless the whole fetched chain validates, so a
        later attempt cannot "remember" a rogue key.
        """
        if target_epoch < 0:
            raise UntrustedError("negative epoch")
        gens = {g.epoch: g for g in self.store.list_generations(source_id)}
        if not gens:
            raise UntrustedError("source has no registered generations")
        anchor = min(gens)
        if anchor not in gens:
            raise UntrustedError("anchor generation missing")
        # Determine highest verified contiguous epoch.
        highest = None
        for epoch in sorted(gens):
            if not gens[epoch].verified:
                break
            if highest is not None and epoch != highest + 1:
                break
            highest = epoch
        if highest is None or highest < anchor:
            raise UntrustedError("no verified generation")
        if target_epoch <= highest:
            if target_epoch not in gens or not gens[target_epoch].verified:
                raise UntrustedError(
                    f"epoch {target_epoch} is not registered")
            return
        status, obj = self.transport.get_json(
            _source_path(source_id, "keys"))
        if status != 200:
            raise UntrustedError(
                f"cannot fetch key material for epoch {target_epoch}"
                f" (status {status})")
        if obj.get("source_id") != source_id:
            raise UntrustedError("key material source_id mismatch")
        entries = protocol.require_list(obj.get("generations"),
                                        "generations")
        frames: dict[int, bytes] = {}
        keys: dict[int, bytes] = {}
        activation: dict[int, int] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise UntrustedError("generation entry is not an object")
            epoch = protocol.require_uint(item.get("epoch"), "epoch")
            key = protocol.hexdecode_strict(item.get("public_key"),
                                            "public_key")
            if len(key) != 32:
                raise UntrustedError("generation key is not 32 bytes")
            act = protocol.require_uint(item.get("activation_size"),
                                        "activation_size")
            if epoch in frames:
                raise UntrustedError("duplicate generation")
            keys[epoch] = key
            activation[epoch] = act
            if item.get("cert_b64") is not None:
                frames[epoch] = protocol.b64decode_strict(
                    item["cert_b64"], "cert_b64")
        # The anchor's key from the manifest is the trust root; the wire
        # key material may not redefine it.
        if keys.get(anchor) != gens[anchor].public_key:
            raise UntrustedError("wire anchor key disagrees with manifest")
        prev_epoch = highest
        prev_key = self.store.get_generation(source_id, highest).public_key
        new = {}
        served = sorted(e for e in keys if e > highest)
        for epoch in range(highest + 1, target_epoch + 1):
            if epoch not in keys or epoch not in frames:
                if served and served[0] > epoch:
                    raise UntrustedError(
                        f"generation jump: next served generation is "
                        f"{served[0]}, expected {epoch}")
                raise UntrustedError(
                    f"missing key certificate for epoch {epoch}")
            cert = protocol.parse_key_cert(frames[epoch],
                                           expected_source=source_id)
            if cert.old_epoch != prev_epoch:
                raise UntrustedError(
                    f"key certificate starts at epoch {cert.old_epoch},"
                    f" expected {prev_epoch} (generation jump)")
            if cert.new_epoch != epoch:
                raise UntrustedError("key certificate epoch mismatch")
            ok, why = protocol.verify_signature(
                prev_key, cert.signed_bytes, cert.signature)
            if not ok:
                raise UntrustedError(
                    f"key certificate epoch {epoch} not signed by previous"
                    f" key: {why}")
            if cert.new_public_key == prev_key:
                raise UntrustedError("successor key equals previous key")
            if keys[epoch] != cert.new_public_key:
                raise UntrustedError(
                    "wire public key differs from certified successor key")
            existing = self.store.get_generation(source_id, epoch)
            if existing is not None and existing.public_key != \
                    cert.new_public_key:
                raise UntrustedError("successor key conflicts with local")
            new[(source_id, epoch, cert.new_public_key,
                 cert.activation_size)] = (
                    source_id, epoch, cert.new_public_key,
                    cert.activation_size, frames[epoch])
            prev_epoch = epoch
            prev_key = cert.new_public_key
        for row in new.values():
            try:
                self.store.add_verified_generation(*row)
            except ConflictError as exc:
                raise UntrustedError(str(exc)) from exc

    def _activate_reachable_generations(self, source_id: str,
                                        committed_size: int) -> None:
        """Activate verified generations whose activation_size is reached.

        Activation only ever happens after a candidate containing at least
        activation_size leaves is committed; a new key never takes effect on
        the strength of a head alone.
        """
        for gen in self.store.list_generations(source_id):
            if gen.verified and not gen.activated and \
                    committed_size >= gen.activation_size:
                self.store.activate_generation(source_id, gen.epoch,
                                               committed_size)
                self.store.event(
                    source_id, "generation_activated",
                    f"epoch={gen.epoch} committed={committed_size}"
                    f" activation_size={gen.activation_size}")

    def _active_epoch_ceiling(self, source_id: str) -> int:
        gens = self.store.list_generations(source_id)
        active = [g.epoch for g in gens if g.activated]
        if not active:
            return self.store.get_source(source_id)["anchor_epoch"]
        return max(active)

    def _trust_head(self, source_id: str, head: protocol.TreeHead,
                    committed_size: int) -> None:
        """Full trust check of a signed head; may activate a new generation."""
        cfg = self._config(source_id)
        if head.source_id != source_id:
            raise UntrustedError("head source_id mismatch")
        self._ensure_generations(source_id, head.epoch)
        gen = self.store.get_generation(source_id, head.epoch)
        if gen is None or not gen.verified:
            raise UntrustedError(f"epoch {head.epoch} not registered")
        ok, why = protocol.verify_signature(gen.public_key,
                                            head.signed_bytes,
                                            head.signature)
        if not ok:
            raise UntrustedError(
                f"tree head signature invalid for epoch {head.epoch}: {why}")
        ceiling = self._active_epoch_ceiling(source_id)
        if head.epoch < ceiling:
            raise UntrustedError(
                f"tree head epoch {head.epoch} is retired "
                f"(active epoch {ceiling})")
        if gen.epoch > 0 and head.tree_size < gen.activation_size:
            raise UntrustedError(
                f"epoch {gen.epoch} head size {head.tree_size} is below"
                f" activation_size {gen.activation_size}")
        if not gen.activated:
            if committed_size < gen.activation_size:
                raise UntrustedError(
                    f"epoch {gen.epoch} key not active: committed size "
                    f"{committed_size} < activation_size "
                    f"{gen.activation_size}")
            if head.tree_size < gen.activation_size:
                raise UntrustedError(
                    f"epoch {gen.epoch} key not active: head size "
                    f"{head.tree_size} < activation_size "
                    f"{gen.activation_size}")
            self.store.activate_generation(source_id, gen.epoch,
                                           committed_size)
            self.store.event(
                source_id, "generation_activated",
                f"epoch={gen.epoch} committed={committed_size}"
                f" activation_size={gen.activation_size}")

    # -- network helpers ----------------------------------------------------
    def _fetch_head(self, source_id: str) -> protocol.TreeHead:
        status, obj = self.transport.get_json(_source_path(source_id, "head"))
        if status != 200:
            raise TransportError(f"head request failed with status {status}")
        if obj.get("source_id") != source_id:
            raise UntrustedError("head response source_id mismatch")
        frame = protocol.b64decode_strict(obj.get("head_b64"), "head_b64")
        head = protocol.parse_tree_head(frame, expected_source=source_id)
        # Cross-check any convenience JSON fields against the signed frame.
        if protocol.require_uint(obj.get("tree_size"), "tree_size") != \
                head.tree_size:
            raise UntrustedError("head JSON tree_size disagrees with frame")
        if protocol.require_uint(obj.get("epoch"), "epoch") != head.epoch:
            raise UntrustedError("head JSON epoch disagrees with frame")
        if protocol.hexdecode_strict(obj.get("root"), "root") != head.root:
            raise UntrustedError("head JSON root disagrees with frame")
        return head

    def _fetch_consistency(self, source_id: str, old_size: int,
                           new_size: int) -> dict:
        status, obj = self.transport.post_json(
            _source_path(source_id, "consistency"),
            {"old_size": old_size, "new_size": new_size})
        if status != 200:
            raise UntrustedError(
                f"consistency request failed with status {status}")
        if obj.get("source_id") != source_id:
            raise UntrustedError("consistency response source_id mismatch")
        if protocol.require_uint(obj.get("old_size"), "old_size") != old_size:
            raise UntrustedError("consistency old_size mismatch")
        if protocol.require_uint(obj.get("new_size"), "new_size") != new_size:
            raise UntrustedError("consistency new_size mismatch")
        return obj

    # -- block / resume -----------------------------------------------------
    def _block(self, source_id: str, reason: str, detail: str,
               payload=None) -> BlockedError:
        self.store.set_blocked(source_id, reason, detail, payload)
        self.store.event(source_id, "sync_aborted", f"{reason}: {detail}")
        return BlockedError(source_id, reason, detail)

    def _check_blocked(self, source_id: str) -> None:
        row = self.store.get_source(source_id)
        if row is None:
            raise UnknownSource(source_id)
        if row["blocked"]:
            raise BlockedError(source_id, row["blocked_reason"],
                               row["blocked_detail"] or "")

    # -- main sync ----------------------------------------------------------
    def sync(self, source_id: str,
             max_pages: Optional[int] = None) -> SyncResult:
        self._config(source_id)
        self._check_blocked(source_id)
        committed = self.store.committed_head(source_id)
        result = SyncResult(source_id=source_id, state="synced",
                            committed_size=committed.size,
                            candidate_size=None)

        # Finish a fully staged candidate before looking at anything newer.
        # This is the only path that promotes without a new consistency call.
        cand = self.store.candidate(source_id)
        if cand is not None and not self.store.missing_ranges(
                source_id, cand.base_size, cand.size):
            promoted = self._promote(source_id, cand)
            self.store.purge_deferred_heads(source_id, promoted.size)
            result.promoted = True
            result.committed_size = promoted.size
            result.state = "synced"
            return result

        head = self._fetch_head(source_id)
        pending = self.store.candidate(source_id)
        try:
            self._trust_head(source_id, head, committed.size)
        except UntrustedError as exc:
            self.store.observation(source_id, "untrusted_head", head,
                                   str(exc))
            if pending is None:
                raise
            # A live candidate is bound to its own already-verified target.
            # Material we cannot yet trust (e.g. a successor-key head before
            # the old-key checkpoint reaches activation_size) is recorded and
            # left for later; it is never mixed into the candidate.
            self.store.observation(
                source_id, "deferred_untrusted_head", head, str(exc))
            result.deferred_heads.append({
                "tree_size": head.tree_size, "epoch": head.epoch,
                "root": head.root.hex(), "relation": "untrusted",
                "note": str(exc)})
            cand = pending
            result.candidate_size = cand.size
            self.last_deferred_heads = []
            self.deferred_during_pages = False
            pages, gaps = self._download(source_id, cand, max_pages)
            result.pages_fetched = pages
            result.gaps = [[a, b] for a, b in gaps]
            result.deferred_heads.extend(self.last_deferred_heads)
            if not gaps:
                promoted = self._promote(source_id, cand)
                self.store.purge_deferred_heads(source_id, promoted.size)
                self._activate_reachable_generations(source_id,
                                                     promoted.size)
                result.promoted = True
                result.committed_size = promoted.size
                result.state = "synced"
            else:
                result.state = "candidate_pending"
                result.committed_size = self.store.committed_head(
                    source_id).size
            return result

        # Equivocation / replay against committed history.
        for past in self.store.heads_history(source_id):
            if past["tree_size"] == head.tree_size and \
                    past["root"] != head.root.hex():
                self.store.observation(
                    source_id, "equivocation_head", head,
                    f"committed root at size {head.tree_size} is "
                    f"{past['root']}, new signed head is {head.root.hex()}")
                raise self._block(
                    source_id, BLOCK_EQUIVOCATION,
                    f"signed head at size {head.tree_size} differs from"
                    f" committed root", {"head": head.frame.hex()})
        if head.tree_size < committed.size:
            self.store.observation(
                source_id, "replay_head", head,
                f"head size {head.tree_size} < committed {committed.size}")
            raise self._block(
                source_id, BLOCK_REPLAY,
                f"tree size went backwards: {head.tree_size} < "
                f"{committed.size}", {"head": head.frame.hex()})
        if head.tree_size == committed.size:
            if head.root != committed.root:
                self.store.observation(
                    source_id, "equivocation_head", head,
                    f"same size {head.tree_size} but different root")
                raise self._block(
                    source_id, BLOCK_EQUIVOCATION,
                    f"same-size head root mismatch at {head.tree_size}",
                    {"head": head.frame.hex(),
                     "committed_root": committed.root.hex()})
            if cand is None:
                result.state = "empty" if head.tree_size == 0 else "synced"
                result.gaps = []
                return result

        cand = self.store.candidate(source_id)
        if cand is not None and cand.size != head.tree_size:
            # A pending candidate is authoritative for its own target even if
            # the newly observed head has a different size.  The new head is
            # only recorded, never mixed in.
            committed_unchanged = (head.tree_size == committed.size and
                                   head.root == committed.root)
            relation = ("committed_view" if committed_unchanged else
                        "newer" if head.tree_size > cand.size else "older")
            self._record_deferred(source_id, head, relation)
            result.deferred_heads.append({
                "tree_size": head.tree_size, "epoch": head.epoch,
                "root": head.root.hex(), "relation": relation,
                "note": "left pending until the active candidate completes "
                        "or is discarded"})
        elif cand is None:
            # A head observed while a candidate was in flight is now eligible
            # (persisted in deferred_heads, so it survives restarts).  Prefer
            # it over whatever the endpoint serves right now, and require a
            # consistency proof that binds it to the current checkpoint.
            deferred = self._pick_deferred_head(source_id, head, committed)
            if deferred is not None:
                cand = self._select_candidate(source_id, deferred, committed)
            elif head.tree_size > committed.size:
                cand = self._select_candidate(source_id, head, committed)
            else:
                # head is below candidate context only; handled below.
                cand = self.store.candidate(source_id)
        elif (head.tree_size, head.root, head.epoch) != \
                (cand.size, cand.root, cand.epoch):
            # A different target head arrived while a candidate is in flight.
            # It is recorded but may not be mixed into the current candidate.
            # Re-signed heads with a new timestamp for the same target are
            # not "different" (the signature is over size/root/epoch).
            committed_unchanged = (head.tree_size == committed.size and
                                   head.root == committed.root)
            relation = ("same_size_different_root"
                        if (head.tree_size == cand.size and
                            head.root != cand.root) else
                        "committed_view" if committed_unchanged else
                        "newer" if head.tree_size > cand.size else "older")
            if relation == "same_size_different_root":
                self.store.observation(
                    source_id, "equivocation_candidate", head,
                    f"candidate size {cand.size} root {cand.root.hex()},"
                    f" new head root {head.root.hex()}")
                raise self._block(
                    source_id, BLOCK_EQUIVOCATION,
                    f"same-size head differs from active candidate at "
                    f"{cand.size}",
                    {"head": head.frame.hex(),
                     "candidate_root": cand.root.hex()})
            self._record_deferred(source_id, head, relation)
            result.deferred_heads.append({
                "tree_size": head.tree_size, "epoch": head.epoch,
                "root": head.root.hex(), "relation": relation,
                "note": "left pending until the active candidate completes "
                        "or is discarded"})

        if cand is None:
            result.state = "synced"
            result.committed_size = committed.size
            return result

        result.candidate_size = cand.size
        self.last_deferred_heads = []
        self.deferred_during_pages = False
        pages, gaps = self._download(source_id, cand, max_pages)
        result.pages_fetched = pages
        result.gaps = [[a, b] for a, b in gaps]
        result.deferred_heads.extend(self.last_deferred_heads)
        if not gaps:
            promoted = self._promote(source_id, cand)
            self.store.purge_deferred_heads(source_id, promoted.size)
            self._activate_reachable_generations(source_id, promoted.size)
            result.promoted = True
            result.committed_size = promoted.size
            result.state = "synced"
        else:
            result.state = "candidate_pending"
            result.committed_size = self.store.committed_head(source_id).size
        return result

    def _pick_deferred_head(self, source_id: str, live: protocol.TreeHead,
                            committed) -> Optional[protocol.TreeHead]:
        """Choose a persisted deferred head to become the next candidate.

        Rows are considered newest-size first.  A head whose signature or key
        generation is not currently usable (for example a successor-key head
        seen before the old-key checkpoint reached activation_size) is skipped
        and left in place for later; it must not block advancement on a head
        we can trust now.  If the live endpoint names the same size with a
        different root, that is an equivocation and the client refuses.
        """
        rows = [r for r in self.store.deferred_heads(source_id)
                if r["tree_size"] > committed.size]
        if not rows:
            return None
        rows.sort(key=lambda r: (r["tree_size"], r["observed_at"]),
                  reverse=True)
        for row in rows:
            if live.tree_size == row["tree_size"] and live.root != row["root"]:
                self.store.observation(
                    source_id, "equivocation_head", live,
                    f"deferred head at {row['tree_size']} root "
                    f"{row['root'].hex()} differs from live head")
                raise self._block(
                    source_id, BLOCK_EQUIVOCATION,
                    f"same-size heads disagree at {row['tree_size']}",
                    {"live_head": live.frame.hex(),
                     "deferred_head": row["frame"].hex()})
            if live.tree_size == row["tree_size"] and \
                    live.root == row["root"] and \
                    live.epoch == row["epoch"]:
                # The live head already names this exact target; no need to
                # resurrect a stale duplicate observation.
                return None
            try:
                head = protocol.parse_tree_head(row["frame"],
                                                expected_source=source_id)
            except protocol.FormatError as exc:
                self.store.forget_deferred_head(source_id, row["tree_size"],
                                                row["root"])
                self.store.observation(source_id, "invalid_deferred_head",
                                       None, str(exc))
                continue
            try:
                self._trust_head(source_id, head, committed.size)
            except UntrustedError as exc:
                self.store.observation(
                    source_id, "deferred_head_not_usable", head, str(exc))
                continue
            return head
        return None

    def _record_deferred(self, source_id: str, head: protocol.TreeHead,
                         relation: str) -> None:
        self.store.add_deferred_head(source_id, head, relation)
        self.store.observation(source_id, "deferred_head", head, relation)

    # -- candidate selection ------------------------------------------------
    def _select_candidate(self, source_id: str, head: protocol.TreeHead,
                          committed) -> CandidateHead:
        try:
            self._trust_head(source_id, head, committed.size)
        except UntrustedError as exc:
            self.store.observation(source_id, "untrusted_head", head,
                                   str(exc))
            raise
        try:
            response = self._fetch_consistency(source_id, committed.size,
                                               head.tree_size)
        except UntrustedError as exc:
            self.store.observation(source_id, "invalid_consistency", head,
                                   str(exc))
            raise self._block(
                source_id, BLOCK_INVALID_CONSISTENCY,
                f"consistency response rejected: {exc}", None)
        # The consistency response carries the head it proves; it must be the
        # same signed head we just fetched (or at least an equally signed head
        # for the same target).  Prefer the response frame and validate it.
        frame = protocol.b64decode_strict(response.get("head_b64"),
                                          "head_b64")
        proof_head = protocol.parse_tree_head(frame,
                                              expected_source=source_id)
        if proof_head.tree_size != head.tree_size or \
                proof_head.root != head.root or \
                proof_head.epoch != head.epoch:
            self.store.observation(
                source_id, "consistency_head_mismatch", proof_head,
                f"head {head.tree_size}/{head.root.hex()} vs proof head "
                f"{proof_head.tree_size}/{proof_head.root.hex()}")
            raise self._block(
                source_id, BLOCK_INVALID_CONSISTENCY,
                "consistency response proves a different tree head",
                {"head": head.frame.hex(),
                 "proof_head": proof_head.frame.hex()})
        self._trust_head(source_id, proof_head, committed.size)
        old_root = protocol.hexdecode_strict(response.get("old_root"),
                                             "old_root")
        new_root = protocol.hexdecode_strict(response.get("new_root"),
                                             "new_root")
        if old_root != committed.root or new_root != head.root:
            self.store.observation(
                source_id, "consistency_root_mismatch", proof_head,
                f"old_root={old_root.hex()} new_root={new_root.hex()}")
            raise self._block(
                source_id, BLOCK_INVALID_CONSISTENCY,
                "consistency response roots do not match local checkpoint",
                {"response": {k: v for k, v in response.items()
                              if isinstance(v, (str, int, list))}})
        raw_proof = protocol.require_list(response.get("proof"), "proof")
        if len(raw_proof) > protocol.MAX_PROOF_NODES:
            raise self._block(source_id, BLOCK_INVALID_CONSISTENCY,
                              "consistency proof too long")
        proof = [protocol.hexdecode_strict(p, "proof node")
                 for p in raw_proof]
        ok, why = merkle.verify_consistency(
            committed.size, head.tree_size, proof, committed.root, head.root)
        if not ok:
            self.store.observation(
                source_id, "invalid_consistency", proof_head,
                f"{why}; proof nodes={len(proof)}")
            raise self._block(
                source_id, BLOCK_INVALID_CONSISTENCY,
                f"consistency proof from {committed.size} to "
                f"{head.tree_size} failed: {why}",
                {"head": head.frame.hex(),
                 "proof": [p.hex() for p in proof]})
        self.store.create_candidate(source_id, proof_head, committed.size,
                                    committed.root, proof)
        self.store.observation(source_id, "candidate_selected", proof_head,
                               f"base={committed.size}")
        cand = self.store.candidate(source_id)
        assert cand is not None
        return cand

    # -- page download ------------------------------------------------------
    def _download(self, source_id: str, cand: CandidateHead,
                  max_pages: Optional[int]) -> tuple[int, list]:
        """Fetch missing indices with inclusion proofs in page_size chunks.

        Each index is requested at most once per round.  A page that returns
        nothing storable for the requested index marks that index as "still
        missing" for this round and the loop moves on, so other gaps are
        still discovered.  An empty or short page is never treated as
        end-of-stream.  Ordering within a page does not matter: the store's
        gap query is the source of truth.
        """
        pages = 0
        skipped: set[int] = set()
        while True:
            gaps = self.store.missing_ranges(source_id, cand.base_size,
                                             cand.size)
            if not gaps:
                return pages, []
            if max_pages is not None and pages >= max_pages:
                return pages, gaps
            choice = None
            for a, b in gaps:
                for idx in range(a, b):
                    if idx not in skipped:
                        choice = (idx, b)
                        break
                if choice is not None:
                    break
            if choice is None:
                # Every remaining index failed to arrive this round.
                return pages, gaps
            start, gap_end = choice
            count = min(self.page_size, gap_end - start)
            self._fetch_page(source_id, cand, start, count)
            pages += 1
            if not self.store.has_staged(source_id, start):
                # The requested index did not arrive (even if sibling
                # indices did); do not ask for it again this round.
                skipped.add(start)
            if self.deferred_during_pages:
                # The log handed us a new head instead of pages.  Stop this
                # round: the candidate stays incomplete and the new head
                # remains deferred until the candidate finishes or is
                # explicitly discarded.
                return pages, self.store.missing_ranges(
                    source_id, cand.base_size, cand.size)

    def _fetch_page(self, source_id: str, cand: CandidateHead, start: int,
                    count: int) -> bool:
        try:
            return self._fetch_page_inner(source_id, cand, start, count)
        except BlockedError:
            self.store.page_stat(source_id, cand.size, start, count, 0,
                                 False, 0, 0)
            raise

    def _fetch_page_inner(self, source_id: str, cand: CandidateHead,
                          start: int, count: int) -> bool:
        status, obj = self.transport.post_json(
            _source_path(source_id, "entries"),
            {"tree_size": cand.size, "start": start, "count": count})
        if status == 409 and obj.get("updated"):
            frame = protocol.b64decode_strict(obj.get("head_b64"),
                                              "head_b64")
            new_head = protocol.parse_tree_head(
                frame, expected_source=source_id)
            try:
                self._trust_head(source_id, new_head,
                                 self.store.committed_head(source_id).size)
            except UntrustedError as exc:
                self.store.observation(source_id, "untrusted_head",
                                       new_head, str(exc))
                self.deferred_during_pages = True
                self.store.observation(
                    source_id, "deferred_untrusted_head", new_head, str(exc))
                self.store.add_deferred_head(source_id, new_head,
                                             "untrusted")
                self.last_deferred_heads.append({
                    "tree_size": new_head.tree_size,
                    "epoch": new_head.epoch,
                    "root": new_head.root.hex(), "relation": "untrusted",
                    "note": str(exc)})
                return False
            if new_head.tree_size == cand.size and \
                    new_head.root != cand.root:
                self.store.observation(
                    source_id, "equivocation_candidate", new_head,
                    "updated head same size, different root")
                raise self._block(
                    source_id, BLOCK_EQUIVOCATION,
                    "updated head conflicts with candidate at same size",
                    {"head": new_head.frame.hex(),
                     "candidate_root": cand.root.hex()})
            self.deferred_during_pages = True
            relation = ("same_size_different_root"
                        if new_head.tree_size == cand.size else
                        "newer" if new_head.tree_size > cand.size
                        else "older")
            self._record_deferred(source_id, new_head, relation)
            self.last_deferred_heads.append({
                "tree_size": new_head.tree_size, "epoch": new_head.epoch,
                "root": new_head.root.hex(), "relation": relation,
                "note": "observed during page fetch; not mixed into the "
                        "active candidate"})
            return False
        if status != 200:
            raise TransportError(
                f"entries request failed with status {status}")
        raw_bytes = len(json.dumps(obj))
        # -- strict target binding -----------------------------------------
        if obj.get("source_id") != source_id:
            raise self._page_block(source_id, cand, "source_id mismatch",
                                   {"response_source":
                                    obj.get("source_id")})
        try:
            tree_size = protocol.require_uint(obj.get("tree_size"),
                                              "tree_size")
            root = protocol.hexdecode_strict(obj.get("root"), "root")
        except protocol.FormatError as exc:
            raise self._page_block(source_id, cand, str(exc), None)
        if tree_size != cand.size or root != cand.root:
            self.store.observation(
                source_id, "page_target_mismatch", None,
                f"response {tree_size}/{root.hex()} candidate "
                f"{cand.size}/{cand.root.hex()}")
            raise self._page_block(
                source_id, cand,
                f"page target mismatch: got size {tree_size} root "
                f"{root.hex()}, candidate is size {cand.size} root "
                f"{cand.root.hex()}", None)

        raw_entries = protocol.require_list(obj.get("entries"), "entries")
        if len(raw_entries) > MAX_PAGE_ENTRIES:
            raise self._page_block(source_id, cand, "page too large", None)
        verified: list[tuple[int, bytes, bytes]] = []
        seen: dict[int, bytes] = {}
        for item in raw_entries:
            if not isinstance(item, dict):
                raise self._page_block(source_id, cand,
                                       "entry is not an object", None)
            try:
                index = protocol.require_uint(item.get("leaf_index"),
                                              "leaf_index")
                leaf = protocol.b64decode_strict(item.get("leaf_b64"),
                                                 "leaf_b64")
                raw_proof = protocol.require_list(item.get("proof"),
                                                 "proof")
                if len(raw_proof) > protocol.MAX_PROOF_NODES:
                    raise protocol.FormatError("inclusion proof too long")
                proof = [protocol.hexdecode_strict(p, "proof node")
                         for p in raw_proof]
            except protocol.FormatError as exc:
                raise self._page_block(source_id, cand, str(exc), None)
            if index < cand.base_size or index >= cand.size:
                raise self._page_block(
                    source_id, cand,
                    f"entry index {index} outside candidate range "
                    f"[{cand.base_size},{cand.size})",
                    {"index": index})
            if index in seen:
                if seen[index] != leaf:
                    # Intra-page duplicate with different bytes is a content
                    # conflict, not merely a malformed page.
                    raise self._block(
                        source_id, BLOCK_CONFLICT,
                        f"page repeats index {index} with different bytes",
                        {"index": index})
                folded_proof_ok, why = merkle.verify_inclusion(
                    leaf, index, cand.size, proof, cand.root)
                if not folded_proof_ok:
                    raise self._page_block(
                        source_id, cand,
                        f"duplicate entry {index} has invalid proof: {why}",
                        {"index": index})
                continue
            ok, why = merkle.verify_inclusion(leaf, index, cand.size, proof,
                                              cand.root)
            if not ok:
                self.store.observation(
                    source_id, "invalid_inclusion", None,
                    f"index {index}: {why}")
                raise self._page_block(
                    source_id, cand,
                    f"inclusion proof failed for index {index}: {why}",
                    {"index": index, "leaf_sha256":
                     merkle.leaf_hash(leaf).hex(),
                     "proof": [p.hex() for p in proof]})
            seen[index] = leaf
            verified.append((index, leaf, merkle.leaf_hash(leaf)))

        try:
            stored = self.store.stage_page(source_id, verified)
        except ConflictError as exc:
            self.store.observation(source_id, "leaf_conflict", None, str(exc))
            raise self._block(source_id, BLOCK_CONFLICT, str(exc), None)
        except StoreError:
            # Candidate vanished (discarded concurrently); do not stage.
            raise
        self.store.page_stat(source_id, cand.size, start, count,
                             len(raw_entries), True, stored, raw_bytes)
        if len(verified) < count:
            self.store.event(
                source_id, "short_page",
                f"requested {count} at {start}, verified {len(verified)}"
                f" new entries; gap remains")
        return bool(verified)

    def _page_block(self, source_id: str, cand: CandidateHead, detail: str,
                    payload) -> BlockedError:
        self.store.observation(source_id, "invalid_page", None,
                               f"candidate size {cand.size}: {detail}")
        return self._block(source_id, BLOCK_INVALID_PAGE, detail, payload)

    def is_staged(self, source_id: str, index: int) -> bool:
        return self.store.has_staged(source_id, index)

    def fetch_page(self, source_id: str, start: int, count: int) -> dict:
        """Fetch one page out of order for the active candidate.

        Pages are independent: requesting a later page before an earlier one
        is allowed, and does not advance the candidate.  This is the same
        code path ``sync`` uses.
        """
        self._config(source_id)
        self._check_blocked(source_id)
        cand = self.store.candidate(source_id)
        if cand is None:
            raise MirrorError("no active candidate; run sync first")
        if not 1 <= count <= MAX_PAGE_ENTRIES:
            raise MirrorError("count out of range")
        if start < cand.base_size or start >= cand.size:
            raise MirrorError("start outside candidate range")
        progressed = self._fetch_page(source_id, cand, start, count)
        return {
            "source_id": source_id,
            "tree_size": cand.size,
            "start": start, "count": count,
            "progressed": progressed,
            "staged_count": self.store.staged_count(source_id),
            "gaps": [[a, b] for a, b in self.store.missing_ranges(
                source_id, cand.base_size, cand.size)],
        }

    # -- promotion ----------------------------------------------------------
    def _promote(self, source_id: str,
                 cand: CandidateHead) -> CandidateHead:
        committed = self.store.committed_head(source_id)
        if (cand.base_size != committed.size or
                cand.base_root != committed.root):
            raise StoreCorrupt("candidate base does not match committed")
        gaps = self.store.missing_ranges(source_id, cand.base_size, cand.size)
        if gaps:
            raise StoreError(
                f"refusing promotion with gaps: {gaps}")
        # Re-check persisted staging (contiguity, leaf hashes) before the
        # atomic move; store.promote_candidate then recomputes the root from
        # those rows and refuses if it differs from the signed head.
        self._reverify_candidate(source_id, cand)
        try:
            return self.store.promote_candidate(source_id)
        except StoreCorrupt as exc:
            self.store.set_blocked(source_id, "promotion_failed", str(exc))
            raise

    def _reverify_candidate(self, source_id: str,
                            cand: CandidateHead) -> None:
        # Streams rows and keeps only one leaf at a time.  Inclusion paths are
        # not persisted (they are not needed downstream), so the root is
        # recomputed; individual inclusion proofs were verified before
        # staging.  The root check plus the verified staging boundary is the
        # promotion invariant.
        expected = cand.base_size
        for index, _leaf in self.store.iter_candidate_entries(
                source_id, cand.base_size, cand.size):
            if index != expected:
                raise StoreCorrupt("candidate staging hole at promotion")
            expected += 1
        if expected != cand.size:
            raise StoreCorrupt("candidate staging incomplete at promotion")

    # -- public API ---------------------------------------------------------
    def status(self, source_id: str) -> dict:
        self._config(source_id)
        snapshot = self.store.committed_snapshot(source_id)
        cand = self.store.candidate(source_id)
        source = self.store.get_source(source_id)
        out = {
            "source_id": source_id,
            "committed": snapshot,
            "candidate": None,
            "gaps": [],
            "staged_count": 0,
            "blocked": bool(source["blocked"]),
            "blocked_reason": source["blocked_reason"],
            "blocked_detail": source["blocked_detail"],
            "deferred_heads": [
                {"tree_size": d["tree_size"], "root": d["root"].hex(),
                 "epoch": d["epoch"], "relation": d["relation"],
                 "observed_at": d["observed_at"]}
                for d in self.store.deferred_heads(source_id)],
            "generations": [
                {"epoch": g.epoch, "public_key": g.public_key.hex(),
                 "activation_size": g.activation_size,
                 "verified": g.verified, "activated": g.activated}
                for g in self.store.list_generations(source_id)],
            "active_epoch": self._active_epoch_ceiling(source_id),
        }
        if snapshot["entry_count"] != snapshot["tree_size"]:
            raise StoreCorrupt("committed head/entry count mismatch")
        if cand is not None:
            gaps = self.store.missing_ranges(source_id, cand.base_size,
                                             cand.size)
            out["candidate"] = {
                "tree_size": cand.size, "root": cand.root.hex(),
                "epoch": cand.epoch, "timestamp_ms": cand.timestamp_ms,
                "base_size": cand.base_size,
                "base_root": cand.base_root.hex(),
                "staged_count": self.store.staged_count(source_id),
                "complete": not gaps,
            }
            out["gaps"] = [[a, b] for a, b in gaps]
            out["staged_count"] = self.store.staged_count(source_id)
        return out

    def diagnostics(self, source_id: str, with_content: bool = False,
                    limit: Optional[int] = None) -> dict:
        self._config(source_id)
        cand = self.store.candidate(source_id)
        out = {
            "source_id": source_id,
            "blocked": bool(self.store.get_source(source_id)["blocked"]),
            "candidate": None,
            "staged": [],
            "missing": [],
            "deferred_heads": [
                {"tree_size": d["tree_size"], "root": d["root"].hex(),
                 "epoch": d["epoch"], "relation": d["relation"],
                 "observed_at": d["observed_at"],
                 "frame_b64": base64.b64encode(d["frame"]).decode("ascii")}
                for d in self.store.deferred_heads(source_id)],
            "observations": self.store.observations(source_id),
            "events": self.store.events(source_id),
            "page_stats": self.store.page_stats(source_id),
            "db": self.store.db_stats(),
        }
        if cand is not None:
            out["candidate"] = {"tree_size": cand.size,
                                "root": cand.root.hex(),
                                "base_size": cand.base_size,
                                "base_root": cand.base_root.hex(),
                                "epoch": cand.epoch}
            out["staged"] = self.store.staged_details(
                source_id, limit=limit, with_content=with_content)
            out["missing"] = [[a, b] for a, b in self.store.missing_ranges(
                source_id, cand.base_size, cand.size)]
        return out

    def entries(self, source_id: str, start: int, end: int
                ) -> Iterator[tuple[int, bytes]]:
        """Stream committed entries; validates the range before returning."""
        self._config(source_id)
        size = self.store.committed_head(source_id).size
        if not 0 <= start <= end <= size:
            raise StoreError(
                f"range [{start},{end}) outside committed size {size}")
        return self.store.iter_committed_entries(source_id, start, end)

    def resume(self, source_id: str) -> dict:
        self._config(source_id)
        source = self.store.get_source(source_id)
        if not source["blocked"]:
            return {"source_id": source_id, "changed": False,
                    "candidate_size": (self.store.candidate(source_id).size
                                       if self.store.candidate(source_id)
                                       else None)}
        cand = self.store.candidate(source_id)
        self.store.clear_blocked(source_id, "resume")
        return {"source_id": source_id, "changed": True,
                "candidate_size": cand.size if cand else None,
                "note": ("continuing the same candidate; no replacement "
                         "head is accepted")
                if cand else ("no candidate retained; next sync restarts "
                              "from the committed checkpoint")}

    def discard_candidate(self, source_id: str) -> dict:
        self._config(source_id)
        cand = self.store.candidate(source_id)
        source = self.store.get_source(source_id)
        if cand is None:
            if source["blocked"]:
                self.store.clear_blocked(source_id, "discard")
            return {"source_id": source_id, "discarded": False,
                    "committed_size": self.store.committed_head(
                        source_id).size}
        size = self.store.discard_candidate(source_id, "explicit")
        if source["blocked"]:
            self.store.clear_blocked(source_id, "discard")
        return {"source_id": source_id, "discarded": True,
                "discarded_size": size,
                "committed_size": self.store.committed_head(
                    source_id).size}
