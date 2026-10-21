"""Shared monitor attribution, quiet windows, portable ownership and graceful reload."""

import hashlib
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


@pytest.mark.parametrize(
    "case",
    [
        ('[{"by": "terry"}]', 0, 1),
        ('[{"by": "bot"}, {"by": "terry"}]', 0, 1),
        ("[{}]", 0, 1),
        ("[]", 0, 1),
        ('[{"by": "bot"}]', 1, 1),
        ('[{"by": "bot"}]', 0, 0),
    ],
)
def test_flush_dispatches_fail_open_activity_once(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    case: tuple[str, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="localswim-cli"))
    output, exit_code, expected_queues = case
    query = Mock(return_value=subprocess.CompletedProcess([], exit_code, output, ""))
    monkeypatch.setattr(watcher.subprocess, "run", query)
    order: list[str] = []

    def record_queue(*_arguments: object) -> None:
        order.append("queue")

    queue = Mock(side_effect=record_queue)
    timestamp = Mock(side_effect=lambda: (order.append("timestamp"), "success-time")[1])
    monkeypatch.setattr(watcher, "queue_alert", queue)
    monkeypatch.setattr(watcher, "now", timestamp)
    result = watcher.flush_pending_alert(config, "mock-codex", pending)
    query.assert_called_once()
    assert queue.call_count == expected_queues
    assert order == (["queue", "timestamp"] if expected_queues else [])
    assert result == ("success-time" if expected_queues else None)
    if expected_queues:
        queue.assert_called_once_with(config, "mock-codex", pending)


def test_flush_failed_queue_cannot_record_success(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(watcher, "bot_only_activity", Mock(return_value=False))
    queue = Mock(side_effect=RuntimeError("queue unavailable"))
    timestamp = Mock()
    monkeypatch.setattr(watcher, "queue_alert", queue)
    monkeypatch.setattr(watcher, "now", timestamp)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        watcher.flush_pending_alert(config, "mock-codex", pending)
    queue.assert_called_once_with(config, "mock-codex", pending)
    timestamp.assert_not_called()
    assert "Queued" not in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [0, 7])
def test_queue_alert_targets_registered_thread_and_sanitizes_payload(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_thread = str(uuid.uuid4())
    prose = "PRIVATE_CARD_PROSE_SENTINEL"
    credential = "PRIVATE_CREDENTIAL_SENTINEL"
    target = Mock(
        return_value={
            "schema": 1,
            "threadId": target_thread,
            "prose": prose,
            "credential": credential,
        }
    )
    transport = Mock(return_value=subprocess.CompletedProcess([], exit_code, prose, credential))
    monkeypatch.setattr(watcher, "read_json", target)
    monkeypatch.setattr(watcher.subprocess, "run", transport)
    if exit_code:
        with pytest.raises(RuntimeError, match="exit status 7"):
            watcher.queue_alert(config, "mock-codex", pending)
    else:
        watcher.queue_alert(config, "mock-codex", pending)
    target.assert_called_once_with(config.target_path)
    transport.assert_called_once()
    arguments = transport.call_args.args[0]
    assert arguments[:5] == ["mock-codex", "queue", "--thread", target_thread, "--message"]
    message = arguments[5]
    assert "activity between 2026-10-16T10:34:46.990571-04:00 " in message
    assert "2026-10-16T10:37:13.308249-04:00" in message
    assert "5 board writes were coalesced" in message
    assert config.board_label in message
    assert "automation actor bot" in message
    captured = capsys.readouterr()
    for private_marker in (prose, credential):
        assert private_marker not in message + captured.out + captured.err


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


def saved_status(config: watcher.Config, pending: watcher.PendingAlert) -> dict[str, object]:
    return {
        "schema": 1,
        "state": "failed",
        "board": str(config.board),
        "lastSignature": list(watcher.board_signature(config.board)),
        "lastSignatureAt": pending.last_observed_at,
        "lastAlertAt": None,
        "pendingWriteCount": pending.write_count,
        "pendingActivityStartedAt": pending.activity_started_at,
        "pendingFirstObservedAt": pending.first_observed_at,
        "pendingLastObservedAt": pending.last_observed_at,
    }


def test_ordinary_restart_retries_failed_queue_without_board_write(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher.write_atomic_json(config.status_path, saved_status(config, pending))
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="mock-codex"))
    monkeypatch.setattr(watcher, "service_is_healthy", Mock(return_value=True))
    monkeypatch.setattr(watcher, "bot_only_activity", Mock(return_value=False))
    monkeypatch.setattr(watcher.time, "monotonic", Mock(side_effect=[100.0, 115.0] * 2))
    queue = Mock(side_effect=[RuntimeError("queue unavailable"), None])
    monkeypatch.setattr(watcher, "queue_alert", queue)

    def idle(_: watcher.Config) -> Iterator[set[object]]:
        status = watcher.read_json(config.status_path)
        assert status["pendingWriteCount"] == pending.write_count
        assert watcher.acquire_lock(config.lock_path) is None
        yield set()

    monkeypatch.setattr(watcher, "watch_board", idle)
    signature = watcher.board_signature(config.board)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        watcher.monitor(config)
    failed = watcher.read_json(config.status_path)
    assert failed["state"] == "failed"
    assert failed["pendingActivityStartedAt"] == pending.activity_started_at
    assert failed["pendingWriteCount"] == pending.write_count
    assert watcher.monitor(config) == 0
    status = watcher.read_json(config.status_path)
    assert queue.call_count == 2
    retried = queue.call_args.args[2]
    assert retried.activity_started_at == pending.activity_started_at
    assert retried.last_observed_at == pending.last_observed_at
    assert status["pendingWriteCount"] == 0
    assert status["lastAlertAt"] is not None
    assert watcher.board_signature(config.board) == signature


@pytest.mark.parametrize("with_pending", [False, True])
def test_ordinary_restart_retains_cursor_and_covers_signature_gap(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    *,
    with_pending: bool,
) -> None:
    saved = saved_status(config, pending)
    saved["lastSignature"] = [0, 0]
    if not with_pending:
        saved.update(
            pendingWriteCount=0,
            pendingActivityStartedAt=None,
            pendingFirstObservedAt=None,
            pendingLastObservedAt=None,
        )
    watcher.write_atomic_json(config.status_path, saved)
    lock, previous = watcher.claim_monitor(config)
    assert lock is not None
    try:
        runtime = watcher.resume_runtime(config, previous, watcher.now())
    finally:
        watcher.release_lock(lock)
    assert runtime.pending_alert is not None
    assert runtime.pending_alert.write_count == (pending.write_count + 1 if with_pending else 1)
    assert runtime.pending_alert.activity_started_at == (
        pending.activity_started_at if with_pending else pending.last_observed_at
    )
    assert runtime.last_signature_at == runtime.pending_alert.last_observed_at


@pytest.mark.parametrize(
    "changes",
    [
        {"board": "foreign-board.json"},
        {"schema": 2},
        {"pendingWriteCount": True},
        {"pendingWriteCount": -1},
        {"pendingWriteCount": 0},
        {"pendingLastObservedAt": None},
        {"pendingFirstObservedAt": "not-a-timestamp"},
        {"pendingActivityStartedAt": "2026-10-17T00:00:00+00:00"},
        {"lastSignatureAt": "2026-10-17T00:00:00+00:00"},
        {"lastSignatureAt": "2026-10-16T00:00:00"},
        {"lastSignature": [True, 2]},
        {"lastAlertAt": 17},
    ],
)
def test_ordinary_restart_protects_uncertain_status_and_releases_lock(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    changes: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = saved_status(config, pending)
    saved.update(changes)
    watcher.write_atomic_json(config.status_path, saved)
    before = config.status_path.read_bytes()
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="mock-codex"))
    watch = Mock()
    monkeypatch.setattr(watcher, "watch_board", watch)
    with pytest.raises(RuntimeError, match="uncertain; preserved for recovery"):
        watcher.monitor(config)
    assert config.status_path.read_bytes() == before
    watch.assert_not_called()
    lock = watcher.acquire_lock(config.lock_path)
    assert lock is not None
    watcher.release_lock(lock)


@pytest.mark.parametrize("contents", ["{", "[]", "{}"])
def test_ordinary_restart_preserves_malformed_or_partial_document(
    config: watcher.Config,
    contents: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.status_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="mock-codex"))
    with pytest.raises(RuntimeError, match="preserved for recovery"):
        watcher.monitor(config)
    assert config.status_path.read_text(encoding="utf-8") == contents


def test_ordinary_restart_retains_idle_cursor_and_previous_acknowledgement(
    config: watcher.Config,
    pending: watcher.PendingAlert,
) -> None:
    saved = saved_status(config, pending)
    saved.update(
        pendingWriteCount=0,
        pendingActivityStartedAt=None,
        pendingFirstObservedAt=None,
        pendingLastObservedAt=None,
        lastAlertAt=pending.last_observed_at,
    )
    watcher.write_atomic_json(config.status_path, saved)
    runtime = watcher.resume_runtime(config, {}, watcher.now())
    assert runtime.pending_alert is None
    assert runtime.last_signature_at == pending.last_observed_at
    assert runtime.last_alert_at == pending.last_observed_at


@pytest.mark.parametrize("events", [[{"by": "bot"}], [{"by": "terry"}], []])
def test_recovered_interval_uses_shared_actor_filter_and_mocked_transport(
    config: watcher.Config,
    pending: watcher.PendingAlert,
    events: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher.write_atomic_json(config.status_path, saved_status(config, pending))
    monkeypatch.setattr(watcher.time, "monotonic", Mock(side_effect=[100.0, 115.0]))
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="mock-executable"))
    monkeypatch.setattr(watcher, "service_is_healthy", Mock(return_value=True))
    transport = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(events), ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(watcher.subprocess, "run", transport)
    empty_changes: set[object] = set()
    monkeypatch.setattr(watcher, "watch_board", Mock(return_value=iter([empty_changes])))
    assert watcher.monitor(config) == 0
    assert transport.call_count == (1 if events == [{"by": "bot"}] else 2)
    status = watcher.read_json(config.status_path)
    assert status["pendingWriteCount"] == 0
    if transport.call_count == 2:
        assert transport.call_args.args[0][:4] == [
            "mock-executable",
            "queue",
            "--thread",
            config.thread_id,
        ]
        assert pending.activity_started_at in transport.call_args.args[0][-1]


@pytest.mark.parametrize("correct_identity", [False, True])
def test_lifecycle_uses_live_descriptor_and_board_identity(
    config: watcher.Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    correct_identity: bool,
) -> None:
    live_port = 52668
    metadata = {
        "schema": 1,
        "host": "127.0.0.1",
        "port": live_port,
        "board": str(config.board),
    }
    monkeypatch.setattr(
        watcher.service_ownership, "read_service_metadata", Mock(return_value=metadata)
    )
    identity_path = config.board if correct_identity else config.board.with_name("other-board.json")
    identity = hashlib.sha256(str(identity_path.resolve()).casefold().encode("utf-8")).hexdigest()
    response = Mock(status=200)
    response.read.return_value = json.dumps({"boardIdentity": identity}).encode("utf-8")
    connection = Mock()
    connection.getresponse.return_value = response
    connect = Mock(return_value=connection)
    monkeypatch.setattr(watcher.service_ownership.http.client, "HTTPConnection", connect)
    legacy_probe = Mock()
    monkeypatch.setattr(watcher.urllib.request, "urlopen", legacy_probe)
    runtime = watcher.MonitorRuntime(watcher.board_signature(config.board), watcher.now())
    watcher.handle_idle_cycle(config, "codex", runtime, Mock())
    assert runtime.service_misses == (0 if correct_identity else 1)
    connect.assert_called_once_with("127.0.0.1", live_port, timeout=1)
    assert live_port != config.port
    legacy_probe.assert_not_called()
    connection.close.assert_called_once()
