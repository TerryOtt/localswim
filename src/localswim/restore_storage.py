"""Internal, append-only restore preparation; never replace the board snapshot.

The caller MUST hold the existing launch gate, writer lease and transaction lock
for the canonical target before entry, and retain them until the prepared handle
is closed (or through the subsequent commit). This module does not acquire or
authenticate maintenance authority. Its filesystem proofs are separate from that
caller precondition. Acquisition and commit classification belong to the next
restore phase.

Every failure preserves existing bytes, partial staging/checkpoints and newly
appended orphan bytes. Closing a handle releases proofs only. Flushes provide the
bounded application-interruption contract, not a space reservation or a physical
power-loss guarantee.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, cast
lazy import pathlib

from . import attachments, board_state, restore_candidate, restore_plan
from .restore_paths import CheckedPath, CreationSession, check_role_paths

if TYPE_CHECKING:
    from collections.abc import Mapping


class StorageError(ValueError):
    """The sealed preparation cannot safely complete without replacing bytes."""


@dataclass(frozen=True)
class PreparedBlob:
    relative_path: str
    size: int
    sha256: str
    appended: bool


def _hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _object(raw: board_state.JsonValue) -> board_state.JsonObject:
    return cast("board_state.JsonObject", raw)


def _rows(raw: board_state.JsonValue) -> list[board_state.JsonObject]:
    return cast("list[board_state.JsonObject]", raw)


def _json(raw: board_state.JsonValue) -> bytes:
    return restore_plan.canonical_json(raw) + b"\n"


def _observation(proof: CheckedPath, maximum: int) -> board_state.JsonObject:
    data = proof.read_bytes(maximum) if proof.exists else None
    return {
        "path": str(proof.path),
        "identity": cast("board_state.JsonValue", asdict(proof.identity))
        if proof.identity is not None
        else None,
        "digest": _hash(data) if data is not None else "missing",
        "size": len(data) if data is not None else None,
    }


def _check_bytes(data: bytes, row: board_state.JsonObject) -> None:
    if (len(data), _hash(data)) != (row["size"], row["sha256"]):
        raise StorageError("referenced bytes differ from sealed manifest")


def _check_expiry(core: board_state.JsonObject) -> None:
    if datetime.now(UTC) >= datetime.fromisoformat(cast("str", core["expiresAt"])):
        raise StorageError("sealed plan expired during preparation")


def _space_check(requirements: Mapping[pathlib.Path, int]) -> None:
    """Aggregate payload/metadata estimates by volume; actual writes can still fail."""
    volumes: dict[str, tuple[pathlib.Path, int]] = {}
    for path, count in requirements.items():
        volume = os.path.normcase(path.anchor)
        anchor, previous = volumes.get(volume, (pathlib.Path(path.anchor), 0))
        volumes[volume] = anchor, previous + count
    for anchor, needed in volumes.values():
        if shutil.disk_usage(anchor).free < needed:
            raise StorageError("insufficient available space for restore preparation estimate")


def _estimate(
    sealed: restore_plan.SealedPlan,
    target: pathlib.Path,
    staging: pathlib.Path,
    checkpoint: pathlib.Path,
) -> dict[pathlib.Path, int]:
    core = sealed.core
    source, current = _object(core["source"]), _object(core["current"])
    source_rows, current_rows = _rows(core["manifest"]), _rows(current["blobs"])
    observations = {cast("str", row["path"]): row for row in _rows(core["observations"])}
    missing = [
        row
        for row in source_rows
        if observations[str(target.parent / cast("str", row["relative_path"]))]["digest"]
        == "missing"
    ]
    available = [row for row in current_rows if row["size"] is not None]
    # Include directory entries and bounded manifests as well as exact payloads.
    # This is deliberately an estimate, never a promise of allocated capacity.
    metadata = 4096
    return {
        staging: cast("int", source["rawSize"])
        + len(sealed.final_snapshot)
        + sum(cast("int", row["size"]) for row in source_rows)
        + metadata * (2 * len(source_rows) + 5),
        checkpoint: cast("int", current["rawSize"] or 0)
        + len(sealed.document)
        + len(sealed.final_snapshot)
        + sum(cast("int", row["size"]) for row in available)
        + metadata * (3 * len(current_rows) + 10),
        target: sum(cast("int", row["size"]) for row in missing)
        + metadata * (2 * len(missing) + 2),
    }


@dataclass(frozen=True)
class PreparedRestore:
    """Frozen content plus live proofs; closing never deletes or releases caller locks.

    The caller must keep maintenance ownership for this handle's entire lifetime.
    A later commit uses ``final_snapshot`` directly, never rereads the supplied
    backup. No snapshot replacement operation is exposed here.
    """

    plan_document: bytes
    plan_digest: str
    final_snapshot: bytes
    operation_id: str
    manifest: bytes
    checkpoint: pathlib.Path
    staging: pathlib.Path
    blobs: tuple[PreparedBlob, ...]
    _session: CreationSession

    def revalidate(self) -> None:
        """Refuse stale/closed proofs before passing this handle to the next phase."""
        self._session.revalidate()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        self.revalidate()
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


def _capture_observations(
    session: CreationSession, core: board_state.JsonObject
) -> dict[pathlib.Path, CheckedPath]:
    source = pathlib.Path(cast("str", _object(core["source"])["path"]))
    target = pathlib.Path(cast("str", core["target"]))
    proofs: dict[pathlib.Path, CheckedPath] = {}
    for row in _rows(core["observations"]):
        path = pathlib.Path(cast("str", row["path"]))
        proof = session.capture(path, allow_missing=True, directory=False)
        maximum = (
            restore_candidate.MAX_SNAPSHOT_BYTES
            if path in {source, target}
            else attachments.MAX_BYTES
        )
        if _observation(proof, maximum) != row:
            raise StorageError("fresh path identity or bytes differ from reviewed observations")
        proofs[path] = proof
    session.revalidate()
    return proofs


def _copy(  # noqa: PLR0913 -- exact bounded native copy contract
    session: CreationSession,
    source: CheckedPath,
    destination: pathlib.Path,
    *,
    maximum: int,
    size: int,
    digest: str,
) -> CheckedPath:
    session.ensure_dir(destination.parent)
    return session.copy_file(
        source, destination, max_bytes=maximum, expected_size=size, expected_sha256=digest
    )


def _stage_source(
    session: CreationSession,
    sealed: restore_plan.SealedPlan,
    proofs: dict[pathlib.Path, CheckedPath],
    staging: pathlib.Path,
) -> dict[str, CheckedPath]:
    core = sealed.core
    source = _object(core["source"])
    source_path = pathlib.Path(cast("str", source["path"]))
    _copy(
        session,
        proofs[source_path],
        staging / "source.json",
        maximum=restore_candidate.MAX_SNAPSHOT_BYTES,
        size=cast("int", source["rawSize"]),
        digest=cast("str", source["rawDigest"]),
    )
    session.create_file(staging / "candidate.json", sealed.final_snapshot)
    staged: dict[str, CheckedPath] = {}
    for row in _rows(core["manifest"]):
        relative = cast("str", row["relative_path"])
        staged[relative] = _copy(
            session,
            proofs[source_path.parent / relative],
            staging / relative,
            maximum=attachments.MAX_BYTES,
            size=cast("int", row["size"]),
            digest=cast("str", row["sha256"]),
        )
    session.revalidate()
    return staged


def _checkpoint(
    session: CreationSession,
    sealed: restore_plan.SealedPlan,
    proofs: dict[pathlib.Path, CheckedPath],
    checkpoint: pathlib.Path,
) -> bytes:
    core = sealed.core
    current = _object(core["current"])
    target = pathlib.Path(cast("str", core["target"]))
    if current["status"] == "missing":
        session.create_file(checkpoint / "current.missing", b"missing\n")
    else:
        _copy(
            session,
            proofs[target],
            checkpoint / "current.json",
            maximum=restore_candidate.MAX_SNAPSHOT_BYTES,
            size=cast("int", current["rawSize"]),
            digest=cast("str", current["rawDigest"]),
        )
    session.create_file(checkpoint / "plan.json", sealed.document)
    session.create_file(checkpoint / "candidate.json", sealed.final_snapshot)
    for row in _rows(current["blobs"]):
        # Preserve readable corrupt bytes too; their manifest status stays corrupt.
        if row["size"] is not None:
            relative = cast("str", row["relative_path"])
            _copy(
                session,
                proofs[target.parent / relative],
                checkpoint / relative,
                maximum=attachments.MAX_BYTES,
                size=cast("int", row["size"]),
                digest=cast("str", row["sha256"]),
            )
    manifest = _json(
        {
            "format": 1,
            "operationId": core["operationId"],
            "planDigest": sealed.plan_digest,
            "finalSnapshotDigest": _hash(sealed.final_snapshot),
            "current": current,
            "acknowledgments": core["acknowledgments"],
            "limitations": core["limitations"],
            "candidateManifest": core["manifest"],
        }
    )
    session.create_file(checkpoint / "manifest.json", manifest)
    session.revalidate()
    # Each payload and manifest is flushed and verified by the native session.
    # The marker is last; directory handles do not promise power-loss durability.
    session.create_file(
        checkpoint / "complete.json",
        _json({"manifestDigest": _hash(manifest), "planDigest": sealed.plan_digest}),
    )
    session.revalidate()
    return manifest


def _append_candidate(
    session: CreationSession,
    core: board_state.JsonObject,
    proofs: dict[pathlib.Path, CheckedPath],
    staged: dict[str, CheckedPath],
    target: pathlib.Path,
) -> tuple[PreparedBlob, ...]:
    result: list[PreparedBlob] = []
    for row in _rows(core["manifest"]):
        relative = cast("str", row["relative_path"])
        proof = proofs[target.parent / relative]
        size, digest = cast("int", row["size"]), cast("str", row["sha256"])
        session.revalidate()
        if proof.exists:
            _check_bytes(proof.read_bytes(attachments.MAX_BYTES), row)
            appended = False
        else:
            _copy(
                session,
                staged[relative],
                target.parent / relative,
                maximum=attachments.MAX_BYTES,
                size=size,
                digest=digest,
            )
            appended = True
        result.append(PreparedBlob(relative, size, digest, appended))
    session.revalidate()
    return tuple(result)


def prepare_restore(
    document: bytes,
    *,
    expect_plan: str,
    target: pathlib.Path,
    staging: pathlib.Path,
    checkpoint: pathlib.Path,
) -> PreparedRestore:
    """Seal bytes and append immutable blobs under caller-held maintenance ownership.

    ``staging`` and ``checkpoint`` are exclusive new directories. Their immediate
    parents must already exist. They must be outside source/target directory roots
    and each other. The caller owns these destinations; no cleanup is performed.
    The target parent exists because real maintenance locks were acquired first.
    """
    sealed = restore_plan.verify_plan(document, expect_plan=expect_plan, target=target)
    core = sealed.core
    target = pathlib.Path(cast("str", core["target"]))
    source = pathlib.Path(cast("str", _object(core["source"])["path"]))
    session = CreationSession()
    try:
        check_role_paths(
            source=session.capture(source.parent, directory=True),
            target=session.capture(target.parent, directory=True),
            staging=session.capture(staging, allow_missing=True, directory=True),
            checkpoint=session.capture(checkpoint, allow_missing=True, directory=True),
        )
        # No file or directory creation before every fresh observation agrees.
        proofs = _capture_observations(session, core)
        _space_check(_estimate(sealed, target, staging, checkpoint))
        session.create_dir(staging)
        session.create_dir(checkpoint)
        staged = _stage_source(session, sealed, proofs, staging)
        manifest = _checkpoint(session, sealed, proofs, checkpoint)
        _check_expiry(core)
        blobs = _append_candidate(session, core, proofs, staged, target)
        # Own append transitions are tracked by the session. Re-running the plan's
        # original missing observations here would reject our own new blobs.
        session.revalidate()
        _check_expiry(core)
        original = next(row for row in _rows(core["observations"]) if row["path"] == str(target))
        if _observation(proofs[target], restore_candidate.MAX_SNAPSHOT_BYTES) != original:
            raise StorageError("current snapshot changed during preparation")  # noqa: TRY301
        return PreparedRestore(
            sealed.document,
            sealed.plan_digest,
            sealed.final_snapshot,
            cast("str", core["operationId"]),
            manifest,
            checkpoint,
            staging,
            blobs,
            session,
        )
    except BaseException:
        session.close()
        raise
