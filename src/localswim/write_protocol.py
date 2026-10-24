"""Generation/revision write tokens and validated maintenance receipts."""

import re
import uuid
from dataclasses import dataclass
from typing import cast

from . import board_state

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOKEN = re.compile(rf'"generation-({_UUID})-revision-(0|[1-9][0-9]*)"')
_DIGEST = re.compile(r"[0-9a-f]{64}")


class InvalidWriteToken(board_state.BoardError):
    """Missing or malformed complete precondition (HTTP 428)."""


class StaleWriteToken(board_state.BoardError):
    """Valid complete precondition disagrees with accepted state (HTTP 412)."""


def validate_generation(raw: object) -> str:
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
        validate_generation(self.generation)
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
class RestoreReceipt:
    operation_id: str
    plan_digest: str
    source_snapshot_digest: str
    prior_raw_digest: str
    timestamp: str

    def __post_init__(self) -> None:
        validate_generation(self.operation_id)
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
    def from_json(cls, raw: board_state.JsonValue) -> RestoreReceipt:
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
