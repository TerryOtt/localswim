"""Synthetic native restore commits, interruption outcomes and real ownership."""

# Native fault injection deliberately targets the narrow private handle boundary.
# ruff: noqa: SLF001

import base64
import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
lazy import pathlib

import pytest

from localswim import (
    board_state,
    restore,
    restore_paths,
    restore_plan,
    restore_storage,
    service_ownership,
    transaction_lock,
)
from tests.test_restore_storage import held, prepare, relative_blob
from tests.test_restore_storage import setup as storage_setup
from tests.test_service_ownership import child_event, maintenance_child, release_child

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


def test_native_session_terminal_replacement(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = setup(tmp_path, board)
    with held(target), prepare(tmp_path, sealed, target) as prepared:
        prepared._session.replace_snapshot(
            target,
            sealed.final_snapshot,
            operation_id=prepared.operation_id,
            before_replace=lambda: None,
        )
    assert target.read_bytes() == sealed.final_snapshot


def setup(
    root: pathlib.Path, board: board_state.Board, *, blob: bool = False
) -> tuple[restore_plan.SealedPlan, pathlib.Path]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        board.port = reservation.getsockname()[1]
    return storage_setup(root, board, blob=blob)


def apply(
    root: pathlib.Path, sealed: restore_plan.SealedPlan, target: pathlib.Path
) -> restore.RestoreResult:
    return restore.apply_restore(
        sealed.document,
        expect_plan=sealed.plan_digest,
        target=target,
        staging=root / "stage",
        checkpoint=root / "checkpoint",
    )


def inspect(sealed: restore_plan.SealedPlan, target: pathlib.Path) -> restore.RestoreResult:
    return restore.inspect_restore(sealed.document, expect_plan=sealed.plan_digest, target=target)


def test_exact_commit_once_retains_checkpoint_blobs_and_all_real_leases(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board, blob=True)
    before = target.read_bytes()
    original = restore_paths._CreatingWindows.replace_child
    attempts: list[str] = []

    def observed(
        native: restore_paths._CreatingWindows, source: int, parent: int, name: str
    ) -> None:
        assert service_ownership.writer_busy(target)
        with (
            pytest.raises(TimeoutError),
            service_ownership.launch_locked(target, timeout=0),
        ):
            pytest.fail("launch gate released before commit")
        with pytest.raises(board_state.BoardError), board_state.locked(target):
            pytest.fail("transaction ownership released before commit")
        # The pinned target parent cannot be renamed during the narrow transition.
        with pytest.raises(PermissionError):
            target.parent.rename(target.parent.with_name("escaped"))
        attempts.append(name)
        original(native, source, parent, name)

    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", observed)
    result = apply(tmp_path, sealed, target)
    assert result.state == "committed", result.detail
    assert attempts == [target.name]
    assert target.read_bytes() == sealed.final_snapshot
    assert result.final_digest == hashlib.sha256(sealed.final_snapshot).hexdigest()
    assert result.generation == json.loads(target.read_bytes())["generation"]
    assert (tmp_path / "checkpoint" / "current.json").read_bytes() == before
    assert (tmp_path / "checkpoint" / "complete.json").is_file()
    assert (tmp_path / "stage" / "candidate.json").read_bytes() == sealed.final_snapshot
    assert (target.parent / relative_blob()).is_file()
    assert not service_ownership.writer_busy(target)
    assert inspect(sealed, target).state == "committed"
    for suffix in (".writer.lock", ".workspace.lock"):
        assert board_state.service_descriptor_path(target).with_suffix(suffix).is_file()
    assert target.with_name(target.name + ".transaction.lock").is_file()


def test_repeat_precedes_expiry_source_drift_and_no_serializer_or_second_replace(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    assert apply(tmp_path, sealed, target).state == "committed"
    source = pathlib.Path(str(sealed.core["source"]["path"]))  # type: ignore[index]
    source.unlink()

    class Later(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # noqa: ARG003 -- datetime seam
            return datetime.now(UTC) + timedelta(hours=1)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("repeat must use persisted receipt without regenerating or replacing")

    monkeypatch.setattr(restore_plan, "datetime", Later)
    monkeypatch.setattr(board_state.Board, "to_json", forbidden)
    monkeypatch.setattr(board_state, "now", forbidden)
    monkeypatch.setattr(restore_plan.uuid, "uuid4", forbidden)
    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", forbidden)
    with pytest.raises(restore_plan.PlanError, match="expired"):
        restore_plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)
    assert apply(tmp_path, sealed, target).state == "committed"
    assert inspect(sealed, target).state == "committed"


def test_first_apply_never_serializes_or_allocates_frozen_content(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("apply must preserve frozen board content")

    monkeypatch.setattr(board_state.Board, "to_json", forbidden)
    monkeypatch.setattr(board_state, "now", forbidden)
    monkeypatch.setattr(restore_plan.uuid, "uuid4", forbidden)
    assert apply(tmp_path, sealed, target).state == "committed"


def test_cleanup_failure_keeps_confirmed_commit_and_all_recovery_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_storage.PreparedRestore.close

    def failed(prepared: restore_storage.PreparedRestore) -> None:
        original(prepared)
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(restore_storage.PreparedRestore, "close", failed)
    result = apply(tmp_path, sealed, target)
    assert result.state == "committed"
    assert result.detail is not None
    assert target.read_bytes() == sealed.final_snapshot
    assert (tmp_path / "stage" / "candidate.json").is_file()
    assert (tmp_path / "checkpoint" / "complete.json").is_file()


def release_faults(
    monkeypatch: pytest.MonkeyPatch, faults: set[str], error: type[BaseException] = OSError
) -> list[str]:
    """Fault actual descriptor release after unlock/close, recording reverse exits."""
    released: list[str] = []
    transaction_release = transaction_lock._DescriptorLease.release
    ownership_release = service_ownership.WriterLease.release

    def transaction(lease: transaction_lock._DescriptorLease) -> None:
        transaction_release(lease)
        released.append("transaction")
        if "transaction" in faults:
            raise error("private cleanup text")

    def ownership(lease: service_ownership.WriterLease) -> None:
        ownership_release(lease)
        name = "writer" if "writer" not in released else "gate"
        released.append(name)
        if name in faults:
            raise error("private cleanup text")

    monkeypatch.setattr(transaction_lock._DescriptorLease, "release", transaction)
    monkeypatch.setattr(service_ownership.WriterLease, "release", ownership)
    return released


@pytest.mark.parametrize("entry", ["apply", "inspect"])
@pytest.mark.parametrize("lease", ["transaction", "writer", "gate", "all"])
def test_actual_lease_release_fault_preserves_owned_committed_result(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    lease: str,
) -> None:
    sealed, target = setup(tmp_path, board)
    if entry == "inspect":
        assert apply(tmp_path, sealed, target).state == "committed"
    released = release_faults(
        monkeypatch, {"transaction", "writer", "gate"} if lease == "all" else {lease}
    )
    original_inspect = restore._inspect

    def owned(sealed: restore_plan.SealedPlan, target: pathlib.Path) -> restore.RestoreResult:
        assert not released, "classification must never run after any lease exit"
        return original_inspect(sealed, target)

    monkeypatch.setattr(restore, "_inspect", owned)
    result = apply(tmp_path, sealed, target) if entry == "apply" else inspect(sealed, target)
    assert released == ["transaction", "writer", "gate"]
    assert result.state == "committed"
    assert result.detail is not None
    assert "release failed" in result.detail
    assert "private" not in result.detail
    assert target.read_bytes() == sealed.final_snapshot


@pytest.mark.parametrize("state", ["uncommitted", "unknown"])
def test_release_fault_keeps_inspected_noncommit_state(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    sealed, target = setup(tmp_path, board)
    if state == "unknown":
        target.write_bytes(b"uncertain bytes")
    released = release_faults(monkeypatch, {"transaction", "writer", "gate"})
    result = inspect(sealed, target)
    assert result.state == state
    assert released == ["transaction", "writer", "gate"]


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, RuntimeError])
def test_primary_failure_survives_all_actual_lease_release_faults(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    sealed, target = setup(tmp_path, board)
    released = release_faults(monkeypatch, {"transaction", "writer", "gate"})
    primary = primary_type("original failure")

    def failed(_sealed: restore_plan.SealedPlan, _target: pathlib.Path) -> None:
        raise primary

    monkeypatch.setattr(restore, "_require_stopped", failed)
    with pytest.raises(primary_type) as caught:
        inspect(sealed, target)
    assert caught.value is primary
    assert released == ["transaction", "writer", "gate"]
    assert len(primary.__notes__) >= 3
    assert not any("outcome inspected" in note for note in primary.__notes__)


def test_release_baseexception_preserves_known_outcome_and_attempts_remaining_exits(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    assert apply(tmp_path, sealed, target).state == "committed"
    released = release_faults(monkeypatch, {"transaction", "writer", "gate"}, KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        inspect(sealed, target)
    assert released == ["transaction", "writer", "gate"]
    assert "Restore outcome inspected under ownership: committed" in caught.value.__notes__


def test_partial_acquisition_refusal_remains_primary_when_gate_release_fails(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    with service_ownership.writer_claim(target):
        original = service_ownership.WriterLease.release

        def failed(lease: service_ownership.WriterLease) -> None:
            original(lease)
            raise OSError("private gate cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(service_ownership.WriterLease, "release", failed)
            with pytest.raises(service_ownership.WriterAlreadyRunningError) as caught:
                inspect(sealed, target)
        assert any("release also failed" in note for note in caught.value.__notes__)


def test_failed_preparation_classifies_prior_bytes_without_replacement(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()

    def unavailable(_requirements: object) -> None:
        raise OSError("synthetic insufficient space")

    monkeypatch.setattr(restore_storage, "_space_check", unavailable)
    result = apply(tmp_path, sealed, target)
    assert result.state == "uncommitted"
    assert target.read_bytes() == before
    assert not (tmp_path / "checkpoint").exists()


def test_repeat_still_requires_original_reviewed_digest(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = setup(tmp_path, board)
    assert apply(tmp_path, sealed, target).state == "committed"
    with pytest.raises(restore_plan.PlanError, match="digest mismatch"):
        restore.inspect_restore(sealed.document, expect_plan="0" * 64, target=target)
    changed = json.loads(sealed.document)
    changed["finalSnapshotDigest"] = "0" * 64
    with pytest.raises(restore_plan.PlanError, match="digest mismatch"):
        restore.inspect_restore(
            json.dumps(changed).encode(), expect_plan=sealed.plan_digest, target=target
        )


def reseal_metadata(envelope: board_state.JsonObject) -> restore_plan.SealedPlan:
    """Bind mutated metadata to valid digest domains for malformed-plan regressions."""
    digest = hashlib.sha256(restore_plan.canonical_json(envelope["core"])).hexdigest()
    raw = json.loads(base64.b64decode(str(envelope["finalSnapshotBase64"])))
    raw["restoreReceipt"]["planDigest"] = digest
    final = restore_plan.canonical_json(raw) + b"\n"
    envelope["planDigest"] = digest
    envelope["finalSnapshotBase64"] = base64.b64encode(final).decode("ascii")
    envelope["finalSnapshotDigest"] = hashlib.sha256(final).hexdigest()
    document = restore_plan.canonical_json(envelope) + b"\n"
    return restore_plan.SealedPlan(document, final, digest)


@pytest.mark.parametrize(
    "kind",
    [
        "null",
        "empty",
        "dropped",
        "wrong-role",
        "duplicate",
        "extra-field",
        "boolean-size",
        "negative-size",
        "oversize",
        "boolean-counter",
        "negative-counter",
        "extra-counter",
        "identity-size",
        "missing-metadata",
        "wrong-digest",
        "wrong-size-claim",
        "null-inventory",
    ],
)
def test_malformed_static_observations_refuse_even_matching_final_receipt_without_io(  # noqa: C901, PLR0912, PLR0915 -- explicit malformed metadata matrix
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    sealed, target = setup(tmp_path, board)
    envelope = json.loads(sealed.document)
    core = envelope["core"]
    rows = core["observations"]
    row = rows[0]
    if kind == "null":
        core["observations"] = None
    elif kind == "empty":
        core["observations"] = []
    elif kind == "dropped":
        rows.pop()
    elif kind == "wrong-role":
        row["path"] = str(target.parent / "foreign.json")
    elif kind == "duplicate":
        rows[1] = row
    elif kind == "extra-field":
        row["unexpected"] = None
    elif kind == "boolean-size":
        row["size"] = True
    elif kind == "negative-size":
        row["size"] = -1
    elif kind == "oversize":
        row["size"] = 64 * 1024 * 1024 + 1
    elif kind == "boolean-counter":
        row["identity"]["volume"] = True
    elif kind == "negative-counter":
        row["identity"]["modified"] = -1
    elif kind == "extra-counter":
        row["identity"]["unexpected"] = 0
    elif kind == "identity-size":
        row["identity"]["size"] += 1
    elif kind == "missing-metadata":
        row["digest"] = "missing"
    elif kind == "wrong-digest":
        row["digest"] = "0" * 64
    elif kind == "wrong-size-claim":
        row["size"] += 1
        row["identity"]["size"] += 1
    else:
        core["current"]["blobs"] = None
    malformed = reseal_metadata(envelope)
    # A copied exact receipt/final digest must not hide a malformed plan artifact.
    target.write_bytes(malformed.final_snapshot)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("malformed static metadata must refuse before filesystem IO")

    monkeypatch.setattr(restore_paths.CheckedPath, "capture", forbidden)
    with pytest.raises(restore_plan.PlanError):
        apply(tmp_path, malformed, target)
    with pytest.raises(restore_plan.PlanError):
        inspect(malformed, target)
    with pytest.raises(restore_plan.PlanError):
        restore_plan.verify_plan(
            malformed.document, expect_plan=malformed.plan_digest, target=target
        )
    assert not (tmp_path / "stage").exists()


def test_valid_static_observations_need_no_fresh_filesystem_io(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("static domain verification must not open any filesystem path")

    monkeypatch.setattr(restore_paths.CheckedPath, "capture", forbidden)
    verified = restore_plan._verify_sealed_plan(
        sealed.document, expect_plan=sealed.plan_digest, target=target
    )
    assert verified.final_snapshot == sealed.final_snapshot


@pytest.mark.parametrize("field", ["generation", "revision", "receipt", "whitespace"])
def test_copied_receipt_or_modified_snapshot_never_proves_commit(
    tmp_path: pathlib.Path, board: board_state.Board, field: str
) -> None:
    sealed, target = setup(tmp_path, board)
    raw = json.loads(sealed.final_snapshot)
    if field == "receipt":
        raw["restoreReceipt"]["planDigest"] = "0" * 64
    elif field == "generation":
        raw[field] = board.generation
    elif field == "revision":
        raw[field] += 1
    target.write_bytes(
        json.dumps(raw).encode() if field != "whitespace" else sealed.final_snapshot + b" "
    )
    assert inspect(sealed, target).state == "unknown"
    with pytest.raises(restore_plan.PlanError, match="drifted"):
        apply(tmp_path, sealed, target)
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("source_drift", [True, False])
def test_stale_plan_refuses_before_creating_recovery_material(
    tmp_path: pathlib.Path, board: board_state.Board, *, source_drift: bool
) -> None:
    sealed, target = setup(tmp_path, board)
    path = tmp_path / "source" / "board.json" if source_drift else target
    path.write_bytes(path.read_bytes() + b" ")
    old = target.read_bytes()
    with pytest.raises(restore_plan.PlanError, match="drifted"):
        apply(tmp_path, sealed, target)
    assert target.read_bytes() == old
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_raw_only_current_commits_exact_sealed_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, kind: str
) -> None:
    _, target = setup(tmp_path, board)
    if kind == "missing":
        target.unlink()
    else:
        target.write_bytes(b"{")
    sealed = restore_plan.create_plan(
        tmp_path / "source" / "board.json", target, port=board.port, acknowledge_raw_recovery=True
    )
    assert inspect(sealed, target).state == "uncommitted"
    result = apply(tmp_path, sealed, target)
    assert result.state == "committed", result.detail
    assert target.read_bytes() == sealed.final_snapshot


@pytest.mark.parametrize("position", ["before", "after", "other"])
def test_raised_native_replace_is_inspected_once_without_retry(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_paths._CreatingWindows.replace_child
    attempts: list[str] = []

    def fault(native: restore_paths._CreatingWindows, source: int, parent: int, name: str) -> None:
        attempts.append(name)
        if position == "after":
            original(native, source, parent, name)
        elif position == "other":
            target.write_bytes(b"uncertain foreign bytes")
        raise OSError("private path or content must not appear in result")

    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", fault)
    result = apply(tmp_path, sealed, target)
    assert (
        result.state
        == {"before": "uncommitted", "after": "committed", "other": "unknown"}[position]
    )
    assert attempts == [target.name]
    assert result.detail is not None
    assert "private" not in result.detail
    assert (tmp_path / "checkpoint" / "complete.json").exists()
    assert (tmp_path / "stage" / "candidate.json").exists()


@pytest.mark.parametrize("committed", [False, True])
def test_baseexception_at_native_bookkeeping_is_inspected_and_preserved(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed: bool,
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_paths._CreatingWindows.replace_child

    def interrupt(
        native: restore_paths._CreatingWindows, source: int, parent: int, name: str
    ) -> None:
        if committed:
            original(native, source, parent, name)
        raise KeyboardInterrupt

    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        apply(tmp_path, sealed, target)
    expected = "committed" if committed else "uncommitted"
    assert any(note.endswith(": " + expected) for note in caught.value.__notes__)
    assert inspect(sealed, target).state == expected
    assert not service_ownership.writer_busy(target)


def test_final_precommit_check_refuses_after_temp_flush_without_replace(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    original = restore_paths.CreationSession.replace_snapshot

    def refuse() -> None:
        raise restore_storage.StorageError("expired at final admission")

    def checked(
        session: restore_paths.CreationSession,
        path: pathlib.Path,
        data: bytes,
        *,
        operation_id: str,
        before_replace: Callable[[], None],
    ) -> None:
        before_replace()
        original(session, path, data, operation_id=operation_id, before_replace=refuse)

    monkeypatch.setattr(restore_paths.CreationSession, "replace_snapshot", checked)
    result = apply(tmp_path, sealed, target)
    assert result.state == "uncommitted"
    assert target.read_bytes() == before
    assert (tmp_path / "checkpoint" / "complete.json").exists()


def test_read_failure_after_replace_remains_unknown(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_paths._CreatingWindows.replace_child
    read = restore_paths.CheckedPath.read_bytes

    def unreadable(proof: restore_paths.CheckedPath, maximum: int) -> bytes:
        if proof.path == target:
            raise OSError("synthetic post-replace read failure")
        return read(proof, maximum)

    def replaced(
        native: restore_paths._CreatingWindows, source: int, parent: int, name: str
    ) -> None:
        original(native, source, parent, name)
        monkeypatch.setattr(restore_paths.CheckedPath, "read_bytes", unreadable)

    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", replaced)
    assert apply(tmp_path, sealed, target).state == "unknown"
    assert target.read_bytes() == sealed.final_snapshot


@pytest.mark.parametrize("entry", ["apply", "inspect"])
def test_legacy_listener_refuses_apply_and_inspect(
    tmp_path: pathlib.Path, board: board_state.Board, entry: str
) -> None:
    sealed, target = setup(tmp_path, board)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", board.port))
        listener.listen()
        invoke = (
            (lambda: apply(tmp_path, sealed, target))
            if entry == "apply"
            else (lambda: inspect(sealed, target))
        )
        with pytest.raises(board_state.BoardError, match="listening"):
            invoke()
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("entry", ["apply", "inspect"])
def test_real_process_writer_claim_refuses_without_listener(
    tmp_path: pathlib.Path, board: board_state.Board, entry: str
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    program = """
import pathlib, sys
from localswim import service_ownership
with service_ownership.writer_claim(pathlib.Path(sys.argv[1])):
    print('held', flush=True)
    sys.stdin.readline()
"""
    with maintenance_child(program, target) as child:
        child_event(child, "held")
        invoke = (
            (lambda: apply(tmp_path, sealed, target))
            if entry == "apply"
            else (lambda: inspect(sealed, target))
        )
        with pytest.raises(service_ownership.WriterAlreadyRunningError):
            invoke()
        assert target.read_bytes() == before
        release_child(child)
        assert child.wait(timeout=10) == 0


def test_two_freshly_planned_restores_allocate_distinct_persisted_generations(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    first, target = setup(tmp_path, board)
    assert apply(tmp_path, first, target).state == "committed"
    second = restore_plan.create_plan(tmp_path / "source" / "board.json", target)
    result = restore.apply_restore(
        second.document,
        expect_plan=second.plan_digest,
        target=target,
        staging=tmp_path / "stage2",
        checkpoint=tmp_path / "checkpoint2",
    )
    assert result.state == "committed"
    assert first.core["generation"] != second.core["generation"]
    loaded = board_state.load(target, install_context=False)
    assert loaded.generation == second.core["generation"]


def test_real_competing_startup_excluded_through_native_transition(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_paths._CreatingWindows.replace_child
    program = """
import pathlib, sys
from localswim import service_ownership
target = pathlib.Path(sys.argv[1])
try:
    with service_ownership.launch_locked(target, timeout=0):
        print('wrong-entry', flush=True)
except TimeoutError:
    print('gate-excluded', flush=True)
try:
    with service_ownership.writer_claim(target):
        print('wrong-writer', flush=True)
except service_ownership.WriterAlreadyRunningError:
    print('writer-excluded', flush=True)
sys.stdin.readline()
"""

    def synchronized(
        native: restore_paths._CreatingWindows, source: int, parent: int, name: str
    ) -> None:
        with maintenance_child(program, target) as child:
            child_event(child, "gate-excluded")
            child_event(child, "writer-excluded")
            release_child(child)
            assert child.wait(timeout=10) == 0
        original(native, source, parent, name)

    monkeypatch.setattr(restore_paths._CreatingWindows, "replace_child", synchronized)
    assert apply(tmp_path, sealed, target).state == "committed"


@pytest.mark.parametrize("position", ["before", "after"])
def test_actual_process_interruption_has_exact_owned_outcome(
    tmp_path: pathlib.Path, board: board_state.Board, position: str
) -> None:
    sealed, target = setup(tmp_path, board)
    (tmp_path / "sealed-plan.json").write_bytes(sealed.document)
    program = """
import json, pathlib, sys
from localswim import restore, restore_paths
target = pathlib.Path(sys.argv[1])
root = target.parent.parent
document = (root / 'sealed-plan.json').read_bytes()
original = restore_paths._CreatingWindows.replace_child
def interrupted(native, source, parent, name):
    if POSITION == 'after':
        original(native, source, parent, name)
    print('interrupt-now', flush=True)
    sys.stdin.readline()
    raise RuntimeError('unexpected release')
restore_paths._CreatingWindows.replace_child = interrupted
restore.apply_restore(document, expect_plan=json.loads(document)['planDigest'], target=target,
    staging=root / 'stage', checkpoint=root / 'checkpoint')
""".replace("POSITION", repr(position))
    with maintenance_child(program, target) as child:
        child_event(child, "interrupt-now")
        child.terminate()
        child.wait(timeout=10)
    expected = "committed" if position == "after" else "uncommitted"
    assert inspect(sealed, target).state == expected
    assert (tmp_path / "checkpoint" / "complete.json").is_file()
    assert (tmp_path / "stage" / "candidate.json").read_bytes() == sealed.final_snapshot
