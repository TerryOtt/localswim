"""Sealed, bounded restore intentions. This module never writes any filesystem path.

The reviewed core digest is integrity evidence, not authentication. Exact final
snapshot bytes are an envelope payload; a plan alone is never accepted board state.
"""

import base64
import binascii
import hashlib
import json
import re
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
lazy import pathlib

from . import attachments, board_state, restore_candidate
from .restore_paths import CheckedPath, check_role_paths
from .write_protocol import RestoreReceipt, validate_generation

MAX_PLAN_BYTES = 96 * 1024 * 1024
EXPIRY_SECONDS = 30 * 60
MAX_PORT = 65535
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CORE_FIELDS = {
    "format",
    "operationId",
    "target",
    "source",
    "current",
    "manifest",
    "observations",
    "generation",
    "revision",
    "timestamp",
    "expiresAt",
    "preparedBodyDigest",
    "port",
    "nextTicket",
    "policyChanged",
    "actorsChanged",
    "policy",
    "actors",
    "promotedCards",
    "acknowledgments",
    "limitations",
}
_ENVELOPE_FIELDS = {"core", "planDigest", "finalSnapshotDigest", "finalSnapshotBase64"}


class PlanError(ValueError):
    """A sealed restore intention is malformed, stale, or differs from review."""


@dataclass(frozen=True)
class SealedPlan:
    document: bytes
    final_snapshot: bytes
    plan_digest: str

    @property
    def core(self) -> board_state.JsonObject:
        """Return a detached view; callers cannot mutate the sealed document."""
        return _mapping(_parse(self.document)["core"], "core")


def _check_values(raw: board_state.JsonValue) -> None:
    pending = [(raw, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > restore_candidate.MAX_JSON_DEPTH:
            raise PlanError("plan JSON exceeds maximum nesting depth")
        if isinstance(value, float):
            raise PlanError("floating-point values are forbidden in sealed JSON")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def canonical_json(raw: board_state.JsonValue) -> bytes:
    """The acyclic core/body domain: UTF-8, sorted compact JSON, no LF or floats."""
    _check_values(raw)
    try:
        return json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanError("sealed JSON cannot be canonically encoded") from exc


def _pairs(pairs: list[tuple[str, board_state.JsonValue]]) -> board_state.JsonObject:
    raw: board_state.JsonObject = {}
    for key, value in pairs:
        if key in raw:
            raise PlanError("duplicate JSON key")
        raw[key] = value
    return raw


def _constant(_value: str) -> object:
    raise PlanError("non-finite JSON number")


def _parse(blob: bytes) -> board_state.JsonObject:
    try:
        raw = cast(
            "board_state.JsonValue",
            json.loads(blob.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant),
        )
        _check_values(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanError("invalid bounded sealed JSON") from exc
    return _mapping(raw, "document")


def _mapping(raw: board_state.JsonValue, name: str) -> board_state.JsonObject:
    if not isinstance(raw, dict):
        raise PlanError(f"{name} must be an object")
    return raw


def _hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _digest(raw: object) -> str:
    if not isinstance(raw, str) or _DIGEST.fullmatch(raw) is None:
        raise PlanError("expected a lowercase SHA-256 digest")
    return raw


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise PlanError("timestamp must be a UTC timestamp")
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PlanError("invalid UTC timestamp") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise PlanError("timestamp must have UTC offset")
    return result


def _blobs(files: tuple[restore_candidate.BlobInventory, ...]) -> list[board_state.JsonValue]:
    return [cast("board_state.JsonValue", asdict(file)) for file in files]


def _summary(snapshot: restore_candidate.SnapshotInventory) -> board_state.JsonObject:
    board = snapshot.board
    return {
        "path": str(snapshot.path),
        "status": snapshot.status,
        "rawDigest": snapshot.raw_sha256 or "missing",
        "rawSize": snapshot.raw_size,
        "schema": snapshot.schema,
        "generation": board.generation if board and snapshot.schema == board_state.SCHEMA else None,
        "revision": board.revision if board else None,
        "nextTicket": board.next_ticket if board else None,
        "project": board.project if board else None,
        "recovery": snapshot.recovery,
        "blobs": _blobs(snapshot.blobs),
    }


def _observation(proof: CheckedPath, maximum: int) -> board_state.JsonObject:
    blob = proof.read_bytes(maximum) if proof.exists else None
    return {
        "path": str(proof.path),
        "identity": cast("board_state.JsonValue", asdict(proof.identity))
        if proof.identity
        else None,
        "digest": _hash(blob) if blob is not None else "missing",
        "size": len(blob) if blob is not None else None,
    }


def _observe(inventory: restore_candidate.CandidateInventory) -> list[board_state.JsonValue]:
    """Pin and read every source/current/collision path after admission."""
    paths = {inventory.source.path, inventory.target.path}
    for root, files in (
        (inventory.source.path.parent, inventory.source.blobs),
        (inventory.target.path.parent, inventory.target.blobs),
        (inventory.target.path.parent, inventory.source.blobs),
    ):
        paths.update(root / file.relative_path for file in files)
    result: list[board_state.JsonValue] = []
    with ExitStack() as stack:
        proofs = [
            stack.enter_context(CheckedPath.capture(path, allow_missing=True))
            for path in sorted(paths)
        ]
        for proof in proofs:
            maximum = (
                restore_candidate.MAX_SNAPSHOT_BYTES
                if proof.path in {inventory.source.path, inventory.target.path}
                else attachments.MAX_BYTES
            )
            result.append(_observation(proof, maximum))
        for proof in proofs:
            proof.revalidate()
    # Admission and the observations must concern the same bytes, including missing.
    observed = {
        cast("str", _mapping(row, "observation")["path"]): _mapping(row, "observation")
        for row in result
    }
    for snapshot in (inventory.source, inventory.target):
        if observed[str(snapshot.path)]["digest"] != (snapshot.raw_sha256 or "missing"):
            raise PlanError("snapshot drift during plan creation")
        for file in snapshot.blobs:
            if observed[str(snapshot.path.parent / file.relative_path)]["digest"] != (
                file.sha256 or "missing"
            ):
                raise PlanError("attachment drift during plan creation")
    for file in inventory.source.blobs:
        collision = observed[str(inventory.target.path.parent / file.relative_path)]
        if collision["digest"] not in {"missing", file.sha256}:
            raise PlanError("immutable target attachment drift during plan creation")
    return result


def _port(inventory: restore_candidate.CandidateInventory, port: int | None) -> int:
    if port is None:
        if inventory.target.board is None:
            raise PlanError("an explicit intended port is required for missing/unparseable current")
        port = inventory.target.board.port
    if type(port) is not int or not 1 <= port <= MAX_PORT:
        raise PlanError("intended port must be an integer from 1 through 65535")
    return port


def create_plan(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    port: int | None = None,
    acknowledge_raw_recovery: bool = False,
    acknowledge_incomplete_recovery: bool = False,
) -> SealedPlan:
    """Read admitted inputs and allocate/freeze all candidate content once."""
    if (
        type(acknowledge_raw_recovery) is not bool
        or type(acknowledge_incomplete_recovery) is not bool
    ):
        raise PlanError("recovery acknowledgments must be Boolean")
    inventory = restore_candidate.preflight(source, target)
    if inventory.target.recovery == "raw-only" and not acknowledge_raw_recovery:
        raise PlanError("missing/corrupt current requires explicit raw recovery acknowledgment")
    if inventory.target.recovery == "incomplete" and not acknowledge_incomplete_recovery:
        raise PlanError("incomplete current requires explicit recovery acknowledgment")
    intended_port = _port(inventory, port)
    observations = _observe(inventory)
    source_summary, current_summary = _summary(inventory.source), _summary(inventory.target)
    board = inventory.board
    board.generation = str(uuid.uuid4())
    current = inventory.target.board if inventory.target.status == "valid" else None
    board.revision = max(board.revision, current.revision if current else 0) + 1
    board.next_ticket = inventory.known_next_ticket
    board.port = intended_port
    body = board.to_json()
    body.pop("restoreReceipt", None)
    timestamp = datetime.now(UTC)
    operation = str(uuid.uuid4())
    core: board_state.JsonObject = {
        "format": 1,
        "operationId": operation,
        "target": str(inventory.target.path),
        "source": source_summary,
        "current": current_summary,
        "manifest": _blobs(inventory.source.blobs),
        "observations": observations,
        "generation": board.generation,
        "revision": board.revision,
        "timestamp": timestamp.isoformat(),
        "expiresAt": (timestamp + timedelta(seconds=EXPIRY_SECONDS)).isoformat(),
        "preparedBodyDigest": _hash(canonical_json(body)),
        "port": intended_port,
        "nextTicket": board.next_ticket,
        "policyChanged": inventory.policy_changed,
        "actorsChanged": inventory.actors_changed,
        "policy": body["policy"],
        "actors": {key: body[key] for key in ("users", "browserUser", "cliUser", "defaultOwner")},
        "promotedCards": list(inventory.promoted_cards),
        "acknowledgments": {
            "rawRecovery": acknowledge_raw_recovery,
            "incompleteRecovery": acknowledge_incomplete_recovery,
        },
        "limitations": list(inventory.limitations),
    }
    digest = _hash(canonical_json(core))
    receipt = RestoreReceipt(
        operation,
        digest,
        cast("str", source_summary["rawDigest"]),
        inventory.target.raw_sha256 or "missing",
        timestamp.isoformat(),
    )
    final = {**body, "restoreReceipt": receipt.to_json()}
    final_bytes = canonical_json(final) + b"\n"
    if len(final_bytes) > restore_candidate.MAX_SNAPSHOT_BYTES:
        raise PlanError("sealed candidate exceeds 64 MiB snapshot limit")
    admitted = board_state.Board.from_json(final, "sealed restore candidate", install_context=False)
    if problems := admitted.verify():
        raise PlanError("sealed output admission failed: " + "; ".join(problems))
    envelope: board_state.JsonObject = {
        "core": core,
        "planDigest": digest,
        "finalSnapshotDigest": _hash(final_bytes),
        "finalSnapshotBase64": base64.b64encode(final_bytes).decode("ascii"),
    }
    document = canonical_json(envelope) + b"\n"
    if len(document) > MAX_PLAN_BYTES:
        raise PlanError("sealed plan exceeds document limit")
    return SealedPlan(document, final_bytes, digest)


def _frozen_output(
    envelope: board_state.JsonObject, core: board_state.JsonObject, digest: str
) -> bytes:
    encoded = envelope["finalSnapshotBase64"]
    if not isinstance(encoded, str):
        raise PlanError("final snapshot payload must be base64")
    try:
        final = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PlanError("invalid final snapshot base64") from exc
    if len(final) > restore_candidate.MAX_SNAPSHOT_BYTES or not final.endswith(b"\n"):
        raise PlanError("final snapshot exceeds limit or lacks final LF")
    if _hash(final) != _digest(envelope["finalSnapshotDigest"]):
        raise PlanError("final snapshot byte digest mismatch")
    raw = _parse(final)
    # Validate the unique wire encoding while retaining the original sealed bytes.
    # The reviewed body/receipt then determine one exact final-byte domain, so an
    # edited envelope cannot substitute alternate whitespace or Unicode escapes.
    if final != canonical_json(raw) + b"\n":
        raise PlanError("final snapshot is not the unique canonical encoding with one LF")
    receipt = raw.pop("restoreReceipt", None)
    if _hash(canonical_json(raw)) != _digest(core["preparedBodyDigest"]):
        raise PlanError("prepared body digest mismatch")
    source = _mapping(core["source"], "source")
    current = _mapping(core["current"], "current")
    expected = RestoreReceipt(
        cast("str", core["operationId"]),
        digest,
        cast("str", source["rawDigest"]),
        cast("str", current["rawDigest"]),
        cast("str", core["timestamp"]),
    ).to_json()
    if receipt != expected:
        raise PlanError("restore receipt does not match reviewed core")
    for key in ("generation", "revision", "port", "nextTicket", "policy"):
        if raw.get(key) != core[key]:
            raise PlanError("frozen output differs from reviewed core")
    actors = _mapping(core["actors"], "actors")
    if actors != {key: raw.get(key) for key in ("users", "browserUser", "cliUser", "defaultOwner")}:
        raise PlanError("frozen actor summary mismatch")
    raw["restoreReceipt"] = receipt
    board = board_state.Board.from_json(raw, "sealed restore candidate", install_context=False)
    if problems := board.verify():
        raise PlanError("sealed output admission failed: " + "; ".join(problems))
    _check_manifest_output(core, board)
    return final


def _check_manifest_output(core: board_state.JsonObject, board: board_state.Board) -> None:
    if _blobs_manifest_paths(core["manifest"]) != [
        reference.relative_path for reference in restore_candidate.exact_references(board)
    ]:
        raise PlanError("manifest is not the exact sorted candidate reference inventory")
    manifest = cast("list[board_state.JsonObject]", core["manifest"])
    for row, reference in zip(manifest, restore_candidate.exact_references(board), strict=True):
        if row["active"] != (reference.active is not None) or row["original_digest_available"] != (
            reference.active is not None
        ):
            raise PlanError("manifest provenance flags disagree with frozen references")
        if reference.active and (row["size"], row["sha256"]) != (
            reference.active.size,
            reference.active.sha256,
        ):
            raise PlanError("manifest differs from frozen active attachment metadata")


def _validate_core(core: board_state.JsonObject) -> None:
    for key, minimum, maximum in (
        ("revision", 1, None),
        ("nextTicket", 1, None),
        ("port", 1, MAX_PORT),
    ):
        value = core[key]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise PlanError("invalid proposed revision, nextTicket or port")
    source = _mapping(core["source"], "source")
    current = _mapping(core["current"], "current")
    _validate_snapshot(source)
    _validate_snapshot(current)
    if source["status"] != "valid" or source["recovery"] != "complete":
        raise PlanError("source must be valid and complete")
    if type(source["revision"]) is not int or type(source["nextTicket"]) is not int:
        raise PlanError("source counters are required")
    prior_revision = current["revision"] if current["status"] == "valid" else 0
    if type(prior_revision) is not int:
        raise PlanError("valid current revision is required")
    if core["revision"] != max(source["revision"], prior_revision) + 1:
        raise PlanError("proposed revision is not newer than valid inputs")
    known_ticket = current["nextTicket"]
    if core["nextTicket"] != max(
        source["nextTicket"], known_ticket if type(known_ticket) is int else 0
    ):
        raise PlanError("proposed nextTicket discards a known allocation")
    if core["generation"] in (source["generation"], current["generation"]):
        raise PlanError("restore generation must be fresh")
    _validate_summaries(core, current)


def _validate_snapshot(snapshot: board_state.JsonObject) -> None:
    fields = {
        "path",
        "status",
        "rawDigest",
        "rawSize",
        "schema",
        "generation",
        "revision",
        "nextTicket",
        "project",
        "recovery",
        "blobs",
    }
    if set(snapshot) != fields or not isinstance(snapshot["path"], str):
        raise PlanError("invalid snapshot summary fields")
    if snapshot["status"] not in ("missing", "valid", "corrupt"):
        raise PlanError("invalid current status")
    if snapshot["recovery"] not in ("raw-only", "complete", "incomplete"):
        raise PlanError("invalid recovery summary")
    size = snapshot["rawSize"]
    if snapshot["status"] == "missing":
        if size is not None or snapshot["rawDigest"] != "missing":
            raise PlanError("invalid missing snapshot marker")
    elif type(size) is not int or not 0 <= size <= restore_candidate.MAX_SNAPSHOT_BYTES:
        raise PlanError("invalid snapshot byte length")
    else:
        _digest(snapshot["rawDigest"])
    schema = snapshot["schema"]
    if schema is not None and (
        type(schema) is not int
        or schema not in {board_state.SCHEMA, board_state.GENERATION_PREVIOUS_SCHEMA}
    ):
        raise PlanError("invalid snapshot schema")
    if schema == board_state.SCHEMA:
        validate_generation(snapshot["generation"])
    elif snapshot["generation"] is not None:
        raise PlanError("legacy/unparseable source cannot claim a generation")
    for key, minimum in (("revision", 0), ("nextTicket", 1)):
        value = snapshot[key]
        if value is not None and (type(value) is not int or value < minimum):
            raise PlanError("invalid snapshot revision or nextTicket")


def _validate_summaries(core: board_state.JsonObject, current: board_state.JsonObject) -> None:
    acknowledgments = _mapping(core["acknowledgments"], "acknowledgments")
    if set(acknowledgments) != {"rawRecovery", "incompleteRecovery"} or any(
        type(value) is not bool for value in acknowledgments.values()
    ):
        raise PlanError("invalid explicit recovery acknowledgments")
    if current["recovery"] == "raw-only" and not acknowledgments["rawRecovery"]:
        raise PlanError("raw recovery limitation is not acknowledged")
    if current["recovery"] == "incomplete" and not acknowledgments["incompleteRecovery"]:
        raise PlanError("incomplete recovery limitation is not acknowledged")
    for key in ("policyChanged", "actorsChanged"):
        if core[key] is not None and type(core[key]) is not bool:
            raise PlanError("policy/actor change summaries must be Boolean or unknown")
    for key in ("promotedCards", "limitations"):
        values = core[key]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise PlanError("plan limitations and promotion summaries must be string lists")


def _blobs_manifest_paths(raw: board_state.JsonValue) -> list[str]:
    if not isinstance(raw, list) or len(raw) > restore_candidate.MAX_REFERENCES:
        raise PlanError("manifest exceeds reference limit or is malformed")
    paths: list[str] = []
    total = 0
    for entry in raw:
        row = _mapping(entry, "manifest entry")
        if set(row) != {
            "relative_path",
            "status",
            "size",
            "sha256",
            "active",
            "original_digest_available",
        } or any(type(row[key]) is not bool for key in ("active", "original_digest_available")):
            raise PlanError("invalid source manifest fields")
        path, size = row.get("relative_path"), row.get("size")
        if (
            not isinstance(path, str)
            or re.fullmatch(r"attachments/[0-9]{4,}/[0-9a-f-]{36}\.[a-z0-9]+", path) is None
        ):
            raise PlanError("invalid manifest relative path")
        if type(size) is not int or not 1 <= size <= attachments.MAX_BYTES:
            raise PlanError("manifest byte size exceeds file limit")
        _digest(row.get("sha256"))
        if row.get("status") != "available":
            raise PlanError("source manifest must contain available bytes")
        total += size
        paths.append(path)
    if total > restore_candidate.MAX_TOTAL_BYTES or paths != sorted(set(paths)):
        raise PlanError("manifest exceeds byte limit or is not sorted and unique")
    return paths


def _observation_roles(core: board_state.JsonObject) -> dict[pathlib.Path, int]:
    source = _mapping(core["source"], "source")
    current = _mapping(core["current"], "current")
    roles: dict[pathlib.Path, int] = {}
    for snapshot in (source, current):
        name, files = snapshot.get("path"), snapshot.get("blobs")
        if not isinstance(name, str) or not isinstance(files, list):
            raise PlanError("invalid snapshot inventory")
        path = pathlib.Path(name)
        roles[path] = restore_candidate.MAX_SNAPSHOT_BYTES
        if len(files) > restore_candidate.MAX_REFERENCES:
            raise PlanError("current inventory exceeds reference limit")
        for file in files:
            row = _mapping(file, "blob")
            relative = row.get("relative_path")
            if (
                not isinstance(relative, str)
                or re.fullmatch(r"attachments/[0-9]{4,}/[0-9a-f-]{36}\.[a-z0-9]+", relative) is None
            ):
                raise PlanError("invalid inventory relative path")
            roles[path.parent / relative] = attachments.MAX_BYTES
    if source["blobs"] != core["manifest"]:
        raise PlanError("source manifest differs from source inventory")
    for relative in _blobs_manifest_paths(core["manifest"]):
        roles[pathlib.Path(cast("str", current["path"])).parent / relative] = attachments.MAX_BYTES
    if len(roles) > 2 * restore_candidate.MAX_REFERENCES + 2:
        raise PlanError("combined inventory exceeds reference limit")
    return roles


def _fresh_observations(core: board_state.JsonObject, target: pathlib.Path) -> None:
    rows = core["observations"]
    source = _mapping(core["source"], "source")
    current = _mapping(core["current"], "current")
    if not isinstance(rows, list) or len(rows) > 2 * restore_candidate.MAX_REFERENCES + 2:
        raise PlanError("invalid observation count")
    source_path = source.get("path")
    if not isinstance(source_path, str):
        raise PlanError("source path must be a string")
    roles = _observation_roles(core)
    with ExitStack() as stack:
        source_proof = stack.enter_context(CheckedPath.capture(pathlib.Path(source_path)))
        target_proof = stack.enter_context(CheckedPath.capture(target, allow_missing=True))
        check_role_paths(source=source_proof, target=target_proof)
        if str(target_proof.path) != core["target"] or current.get("path") != core["target"]:
            raise PlanError("canonical target differs from reviewed target")
        proofs: list[CheckedPath] = []
        actual: dict[str, board_state.JsonObject] = {}
        for value in rows:
            row = _mapping(value, "observation")
            name = row.get("path")
            if not isinstance(name, str) or name in actual:
                raise PlanError("invalid or duplicate observation path")
            path = pathlib.Path(name)
            # Paths are constrained before capture, never arbitrary envelope reads.
            if path not in roles:
                raise PlanError("observation path is outside exact sealed roles")
            proof = stack.enter_context(CheckedPath.capture(path, allow_missing=True))
            proofs.append(proof)
            actual[name] = _observation(proof, roles[path])
            if actual[name] != row:
                raise PlanError("source/target path identity or bytes drifted")
        if {pathlib.Path(name) for name in actual} != set(roles):
            raise PlanError("sealed observations omit a referenced role")
        _check_observation_claims(core, actual)
        for proof in (*proofs, source_proof, target_proof):
            proof.revalidate()


def _check_observation_claims(
    core: board_state.JsonObject, actual: dict[str, board_state.JsonObject]
) -> None:
    source = _mapping(core["source"], "source")
    current = _mapping(core["current"], "current")
    source_path = pathlib.Path(cast("str", source["path"]))
    target_path = pathlib.Path(cast("str", current["path"]))
    for snapshot in (source, current):
        observed = actual.get(cast("str", snapshot["path"]))
        if observed is None or observed["digest"] != snapshot.get("rawDigest"):
            raise PlanError("snapshot observation is missing or inconsistent")
        for file in cast("list[board_state.JsonObject]", snapshot["blobs"]):
            path = pathlib.Path(cast("str", snapshot["path"])).parent / cast(
                "str", file["relative_path"]
            )
            if (actual[str(path)]["digest"], actual[str(path)]["size"]) != (
                file["sha256"] or "missing",
                file["size"],
            ):
                raise PlanError("manifest byte claim differs from fresh observations")
    sizes = [
        cast("int", row["size"])
        for name, row in actual.items()
        if pathlib.Path(name) not in {source_path, target_path} and row["digest"] != "missing"
    ]
    if (
        len(sizes) > restore_candidate.MAX_REFERENCES
        or sum(sizes) > restore_candidate.MAX_TOTAL_BYTES
    ):
        raise PlanError("fresh referenced bytes exceed combined restore limits")
    for file in cast("list[board_state.JsonObject]", core["manifest"]):
        path = target_path.parent / cast("str", file["relative_path"])
        if actual[str(path)]["digest"] not in ("missing", file["sha256"]):
            raise PlanError("immutable target byte collision")


def _verify_plan(document: bytes, *, expect_plan: str, target: pathlib.Path) -> SealedPlan:
    """Check reviewed domains and fresh proofs; never regenerate frozen content."""
    if len(document) > MAX_PLAN_BYTES:
        raise PlanError("plan exceeds bounded document limit")
    envelope = _parse(document)
    if set(envelope) != _ENVELOPE_FIELDS:
        raise PlanError("unexpected sealed envelope fields")
    core = _mapping(envelope["core"], "core")
    if set(core) != _CORE_FIELDS or type(core["format"]) is not int or core["format"] != 1:
        raise PlanError("invalid format-1 core fields")
    digest = _hash(canonical_json(core))
    if digest != _digest(expect_plan) or digest != _digest(envelope["planDigest"]):
        raise PlanError("reviewed plan digest mismatch")
    validate_generation(core["operationId"])
    validate_generation(core["generation"])
    _validate_core(core)
    timestamp, expires = _timestamp(core["timestamp"]), _timestamp(core["expiresAt"])
    if expires - timestamp != timedelta(seconds=EXPIRY_SECONDS):
        raise PlanError("plan expiry must be exactly 30 minutes")
    if datetime.now(UTC) >= expires:
        raise PlanError("sealed plan has expired")
    final = _frozen_output(envelope, core, digest)
    _fresh_observations(core, target)
    return SealedPlan(document, final, digest)


def verify_plan(document: bytes, *, expect_plan: str, target: pathlib.Path) -> SealedPlan:
    """Verify bounded domains and proofs; normalize malformed envelope failures."""
    try:
        return _verify_plan(document, expect_plan=expect_plan, target=target)
    except (KeyError, TypeError, IndexError, OverflowError, board_state.BoardError) as exc:
        raise PlanError("malformed sealed restore plan") from exc
