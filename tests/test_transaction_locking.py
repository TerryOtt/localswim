"""Real local process ownership and transaction failure boundaries."""

# ruff: noqa: SLF001 -- focused tests exercise owned-descriptor fault boundaries

import base64
import errno
import os
import pathlib
import threading
import time
from typing import TYPE_CHECKING

import pytest

from localswim import attachments, board_state, transaction_lock
from tests.test_attachments import PNG
from tests.test_serialization import saved_board
from tests.test_service_ownership import child_event, maintenance_child, release_child
from tests.test_store import make_store

if TYPE_CHECKING:
    from collections.abc import Callable

HOLDER = """
import pathlib, sys
from localswim import board_state
with board_state.locked(pathlib.Path(sys.argv[1])):
    print('held', flush=True)
    sys.stdin.readline()
print('released', flush=True)
"""


def sidecar(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".transaction.lock")


def test_live_process_past_old_age_excludes_load_body_save(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    before = path.read_bytes()
    monkeypatch.setattr(board_state, "LOCK_WAIT_S", 0.08)
    reached: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> board_state.Board:
        reached.append("load/save")
        raise AssertionError("contender entered snapshot")

    with maintenance_child(HOLDER, path) as child:
        child_event(child, "held")
        identity = sidecar(path).stat().st_ino
        # Actual live ownership exceeds the removed ten-second stale threshold.
        time.sleep(10.05)
        with monkeypatch.context() as fault:
            fault.setattr(board_state, "load", forbidden)
            fault.setattr(board_state, "save", forbidden)
            with pytest.raises(board_state.BoardError, match="locked"), board_state.edit(path):
                reached.append("body")
        assert reached == []
        assert sidecar(path).stat().st_ino == identity
        assert path.read_bytes() == before
        release_child(child)
        child_event(child, "released")
    with board_state.edit(path) as changed:
        changed.project = "Successor"
    assert sidecar(path).stat().st_ino == identity
    assert board_state.load(path).project == "Successor"


def test_dead_process_reacquires_same_persistent_sidecar(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    with maintenance_child(HOLDER, path) as child:
        child_event(child, "held")
        identity = sidecar(path).stat().st_ino
        child.terminate()
        child.wait(timeout=10)
        started = time.monotonic()
        with board_state.locked(path):
            assert sidecar(path).stat().st_ino == identity
        assert time.monotonic() - started < 1
    assert sidecar(path).read_bytes() == b"\0"


def test_repeated_old_release_cannot_unlock_successor(tmp_path: pathlib.Path) -> None:
    board = tmp_path / "board.json"
    path = sidecar(board)
    old = transaction_lock._claim(path, time.monotonic())
    old.release()
    identity = path.stat().st_ino
    with maintenance_child(HOLDER, board) as child:
        child_event(child, "held")
        old.release()
        assert path.stat().st_ino == identity
        descriptor = os.open(path, os.O_RDWR)
        try:
            assert not transaction_lock._try_lock(descriptor)
        finally:
            os.close(descriptor)
        release_child(child)
        child_event(child, "released")
    assert path.stat().st_ino == identity


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan"), True])
def test_invalid_timeout_never_creates_sidecar(tmp_path: pathlib.Path, timeout: float) -> None:
    path = tmp_path / "board.json"
    with pytest.raises(ValueError, match="timeout"), transaction_lock.hold(path, timeout):
        pytest.fail("invalid timeout entered")
    assert not sidecar(path).exists()


def test_immediate_timeout_still_allows_uncontended_claim(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    with transaction_lock.hold(path, 0):
        assert sidecar(path).exists()
    with maintenance_child(HOLDER, path) as child:
        child_event(child, "held")
        started = time.monotonic()
        with pytest.raises(transaction_lock.LockRefusedError), transaction_lock.hold(path, 0):
            pytest.fail("contender entered")
        assert time.monotonic() - started < 0.2


def test_threads_serialize_and_timeout_without_entering(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        try:
            with board_state.locked(path):
                held.set()
                assert release.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001 -- surface thread failures in parent assertions
            errors.append(exc)

    worker = threading.Thread(target=holder)
    worker.start()
    try:
        assert held.wait(timeout=5)
        with pytest.raises(transaction_lock.LockRefusedError), transaction_lock.hold(path, 0.03):
            pytest.fail("thread contender entered")
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    with board_state.locked(path):
        assert sidecar(path).exists()


def test_thread_edits_preserve_all_updates(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    initial = board_state.load(path).revision
    ready = threading.Barrier(4)
    errors: list[BaseException] = []

    def edit_repeatedly() -> None:
        try:
            ready.wait(timeout=5)
            for _ in range(4):
                with board_state.edit(path, install_context=False) as board:
                    board.revision += 1
                    time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001 -- propagate thread test failures
            errors.append(exc)

    workers = [threading.Thread(target=edit_repeatedly) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert board_state.load(path).revision == initial + 16


def test_one_budget_covers_thread_and_os_wait(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    now = [100.0]
    deadlines: list[float] = []
    state = transaction_lock._PathMutex()

    class DelayedMutex:
        def acquire(self, *, timeout: float) -> bool:
            assert timeout == pytest.approx(0.3)
            now[0] += 0.2
            return True

        def release(self) -> None:
            pass

    def claim(_path: pathlib.Path, deadline: float) -> transaction_lock._DescriptorLease:
        deadlines.append(deadline)
        assert deadline - now[0] == pytest.approx(0.1)
        raise OSError("stop before OS wait")

    def path_mutex(_path: pathlib.Path) -> transaction_lock._PathMutex:
        return state

    monkeypatch.setattr(state, "mutex", DelayedMutex())
    monkeypatch.setattr(transaction_lock, "_path_mutex", path_mutex)
    monkeypatch.setattr(transaction_lock.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transaction_lock, "_claim", claim)
    with pytest.raises(OSError, match="stop"), transaction_lock.hold(path, 0.3):
        pytest.fail("fault entered")
    assert deadlines == [100.3]


def test_canonical_alias_refuses_recursion_and_targets_one_snapshot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    folder = tmp_path / "sub"
    folder.mkdir()
    alias = folder / ".." / path.name
    with board_state.locked(path):
        with pytest.raises(board_state.BoardError, match="recursive"), board_state.locked(alias):
            pytest.fail("recursive transaction entered")
        with board_state.locked(tmp_path / "other.json"):
            pass
    seen: list[pathlib.Path] = []
    load = board_state.load
    save = board_state.save

    def recording_load(
        target: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> board_state.Board:
        assert not install_context
        seen.append(target)
        return load(target, policy, install_context=install_context)

    def recording_save(changed: board_state.Board, target: pathlib.Path) -> None:
        seen.append(target)
        save(changed, target)

    monkeypatch.setattr(board_state, "load", recording_load)
    monkeypatch.setattr(board_state, "save", recording_save)
    with board_state.edit(alias, install_context=False) as changed:
        changed.project = "Canonical"
    assert seen == [path.resolve(), path.resolve()]
    assert not sidecar(folder / path.name).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path case aliases")
def test_windows_case_alias_shares_thread_mutex(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    with (
        board_state.locked(path),
        pytest.raises(board_state.BoardError, match="recursive"),
        board_state.locked(pathlib.Path(str(path).upper())),
    ):
        pytest.fail("case alias entered")


def test_symbolic_alias_uses_canonical_sidecar(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(path)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows account lacks symbolic-link privilege")
        raise
    with (
        board_state.locked(path),
        pytest.raises(board_state.BoardError, match="recursive"),
        board_state.locked(alias),
    ):
        pytest.fail("symbolic alias entered")
    with board_state.edit(alias, install_context=False) as changed:
        changed.project = "Through symbolic alias"
    assert alias.is_symlink()
    assert board_state.load(path).project == "Through symbolic alias"
    assert sidecar(path).exists()
    assert not sidecar(alias).exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_legacy_presence_fails_closed_without_age_recovery(
    tmp_path: pathlib.Path, kind: str
) -> None:
    path = tmp_path / "board.json"
    legacy = path.with_name(path.name + ".lock")
    if kind == "file":
        legacy.write_bytes(b"old pid")
        os.utime(legacy, (1, 1))
    else:
        legacy.mkdir()
    with pytest.raises(board_state.BoardError, match="legacy"), board_state.locked(path):
        pytest.fail("legacy lock ignored")
    assert legacy.exists()
    assert not sidecar(path).exists()


def test_legacy_appearing_during_claim_refuses_body(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    claim = transaction_lock._claim

    def legacy_claim(target: pathlib.Path, deadline: float) -> transaction_lock._DescriptorLease:
        lease = claim(target, deadline)
        path.with_name(path.name + ".lock").touch()
        return lease

    monkeypatch.setattr(transaction_lock, "_claim", legacy_claim)
    with pytest.raises(board_state.BoardError, match="legacy"), board_state.locked(path):
        pytest.fail("legacy lock ignored")
    monkeypatch.setattr(transaction_lock, "_claim", claim)
    with transaction_lock.hold(tmp_path / "other.json", 0):
        pass


def test_unexpected_os_failure_is_not_treated_as_contention(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    failure = OSError(errno.EIO, "unexpected lock failure")
    descriptors: list[int] = []

    def fail_lock(descriptor: int) -> bool:
        descriptors.append(descriptor)
        raise failure

    with monkeypatch.context() as fault:
        fault.setattr(transaction_lock, "_try_lock", fail_lock)
        with (
            pytest.raises(OSError, match="unexpected lock failure") as caught,
            board_state.locked(path),
        ):
            pytest.fail("unexpected failure admitted")
    assert caught.value is failure
    [descriptor] = descriptors
    with pytest.raises(OSError, match=r"Bad file descriptor|handle is invalid"):
        os.fstat(descriptor)
    with board_state.locked(path):
        pass


def test_descriptor_is_noninheritable_and_written_only_after_lock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    acquired: list[int] = []
    try_lock = transaction_lock._try_lock
    write = os.write

    def recording_lock(descriptor: int) -> bool:
        assert os.fstat(descriptor).st_size == 0
        assert not os.get_inheritable(descriptor)
        result = try_lock(descriptor)
        if result:
            acquired.append(descriptor)
        return result

    def recording_write(descriptor: int, data: bytes) -> int:
        assert descriptor in acquired
        return write(descriptor, data)

    monkeypatch.setattr(transaction_lock, "_try_lock", recording_lock)
    monkeypatch.setattr(transaction_lock.os, "write", recording_write)
    with board_state.locked(path):
        assert len(acquired) == 1
    assert sidecar(path).read_bytes() == b"\0"


@pytest.mark.parametrize("boundary", ["open", "noninherit", "write"])
@pytest.mark.parametrize("interruption", [False, True])
def test_acquisition_faults_close_and_release_before_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    *,
    interruption: bool,
) -> None:
    path = tmp_path / "board.json"
    opened: list[int] = []
    open_descriptor = os.open
    failure = KeyboardInterrupt(boundary) if interruption else OSError(boundary)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    def recording_open(target: pathlib.Path, flags: int, mode: int) -> int:
        if boundary == "open":
            raise failure
        descriptor = open_descriptor(target, flags, mode)
        opened.append(descriptor)
        return descriptor

    with monkeypatch.context() as fault:
        fault.setattr(transaction_lock.os, "open", recording_open)
        if boundary == "noninherit":
            fault.setattr(transaction_lock.os, "set_inheritable", fail)
        elif boundary == "write":
            fault.setattr(transaction_lock.os, "write", fail)
        with pytest.raises(type(failure)) as caught, board_state.locked(path):
            pytest.fail("acquisition failure entered body")
    assert caught.value is failure
    for descriptor in opened:
        with pytest.raises(OSError, match=r"Bad file descriptor|handle is invalid"):
            os.fstat(descriptor)
    with board_state.locked(path):
        pass


@pytest.mark.parametrize("boundary", ["load", "body", "serialization", "fsync", "replace"])
@pytest.mark.parametrize("interruption", [False, True])
def test_edit_fault_matrix_preserves_error_snapshot_outcome_and_releases(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    *,
    interruption: bool,
) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    before = path.read_bytes()
    outcome = board_state.SaveOutcome("committed")
    failure = KeyboardInterrupt(boundary) if interruption else OSError(boundary)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    with monkeypatch.context() as fault:
        if boundary == "load":
            fault.setattr(board_state, "load", fail)
        elif boundary == "serialization":
            fault.setattr(board_state.json, "dumps", fail)
        elif boundary == "fsync":
            fault.setattr(board_state.os, "fsync", fail)
        elif boundary == "replace":
            fault.setattr(pathlib.Path, "replace", fail)
        with (  # noqa: PT012 -- exercise the complete context-manager boundary
            pytest.raises(type(failure)) as caught,
            board_state.edit(path, outcome=outcome) as board,
        ):
            board.project = "Uncommitted"
            if boundary == "body":
                raise failure
    assert caught.value is failure
    assert path.read_bytes() == before
    assert outcome.state == ("unknown" if boundary == "replace" else "uncommitted")
    with board_state.locked(path):
        pass


@pytest.mark.parametrize("boundary", ["unlock", "unlock_before", "close", "both"])
@pytest.mark.parametrize("primary", ["none", "exception", "base_exception"])
@pytest.mark.parametrize("interruption", [False, True])
def test_release_faults_preserve_primary_and_attempt_close(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    primary: str,
    *,
    interruption: bool,
) -> None:
    path = tmp_path / "board.json"
    close = transaction_lock._close
    unlock = transaction_lock._unlock
    closed: list[int] = []
    release_failure = (
        KeyboardInterrupt("unlock failed") if interruption else OSError("unlock failed")
    )
    close_failure = (
        KeyboardInterrupt("close interrupted") if interruption else OSError("close failed")
    )
    body_failure = KeyboardInterrupt("body") if primary == "base_exception" else ValueError("body")

    def fail_unlock(descriptor: int) -> None:
        if boundary == "unlock_before":
            raise release_failure
        unlock(descriptor)
        if boundary in {"unlock", "both"}:
            raise release_failure

    def fail_close(descriptor: int) -> None:
        close(descriptor)
        closed.append(descriptor)
        if boundary in {"close", "both"}:
            raise close_failure

    expected = (
        body_failure
        if primary != "none"
        else (close_failure if boundary == "close" else release_failure)
    )
    with monkeypatch.context() as fault:
        fault.setattr(transaction_lock, "_unlock", fail_unlock)
        fault.setattr(transaction_lock, "_close", fail_close)
        with pytest.raises(type(expected)) as caught, board_state.locked(path):  # noqa: PT012 -- body/cleanup matrix
            if primary != "none":
                raise body_failure
    assert caught.value is expected
    assert len(closed) == 1
    with pytest.raises(OSError, match=r"Bad file descriptor|handle is invalid"):
        os.fstat(closed[0])
    with board_state.locked(path):
        pass


@pytest.mark.parametrize("boundary", ["unlock", "close", "unknown"])
def test_upload_keeps_bytes_after_real_transaction_cleanup_or_unknown_replace(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    original: Callable[[int], None] = (
        transaction_lock._close if boundary == "close" else transaction_lock._unlock
    )
    replace = pathlib.Path.replace
    failure = KeyboardInterrupt("uncertain replace") if boundary == "unknown" else OSError(boundary)

    def fail_release(descriptor: int) -> None:
        original(descriptor)
        raise failure

    def uncertain_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        result = replace(source, target)
        if target == path:
            raise failure
        return result

    with monkeypatch.context() as fault:
        if boundary == "unknown":
            fault.setattr(pathlib.Path, "replace", uncertain_replace)
        else:
            fault.setattr(
                transaction_lock, "_close" if boundary == "close" else "_unlock", fail_release
            )
        with pytest.raises(type(failure)) as caught:
            store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert caught.value is failure
    current = board_state.load(path)
    assert current.revision == 2
    [file] = current.find("card").attachments
    assert (
        attachments.blob_path(path, 1, file.id, attachments.extension(file.name)).read_bytes()
        == PNG
    )
    assert store.snapshot().revision == 2
