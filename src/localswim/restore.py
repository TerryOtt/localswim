"""Owned offline restore: one sealed snapshot replacement, then exact inspection.

Recovery/staging files are retained on every path. Only snapshot bytes and their
embedded receipt establish commit; checkpoints and filesystem sidecars do not.
Service lifecycle, rollout authority, rollback and garbage collection are separate.
"""

# Narrow private seams reuse accepted plan, preparation and maintenance boundaries.
# ruff: noqa: SLF001
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast
lazy import pathlib

from . import board_state, restore_candidate, restore_plan, restore_storage, service_ownership
from .restore_paths import CheckedPath, PathBoundaryError

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


@dataclass(frozen=True)
class RestoreResult:
    state: Literal["committed", "uncommitted", "unknown"]
    operation_id: str
    plan_digest: str
    generation: str
    final_digest: str
    detail: str | None = None


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sealed(document: bytes, expect_plan: str, target: pathlib.Path) -> restore_plan.SealedPlan:
    try:
        sealed = restore_plan._verify_sealed_plan(document, expect_plan=expect_plan, target=target)
    except (KeyError, TypeError, IndexError, OverflowError, board_state.BoardError) as exc:
        raise restore_plan.PlanError("malformed sealed restore plan") from exc
    # Preserve original spelling for alias detection before resolving lock paths.
    with CheckedPath.capture(target, allow_missing=True) as proof:
        if proof.identity is not None and proof.identity.attributes & 0x10:
            raise PathBoundaryError("Snapshot target must be a regular file or missing")
    return sealed


def _result(
    sealed: restore_plan.SealedPlan, state: Literal["committed", "uncommitted", "unknown"]
) -> RestoreResult:
    core = sealed.core
    return RestoreResult(
        state,
        cast("str", core["operationId"]),
        sealed.plan_digest,
        cast("str", core["generation"]),
        _hash(sealed.final_snapshot),
    )


def _inspect(sealed: restore_plan.SealedPlan, target: pathlib.Path) -> RestoreResult:
    """Exact-byte proof under caller-held ownership; unreadable/other state is unknown."""
    result = _result(sealed, "unknown")
    try:
        with CheckedPath.capture(target, allow_missing=True) as proof:
            data = proof.read_bytes(restore_candidate.MAX_SNAPSHOT_BYTES) if proof.exists else None
    except OSError, ValueError:
        return replace(
            result, detail="Snapshot could not be safely inspected; retain recovery bytes"
        )
    # Static verification already proved the sealed receipt, generation and final
    # digest. Full exact equality proves those fields together, without accepting
    # a receipt copied into a subsequently modified or damaged snapshot.
    if data == sealed.final_snapshot:
        return replace(result, state="committed")
    current = cast("board_state.JsonObject", sealed.core["current"])
    if (data is None and current["rawDigest"] == "missing") or (
        data is not None and _hash(data) == current["rawDigest"]
    ):
        return replace(result, state="uncommitted")
    return replace(result, detail="Snapshot differs from both sealed final and prior bytes")


def _require_stopped(sealed: restore_plan.SealedPlan, target: pathlib.Path) -> None:
    """Conservative legacy listener refusal remains inside all maintenance locks."""
    ports = {cast("int", sealed.core["port"])}
    try:
        with CheckedPath.capture(target, allow_missing=True) as proof:
            if proof.exists:
                data = proof.read_bytes(restore_candidate.MAX_SNAPSHOT_BYTES)
                raw = restore_plan._parse(data)
                port = raw.get("port")
                if type(port) is int and board_state.MIN_PORT <= port <= board_state.MAX_PORT:
                    ports.add(port)
    except restore_plan.PlanError:
        # Raw-only recovery uses the reviewed explicit intended port.
        pass
    for port in sorted(ports):
        board_state._require_board_port_stopped(target, {"port": port})


def _owned_call(target: pathlib.Path, operation: Callable[[], RestoreResult]) -> RestoreResult:
    """Preserve the owned result/primary failure while attempting every lease exit.

    No classification happens after an exit begins. Ordinary release failures add
    sanitized diagnostics to a result already observed under all leases. A primary
    failure, especially asynchronous interruption, remains the raised failure.
    """
    managers: tuple[AbstractContextManager[object], ...] = (
        service_ownership.launch_locked(target),
        service_ownership.writer_claim(target),
        board_state.locked(target),
    )
    entered: list[AbstractContextManager[object]] = []
    result: RestoreResult | None = None
    failure: BaseException | None = None
    cleanup_names: list[str] = []
    try:
        for manager in managers:
            manager.__enter__()
            entered.append(manager)
        result = operation()
    except BaseException as exc:  # noqa: BLE001 -- preserve primary through all lease exits
        failure = exc
    finally:
        for manager in reversed(entered):
            try:
                manager.__exit__(
                    type(failure) if failure is not None else None,
                    failure,
                    failure.__traceback__ if failure is not None else None,
                )
            except BaseException as cleanup:  # noqa: BLE001 -- attempt every entered lease exit
                if failure is not None:
                    failure.add_note(f"Restore lease release also failed: {type(cleanup).__name__}")
                elif isinstance(cleanup, Exception):
                    cleanup_names.append(type(cleanup).__name__)
                else:
                    failure = cleanup
    if failure is not None:
        if result is not None:
            failure.add_note(f"Restore outcome inspected under ownership: {result.state}")
        raise failure
    if result is None:
        raise RuntimeError("Restore ownership completed without a result")
    if cleanup_names:
        diagnostic = "Restore lease release failed: " + ", ".join(cleanup_names)
        return replace(
            result, detail=f"{result.detail}; {diagnostic}" if result.detail else diagnostic
        )
    return result


def inspect_restore(document: bytes, *, expect_plan: str, target: pathlib.Path) -> RestoreResult:
    """Inspect without expiry/source checks, under actual offline maintenance ownership."""
    sealed = _sealed(document, expect_plan, target)

    def operation() -> RestoreResult:
        _require_stopped(sealed, target)
        return _inspect(sealed, target)

    return _owned_call(target, operation)


def _commit(
    sealed: restore_plan.SealedPlan,
    target: pathlib.Path,
    staging: pathlib.Path,
    checkpoint: pathlib.Path,
) -> RestoreResult:
    prepared: restore_storage.PreparedRestore | None = None
    failure: BaseException | None = None
    try:
        prepared = restore_storage.prepare_restore(
            sealed.document,
            expect_plan=sealed.plan_digest,
            target=target,
            staging=staging,
            checkpoint=checkpoint,
        )
        # Preparation admitted and read back exact candidate/checkpoint/blob bytes.
        # Its live session pins them until the one terminal native transition.
        prepared.revalidate()
        prepared._session.replace_snapshot(
            target,
            sealed.final_snapshot,
            operation_id=prepared.operation_id,
            before_replace=lambda: restore_storage._check_expiry(sealed.core),
        )
    except BaseException as exc:  # noqa: BLE001 -- inspect even asynchronous commit interruption
        failure = exc
    finally:
        if prepared is not None:
            try:
                prepared.close()
            except BaseException as cleanup:  # noqa: BLE001 -- preserve interruption and outcome
                if failure is None:
                    failure = cleanup
                else:
                    failure.add_note("Restore proof cleanup also failed; retain recovery bytes")
    # Even a native call interrupted after replacement cannot be replayed or
    # guessed from return bookkeeping. Reopen safely under the same leases.
    result = _inspect(sealed, target)
    if failure is None:
        return result
    if not isinstance(failure, Exception):
        failure.add_note(f"Restore outcome inspected under ownership: {result.state}")
        raise failure
    return replace(
        result, detail=f"Restore interrupted by {type(failure).__name__}; inspected outcome"
    )


def apply_restore(
    document: bytes,
    *,
    expect_plan: str,
    target: pathlib.Path,
    staging: pathlib.Path,
    checkpoint: pathlib.Path,
) -> RestoreResult:
    """Apply one reviewed operation; an exact already-committed repeat never writes."""
    sealed = _sealed(document, expect_plan, target)

    def operation() -> RestoreResult:
        _require_stopped(sealed, target)
        existing = _inspect(sealed, target)
        if existing.state == "committed":
            return existing
        # Default verification stays strict. A nonmatching prior operation needs
        # fresh source/current observations and expiry before any preparation.
        restore_plan.verify_plan(document, expect_plan=expect_plan, target=target)
        return _commit(sealed, target, staging, checkpoint)

    return _owned_call(target, operation)
