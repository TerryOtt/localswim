"""Gateway launch coordination around live, restarting, and crashed writers."""

# ruff: noqa: SLF001 -- ownership tests inspect gateway process and discovery seams

import json
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from localswim import board_state, service_ownership, workspace
from tests.test_workspace import existing_writer, make_board

if TYPE_CHECKING:
    import pathlib
    from typing import Never


def reject_spawn(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("a live writer must never cause another process launch")


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
