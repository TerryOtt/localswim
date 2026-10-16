"""Shared monitor attribution, quiet windows, portable ownership and graceful reload."""

import json
import subprocess
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from localswim import monitor as watcher

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def config(tmp_path: Path) -> watcher.Config:
    board = tmp_path / "board.json"
    board.write_text("{}", encoding="utf-8")
    return watcher.Config(
        board,
        "test board",
        str(uuid.uuid4()),
        8795,
        tmp_path / "status.json",
        tmp_path / "target.json",
        tmp_path / "monitor.lock",
        15.0,
    )


@pytest.fixture
def pending() -> watcher.PendingAlert:
    return watcher.PendingAlert(
        "2026-10-16T10:34:46.990571-04:00",
        "2026-10-16T10:37:11.734748-04:00",
        "2026-10-16T10:37:13.308249-04:00",
        100.0,
        5,
    )


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"by": "bot"}, {"by": "terry"}],
        [{"by": "terry"}],
        [{}],
        [{"by": None}],
        [None],
        {"by": "bot"},
        "bad JSON",
    ],
)
def test_uncertain_activity_alerts(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    events: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = events if isinstance(events, str) else json.dumps(events)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="localswim-cli"))
    monkeypatch.setattr(watcher.subprocess, "run", run)
    assert not watcher.bot_only_activity(config, pending)


@pytest.mark.parametrize("count", [1, 4, 9])
def test_automation_activity_ignores_filesystem_counts(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, json.dumps([{"by": "robot"}] * count), "")
    )
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="localswim-cli"))
    monkeypatch.setattr(watcher.subprocess, "run", run)
    assert (
        watcher.flush_pending_alert(replace(config, automation_actor="robot"), "codex", pending)
        is None
    )
    assert run.call_count == 1
    assert run.call_args.args[0][-2:] == [pending.activity_started_at, pending.last_observed_at]


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("cli", 10)])
def test_activity_failures_alert(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="localswim-cli"))
    monkeypatch.setattr(watcher.subprocess, "run", Mock(side_effect=failure))
    assert not watcher.bot_only_activity(config, pending)


def test_missing_cli_and_failed_cli_alert(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value=None))
    assert not watcher.bot_only_activity(config, pending)
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="localswim-cli"))
    monkeypatch.setattr(
        watcher.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1, "[]", ""))
    )
    assert not watcher.bot_only_activity(config, pending)


def test_quiet_window_is_trailing_edge(pending: watcher.PendingAlert) -> None:
    extended = watcher.note_board_write(
        pending,
        activity_started_at="unused",
        observed_at=pending.last_observed_at,
        observed_monotonic=110,
    )
    assert extended.activity_started_at == pending.activity_started_at
    assert extended.write_count == 6
    assert not watcher.quiet_window_elapsed(extended, current_monotonic=124.99, quiet_seconds=15)
    assert watcher.quiet_window_elapsed(extended, current_monotonic=125, quiet_seconds=15)


def test_lock_is_exclusive_and_persistent(config: watcher.Config) -> None:
    first = watcher.acquire_lock(config.lock_path)
    assert first is not None
    try:
        assert watcher.acquire_lock(config.lock_path) is None
    finally:
        watcher.release_lock(first)
    assert config.lock_path.exists()
    second = watcher.acquire_lock(config.lock_path)
    assert second is not None
    watcher.release_lock(second)


def test_requests_are_pid_specific(config: watcher.Config) -> None:
    assert not watcher.reload_requested(config.target_path, 123)
    watcher.write_atomic_json(config.target_path, {"schema": 1, "reloadPid": 123, "stopPid": 456})
    assert watcher.reload_requested(config.target_path, 123)
    assert not watcher.reload_requested(config.target_path, 456)
    assert watcher.stop_requested(config.target_path, 456)
    assert not watcher.stop_requested(config.target_path, 123)


@pytest.mark.parametrize("native_change", [False, True])
def test_reload_debounces_initialization_write_with_or_without_notification(
    config: watcher.Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    native_change: bool,
) -> None:
    clock = Mock(side_effect=[100.0, 101.0, 116.0])
    monkeypatch.setattr(watcher.time, "monotonic", clock)
    monkeypatch.setattr(watcher.time, "sleep", Mock(return_value=None))
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="codex"))
    monkeypatch.setattr(watcher, "service_is_healthy", Mock(return_value=True))
    flush = Mock(return_value="alert stamp")
    monkeypatch.setattr(watcher, "flush_pending_alert", flush)

    def changes(_: watcher.Config) -> Iterator[set[object]]:
        config.board.write_text('{"changed":true}', encoding="utf-8")
        watcher.write_atomic_json(
            config.target_path,
            {"schema": 1, "threadId": config.thread_id, "reloadPid": watcher.os.getpid()},
        )
        yield {object()} if native_change else set()
        status = watcher.read_json(config.status_path)
        assert status["state"] == "running"
        assert status["pendingWriteCount"] == 1
        assert flush.call_count == 0
        yield set()
        if native_change:
            assert flush.call_count == 0
            yield set()

    monkeypatch.setattr(watcher, "watch_board", changes)
    assert watcher.monitor(config) == 0
    assert flush.call_count == 1
    status = watcher.read_json(config.status_path)
    assert status["state"] == "reload-ready"
    assert status["pendingWriteCount"] == 0
    assert status["lastAlertAt"] == "alert stamp"
    lock = watcher.acquire_lock(config.lock_path)
    assert lock is not None
    watcher.release_lock(lock)


def test_reload_preserves_handoff_gap_activity(config: watcher.Config) -> None:
    cursor = "2026-10-16T10:34:46.990571-04:00"
    watcher.write_atomic_json(
        config.status_path, {"lastSignatureAt": cursor, "lastSignature": [0, 0]}
    )
    runtime = watcher.resume_runtime(replace(config, reload=True), {}, watcher.now())
    assert runtime.pending_alert is not None
    assert runtime.pending_alert.activity_started_at == cursor


def test_reload_launch_waits_for_exact_owner(
    config: watcher.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher.write_atomic_json(config.status_path, {"schema": 1, "pid": 123})
    handle = Mock()
    acquire = Mock(side_effect=[None, None, handle])
    monkeypatch.setattr(watcher, "acquire_lock", acquire)
    monkeypatch.setattr(watcher.time, "sleep", Mock(return_value=None))
    claimed, _ = watcher.claim_monitor(replace(config, reload=True))
    assert claimed is handle
    assert acquire.call_count == 3
    assert watcher.read_json(config.target_path)["reloadPid"] == 123


def test_cli_requires_board_specific_runtime_paths(config: watcher.Config) -> None:
    args = ["--board-path", str(config.board), "--thread-id", config.thread_id, "--port", "8795"]
    with pytest.raises(SystemExit):
        watcher.parse_args(args)
    for flag, path in [
        ("status", config.status_path),
        ("target", config.target_path),
        ("lock", config.lock_path),
    ]:
        args.extend([f"--{flag}-path", str(path)])
    parsed = watcher.parse_args([*args, "--automation-actor", "robot", "--reload"])
    assert parsed.automation_actor == "robot"
    assert parsed.reload
    assert parsed.board_label == "localswim"


def test_optional_backend_failure_has_install_hint(
    monkeypatch: pytest.MonkeyPatch, config: watcher.Config
) -> None:
    monkeypatch.setattr(watcher.importlib, "import_module", Mock(side_effect=ImportError()))
    with pytest.raises(RuntimeError, match=r"localswim\[monitor\]"):
        watcher.watch_board(config)


def test_reload_retains_unflushed_activity_after_failed_drain(config: watcher.Config) -> None:
    cursor = "2026-10-16T10:34:46.990571-04:00"
    watcher.write_atomic_json(
        config.status_path,
        {
            "lastSignatureAt": cursor,
            "lastSignature": list(watcher.board_signature(config.board)),
            "pendingWriteCount": 3,
            "pendingActivityStartedAt": cursor,
            "pendingFirstObservedAt": cursor,
            "pendingLastObservedAt": cursor,
        },
    )
    runtime = watcher.resume_runtime(replace(config, reload=True), {}, watcher.now())
    assert runtime.pending_alert is not None
    assert runtime.pending_alert.write_count == 3
    assert runtime.pending_alert.activity_started_at == cursor


def test_reload_timeout_cancels_request(
    config: watcher.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher.write_atomic_json(config.status_path, {"schema": 1, "pid": 123})
    monkeypatch.setattr(watcher, "acquire_lock", Mock(return_value=None))
    monkeypatch.setattr(watcher.time, "monotonic", Mock(side_effect=[0.0, 331.0]))
    with pytest.raises(RuntimeError, match="timed out"):
        watcher.claim_monitor(replace(config, reload=True))
    assert "reloadPid" not in watcher.read_json(config.target_path)
