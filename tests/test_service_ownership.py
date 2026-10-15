"""Canonical writer leases and supervised board replacement prevent duplicate writers."""

import contextlib
import json
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, board_state, service_ownership
from tests.test_api import request_json
from tests.test_workspace import make_board

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator


@contextlib.contextmanager
def live_board(
    path: pathlib.Path, *, restartable: bool = False
) -> Generator[subprocess.Popen[bytes]]:
    make_board(path, "Ownership fixture")
    arguments = [sys.executable, "-m", "localswim.api_endpoint", str(path), "--port", "0"]
    if restartable:
        program = (
            "import sys,time; from localswim import api_endpoint as api; "
            "api._BOOT_CODE=tuple((m,d+'old') for m,d in api._BOOT_CODE); "
            "api.code_is_stale=lambda:True; api._source_startup_problem=lambda:None; "
            "api.RESTART_DEBOUNCE_S=0.01; original=api._handoff_board; "
            "api._handoff_board=lambda *args:(time.sleep(0.4),original(*args))[-1]; "
            "sys.argv=['localswim',*sys.argv[1:]]; api.main()"
        )
        arguments = [sys.executable, "-c", program, str(path), "--port", "0"]
    environment = {**os.environ, "LOCALSWIM_WORKSPACE_LAUNCH": "fixture-launch"}
    arguments = service_ownership.python_child_command(arguments[1:], environment)
    child = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 15
        while not service_ownership.service_is_healthy(path):
            if child.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("board service did not start")
            time.sleep(0.02)
        yield child
    finally:
        descriptor = board_state.service_descriptor_path(path)
        with contextlib.suppress(OSError, ValueError):
            raw = json.loads(descriptor.read_text(encoding="utf-8"))
            request_json(
                f"http://127.0.0.1:{raw['port']}" + board_state.API_PREFIX + "/shutdown",
                token=raw["token"],
                body={},
            )
        deadline = time.monotonic() + 10
        while service_ownership.writer_busy(path) and time.monotonic() < deadline:
            time.sleep(0.02)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        assert not service_ownership.writer_busy(path)


def test_writer_lease_is_exclusive_and_releases_without_unlinking(tmp_path: pathlib.Path) -> None:
    board = tmp_path / "board.json"
    lock = board_state.service_descriptor_path(board).with_suffix(".writer.lock")
    with service_ownership.writer_claim(board):
        assert service_ownership.writer_busy(board)
        with (
            pytest.raises(service_ownership.WriterAlreadyRunningError),
            service_ownership.writer_claim(board),
        ):
            pytest.fail("a duplicate acquired ownership")
    assert lock.exists()
    assert not service_ownership.writer_busy(board)


def test_launch_capability_is_one_use_and_bound_to_parent_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    with service_ownership.launch_locked(board) as gate:
        marker = gate.child_environment()[service_ownership.LAUNCH_MARKER_ENV]
        monkeypatch.setenv(service_ownership.LAUNCH_MARKER_ENV, marker)
        assert service_ownership.consume_launch(board)
        monkeypatch.setenv(service_ownership.LAUNCH_MARKER_ENV, marker)
        with pytest.raises(OSError, match="invalid board launch capability"):
            service_ownership.consume_launch(board)
        environment = gate.child_environment()
        monkeypatch.setenv(
            service_ownership.LAUNCH_MARKER_ENV, environment[service_ownership.LAUNCH_MARKER_ENV]
        )
        with pytest.raises(OSError, match="invalid board launch capability"):
            service_ownership.consume_launch(tmp_path / "other.json")
    assert service_ownership.LAUNCH_MARKER_ENV not in os.environ


@pytest.mark.parametrize(
    "payload", [b"[" * 2000 + b"]" * 2000, b"x" * 5000], ids=["deep", "oversized"]
)
def test_invalid_launch_ticket_is_bounded_and_releases_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    board = tmp_path / "board.json"
    with service_ownership.launch_locked(board) as gate:
        environment = gate.child_environment()
        ticket = board_state.service_descriptor_path(board).with_suffix(".launch-token")
        ticket.write_bytes(payload)
        monkeypatch.setenv(
            service_ownership.LAUNCH_MARKER_ENV, environment[service_ownership.LAUNCH_MARKER_ENV]
        )
        with pytest.raises(OSError, match="invalid board launch capability"):
            service_ownership.consume_launch(board)
    with service_ownership.launch_locked(board, timeout=0):
        pass


def test_direct_duplicate_preserves_existing_descriptor(tmp_path: pathlib.Path) -> None:
    board = tmp_path / "board.json"
    with live_board(board):
        descriptor = board_state.service_descriptor_path(board)
        before = descriptor.read_bytes()
        result = subprocess.run(
            [sys.executable, "-m", "localswim.api_endpoint", str(board), "--port", "0"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode != 0
        assert b"already owns this board" in result.stderr
        assert descriptor.read_bytes() == before
        assert service_ownership.service_is_healthy(board)
        descriptor.write_text("{}", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "localswim.api_endpoint", str(board), "--port", "0"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            assert result.returncode != 0
            assert b"already owns this board" in result.stderr
            assert descriptor.read_text(encoding="utf-8") == "{}"
        finally:
            descriptor.write_bytes(before)


def test_real_restart_preserves_ephemeral_port_and_launch_ownership(tmp_path: pathlib.Path) -> None:
    board = tmp_path / "board.json"
    with live_board(board, restartable=True) as parent:
        descriptor = board_state.service_descriptor_path(board)
        before = json.loads(descriptor.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{before['port']}"
        status, _ = request_json(base + board_state.API_PREFIX + "/status")
        assert status == 200
        deadline = time.monotonic() + 10
        saw_gap = False
        while time.monotonic() < deadline:
            raw = json.loads(descriptor.read_text(encoding="utf-8"))
            if raw["pid"] != before["pid"] and service_ownership.service_is_healthy(board):
                break
            if not service_ownership.service_is_healthy(board):
                saw_gap = True
                assert descriptor.exists()
                try:
                    with service_ownership.launch_locked(board, timeout=0):
                        latest = json.loads(descriptor.read_text(encoding="utf-8"))
                        assert latest["pid"] != before["pid"]
                        assert service_ownership.service_is_healthy(board)
                except TimeoutError:
                    pass
            time.sleep(0.01)
        else:
            pytest.fail("replacement did not become healthy")
        assert saw_gap
        assert raw["port"] == before["port"]
        assert raw["workspaceLaunch"] == before["workspaceLaunch"] == "fixture-launch"
        assert parent.wait(timeout=5) == 0
        assert service_ownership.writer_busy(board)
        assert not request_json(base + board_state.API_PREFIX + "/status")[1]["codeStale"]


def test_shutdown_takes_priority_over_pending_restart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    make_board(board, "Shutdown")
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", board)
    api_endpoint.publish_service(board, 12345)
    api_endpoint._shutdown_requested.set()  # noqa: SLF001 -- exercise shutdown/restart ordering
    try:
        with service_ownership.writer_claim(board) as lease:
            api_endpoint._handoff_board(  # noqa: SLF001 -- exercise the owned handoff seam
                api_endpoint.parse_args([str(board)]), 12345, lease
            )
            assert service_ownership.writer_busy(board)
        assert not board_state.service_descriptor_path(board).exists()
    finally:
        api_endpoint._shutdown_requested.clear()  # noqa: SLF001 -- reset lifecycle seam


def test_failed_replacement_releases_gate_and_writer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    make_board(board, "Failed replacement")
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", board)
    server = api_endpoint.BoardHttpServer((api_endpoint.HOST, 0), api_endpoint.Handler)
    try:
        with service_ownership.writer_claim(board) as lease:
            assert api_endpoint._reserve_board_restart(server)  # noqa: SLF001 -- reserve lifecycle gate
            api_endpoint.publish_service(board, server.server_address[1])

            def fail_launch(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
                raise OSError("failed startup")

            monkeypatch.setattr(api_endpoint.subprocess, "Popen", fail_launch)
            with pytest.raises(SystemExit, match="board restart failed"):
                api_endpoint._handoff_board(  # noqa: SLF001 -- exercise failed startup cleanup
                    api_endpoint.parse_args([str(board)]), server.server_address[1], lease
                )
        assert not service_ownership.writer_busy(board)
        assert not board_state.service_descriptor_path(board).exists()
        with service_ownership.launch_locked(board, timeout=0):
            pass
    finally:
        server.server_close()
        api_endpoint._release_restart_gate()  # noqa: SLF001 -- reset lifecycle gate


def test_replacement_timeout_terminates_actual_python_process(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    make_board(board, "Timed out replacement")
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", board)
    monkeypatch.setattr(service_ownership, "LOCK_WAIT_SECONDS", 0.2)
    server = api_endpoint.BoardHttpServer((api_endpoint.HOST, 0), api_endpoint.Handler)
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def stalled_replacement(_arguments: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        environment = dict(os.environ)
        arguments = service_ownership.python_child_command(
            [
                "-c",
                (
                    "import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "print(os.getpid(),flush=True);time.sleep(60)"
                ),
            ],
            environment,
        )
        child = original_popen(
            arguments,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        children.append(child)
        assert child.stdout is not None
        assert int(child.stdout.readline()) == child.pid
        child.stdout.close()
        return child

    try:
        with service_ownership.writer_claim(board) as lease:
            assert api_endpoint._reserve_board_restart(server)  # noqa: SLF001 -- reserve handoff
            api_endpoint.publish_service(board, server.server_address[1])
            monkeypatch.setattr(api_endpoint.subprocess, "Popen", stalled_replacement)
            with pytest.raises(SystemExit, match="board restart failed"):
                api_endpoint._handoff_board(  # noqa: SLF001 -- test timeout cleanup
                    api_endpoint.parse_args([str(board)]), server.server_address[1], lease
                )
        assert len(children) == 1
        assert children[0].poll() is not None
        assert not service_ownership.writer_busy(board)
        assert not board_state.service_descriptor_path(board).exists()
        with service_ownership.launch_locked(board, timeout=0):
            pass
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        server.server_close()
        api_endpoint._release_restart_gate()  # noqa: SLF001 -- reset handoff state


@pytest.mark.parametrize(
    "payload", [b"[" * 2000 + b"]" * 2000, b"x" * 70000], ids=["deep", "oversized"]
)
def test_failed_replacement_cleanup_rejects_bounded_bad_metadata(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    board = tmp_path / "board.json"
    make_board(board, "Bad replacement metadata")
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", board)
    environment = dict(os.environ)
    arguments = service_ownership.python_child_command(
        ["-c", "import time;time.sleep(60)"], environment
    )
    child = subprocess.Popen(
        arguments,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        board_state.service_descriptor_path(board).write_bytes(payload)
        assert not api_endpoint._replacement_ready(child, 12345)  # noqa: SLF001 -- malformed boundary
        api_endpoint._remove_failed_replacement(child)  # noqa: SLF001 -- cleanup must stop child
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        board_state.service_descriptor_path(board).unlink(missing_ok=True)
