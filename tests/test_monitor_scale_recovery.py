"""Independent scale, native-watch and restart qualification for shared monitors.

All files and targets are synthetic. Alert transport is replaced by an in-memory
sink; these tests never start a production monitor or contact a Codex chat.
"""

import io
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from localswim import activity_feed
from localswim import monitor as watcher
from localswim import monitor_pool as pool

BOARD_COUNT = 12
BOARD_BYTES = 20 * 1024 * 1024


def test_delayed_producer_cannot_replace_newer_observation_with_older_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    dirty = pool.DirtyBoards([board])
    first_waiting = threading.Event()
    second_finished = threading.Event()
    mutex = threading.Lock()
    calls = 0

    class ReorderedLock:
        def __enter__(self) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_waiting.set()
                assert second_finished.wait(3)
            mutex.acquire()

        def __exit__(self, *_args: object) -> None:
            mutex.release()
            second_finished.set()

    monkeypatch.setattr(dirty, "lock", ReorderedLock())
    clock = Mock(side_effect=[1.0, 2.0])
    monkeypatch.setattr(pool.time, "monotonic", clock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(dirty.mark, [board])
        assert first_waiting.wait(3)
        second = executor.submit(dirty.mark, [board])
        second.result(timeout=3)
        first.result(timeout=3)
    assert dirty.take()[pool.canonical(board)][1] == 2.0


def test_twenty_mib_feed_queries_reuse_metadata_without_content_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = large_boards(tmp_path, 1)[0]
    feed = activity_feed.ActivityFeed(path, path.read_bytes())
    cursor = f"{feed.epoch}:0"
    feed.append([{"at": datetime.now(UTC).isoformat(), "by": "bot"}], complete=True)
    content = Mock(side_effect=AssertionError("query rehashed board content"))
    monkeypatch.setattr(activity_feed, "bind_written", content)
    monkeypatch.setattr(activity_feed.hashlib, "file_digest", content)
    monkeypatch.setattr(Path, "read_bytes", content)
    monkeypatch.setattr(Path, "read_text", content)
    monkeypatch.setattr(activity_feed.board_state, "load", content)
    for _ in range(100):
        result = feed.query(activity_feed.MIN_TIME, None, cursor)
        assert result["coverage"] == {"complete": True, "code": "complete"}
        assert result["events"] == [{"at": feed.records[0].events[0]["at"], "by": "bot"}]
    content.assert_not_called()


@pytest.mark.parametrize("gap", ["restart", "replacement", "retention", "uncertain_commit"])
def test_twenty_mib_feed_never_proves_bot_only_coverage_across_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    path = large_boards(tmp_path, 1)[0]
    data = path.read_bytes()
    feed = activity_feed.ActivityFeed(path, data)
    cursor = f"{feed.epoch}:0"
    event: activity_feed.board_state.JsonObject = {"at": datetime.now(UTC).isoformat(), "by": "bot"}
    feed.append([event], complete=True)
    if gap == "restart":
        feed = activity_feed.ActivityFeed(path, data)
    elif gap == "replacement":
        before = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(data)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
    elif gap == "retention":
        monkeypatch.setattr(activity_feed, "MAX_RECORDS", 1)
        feed.append([event], complete=True)
    else:
        feed.append([event], complete=False)
    result = feed.query(activity_feed.MIN_TIME, None, cursor)
    assert result["coverage"] != {"complete": True, "code": "complete"}


class ReportedBackend:
    """Deterministic backend identity, distinct from the requested polling flag."""

    def __init__(self, description: str) -> None:
        self.description = description
        self.closed = False

    def __repr__(self) -> str:
        return self.description

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("allow_polling", [False, True])
@pytest.mark.parametrize(
    "description", ["RustNotify(Poll(PollWatcher))", "RustNotify(Unknown(FutureWatcher))"]
)
def test_native_mode_is_observed_and_fallback_requires_explicit_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: str, *, allow_polling: bool
) -> None:
    backend = ReportedBackend(description)
    factory = Mock(return_value=backend)
    monkeypatch.setattr(
        pool.importlib, "import_module", Mock(return_value=SimpleNamespace(RustNotify=factory))
    )
    if description.startswith("RustNotify(Poll(") and allow_polling:
        subscription = pool.NativeSubscription([tmp_path / "board.json"], allow_polling=True)
        assert subscription.mode == "polling"
        subscription.close()
    else:
        with pytest.raises(RuntimeError, match="native monitor backend"):
            pool.NativeSubscription([tmp_path / "board.json"], allow_polling=allow_polling)
    assert backend.closed


def make_entries(configs: list[watcher.Config], coordinator: pool.Coordinator) -> None:
    """Set up deterministic workers without claiming or launching real monitors."""
    clock = time.monotonic()
    for config in configs:
        runtime = watcher.MonitorRuntime(
            watcher.board_signature(config.board), "2026-11-07T10:01:00+00:00"
        )
        entry = pool.Entry(
            config, io.BytesIO(), "fake-transport", watcher.now(), runtime, str(uuid.uuid4())
        )
        entry.next_reconcile = clock + 60
        coordinator.entries[pool.canonical(config.board)] = entry


def single_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[pool.Coordinator, pool.Entry]:
    config = board_config(large_boards(tmp_path, 1)[0])
    coordinator = pool.Coordinator([config])
    make_entries([config], coordinator)
    entry = coordinator.entries[pool.canonical(config.board)]
    entry.runtime.last_file_identity = activity_feed.fingerprint(config.board)
    entry.runtime.last_observation_started_at = entry.runtime.last_signature_at
    entry.control_changed = False
    monkeypatch.setattr(pool.Entry, "status", Mock())
    monkeypatch.setattr(watcher, "stop_requested", Mock(return_value=False))
    monkeypatch.setattr(watcher, "reload_requested", Mock(return_value=False))
    monkeypatch.setattr(watcher, "drain_requested", Mock(return_value=False))
    monkeypatch.setattr(
        watcher.service_ownership,
        "probe_monitor_lifecycle",
        Mock(return_value=watcher.service_ownership.ServiceHealth(True, "complete", "healthy", 1)),
    )
    return coordinator, entry


def test_overdue_reconciliation_recovers_write_without_native_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, entry = single_worker(tmp_path, monkeypatch)
    entry.next_reconcile = 200.0
    clock = Mock(return_value=199.0)
    monkeypatch.setattr(pool.time, "monotonic", clock)
    query = Mock(return_value=[{"by": "terry"}])
    sink = Mock()
    monkeypatch.setattr(watcher, "read_activity", query)
    monkeypatch.setattr(watcher, "queue_alert", sink)
    entry.config.board.write_text("{}", encoding="utf-8")
    coordinator.cycle()
    assert entry.runtime.pending_alert is None
    cast_status = cast("Mock", pool.Entry.status)
    cast_status.assert_not_called()
    query.assert_not_called()
    sink.assert_not_called()
    clock.return_value = 200.0
    coordinator.cycle()
    assert entry.runtime.pending_alert is not None
    assert entry.runtime.pending_alert.last_write_monotonic == 200.0
    assert entry.next_reconcile > 200.0
    query.assert_not_called()
    clock.return_value = 216.0
    coordinator.cycle()
    query.assert_called_once()
    sink.assert_called_once()
    assert entry.runtime.pending_alert is None


def test_bot_query_suppression_cannot_consume_human_write_during_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, entry = single_worker(tmp_path, monkeypatch)
    lower = watcher.now()
    entry.runtime.last_signature_at = lower
    entry.runtime.last_observation_started_at = lower
    old_pending = watcher.PendingAlert(lower, lower, lower, time.monotonic() - 16, 1)
    entry.runtime.pending_alert = old_pending
    human_at: list[str] = []

    def concurrent_query(_config: watcher.Config, pending: watcher.PendingAlert) -> list[object]:
        if not human_at:
            assert pending == old_pending
            human_at.append(watcher.now())
            entry.config.board.write_text("{}", encoding="utf-8")
            return [{"by": "bot"}]
        assert datetime.fromisoformat(pending.activity_started_at) <= datetime.fromisoformat(
            human_at[0]
        )
        return [{"by": "terry"}]

    query = Mock(side_effect=concurrent_query)
    sink = Mock()
    monkeypatch.setattr(watcher, "read_activity", query)
    monkeypatch.setattr(watcher, "queue_alert", sink)
    coordinator.cycle()
    assert entry.runtime.pending_alert is None
    sink.assert_not_called()
    entry.next_reconcile = 0.0
    coordinator.cycle()
    assert entry.runtime.pending_alert is not None
    assert entry.runtime.pending_alert.activity_started_at == lower
    assert query.call_count == 1
    entry.runtime.pending_alert = watcher.PendingAlert(
        entry.runtime.pending_alert.activity_started_at,
        entry.runtime.pending_alert.first_observed_at,
        entry.runtime.pending_alert.last_observed_at,
        time.monotonic() - 16,
        1,
    )
    coordinator.cycle()
    assert query.call_count == 2
    sink.assert_called_once()
    assert entry.runtime.pending_alert is None


def test_real_native_intake_records_new_write_while_sink_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("watchfiles")
    coordinator, entry = single_worker(tmp_path, monkeypatch)
    lower = watcher.now()
    entry.runtime.pending_alert = watcher.PendingAlert(
        lower, lower, lower, time.monotonic() - 16, 1
    )
    monkeypatch.setattr(watcher, "read_activity", Mock(return_value=[{"by": "terry"}]))
    entered = threading.Event()
    release = threading.Event()

    def blocked_sink(*_args: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(watcher, "queue_alert", Mock(side_effect=blocked_sink))
    subscription = pool.NativeSubscription([entry.config.board])
    intake = threading.Thread(target=coordinator.intake, args=(subscription,))
    worker = threading.Thread(target=coordinator.cycle)
    try:
        assert subscription.mode == "native"
        intake.start()
        worker.start()
        assert entered.wait(3)
        replacement = entry.config.board.with_suffix(".replacement")
        replacement.write_text("{}", encoding="utf-8")
        os.replace(replacement, entry.config.board)
        deadline = time.monotonic() + 3
        key = pool.canonical(entry.config.board)
        while key not in coordinator.dirty.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert key in coordinator.dirty.pending
        assert worker.is_alive()
        release.set()
        worker.join(timeout=3)
        assert not worker.is_alive() and not coordinator.failed
        coordinator.cycle()
        assert entry.runtime.pending_alert is not None
        assert datetime.fromisoformat(
            entry.runtime.pending_alert.activity_started_at
        ) <= datetime.fromisoformat(lower)
    finally:
        release.set()
        worker.join(timeout=3)
        coordinator.stop.set()
        intake.join(timeout=3)
        subscription.close()
    assert not intake.is_alive() and coordinator.intake_error is None


def test_many_idle_boards_do_not_read_content_query_or_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = [board_config(board) for board in large_boards(tmp_path)]
    coordinator = pool.Coordinator(configs)
    make_entries(configs, coordinator)
    monkeypatch.setattr(watcher, "stop_requested", Mock(return_value=False))
    monkeypatch.setattr(watcher, "reload_requested", Mock(return_value=False))
    reads = Mock(side_effect=AssertionError("idle content read"))
    monkeypatch.setattr(Path, "read_bytes", reads)
    monkeypatch.setattr(Path, "read_text", reads)
    signatures = Mock(side_effect=AssertionError("idle signature read"))
    monkeypatch.setattr(watcher, "board_signature", signatures)
    query = Mock(side_effect=AssertionError("idle activity query"))
    monkeypatch.setattr(watcher, "read_activity", query)
    health = Mock(side_effect=AssertionError("idle lifecycle query"))
    monkeypatch.setattr(watcher.service_ownership, "probe_monitor_lifecycle", health)
    writes = Mock(side_effect=AssertionError("idle status write"))
    monkeypatch.setattr(watcher, "write_atomic_json", writes)
    for _ in range(100):
        coordinator.cycle()
    reads.assert_not_called()
    signatures.assert_not_called()
    query.assert_not_called()
    health.assert_not_called()
    writes.assert_not_called()


def test_stale_intake_timestamp_does_not_backdate_actual_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = board_config(large_boards(tmp_path, 1)[0])
    coordinator = pool.Coordinator([config])
    make_entries([config], coordinator)
    entry = coordinator.entries[pool.canonical(config.board)]
    monkeypatch.setattr(pool.Entry, "status", Mock())
    monkeypatch.setattr(watcher, "now", Mock(return_value="2026-11-07T11:00:00+00:00"))
    monkeypatch.setattr(pool.time, "monotonic", Mock(return_value=200.0))
    coordinator.observe(entry, ("2026-11-07T10:00:00+00:00", 100.0))
    assert entry.runtime.pending_alert is not None
    assert entry.runtime.pending_alert.last_observed_at == "2026-11-07T11:00:00+00:00"
    assert entry.runtime.pending_alert.last_write_monotonic == 200.0


def test_one_status_failure_does_not_discard_other_board_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = [board_config(board) for board in large_boards(tmp_path)]
    coordinator = pool.Coordinator(configs)
    make_entries(configs, coordinator)
    monkeypatch.setattr(watcher, "stop_requested", Mock(return_value=False))
    monkeypatch.setattr(watcher, "reload_requested", Mock(return_value=False))
    failed_key = pool.canonical(configs[0].board)
    failed_entry = coordinator.entries[failed_key]

    def status(entry: pool.Entry, _state: str, **_extra: object) -> None:
        if entry is failed_entry:
            raise OSError("synthetic status failure")

    monkeypatch.setattr(pool.Entry, "status", status)
    released = Mock()
    monkeypatch.setattr(coordinator, "release_entry", released)
    coordinator.dirty.mark([config.board for config in configs])
    coordinator.cycle()
    assert failed_key not in coordinator.entries
    released.assert_called_once_with(failed_entry)
    assert len(coordinator.entries) == BOARD_COUNT - 1
    assert all(entry.runtime.pending_alert is not None for entry in coordinator.entries.values())
    assert failed_entry.runtime.pending_alert is not None


def test_simultaneous_quiet_bursts_query_serially_and_keep_sink_time_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = [board_config(board) for board in large_boards(tmp_path)]
    coordinator = pool.Coordinator(configs)
    make_entries(configs, coordinator)
    monkeypatch.setattr(watcher, "stop_requested", Mock(return_value=False))
    monkeypatch.setattr(watcher, "reload_requested", Mock(return_value=False))
    monkeypatch.setattr(pool.Entry, "status", Mock())
    for entry in coordinator.entries.values():
        entry.runtime.pending_alert = watcher.PendingAlert(
            "2026-11-07T10:00:00+00:00",
            "2026-11-07T10:00:01+00:00",
            "2026-11-07T10:01:00+00:00",
            time.monotonic() - 16,
            100,
        )
    query = Mock(return_value=[{"by": "terry"}])
    monkeypatch.setattr(watcher, "read_activity", query)
    active = peak = 0

    def sink(config: watcher.Config, *_args: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        # Intake continues independently while a slow sink is busy.
        config.board.write_text("{}", encoding="utf-8")
        for _ in range(100):
            coordinator.dirty.mark([config.board])
        active -= 1

    delivered = Mock(side_effect=sink)
    monkeypatch.setattr(watcher, "queue_alert", delivered)
    coordinator.cycle()
    assert query.call_count == delivered.call_count == BOARD_COUNT
    assert peak == 1
    assert all(entry.runtime.pending_alert is None for entry in coordinator.entries.values())
    # The in-flight bursts remain bounded and are observed during the next cycle.
    assert len(coordinator.dirty.pending) == BOARD_COUNT
    coordinator.cycle()
    assert all(entry.runtime.pending_alert is not None for entry in coordinator.entries.values())
    assert query.call_count == BOARD_COUNT


def large_boards(tmp_path: Path, count: int = BOARD_COUNT) -> list[Path]:
    """Create valid JSON at the qualification size without a private board fixture."""
    padding = b" " * (BOARD_BYTES - 2)
    boards = [tmp_path / f"board-{index}.json" for index in range(count)]
    for board in boards:
        board.write_bytes(b"{}" + padding)
        assert board.stat().st_size == BOARD_BYTES
    return boards


def board_config(board: Path) -> watcher.Config:
    """Allocate disjoint runtime paths and a synthetic saved target per board."""
    return watcher.Config(
        board,
        "synthetic",
        str(uuid.uuid4()),
        8795,
        board.with_suffix(".status"),
        board.with_suffix(".target"),
        board.with_suffix(".lock"),
        15.0,
    )


def test_native_subscription_shares_parent_and_detects_atomic_replacements(tmp_path: Path) -> None:
    """Real native events cover replacements of all 20 MiB files using one parent."""
    pytest.importorskip("watchfiles")
    boards = large_boards(tmp_path)
    subscription = pool.NativeSubscription(boards)
    try:
        assert subscription.mode == "native"
        assert subscription.directories == (pool.canonical(tmp_path),)
        replacement_bytes = b"{}" + b" " * (BOARD_BYTES - 2)
        for board in boards:
            replacement = board.with_suffix(".replacement")
            replacement.write_bytes(replacement_bytes)
            os.replace(replacement, board)
        observed: set[Path] = set()
        deadline = time.monotonic() + 8
        expected = {pool.canonical(board) for board in boards}
        while observed != expected and time.monotonic() < deadline:
            observed.update(subscription.poll())
        assert observed == expected
    finally:
        subscription.close()


def test_dirty_board_bursts_are_bounded_and_do_not_read_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boards = large_boards(tmp_path)
    dirty = pool.DirtyBoards(boards)
    read = Mock(side_effect=AssertionError("board content read"))
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "read_text", read)

    def mark_burst(_index: int) -> None:
        dirty.mark(boards)

    # Concurrent notification batches emulate a simultaneous many-board burst.
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(mark_burst, range(100)))
    result = dirty.take()
    assert set(result) == {pool.canonical(board) for board in boards}
    assert len(result) == BOARD_COUNT
    assert all(
        isinstance(stamp, str) and isinstance(clock, float) for stamp, clock in result.values()
    )
    assert dirty.take() == {}
    read.assert_not_called()


@pytest.mark.parametrize("with_pending", [False, True])
def test_large_board_restart_preserves_saved_target_and_gap_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_pending: bool
) -> None:
    board = large_boards(tmp_path, 1)[0]
    config = board_config(board)
    start = "2026-11-07T10:00:00.000000+00:00"
    end = "2026-11-07T10:01:00.000000+00:00"
    watcher.write_atomic_json(config.target_path, {"threadId": config.thread_id})
    watcher.write_atomic_json(
        config.status_path,
        {
            "schema": 1,
            "state": "failed",
            "board": str(board),
            "lastSignature": [0, 0],
            "lastSignatureAt": end,
            "lastAlertAt": None,
            "pendingWriteCount": 3 if with_pending else 0,
            "pendingActivityStartedAt": start if with_pending else None,
            "pendingFirstObservedAt": start if with_pending else None,
            "pendingLastObservedAt": end if with_pending else None,
        },
    )
    target_before = config.target_path.read_bytes()
    original_read = Path.read_bytes

    def forbid_board_read(path: Path) -> bytes:
        assert path != board
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_board_read)
    runtime = watcher.resume_runtime(config, {}, watcher.now())
    assert runtime.pending_alert is not None
    assert runtime.pending_alert.activity_started_at == (start if with_pending else end)
    assert runtime.pending_alert.write_count == (4 if with_pending else 1)
    assert runtime.pending_alert.last_observed_at == runtime.last_signature_at
    assert config.target_path.read_bytes() == target_before


@pytest.mark.parametrize("events", [None, [], [{}], [{"by": "terry"}], [{"by": "bot"}]])
def test_large_board_recovered_alert_retains_fail_open_actor_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[object] | None
) -> None:
    config = board_config(large_boards(tmp_path, 1)[0])
    pending = watcher.PendingAlert(
        "2026-11-07T10:00:00+00:00",
        "2026-11-07T10:00:01+00:00",
        "2026-11-07T10:01:00+00:00",
        1.0,
        100,
    )
    monkeypatch.setattr(watcher, "read_activity", Mock(return_value=events))
    sink = Mock()
    monkeypatch.setattr(watcher, "queue_alert", sink)
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("board read")))
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("board read")))
    watcher.flush_pending_alert(config, "fake-transport", pending)
    assert sink.call_count == (0 if events == [{"by": "bot"}] else 1)
    if sink.called:
        assert sink.call_args.args[0].thread_id == config.thread_id
        assert sink.call_args.args[2] == pending
