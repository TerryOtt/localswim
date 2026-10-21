"""Gateway launch coordination around live, restarting, and crashed writers."""

# ruff: noqa: SLF001 -- ownership tests inspect gateway process and discovery seams

import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

from localswim import board_state, service_ownership, workspace
from tests.test_workspace import existing_writer, make_board

if TYPE_CHECKING:
    import pathlib
    from typing import Never


def reject_spawn(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("a live writer must never cause another process launch")


@pytest.mark.parametrize("kind", ["deep", "oversized", "malformed", "nonregular"])
def test_gateway_bad_descriptor_is_unavailable_without_network(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "board.json"
    descriptor = board_state.service_descriptor_path(path)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    if kind == "nonregular":
        descriptor.mkdir()
    else:
        payload = {"deep": b"[" * 2000 + b"]" * 2000, "oversized": b"x" * 65537, "malformed": b"{"}[
            kind
        ]
        descriptor.write_bytes(payload)
    instance = workspace.Workspace(tmp_path)
    monkeypatch.setattr(workspace.http.client, "HTTPConnection", reject_spawn)
    try:
        assert instance._existing(path) is None
    finally:
        instance.close()


@pytest.mark.parametrize("initial", ["missing", "malformed", "refused"])
def test_gateway_waits_through_metadata_gap_and_connection_refusal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: str,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Lease test")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "browser-capability", "")
    descriptor = board_state.service_descriptor_path(path)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    if initial == "malformed":
        descriptor.write_text("{", encoding="utf-8")
    document = {"schema": 1, "host": "127.0.0.1", "port": expected.port, "board": str(path)}
    if initial == "refused":
        descriptor.write_text(json.dumps(document), encoding="utf-8")
    original = instance._existing
    attempts = 0
    connections = 0

    def browser_endpoint(_path: pathlib.Path, _port: int) -> workspace.Endpoint:
        nonlocal connections
        connections += 1
        if connections == 1:
            raise ConnectionRefusedError("restart listener is between processes")
        return expected

    def existing(board: pathlib.Path) -> workspace.Endpoint | None:
        nonlocal attempts
        attempts += 1
        result = original(board)
        if attempts == 2:
            descriptor.write_text(json.dumps(document), encoding="utf-8")
        return result

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(instance, "_browser_endpoint", browser_endpoint)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0.001)
    monkeypatch.setattr(subprocess, "Popen", reject_spawn)
    try:
        with service_ownership.writer_claim(path):
            assert instance.endpoint(path) == expected
        assert connections == 2
        assert not instance._children
    finally:
        descriptor.unlink(missing_ok=True)
        instance.close()


def test_busy_writer_without_endpoint_times_out_without_spawn(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Unresponsive writer")
    instance = workspace.Workspace(tmp_path)
    monkeypatch.setattr(workspace, "START_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0.001)
    monkeypatch.setattr(subprocess, "Popen", reject_spawn)
    try:
        with (
            service_ownership.writer_claim(path),
            pytest.raises(TimeoutError, match="healthy endpoint"),
        ):
            instance.endpoint(path)
        assert not instance._children
    finally:
        instance.close()


def test_gateway_recovers_crashed_writer_with_stale_descriptor(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Crash recovery")
    instance = workspace.Workspace(tmp_path)
    try:
        with existing_writer(path) as old:
            os.kill(int(old["pid"]), signal.SIGTERM)
            deadline = time.monotonic() + 5
            while service_ownership.writer_busy(path):
                assert time.monotonic() < deadline, "crashed writer retained ownership"
                time.sleep(0.01)
            descriptor = board_state.service_descriptor_path(path)
            assert descriptor.exists()
            endpoint = instance.endpoint(path)
            current = json.loads(descriptor.read_text(encoding="utf-8"))
            assert current["pid"] != old["pid"]
            assert endpoint.port == current["port"]
            assert board_state.load(path).revision == 0
            instance.close()
    finally:
        instance.close()


def test_gateway_reuses_winner_when_its_child_loses_ownership_race(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Concurrent writer")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "winner-browser-capability", "")
    attempts = 0
    ownership_checks = 0
    launches = 0

    class ExitedChild:
        @staticmethod
        def poll() -> int:
            return 1

    def spawn(*_args: object, **_kwargs: object) -> ExitedChild:
        nonlocal launches
        launches += 1
        return ExitedChild()

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        nonlocal attempts
        attempts += 1
        return expected if attempts >= 3 else None

    def busy(_path: pathlib.Path) -> bool:
        nonlocal ownership_checks
        ownership_checks += 1
        return ownership_checks > 1

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0.001)
    try:
        assert instance.endpoint(path) == expected
        assert launches == 1
        assert attempts == 3
    finally:
        instance._children.clear()
        instance._launch_ids.clear()
        instance.close()


def test_gateway_releases_launch_gate_before_waiting_for_legacy_upgrade(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Legacy upgrade")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "replacement-browser-capability", "")
    attempts = 0

    def existing(board: pathlib.Path) -> workspace.Endpoint:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise workspace.ServiceUpgradePendingError("legacy writer needs upgrade")
        # A second acquisition would time out if the gateway still held its gate.
        with service_ownership.launch_locked(board, timeout=0.1):
            return expected

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(subprocess, "Popen", reject_spawn)
    try:
        assert instance.endpoint(path) == expected
        assert attempts == 2
    finally:
        instance.close()


def test_gateway_owns_actual_writer_process_and_termination_releases_lease(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Native child process")
    instance = workspace.Workspace(tmp_path)
    descriptor_path = board_state.service_descriptor_path(path)
    try:
        instance.endpoint(path)
        child = instance._children[path.resolve()]
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        assert child.pid == descriptor["pid"]
        assert service_ownership.writer_busy(path)
        child.terminate()
        child.wait(timeout=5)
        deadline = time.monotonic() + 5
        while service_ownership.writer_busy(path):
            assert time.monotonic() < deadline, "terminated writer retained its lease"
            time.sleep(0.01)
        assert instance._existing(path) is None
        # A forcefully terminated writer cannot remove its rendezvous metadata.
        descriptor_path.unlink(missing_ok=True)
    finally:
        instance.close()


def test_busy_board_recovery_does_not_block_another_healthy_board(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    make_board(first, "Busy")
    make_board(second, "Healthy")
    instance = workspace.Workspace(tmp_path)
    waiting, release = threading.Event(), threading.Event()
    expected = workspace.Endpoint(12345, "browser", "")

    def existing(path: pathlib.Path) -> workspace.Endpoint | None:
        if path == first:
            if not waiting.is_set():
                waiting.set()
                return None
            assert release.wait(5), "busy recovery was not released"
        return expected

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(subprocess, "Popen", reject_spawn)
    with ThreadPoolExecutor(max_workers=2) as pool, service_ownership.writer_claim(first):
        blocked = pool.submit(instance.endpoint, first)
        try:
            assert waiting.wait(5)
            healthy = pool.submit(instance.endpoint, second)
            assert healthy.result(timeout=5) == expected
            assert not blocked.done()
        finally:
            release.set()
        assert blocked.result(timeout=5) == expected
    instance.close()


def test_equivalent_board_requests_share_one_owned_launch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "One launch")
    instance = workspace.Workspace(tmp_path)
    spawning, release, second_acquiring = (threading.Event() for _ in range(3))
    expected = workspace.Endpoint(12345, "browser", "")
    launches = 0
    calls = 0
    original = instance._board_lock

    class Child:
        @staticmethod
        def poll() -> None:
            return None

    def spawn(*_args: object, **_kwargs: object) -> Child:
        nonlocal launches
        launches += 1
        spawning.set()
        assert release.wait(5)
        return Child()

    def lock_for(board: pathlib.Path) -> threading.Lock:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_acquiring.set()
        return original(board)

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        return expected if release.is_set() else None

    def clean(*_args: object) -> None:
        pass

    monkeypatch.setattr(instance, "_board_lock", lock_for)
    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(instance, "_close_child", clean)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(instance.endpoint, path)
        try:
            assert spawning.wait(5)
            second = pool.submit(instance.endpoint, tmp_path / "unused" / ".." / "board.json")
            assert second_acquiring.wait(5)
            assert not second.done()
            assert launches == 1
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == expected
    assert len(instance._children) == len(instance._launch_ids) == 1
    instance.close()
    assert not instance._children
    assert not instance._launch_ids


def test_close_waits_for_child_registration_without_holding_bookkeeping(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Closing launch")
    instance = workspace.Workspace(tmp_path)
    spawning, release, closing, cleanup = (threading.Event() for _ in range(4))
    board_lock = threading.Lock()
    expected = workspace.Endpoint(12345, "browser", "")
    cleaned: list[str] = []

    class ObservedLock:
        def __enter__(self) -> None:
            if spawning.is_set():
                closing.set()
            board_lock.acquire()

        def __exit__(self, *_args: object) -> None:
            board_lock.release()

    class Child:
        @staticmethod
        def poll() -> None:
            return None

    def spawn(*_args: object, **_kwargs: object) -> Child:
        spawning.set()
        assert release.wait(5)
        return Child()

    def clean(_path: pathlib.Path, _child: object, launch_id: str) -> None:
        assert instance._bookkeeping_lock.acquire(blocking=False)
        instance._bookkeeping_lock.release()
        cleaned.append(launch_id)
        cleanup.set()

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        return expected if release.is_set() else None

    instance._board_locks[path.resolve()] = cast("threading.Lock", ObservedLock())
    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(instance, "_close_child", clean)
    with ThreadPoolExecutor(max_workers=2) as pool:
        acquisition = pool.submit(instance.endpoint, path)
        try:
            assert spawning.wait(5)
            shutdown = pool.submit(instance.close)
            assert closing.wait(5)
            assert not shutdown.done()
            assert instance._bookkeeping_lock.acquire(blocking=False)
            instance._bookkeeping_lock.release()
        finally:
            release.set()
        assert acquisition.result(timeout=5) == expected
        shutdown.result(timeout=5)
    assert cleanup.is_set()
    assert len(cleaned) == 1
    assert not instance._children
    assert not instance._launch_ids
    with pytest.raises(OSError, match="closed"):
        instance.endpoint(path)


@pytest.mark.parametrize("timeout", [False, True])
def test_close_preserves_replacement_service_and_bounds_owned_child_cleanup(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, timeout: bool
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Replacement writer")
    instance = workspace.Workspace(tmp_path)
    instance._board_lock(path.resolve())
    descriptor = board_state.service_descriptor_path(path)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    document = {"port": 12345, "token": "replacement-token", "workspaceLaunch": "replacement"}
    descriptor.write_text(json.dumps(document), encoding="utf-8")
    waits: list[float] = []
    terminations: list[bool] = []

    class Child:
        @staticmethod
        def wait(*, timeout: float) -> int:
            waits.append(timeout)
            if len(waits) == 1 and timeout_child:
                raise subprocess.TimeoutExpired("owned-child", timeout)
            return 0

        @staticmethod
        def terminate() -> None:
            terminations.append(True)

    timeout_child = timeout
    instance._children[path.resolve()] = cast("subprocess.Popen[bytes]", Child())
    instance._launch_ids[path.resolve()] = "original"
    monkeypatch.setattr(instance, "_existing", reject_spawn)
    monkeypatch.setattr(workspace.http.client, "HTTPConnection", reject_spawn)
    instance.close()
    assert json.loads(descriptor.read_text(encoding="utf-8")) == document
    assert waits == [workspace.REQUEST_TIMEOUT] * (2 if timeout else 1)
    assert terminations == ([True] if timeout else [])
    assert not instance._children
    assert not instance._launch_ids


def test_close_retains_owned_pair_after_cleanup_failure_for_retry(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Retry cleanup")
    instance = workspace.Workspace(tmp_path)
    instance._board_lock(path.resolve())
    attempts: list[str] = []

    class Child:
        @staticmethod
        def terminate() -> None:
            assert instance._bookkeeping_lock.acquire(blocking=False)
            instance._bookkeeping_lock.release()
            attempts.append("terminate")
            if len(attempts) == 1:
                msg = "controlled transient termination failure"
                raise PermissionError(msg)

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == workspace.REQUEST_TIMEOUT
            return 0

    child = cast("subprocess.Popen[bytes]", Child())
    instance._children[path.resolve()] = child
    instance._launch_ids[path.resolve()] = "original"
    with pytest.raises(OSError, match="incomplete for 1"):
        instance.close()
    assert instance._children[path.resolve()] is child
    assert instance._launch_ids[path.resolve()] == "original"
    with pytest.raises(OSError, match="closed"):
        instance.endpoint(path)
    instance.close()
    assert attempts == ["terminate", "terminate"]
    assert not instance._children
    assert not instance._launch_ids


@pytest.mark.parametrize("residual", [False, True])
def test_failed_start_escalates_captured_child_and_retains_only_residual(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, residual: bool
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Failed launch")
    instance = workspace.Workspace(tmp_path)
    actions: list[str] = []

    class Child:
        @staticmethod
        def terminate() -> None:
            actions.append("terminate")

        @staticmethod
        def kill() -> None:
            actions.append("kill")

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == workspace.REQUEST_TIMEOUT
            assert instance._bookkeeping_lock.acquire(blocking=False)
            instance._bookkeeping_lock.release()
            actions.append("wait")
            if actions.count("wait") == 1 or residual:
                raise subprocess.TimeoutExpired("private-command-secret", timeout)
            return 0

    child = cast("subprocess.Popen[bytes]", Child())

    def spawn(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return child

    def unavailable(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(instance, "_await_existing", unavailable)
    monkeypatch.setattr(workspace, "START_WAIT_SECONDS", 0)
    message = "cleanup did not complete" if residual else "healthy endpoint"
    with pytest.raises(OSError, match=message) as failure:
        instance.endpoint(path)
    assert "private-command-secret" not in str(failure.value)
    assert actions == ["terminate", "wait", "kill", "wait"]
    if residual:
        assert instance._children[path.resolve()] is child
        assert instance._launch_ids[path.resolve()]
        with pytest.raises(OSError, match="cleanup did not complete"):
            instance.endpoint(path)
        assert instance._children[path.resolve()] is child
    else:
        assert not instance._children
        assert not instance._launch_ids


def test_close_attempts_later_child_and_retries_residual_without_bookkeeping_lock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = workspace.Workspace(tmp_path)
    paths = [tmp_path / name for name in ("first.json", "second.json")]
    actions: list[str] = []
    residual = True

    class Child:
        def __init__(self, name: str) -> None:
            self.name = name

        def terminate(self) -> None:
            actions.append(self.name + ":terminate")

        def kill(self) -> None:
            actions.append(self.name + ":kill")

        def wait(self, *, timeout: float) -> int:
            assert timeout == workspace.REQUEST_TIMEOUT
            assert instance._bookkeeping_lock.acquire(blocking=False)
            instance._bookkeeping_lock.release()
            actions.append(self.name + ":wait")
            if self.name == "first" and residual:
                raise subprocess.TimeoutExpired("private-command-secret", timeout)
            return 0

    for path in paths:
        instance._board_lock(path)
        instance._children[path] = cast("subprocess.Popen[bytes]", Child(path.stem))
        instance._launch_ids[path] = path.stem

    def unavailable(_path: pathlib.Path) -> None:
        return None

    monkeypatch.setattr(service_ownership, "read_service_metadata", unavailable)
    with pytest.raises(OSError, match="incomplete for 1") as failure:
        instance.close()
    assert "private-command-secret" not in str(failure.value)
    assert actions == [
        "first:terminate",
        "first:wait",
        "first:kill",
        "first:wait",
        "second:terminate",
        "second:wait",
    ]
    assert list(instance._children) == paths[:1]
    assert list(instance._launch_ids) == paths[:1]
    residual = False
    instance.close()
    assert not instance._children
    assert not instance._launch_ids


@pytest.mark.parametrize("changed", ["child", "launch"])
def test_close_does_not_forget_replaced_ownership_pair(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = tmp_path / "board.json"
    instance = workspace.Workspace(tmp_path)
    instance._board_lock(path)
    child = cast("subprocess.Popen[bytes]", object())
    replacement = cast("subprocess.Popen[bytes]", object())
    instance._children[path] = child
    instance._launch_ids[path] = "original"

    def clean(_path: pathlib.Path, _child: object, _launch_id: str) -> None:
        if changed == "child":
            instance._children[path] = replacement
        else:
            instance._launch_ids[path] = "replacement"

    monkeypatch.setattr(instance, "_close_child", clean)
    instance.close()
    assert path in instance._children
    assert path in instance._launch_ids


def test_new_launch_reaps_previous_captured_child_before_replacing_handle(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Retry launch")
    instance = workspace.Workspace(tmp_path)
    instance._board_lock(path)
    actions: list[str] = []

    class Child:
        def __init__(self, name: str) -> None:
            self.name = name

        def terminate(self) -> None:
            actions.append(self.name + ":terminate")

        def wait(self, *, timeout: float) -> int:
            assert timeout == workspace.REQUEST_TIMEOUT
            actions.append(self.name + ":wait")
            return 0

    previous = cast("subprocess.Popen[bytes]", Child("previous"))
    instance._children[path] = previous
    instance._launch_ids[path] = "previous"

    def spawn(*_args: object, **_kwargs: object) -> Child:
        assert path not in instance._children
        assert path not in instance._launch_ids
        actions.append("spawn")
        return Child("new")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(instance, "_await_existing", unavailable)
    monkeypatch.setattr(workspace, "START_WAIT_SECONDS", 0)
    with pytest.raises(TimeoutError, match="healthy endpoint"):
        instance.endpoint(path)
    assert actions == ["previous:terminate", "previous:wait", "spawn", "new:terminate", "new:wait"]
    assert not instance._children
    assert not instance._launch_ids
