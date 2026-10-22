"""Inert schema-5 preparation; production entrypoints remain schema 4/protocol 1.

Nothing here reads/writes files, installs ambient policy, or allocates UUIDs implicitly.
The private snapshot envelope deliberately has no Board.to_json/save interface.
"""

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from . import board_state

__all__ = [
    "_parse_schema5",
    "_prepare_initial_schema5",
    "_prepare_migration_schema5",
    "_serialize_schema5",
]

if TYPE_CHECKING:
    from collections.abc import Callable

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOKEN = re.compile(rf'"generation-({_UUID})-revision-(0|[1-9][0-9]*)"')
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SCHEMA5 = 5


class InvalidWriteToken(board_state.BoardError):
    """Missing or malformed complete precondition (future adapter: 428)."""


class StaleWriteToken(board_state.BoardError):
    """Valid complete precondition disagrees with accepted state (future adapter: 412)."""


def _generation(raw: object) -> str:
    if not isinstance(raw, str) or re.fullmatch(_UUID, raw) is None:
        raise board_state.BoardError("generation must be a canonical lowercase UUID")
    if str(uuid.UUID(raw)) != raw:
        raise board_state.BoardError("generation must be a canonical lowercase UUID")
    return raw


def _revision(raw: object) -> int:
    if type(raw) is not int or raw < 0:
        raise board_state.BoardError("revision must be a non-Boolean nonnegative integer")
    return raw


@dataclass(frozen=True)
class WriteToken:
    generation: str
    revision: int

    def __post_init__(self) -> None:
        _generation(self.generation)
        _revision(self.revision)

    def format(self) -> str:
        return f'"generation-{self.generation}-revision-{self.revision}"'

    @classmethod
    def parse(cls, raw: object) -> WriteToken:
        match = _TOKEN.fullmatch(raw) if isinstance(raw, str) else None
        if match is None:
            raise InvalidWriteToken("a complete quoted generation/revision write token is required")
        try:
            return cls(match[1], int(match[2]))
        except (ValueError, board_state.BoardError) as exc:
            raise InvalidWriteToken("invalid generation/revision write token") from exc

    def require_match(self, raw: object) -> None:
        if self.parse(raw) != self:
            raise StaleWriteToken("state_changed")


def _digest(raw: object, field: str) -> str:
    if not isinstance(raw, str) or _DIGEST.fullmatch(raw) is None:
        raise board_state.BoardError(f"restoreReceipt.{field} must be a lowercase SHA-256 digest")
    return raw


def _timestamp(raw: object) -> None:
    if not isinstance(raw, str):
        raise board_state.BoardError("restoreReceipt.timestamp must be an RFC 3339 timestamp")
    board_state.parse_activity_bound(raw, "restoreReceipt.timestamp")


@dataclass(frozen=True)
class _RestoreReceipt:
    operation_id: str
    plan_digest: str
    source_snapshot_digest: str
    prior_raw_digest: str
    timestamp: str

    def __post_init__(self) -> None:
        _generation(self.operation_id)
        _digest(self.plan_digest, "planDigest")
        _digest(self.source_snapshot_digest, "sourceSnapshotDigest")
        if self.prior_raw_digest != "missing":
            _digest(self.prior_raw_digest, "priorRawDigest")
        _timestamp(self.timestamp)

    def to_json(self) -> board_state.JsonObject:
        return {
            "operationId": self.operation_id,
            "planDigest": self.plan_digest,
            "sourceSnapshotDigest": self.source_snapshot_digest,
            "priorRawDigest": self.prior_raw_digest,
            "timestamp": self.timestamp,
            "authority": "local-maintenance",
        }

    @classmethod
    def from_json(cls, raw: board_state.JsonValue) -> _RestoreReceipt:
        fields = {
            "operationId",
            "planDigest",
            "sourceSnapshotDigest",
            "priorRawDigest",
            "timestamp",
            "authority",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise board_state.BoardError(
                "restoreReceipt must contain exactly the maintenance fields"
            )
        if raw["authority"] != "local-maintenance":
            raise board_state.BoardError("restoreReceipt.authority must be 'local-maintenance'")
        # Individual validators reject nonstrings; casts do not convert input.
        return cls(
            cast("str", raw["operationId"]),
            cast("str", raw["planDigest"]),
            cast("str", raw["sourceSnapshotDigest"]),
            cast("str", raw["priorRawDigest"]),
            cast("str", raw["timestamp"]),
        )


@dataclass(frozen=True)
class _Schema5Snapshot:
    board: board_state.Board
    generation: str
    restore_receipt: _RestoreReceipt | None = None

    def __post_init__(self) -> None:
        _generation(self.generation)
        _revision(self.board.revision)

    @property
    def write_token(self) -> WriteToken:
        return WriteToken(self.generation, self.board.revision)


def _admitted_board(
    raw: board_state.JsonValue, where: str, *, reconcile: bool
) -> board_state.Board:
    """Use #0119's isolated parse, supported adoption, then complete audit verification."""
    candidate = board_state.Board.from_json(raw, where, install_context=False)
    if reconcile:
        candidate.enforce_dependency_priorities(candidate.cli_user)
    if problems := candidate.verify():
        raise board_state.BoardError(f"{where}: snapshot rejected: " + "; ".join(problems))
    return candidate


def _parse_schema5(raw: board_state.JsonValue, where: str) -> _Schema5Snapshot:
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != _SCHEMA5:
        raise board_state.BoardError(f"{where}: expected private schema 5")
    generation = _generation(raw.get("generation"))
    _revision(raw.get("revision"))
    receipt = _RestoreReceipt.from_json(raw["restoreReceipt"]) if "restoreReceipt" in raw else None
    body = dict(raw)
    body["schema"] = board_state.SCHEMA
    body.pop("generation")
    body.pop("restoreReceipt", None)
    return _Schema5Snapshot(_admitted_board(body, where, reconcile=False), generation, receipt)


def _serialize_schema5(snapshot: _Schema5Snapshot) -> board_state.JsonObject:
    # Validate mutable Board state without reconciliation, allocation or ambient publication.
    body = snapshot.board.to_json()
    _admitted_board(body, "private schema-5 serialization", reconcile=False)
    _generation(snapshot.generation)
    _revision(snapshot.board.revision)
    body["schema"] = _SCHEMA5
    body["generation"] = snapshot.generation
    if snapshot.restore_receipt is not None:
        body["restoreReceipt"] = snapshot.restore_receipt.to_json()
    return body


def _prepare_initial_schema5(
    raw: board_state.JsonValue, where: str, *, allocate_generation: Callable[[], str]
) -> _Schema5Snapshot:
    """Explicit initialization preparation; retain the supplied initial revision."""
    candidate = _admitted_board(raw, where, reconcile=True)
    return _Schema5Snapshot(candidate, allocate_generation())


def _prepare_migration_schema5(
    raw: board_state.JsonValue, where: str, *, allocate_generation: Callable[[], str]
) -> _Schema5Snapshot:
    """Pure schema-4 migration candidate; allocate once and advance revision once."""
    candidate = _admitted_board(raw, where, reconcile=True)
    candidate.revision += 1
    return _Schema5Snapshot(candidate, allocate_generation())
