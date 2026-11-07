"""Private writers, real child CLI and native coordinator with fake alert sinks."""

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from localswim import board_state, service_ownership
from localswim import monitor as watcher
from localswim import monitor_pool as pool
from tests.support import USERS


def wait_until(predicate: Callable[[], bool], seconds: float = 8) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("synthetic integration deadline exceeded")
        time.sleep(0.025)


def human_comment(config: watcher.Config) -> None:
    """Use the genuine browser capability and actor through the private API."""
    descriptor = service_ownership.service_descriptor(config.board)
    connection = http.client.HTTPConnection("127.0.0.1", cast("int", descriptor["port"]), timeout=3)
    try:
        connection.request("GET", board_state.API_PREFIX + "/browser-session")
        response = connection.getresponse()
        assert response.status == 200
        session = json.loads(response.read())
        connection.request("GET", board_state.API_PREFIX + "/board")
        response = connection.getresponse()
        snapshot = json.loads(response.read())
        connection.request(
            "POST",
            board_state.API_PREFIX + "/cards/alpha/comment",
            body=json.dumps({"text": "Synthetic"}),
            headers={
                "Authorization": "Bearer " + session["token"],
                "Content-Type": "application/json",
                "If-Match": str(snapshot["revision"]),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
    finally:
        connection.close()


def assert_alert(
    config: watcher.Config,
    alerts: list[tuple[Path, str, list[object] | None]],
    expected_actors: frozenset[str] = frozenset({"terry"}),
) -> None:
    matched = [(target, events) for path, target, events in alerts if path == config.board]
    assert len(matched) == 1
    target, events = matched[0]
    assert target == config.thread_id
    assert events is not None
    actors = {str(cast("dict[str, object]", event).get("by")) for event in events}
    assert expected_actors <= actors


def cleanup_writers(
    configs: list[watcher.Config],
    writers: list[subprocess.Popen[bytes]],
    run_cli: Callable[..., None],
) -> None:
    for config, process in zip(configs, writers, strict=False):
        if process.poll() is None:
            run_cli(config, "board", "shutdown")
        process.wait(timeout=8)


def run_worker(
    coordinator: pool.Coordinator, result: list[int], failures: list[BaseException]
) -> None:
    try:
        result.append(coordinator.run())
    except BaseException as error:  # noqa: BLE001 -- join and report worker failure
        failures.append(error)


def assert_idle_io(configs: list[watcher.Config], calls: Mock) -> None:
    """Observe actual coordinator idle time without replacing query or watch paths."""

    def query_count() -> int:
        return sum("monitor-between" in call.args[0] for call in calls.call_args_list)

    before = query_count()
    assert before >= len(configs)
    signatures = [watcher.board_signature(config.status_path) for config in configs]
    deadline = time.monotonic() + 0.6
    wait_until(lambda: time.monotonic() >= deadline, seconds=2)
    assert query_count() == before
    assert [watcher.board_signature(config.status_path) for config in configs] == signatures


def test_real_writer_cli_native_pool_actor_filter_and_graceful_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("watchfiles")
    source = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("PYTHONPATH", str(source))
    child_env = os.environ.copy()
    cli = shutil.which("localswim-cli")
    assert cli is not None
    original_which = shutil.which

    def which(name: str) -> str | None:
        return sys.executable if name == "codex" else original_which(name)

    monkeypatch.setattr(watcher.shutil, "which", which)
    monkeypatch.setattr(pool, "RECONCILE_SECONDS", 1.0)
    actual_cli_calls = Mock(wraps=subprocess.run)
    monkeypatch.setattr(watcher.subprocess, "run", actual_cli_calls)
    configs: list[watcher.Config] = []
    writers: list[subprocess.Popen[bytes]] = []
    alerts: list[tuple[Path, str, list[object] | None]] = []
    result: list[int] = []
    failures: list[BaseException] = []

    def fake_sink(
        config: watcher.Config,
        _codex: str,
        _pending: watcher.PendingAlert,
        activity: list[object] | None = None,
    ) -> None:
        alerts.append(
            (config.board, str(watcher.read_json(config.target_path)["threadId"]), activity)
        )

    monkeypatch.setattr(watcher, "queue_alert", fake_sink)

    def run_cli(config: watcher.Config, *args: str) -> None:
        completed = subprocess.run(
            [cli, str(config.board), *args],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        assert completed.returncode == 0

    def status(config: watcher.Config) -> dict[str, object]:
        try:
            return watcher.read_json(config.status_path)
        except FileNotFoundError:
            return {}

    coordinator: pool.Coordinator | None = None
    worker: threading.Thread | None = None
    try:
        for index in range(2):
            board = tmp_path / f"board-{index}.json"
            board_state.save(
                board_state.Board(
                    project="synthetic",
                    users=USERS,
                    browser_user="terry",
                    cli_user="bot",
                    default_owner="bot",
                ),
                board,
            )
            config = watcher.Config(
                board,
                "synthetic",
                str(uuid.uuid4()),
                8795,
                board.with_suffix(".status"),
                board.with_suffix(".target"),
                board.with_suffix(".lock"),
                0.4,
            )
            configs.append(config)
            with (
                board.with_suffix(".stdout").open("wb") as out,
                board.with_suffix(".stderr").open("wb") as err,
            ):
                process = subprocess.Popen(
                    [sys.executable, "-m", "localswim.api_endpoint", str(board), "--port", "0"],
                    env=child_env,
                    stdout=out,
                    stderr=err,
                )
            writers.append(process)
            wait_until(lambda board=board: board_state.service_descriptor_path(board).exists())
            assert process.poll() is None
        coordinator = pool.Coordinator(configs)

        worker = threading.Thread(
            target=run_worker, args=(coordinator, result, failures), name="synthetic-native-pool"
        )
        worker.start()
        wait_until(lambda: all(status(config).get("state") == "running" for config in configs))
        starts = [str(status(config)["lastSignatureAt"]) for config in configs]
        for config in configs:
            run_cli(config, "card", "create", "alpha", "Synthetic", "--state", "backlog")
        wait_until(
            lambda: all(
                str(status(config).get("lastSignatureAt")) != start
                and status(config).get("pendingWriteCount") == 0
                for config, start in zip(configs, starts, strict=True)
            )
        )
        assert alerts == []
        assert not failures
        assert_idle_io(configs, actual_cli_calls)
        for config in configs:
            assert status(config)["backendMode"] == "native"
            assert watcher.read_json(config.target_path)["threadId"] == config.thread_id

        # Browser capability supplies the actual human actor; no actor mocking.
        for index, config in enumerate(configs):
            human_comment(config)
            if index == 1:
                run_cli(config, "card", "comment", "alpha", "Synthetic")
        wait_until(lambda: len(alerts) == 2)
        wait_until(lambda: all(status(config).get("pendingWriteCount") == 0 for config in configs))
        assert {path for path, _, _ in alerts} == {config.board for config in configs}
        assert_alert(configs[1], alerts, frozenset({"terry", "bot"}))
        for config in configs:
            assert_alert(config, alerts)
            assert status(config)["lastAlertAt"] is not None
            # Exact owner UUID control request, then graceful writer shutdown.
            with watcher.target_transaction(config):
                target_data = watcher.read_json(config.target_path)
                target_data.update(stopPid=os.getpid(), stopOwnerId=status(config)["ownerId"])
                watcher.write_atomic_json(config.target_path, target_data)
            run_cli(config, "board", "shutdown")
        worker.join(timeout=8)
        assert not worker.is_alive()
        assert not failures and result == [0]
        assert all(status(config)["state"] == "stopped" for config in configs)
        assert len(alerts) == 2
    finally:
        if coordinator is not None:
            coordinator.stop.set()
        if worker is not None:
            worker.join(timeout=3)
        cleanup_writers(configs, writers, run_cli)
