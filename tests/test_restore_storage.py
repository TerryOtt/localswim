"""Synthetic append-only preparation under the actual maintenance ownership locks."""

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING
lazy import pathlib

import pytest

from localswim import board_state, restore_paths, restore_plan, service_ownership
from localswim import restore_storage as storage
from tests.test_restore_candidate import AID, BLOB, attach, snapshot

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


@contextmanager
def held(target: pathlib.Path) -> Generator[None]:
    with (
        service_ownership.launch_locked(target),
        service_ownership.writer_claim(target),
        board_state.locked(target),
    ):
        yield


def setup(
    root: pathlib.Path, board: board_state.Board, *, blob: bool = True
) -> tuple[restore_plan.SealedPlan, pathlib.Path]:
    if blob:
        attach(board, root / "source")
    source = snapshot(root / "source", board)
    target = snapshot(root / "target", board)
    sealed = restore_plan.create_plan(source, target, acknowledge_incomplete_recovery=True)
    return sealed, target


def prepare(
    root: pathlib.Path, sealed: restore_plan.SealedPlan, target: pathlib.Path
) -> storage.PreparedRestore:
    return storage.prepare_restore(
        sealed.document,
        expect_plan=sealed.plan_digest,
        target=target,
        staging=root / "stage",
        checkpoint=root / "checkpoint",
    )


def relative_blob() -> pathlib.Path:
    return pathlib.Path("attachments") / "0001" / f"{AID}.bin"


def test_prepared_binds_frozen_bytes_checkpoint_and_live_maintenance(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    with held(target):
        with prepare(tmp_path, sealed, target) as prepared:
            assert prepared.plan_document == sealed.document
            assert prepared.plan_digest == sealed.plan_digest
            assert prepared.final_snapshot == sealed.final_snapshot
            assert prepared.operation_id == sealed.core["operationId"]
            assert target.read_bytes() == before
            assert (prepared.staging / "source.json").read_bytes() == (
                tmp_path / "source" / "board.json"
            ).read_bytes()
            assert (prepared.staging / "candidate.json").read_bytes() == sealed.final_snapshot
            assert (prepared.staging / relative_blob()).read_bytes() == BLOB
            assert (prepared.checkpoint / "current.json").read_bytes() == before
            assert (prepared.checkpoint / "plan.json").read_bytes() == sealed.document
            assert (prepared.checkpoint / "candidate.json").read_bytes() == sealed.final_snapshot
            manifest = json.loads(prepared.manifest)
            assert manifest["current"]["recovery"] == "incomplete"
            assert manifest["current"]["blobs"][0]["status"] == "missing"
            assert json.loads((prepared.checkpoint / "complete.json").read_bytes()) == {
                "manifestDigest": hashlib.sha256(prepared.manifest).hexdigest(),
                "planDigest": sealed.plan_digest,
            }
            assert prepared.blobs[0].appended
            assert (target.parent / relative_blob()).read_bytes() == BLOB
            with pytest.raises(FrozenInstanceError):
                prepared.final_snapshot = b"changed"  # type: ignore[misc]
            with (
                pytest.raises(service_ownership.WriterAlreadyRunningError),
                service_ownership.writer_claim(target),
            ):
                pytest.fail("maintenance writer ownership was released")
            with (
                pytest.raises(TimeoutError),
                service_ownership.launch_locked(target, timeout=0),
            ):
                pytest.fail("maintenance launch ownership was released")
        assert service_ownership.writer_busy(target)
        with pytest.raises(restore_paths.PathBoundaryError, match="closed"):
            prepared.revalidate()
    assert not service_ownership.writer_busy(target)
    assert (tmp_path / "checkpoint" / "complete.json").exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "empty"])
def test_raw_only_current_is_preserved_without_rejecting_valid_recovery(
    tmp_path: pathlib.Path, board: board_state.Board, kind: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "target" / "board.json"
    target.parent.mkdir()
    raw = b"{" if kind == "corrupt" else b""
    if kind != "missing":
        target.write_bytes(raw)
    sealed = restore_plan.create_plan(source, target, port=9040, acknowledge_raw_recovery=True)
    with held(target), prepare(tmp_path, sealed, target) as prepared:
        assert json.loads(prepared.manifest)["current"]["recovery"] == "raw-only"
        if kind == "missing":
            assert (prepared.checkpoint / "current.missing").read_bytes() == b"missing\n"
            assert not target.exists()
        else:
            assert (prepared.checkpoint / "current.json").read_bytes() == raw
            assert target.read_bytes() == raw
        assert not prepared.blobs


@pytest.mark.parametrize("retained", [False, True])
def test_identical_immutable_bytes_are_reused_and_retained_sources_are_staged(
    tmp_path: pathlib.Path, board: board_state.Board, *, retained: bool
) -> None:
    source_blob = attach(board, tmp_path / "source", removed=retained)
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    target_blob = target.parent / relative_blob()
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(BLOB)
    identity = target_blob.stat().st_ino
    source_blob.parent.joinpath("unreferenced.bin").write_bytes(b"never import")
    sealed = restore_plan.create_plan(source, target)
    with held(target), prepare(tmp_path, sealed, target) as prepared:
        assert not prepared.blobs[0].appended
        assert target_blob.stat().st_ino == identity
        assert (prepared.checkpoint / relative_blob()).read_bytes() == BLOB
        assert not (prepared.staging / relative_blob().parent / "unreferenced.bin").exists()
        assert json.loads(prepared.manifest)["current"]["recovery"] == "complete"


def test_readable_corrupt_current_reference_is_preserved_truthfully(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    # Keep the old reference separate from the supplied candidate's manifest so
    # immutable collision refusal does not prevent the legitimate recovery case.
    old = attach(board, tmp_path / "target")
    old.write_bytes(b"bad old bytes")
    target = snapshot(tmp_path / "target", board)
    source_board = board_state.Board.from_json(
        json.loads(target.read_bytes()), "fixture", install_context=False
    )
    source_board.find("card").attachments.clear()
    source_board.find("card").attachment_history.clear()
    source = snapshot(tmp_path / "source", source_board)
    sealed = restore_plan.create_plan(source, target, acknowledge_incomplete_recovery=True)
    with held(target), prepare(tmp_path, sealed, target) as prepared:
        assert (prepared.checkpoint / relative_blob()).read_bytes() == b"bad old bytes"
        assert json.loads(prepared.manifest)["current"]["blobs"][0]["status"] == "corrupt"
        assert old.read_bytes() == b"bad old bytes"


def test_all_checkpoint_files_are_flushed_before_first_target_copy(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    original_copy = restore_paths.CreationSession.copy_file
    copies: list[pathlib.Path] = []

    def observed_copy(  # noqa: PLR0913 -- mirror exact native copy signature
        session: restore_paths.CreationSession,
        source: restore_paths.CheckedPath,
        path: pathlib.Path,
        *,
        max_bytes: int,
        expected_size: int,
        expected_sha256: str,
    ) -> restore_paths.CheckedPath:
        if path.is_relative_to(target.parent):
            complete = tmp_path / "checkpoint" / "complete.json"
            assert complete.exists()
            completion = json.loads(session.capture(complete).read_bytes(4096))
            manifest = session.capture(complete.parent / "manifest.json").read_bytes(1024 * 1024)
            assert completion["manifestDigest"] == hashlib.sha256(manifest).hexdigest()
            copies.append(path)
        return original_copy(
            session,
            source,
            path,
            max_bytes=max_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    monkeypatch.setattr(restore_paths.CreationSession, "copy_file", observed_copy)
    with held(target), prepare(tmp_path, sealed, target):
        assert copies == [target.parent / relative_blob()]


@pytest.mark.parametrize(
    "failure",
    ["source", "stage_blob", "checkpoint_raw", "plan", "manifest", "complete", "target"],
)
def test_copy_and_checkpoint_failures_preserve_current_and_partial_material(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    destinations = {
        "source": tmp_path / "stage" / "source.json",
        "stage_blob": tmp_path / "stage" / relative_blob(),
        "checkpoint_raw": tmp_path / "checkpoint" / "current.json",
        "plan": tmp_path / "checkpoint" / "plan.json",
        "manifest": tmp_path / "checkpoint" / "manifest.json",
        "complete": tmp_path / "checkpoint" / "complete.json",
        "target": target.parent / relative_blob(),
    }
    original_create = restore_paths.CreationSession.create_file
    original_copy = restore_paths.CreationSession.copy_file

    def fail_create(
        session: restore_paths.CreationSession, path: pathlib.Path, data: bytes
    ) -> restore_paths.CheckedPath:
        if path == destinations[failure]:
            raise OSError("injected file flush/create failure")
        return original_create(session, path, data)

    def fail_copy(  # noqa: PLR0913 -- mirror exact native copy signature
        session: restore_paths.CreationSession,
        source: restore_paths.CheckedPath,
        path: pathlib.Path,
        *,
        max_bytes: int,
        expected_size: int,
        expected_sha256: str,
    ) -> restore_paths.CheckedPath:
        if path == destinations[failure]:
            raise OSError("injected read/copy failure")
        return original_copy(
            session,
            source,
            path,
            max_bytes=max_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    monkeypatch.setattr(restore_paths.CreationSession, "create_file", fail_create)
    monkeypatch.setattr(restore_paths.CreationSession, "copy_file", fail_copy)
    with held(target), pytest.raises(OSError, match="injected"):
        prepare(tmp_path, sealed, target)
    assert target.read_bytes() == before
    assert not (target.parent / relative_blob()).exists()
    assert (tmp_path / "stage").is_dir()
    assert (tmp_path / "checkpoint").is_dir()
    assert (tmp_path / "checkpoint" / "complete.json").exists() == (failure == "target")


def test_baseexception_keeps_successfully_appended_orphan_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    original = storage._append_candidate  # noqa: SLF001 -- deterministic interruption seam

    def interrupted(
        session: restore_paths.CreationSession,
        core: board_state.JsonObject,
        proofs: dict[pathlib.Path, restore_paths.CheckedPath],
        staged: dict[str, restore_paths.CheckedPath],
        destination: pathlib.Path,
    ) -> tuple[storage.PreparedBlob, ...]:
        original(session, core, proofs, staged, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_append_candidate", interrupted)
    with held(target), pytest.raises(KeyboardInterrupt):
        prepare(tmp_path, sealed, target)
    assert target.read_bytes() == before
    assert (target.parent / relative_blob()).read_bytes() == BLOB
    assert (tmp_path / "checkpoint" / "complete.json").exists()


def test_space_estimate_is_aggregated_before_any_storage_creation(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    calls: list[pathlib.Path] = []

    def no_space(path: pathlib.Path) -> object:
        calls.append(path)
        return type("Usage", (), {"free": 0})()

    monkeypatch.setattr(storage.shutil, "disk_usage", no_space)
    with held(target), pytest.raises(storage.StorageError, match="space"):
        prepare(tmp_path, sealed, target)
    assert calls == [pathlib.Path(target.anchor)]
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()
    assert not (target.parent / relative_blob()).exists()


@pytest.mark.parametrize("role", ["source", "target", "same", "nested", "existing"])
def test_role_overlap_and_reused_storage_destinations_refuse_before_blob_writes(
    tmp_path: pathlib.Path, board: board_state.Board, role: str
) -> None:
    sealed, target = setup(tmp_path, board)
    stage, checkpoint = tmp_path / "stage", tmp_path / "checkpoint"
    if role == "source":
        stage = tmp_path / "source" / "stage"
    elif role == "target":
        checkpoint = target.parent / "checkpoint"
    elif role == "same":
        checkpoint = stage
    elif role == "nested":
        checkpoint = stage / "checkpoint"
    else:
        stage.mkdir()
    with held(target), pytest.raises(restore_paths.PathBoundaryError):
        storage.prepare_restore(
            sealed.document,
            expect_plan=sealed.plan_digest,
            target=target,
            staging=stage,
            checkpoint=checkpoint,
        )
    assert not (target.parent / relative_blob()).exists()


@pytest.mark.parametrize("drift", ["source", "target", "collision", "identity"])
def test_drift_between_plan_verification_and_fresh_session_refuses(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    sealed, target = setup(tmp_path, board)
    original = restore_plan.verify_plan

    def drift_after_verification(
        document: bytes, *, expect_plan: str, target: pathlib.Path
    ) -> restore_plan.SealedPlan:
        verified = original(document, expect_plan=expect_plan, target=target)
        path = tmp_path / "source" / "board.json"
        if drift == "identity":
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)
        elif drift == "collision":
            path = target.parent / relative_blob()
            path.parent.mkdir(parents=True)
            path.write_bytes(b"conflicting newer bytes")
        else:
            path = target if drift == "target" else path
            path.write_bytes(b"changed after verification")
        return verified

    monkeypatch.setattr(restore_plan, "verify_plan", drift_after_verification)
    with held(target), pytest.raises(storage.StorageError, match="observations"):
        prepare(tmp_path, sealed, target)
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


def test_target_source_and_new_files_stay_pinned_through_prepared_lifetime(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = setup(tmp_path, board)
    with held(target), prepare(tmp_path, sealed, target) as prepared:
        for path in (
            target,
            tmp_path / "source" / "board.json",
            tmp_path / "source" / relative_blob(),
            target.parent / relative_blob(),
            prepared.checkpoint / "current.json",
            prepared.staging / "candidate.json",
        ):
            with pytest.raises(PermissionError):
                path.write_bytes(b"unexpected mutation")
            with pytest.raises(PermissionError):
                path.unlink()
        prepared.revalidate()


def test_wrong_expected_digest_and_unreadable_existing_current_never_create_storage(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = setup(tmp_path, board)
    with held(target):
        with pytest.raises(restore_plan.PlanError, match="digest"):
            storage.prepare_restore(
                sealed.document,
                expect_plan="f" * 64,
                target=target,
                staging=tmp_path / "stage",
                checkpoint=tmp_path / "checkpoint",
            )
        # A genuine incompatible writer handle prevents fresh native admission.
        with target.open("r+b"), pytest.raises(restore_paths.PathBoundaryError):
            prepare(tmp_path, sealed, target)
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("failure", ["copy_read", "readback", "checkpoint_flush", "target_flush"])
def test_native_read_hash_and_flush_faults_keep_conservative_bytes(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()
    native = restore_paths._CreatingWindows  # noqa: SLF001 -- native deterministic fault seam
    original_read = native.read_chunk
    original_flush = native.flush
    source_blob = tmp_path / "source" / relative_blob()
    stage_blob = tmp_path / "stage" / relative_blob()
    target_blob = target.parent / relative_blob()
    injected = False

    def failed_read(instance: restore_paths._CreatingWindows, handle: int, maximum: int) -> bytes:
        nonlocal injected
        path = pathlib.Path(instance.canonical(handle))
        if not injected and failure == "copy_read" and path == source_blob:
            injected = True
            raise OSError("injected native read")
        result = original_read(instance, handle, maximum)
        if not injected and failure == "readback" and path == stage_blob and result:
            injected = True
            return bytes([result[0] ^ 1]) + result[1:]
        return result

    def failed_flush(instance: restore_paths._CreatingWindows, handle: int) -> None:
        nonlocal injected
        path = pathlib.Path(instance.canonical(handle))
        if (
            failure == "checkpoint_flush" and path == tmp_path / "checkpoint" / "candidate.json"
        ) or (failure == "target_flush" and path == target_blob):
            injected = True
            raise OSError("injected native flush")
        original_flush(instance, handle)

    monkeypatch.setattr(native, "read_chunk", failed_read)
    monkeypatch.setattr(native, "flush", failed_flush)
    with held(target), pytest.raises((OSError, restore_paths.PathBoundaryError)):
        prepare(tmp_path, sealed, target)
    assert injected
    assert target.read_bytes() == before
    assert (tmp_path / "stage").exists()
    assert (tmp_path / "checkpoint").exists()
    if failure == "target_flush":
        assert target_blob.read_bytes() == BLOB
        assert (tmp_path / "checkpoint" / "complete.json").exists()
    else:
        assert not target_blob.exists()
        assert not (tmp_path / "checkpoint" / "complete.json").exists()


def test_volume_space_estimate_combines_same_volume_and_keeps_other_volume_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[pathlib.Path] = []

    def disk_usage(path: pathlib.Path) -> object:
        calls.append(path)
        return type("Usage", (), {"free": 99 if path.drive == "C:" else 500})()

    monkeypatch.setattr(storage.shutil, "disk_usage", disk_usage)
    requirements = {
        pathlib.Path("C:/stage"): 40,
        pathlib.Path("c:/checkpoint"): 60,
        pathlib.Path("D:/target/board.json"): 400,
    }
    with pytest.raises(storage.StorageError, match="space"):
        storage._space_check(requirements)  # noqa: SLF001 -- arithmetic refusal boundary
    assert calls == [pathlib.Path("C:/")]
    calls.clear()
    requirements[pathlib.Path("c:/checkpoint")] = 59
    storage._space_check(requirements)  # noqa: SLF001 -- arithmetic acceptance boundary
    assert calls == [pathlib.Path("C:/"), pathlib.Path("D:/")]


def test_checkpoint_failure_preserves_preexisting_attachment_and_excludes_sidecars(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    existing = target.parent / relative_blob()
    existing.parent.mkdir(parents=True)
    existing.write_bytes(BLOB)
    for root in (source.parent, target.parent):
        (root / "credentials.json").write_bytes(b"private unrelated credentials")
        (root / "board.service.json").write_bytes(b"unrelated descriptor")
        (root / "hook.py").write_bytes(b"unrelated executable")
    sealed = restore_plan.create_plan(source, target)
    before, identity = target.read_bytes(), existing.stat().st_ino
    original_create = restore_paths.CreationSession.create_file

    def fail_manifest(
        session: restore_paths.CreationSession, path: pathlib.Path, data: bytes
    ) -> restore_paths.CheckedPath:
        if path == tmp_path / "checkpoint" / "manifest.json":
            raise OSError("injected checkpoint metadata failure")
        return original_create(session, path, data)

    monkeypatch.setattr(restore_paths.CreationSession, "create_file", fail_manifest)
    with held(target), pytest.raises(OSError, match="metadata"):
        prepare(tmp_path, sealed, target)
    assert target.read_bytes() == before
    assert existing.read_bytes() == BLOB
    assert existing.stat().st_ino == identity
    assert (tmp_path / "checkpoint" / relative_blob()).read_bytes() == BLOB
    for root in (tmp_path / "stage", tmp_path / "checkpoint"):
        assert not (root / "credentials.json").exists()
        assert not (root / "board.service.json").exists()
        assert not (root / "hook.py").exists()
    assert not (tmp_path / "checkpoint" / "complete.json").exists()


def test_expiry_during_preparation_keeps_complete_checkpoint_without_target_append(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = setup(tmp_path, board)
    before = target.read_bytes()

    def expired(_core: board_state.JsonObject) -> None:
        raise storage.StorageError("sealed plan expired during preparation")

    monkeypatch.setattr(storage, "_check_expiry", expired)
    with held(target), pytest.raises(storage.StorageError, match="expired"):
        prepare(tmp_path, sealed, target)
    assert target.read_bytes() == before
    assert (tmp_path / "checkpoint" / "complete.json").exists()
    assert not (target.parent / relative_blob()).exists()
