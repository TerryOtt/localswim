"""Read-only, bounded restore admission and exact referenced-byte inventories.

This preparation allocates no operation/generation identity and writes nothing.
Its results are observations, not a sealed plan or authority to apply a restore.
"""

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass
from typing import cast
lazy import pathlib

from . import attachments, board_state
from .restore_paths import CheckedPath, check_role_paths

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_REFERENCES = 10_000
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_DEPTH = 128
DIRECTORY_ATTRIBUTE = 0x10
LEGACY_GENERATION = "00000000-0000-0000-0000-000000000000"
RETAINED_LIMITATION = "present and sealed from supplied backup; original upload digest unavailable"


class CandidateError(ValueError):
    """A restore input cannot safely participate in a later restore plan."""


class _ReferenceConflictError(CandidateError):
    """Invalid logical references cannot grant authority to follow current files."""


class _Proofs(ExitStack):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[CheckedPath] = []

    def capture(self, path: pathlib.Path, *, allow_missing: bool = False) -> CheckedPath:
        proof = self.enter_context(CheckedPath.capture(path, allow_missing=allow_missing))
        self.paths.append(proof)
        return proof


class _Budget:
    def __init__(self) -> None:
        self.paths: set[pathlib.Path] = set()
        self.bytes = 0

    def observe(self, path: pathlib.Path, size: int = 0) -> None:
        if path in self.paths:
            return
        self.paths.add(path)
        self.bytes += size
        if len(self.paths) > MAX_REFERENCES:
            raise CandidateError("restore inventory exceeds 10,000 distinct referenced files")
        if self.bytes > MAX_TOTAL_BYTES:
            raise CandidateError("restore inventory exceeds 4 GiB total attachment bytes")


@dataclass(frozen=True)
class Reference:
    """One exact immutable path, with authenticated metadata only when active."""

    relative_path: str
    active: attachments.Attachment | None


@dataclass(frozen=True)
class BlobInventory:
    """Observed bytes; a retained digest authenticates this observation only."""

    relative_path: str
    status: str
    size: int | None
    sha256: str | None
    active: bool
    original_digest_available: bool


@dataclass(frozen=True)
class SnapshotInventory:
    """Raw preservation and validated recovery are deliberately distinct."""

    path: pathlib.Path
    status: str
    raw_sha256: str | None
    raw_size: int | None
    schema: int | None
    board: board_state.Board | None
    blobs: tuple[BlobInventory, ...]
    recovery: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class CandidateInventory:
    """Isolated admitted candidate plus current recovery/provenance observations."""

    board: board_state.Board
    source: SnapshotInventory
    target: SnapshotInventory
    promoted_cards: tuple[str, ...]
    policy_changed: bool | None
    actors_changed: bool | None
    known_next_ticket: int
    preferred_port: int
    limitations: tuple[str, ...]


def _object(pairs: list[tuple[str, board_state.JsonValue]]) -> board_state.JsonObject:
    result: board_state.JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError("duplicate JSON object key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise CandidateError("non-finite JSON number")


def _parse(blob: bytes) -> board_state.JsonObject:
    try:
        raw = cast(
            "board_state.JsonValue",
            json.loads(blob.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CandidateError("snapshot is not valid bounded JSON") from exc
    pending = [(raw, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise CandidateError("snapshot JSON exceeds maximum nesting depth")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    if not isinstance(raw, dict):
        raise CandidateError("snapshot must be a JSON object")
    return raw


def _structural(raw: board_state.JsonObject) -> tuple[board_state.Board, int]:
    schema = raw.get("schema")
    if type(schema) is not int or schema not in {4, 5} or not isinstance(raw.get("policy"), dict):
        raise CandidateError("restore requires embedded-policy schema 4 or 5")
    adapted = dict(raw)
    if schema == board_state.GENERATION_PREVIOUS_SCHEMA:
        # A codec sentinel is never an allocated or proposed restore generation.
        adapted["schema"] = board_state.SCHEMA
        adapted["generation"] = LEGACY_GENERATION
        adapted.setdefault("revision", 0)
    return board_state.Board.from_json(adapted, "restore snapshot", install_context=False), schema


def exact_references(board: board_state.Board) -> tuple[Reference, ...]:
    """Enumerate active and every retained event reference, never a filesystem glob."""
    result: dict[str, Reference] = {}
    identities: dict[str, tuple[int, str]] = {}
    for item in board.items:
        descriptors = [(event.attachment_id, event.extension) for event in item.attachment_history]
        descriptors.extend((file.id, attachments.extension(file.name)) for file in item.attachments)
        active = {file.id: file for file in item.attachments}
        for aid, suffix in descriptors:
            identity = (item.ticket, suffix)
            if identities.setdefault(aid, identity) != identity:
                raise _ReferenceConflictError(
                    "attachment ID has conflicting card or extension references"
                )
            if item.ticket < 1:
                raise _ReferenceConflictError("attachment reference requires a positive ticket")
            relative = f"attachments/{item.ticket:04d}/{aid}{suffix}"
            result[relative] = Reference(relative, active.get(aid))
            if len(result) > MAX_REFERENCES:
                raise CandidateError("snapshot exceeds 10,000 distinct attachment references")
    return tuple(result[key] for key in sorted(result))


def _matches(blob: bytes, file: attachments.Attachment) -> bool:
    return (
        len(blob) == file.size
        and hashlib.sha256(blob).hexdigest() == file.sha256
        and attachments.media_type(blob) == file.media_type
    )


def _blobs(
    snapshot: CheckedPath,
    references: tuple[Reference, ...],
    stack: _Proofs,
    budget: _Budget,
    *,
    required: bool,
) -> tuple[BlobInventory, ...]:
    result: list[BlobInventory] = []
    for reference in references:
        proof = stack.capture(snapshot.path.parent / reference.relative_path, allow_missing=True)
        if not proof.exists:
            budget.observe(proof.path)
            if required:
                raise CandidateError("supplied backup is missing referenced attachment bytes")
            result.append(
                BlobInventory(
                    reference.relative_path,
                    "missing",
                    None,
                    None,
                    bool(reference.active),
                    reference.active is not None,
                )
            )
            continue
        blob = proof.read_bytes(attachments.MAX_BYTES)
        budget.observe(proof.path, len(blob))
        valid = bool(blob) and (reference.active is None or _matches(blob, reference.active))
        if not valid and required:
            raise CandidateError("supplied attachment bytes mismatch active size, digest or type")
        result.append(
            BlobInventory(
                reference.relative_path,
                "available" if valid else "corrupt",
                len(blob),
                hashlib.sha256(blob).hexdigest(),
                reference.active is not None,
                reference.active is not None,
            )
        )
    return tuple(result)


def _target(proof: CheckedPath, stack: _Proofs, budget: _Budget) -> SnapshotInventory:
    if not proof.exists:
        return SnapshotInventory(
            proof.path,
            "missing",
            None,
            None,
            None,
            None,
            (),
            "raw-only",
            ("current snapshot is missing; no validated recovery point",),
        )
    blob = proof.read_bytes(MAX_SNAPSHOT_BYTES)
    board: board_state.Board | None = None
    schema: int | None = None
    limitations: tuple[str, ...] = ()
    try:
        board, schema = _structural(_parse(blob))
        problems = board.verify()
    except ValueError, TypeError, RecursionError:
        problems = ["current snapshot is malformed"]
    try:
        references = exact_references(board) if board is not None else ()
    except _ReferenceConflictError:
        problems.append("current logical attachment references conflict")
        references = ()
    if problems:
        # Readable corrupt raw is useful recovery material, never validated state.
        limitations = ("raw preservation, not a validated recovery point",)
    inventory = _blobs(proof, references, stack, budget, required=False)
    recovery = "raw-only" if problems else "complete"
    if not problems and any(file.status != "available" for file in inventory):
        recovery = "incomplete"
        limitations = ("current referenced bytes are missing or corrupt; recovery is incomplete",)
    return SnapshotInventory(
        proof.path,
        "corrupt" if problems else "valid",
        hashlib.sha256(blob).hexdigest(),
        len(blob),
        schema,
        board,
        inventory,
        recovery,
        limitations,
    )


def preflight(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    checkpoint: pathlib.Path | None = None,
    staging: pathlib.Path | None = None,
) -> CandidateInventory:
    """Admit a supplied snapshot and inventory bytes without writing any path.

    All proofs are held until the final component revalidation. Future phases must
    reacquire proofs; returning an inventory does not retain filesystem authority.
    Combined source/current/collision paths share the 10,000-reference and 4 GiB
    caps. Re-reading one physical path does not consume that budget twice.
    """
    with _Proofs() as stack:
        roles = {
            "source": stack.capture(source),
            "target": stack.capture(target, allow_missing=True),
        }
        for name, path in (("checkpoint", checkpoint), ("staging", staging)):
            if path is not None:
                roles[name] = stack.capture(path, allow_missing=True)
        check_role_paths(**roles)
        source_proof, target_proof = roles["source"], roles["target"]
        roots = {
            "source_attachments": stack.capture(
                source_proof.path.parent / "attachments", allow_missing=True
            ),
            "target_attachments": stack.capture(
                target_proof.path.parent / "attachments", allow_missing=True
            ),
        }
        directory_roles = {
            **roots,
            **{key: value for key, value in roles.items() if key in {"checkpoint", "staging"}},
        }
        for proof in directory_roles.values():
            if proof.exists and (
                proof.identity is None or not proof.identity.attributes & DIRECTORY_ATTRIBUTE
            ):
                raise CandidateError("attachment, checkpoint and staging roles require directories")
        check_role_paths(
            **directory_roles,
        )
        raw_blob = source_proof.read_bytes(MAX_SNAPSHOT_BYTES)
        board, schema = _structural(_parse(raw_blob))
        if problems := board.verify(include_priority_floors=False):
            raise CandidateError("source audit rejected: " + "; ".join(problems))
        promoted = board.enforce_dependency_priorities(board.cli_user)
        if problems := board.verify():
            raise CandidateError("source candidate rejected: " + "; ".join(problems))
        budget = _Budget()
        source_blobs = _blobs(source_proof, exact_references(board), stack, budget, required=True)
        current = _target(target_proof, stack, budget)
        # Every candidate path is immutable even when absent from current metadata.
        for file in source_blobs:
            existing = stack.capture(
                target_proof.path.parent / file.relative_path, allow_missing=True
            )
            if existing.exists:
                blob = existing.read_bytes(attachments.MAX_BYTES)
                budget.observe(existing.path, len(blob))
                if len(blob) != file.size or hashlib.sha256(blob).hexdigest() != file.sha256:
                    raise CandidateError("immutable target attachment path has conflicting bytes")
        limitations = tuple(
            dict.fromkeys(
                (
                    *((RETAINED_LIMITATION,) if any(not f.active for f in source_blobs) else ()),
                    *current.limitations,
                    "inventory requires fresh path and byte validation before later use",
                )
            )
        )
        source_inventory = SnapshotInventory(
            source_proof.path,
            "valid",
            hashlib.sha256(raw_blob).hexdigest(),
            len(raw_blob),
            schema,
            board,
            source_blobs,
            "complete",
            tuple(x for x in limitations if x == RETAINED_LIMITATION),
        )
        previous = current.board
        result = CandidateInventory(
            board,
            source_inventory,
            current,
            promoted,
            previous.policy.to_json() != board.policy.to_json() if previous else None,
            (previous.users, previous.browser_user, previous.cli_user, previous.default_owner)
            != (board.users, board.browser_user, board.cli_user, board.default_owner)
            if previous
            else None,
            max(board.next_ticket, previous.next_ticket if previous else 0),
            previous.port if previous else board.port,
            limitations,
        )
        for proof in stack.paths:
            proof.revalidate()
        return result
