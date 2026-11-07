"""Synthetic run-loop lifecycle checks; never contact production services or chats."""

import os
import threading
import uuid
from pathlib import Path
from typing import IO, cast
from unittest.mock import Mock

import pytest

from localswim import monitor as watcher
from localswim import monitor_pool as pool


@pytest.fixture
def setup_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[pool.Coordinator, Mock]:
    configs: list[watcher.Config] = []
    for index in range(2):
        board = tmp_path / f"board{index}.json"
        board.write_text("{}", encoding="utf-8")
        configs.append(
            watcher.Config(
                board,
                f"board{index}",
                str(uuid.uuid4()),
                1234,
                tmp_path / f"status{index}.json",
                tmp_path / f"target{index}.json",
                tmp_path / f"lock{index}",
                15.0,
            )
        )
    closed = Mock()

    class Subscription:
        mode = "native"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.wakeup = threading.Event()

        def poll(self) -> set[Path]:
            self.wakeup.wait(0.005)
            return set()

        def close(self) -> None:
            closed()

    monkeypatch.setattr(pool, "NativeSubscription", Subscription)
    monkeypatch.setattr(watcher.shutil, "which", Mock(return_value="fake-codex"))
    monkeypatch.setattr(
        watcher.service_ownership,
        "probe_monitor_lifecycle",
        Mock(return_value=watcher.service_ownership.ServiceHealth(True, "complete", "healthy", 0)),
    )
    return pool.Coordinator(configs), closed


def assert_released(configs: tuple[watcher.Config, ...]) -> None:
    for config in configs:
        lock = watcher.acquire_lock(config.lock_path)
        assert lock is not None
        watcher.release_lock(lock)


def test_startup_claim_failure_releases_already_claimed_member(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, closed = setup_pool
    claim = watcher.claim_monitor
    count = 0

    def claims(config: watcher.Config) -> object:
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic claim refusal")
        return claim(config)

    monkeypatch.setattr(watcher, "claim_monitor", claims)
    with pytest.raises(RuntimeError, match="claim refusal"):
        coordinator.run()
    closed.assert_called_once()
    assert_released(coordinator.configs)
    assert watcher.read_json(coordinator.configs[0].status_path)["state"] == "failed"


@pytest.mark.parametrize("action", ["reload", "stop", "drain"])
def test_member_control_retires_only_exact_generation(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    coordinator, closed = setup_pool
    original = coordinator.cycle
    seen: list[str] = []

    def cycle() -> None:
        first, second = list(coordinator.entries.values())
        target = watcher.read_json(first.config.target_path)
        prefix = {"reload": "reload", "stop": "stop", "drain": "drain"}[action]
        target.update({f"{prefix}Pid": os.getpid(), f"{prefix}OwnerId": "wrong-generation"})
        watcher.write_atomic_json(first.config.target_path, target)
        original()
        assert len(coordinator.entries) == 2
        target[f"{prefix}OwnerId"] = first.owner_id
        watcher.write_atomic_json(first.config.target_path, target)
        first.control_changed = True
        if action == "stop":
            first.runtime.last_health = watcher.service_ownership.ServiceHealth(
                False, "response_headers", "response_headers_failed", 0
            )
        original()
        assert list(coordinator.entries.values()) == [second]
        seen.append(action)
        coordinator.request_stop()

    monkeypatch.setattr(coordinator, "cycle", cycle)
    assert coordinator.run() == 0
    assert seen == [action]
    closed.assert_called_once()
    assert_released(coordinator.configs)


@pytest.mark.parametrize("failed_first_sink", [False, True])
def test_pool_stop_drains_every_member_and_observes_writes_during_sink(
    setup_pool: tuple[pool.Coordinator, Mock],
    monkeypatch: pytest.MonkeyPatch,
    failed_first_sink: bool,
) -> None:
    coordinator, closed = setup_pool
    starts: list[str] = []
    delivered: list[tuple[Path, watcher.PendingAlert]] = []

    def cycle() -> None:
        for entry in coordinator.entries.values():
            start = entry.runtime.last_signature_at
            starts.append(start)
            entry.runtime.pending_alert = watcher.PendingAlert(start, start, start, 0.0, 1)
            entry.status("running")
        coordinator.request_stop()

    def flush(config: watcher.Config, _codex: str, pending: watcher.PendingAlert) -> str:
        delivered.append((config.board, pending))
        if config == coordinator.configs[0]:
            second = coordinator.configs[1]
            second.board.write_text('{"changed":true}', encoding="utf-8")
            coordinator.dirty.mark([second.board])
            if failed_first_sink:
                raise RuntimeError("synthetic sink refusal")
        return watcher.now()

    monkeypatch.setattr(coordinator, "cycle", cycle)
    monkeypatch.setattr(watcher, "flush_pending_alert", flush)
    assert coordinator.run() == int(failed_first_sink)
    assert len(delivered) == 2
    assert delivered[1][1].activity_started_at == starts[1]
    assert delivered[1][1].write_count == 2
    for index, config in enumerate(coordinator.configs):
        status = watcher.read_json(config.status_path)
        assert status["pendingWriteCount"] == (1 if index == 0 and failed_first_sink else 0)
        assert status["state"] == ("failed" if index == 0 and failed_first_sink else "stopped")
    closed.assert_called_once()
    assert_released(coordinator.configs)


def test_explicit_manifest_stop_preserves_targets_and_matches_each_owner(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _closed = setup_pool

    def cycle() -> None:
        assert pool.request_pool_stop(coordinator.configs) == 0
        for entry in coordinator.entries.values():
            target = watcher.read_json(entry.config.target_path)
            assert target["threadId"] == entry.config.thread_id
            assert target["drainOwnerId"] == entry.owner_id
            assert target["drainPid"] == os.getpid()
        coordinator.request_stop()

    monkeypatch.setattr(coordinator, "cycle", cycle)
    assert coordinator.run() == 0


@pytest.mark.parametrize("status_failure", [False, True])
def test_cleanup_close_failure_does_not_skip_other_members(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch, status_failure: bool
) -> None:
    coordinator, _closed = setup_pool
    original = coordinator.cycle
    progress: list[Path] = []

    class FailingClose:
        def __init__(self, wrapped: IO[bytes]) -> None:
            self.wrapped = wrapped

        def close(self) -> None:
            self.wrapped.close()
            raise OSError("synthetic close failure")

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

    def cycle() -> None:
        first, second = list(coordinator.entries.values())
        first.lock = cast("IO[bytes]", FailingClose(first.lock))
        if status_failure:
            old_status = pool.Entry.status

            def status(entry: pool.Entry, state: str, **extra: object) -> None:
                if entry is first:
                    raise OSError("synthetic status failure")
                old_status(entry, state, **extra)

            monkeypatch.setattr(pool.Entry, "status", status)
            first.runtime.last_signature = (0, 0)
            original()
            assert list(coordinator.entries.values()) == [second]
        progress.append(second.config.board)
        coordinator.request_stop()

    monkeypatch.setattr(coordinator, "cycle", cycle)
    assert coordinator.run() == 1
    assert progress == [coordinator.configs[1].board]
    assert_released(coordinator.configs)


def test_final_drain_retains_same_or_earlier_member_write(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _closed = setup_pool

    def cycle() -> None:
        for entry in coordinator.entries.values():
            start = entry.runtime.last_signature_at
            entry.runtime.pending_alert = watcher.PendingAlert(start, start, start, 0.0, 1)
            entry.status("running")
        coordinator.request_stop()

    def flush(config: watcher.Config, _codex: str, _pending: watcher.PendingAlert) -> str:
        # The later sink changes both itself and the already-drained first member.
        if config == coordinator.configs[1]:
            for member in coordinator.configs:
                member.board.write_text('{"duringSink":true}', encoding="utf-8")
        return watcher.now()

    monkeypatch.setattr(coordinator, "cycle", cycle)
    monkeypatch.setattr(watcher, "flush_pending_alert", flush)
    assert coordinator.run() == 0
    for config in coordinator.configs:
        status = watcher.read_json(config.status_path)
        assert status["state"] == "stopped"
        assert status["pendingWriteCount"] == 1
        assert status["pendingLastObservedAt"] == status["lastSignatureAt"]
    assert_released(coordinator.configs)


def test_fresh_start_status_resumes_without_invalid_boundary(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _closed = setup_pool
    monkeypatch.setattr(coordinator, "cycle", coordinator.request_stop)
    assert coordinator.run() == 0
    for config in coordinator.configs:
        saved = watcher.orphaned_status(config)
        assert saved["lastObservationStartedAt"] <= saved["lastSignatureAt"]
        resumed = watcher.resume_runtime(config, {}, watcher.now())
        assert resumed.pending_alert is None


def test_same_signature_atomic_replacement_restores_identity_gap(
    setup_pool: tuple[pool.Coordinator, Mock],
) -> None:
    coordinator, _closed = setup_pool
    config = coordinator.configs[0]
    runtime = watcher.resume_runtime(config, {}, watcher.now())
    watcher.publish_runtime_status(config, runtime, watcher.now(), str(uuid.uuid4()), "stopped")
    stat = config.board.stat()
    replacement = config.board.with_suffix(".replacement")
    replacement.write_bytes(config.board.read_bytes())
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(config.board)
    assert watcher.board_signature(config.board) == runtime.last_signature
    resumed = watcher.resume_runtime(config, {}, watcher.now())
    assert resumed.pending_alert is not None
    assert resumed.pending_alert.activity_started_at == runtime.last_observation_started_at


@pytest.mark.parametrize(
    "field,value",
    [
        ("lastFileIdentity", None),
        ("lastFileIdentity", [1, 2]),
        ("lastFileIdentity", [1, 2, 3, 4, True]),
        ("lastObservationStartedAt", "2099-01-01T00:00:00+00:00"),
    ],
)
def test_uncertain_saved_identity_or_boundary_is_preserved(
    setup_pool: tuple[pool.Coordinator, Mock], field: str, value: object
) -> None:
    coordinator, _closed = setup_pool
    config = coordinator.configs[0]
    runtime = watcher.resume_runtime(config, {}, watcher.now())
    watcher.publish_runtime_status(config, runtime, watcher.now(), str(uuid.uuid4()), "stopped")
    saved = watcher.read_json(config.status_path)
    saved[field] = value
    watcher.write_atomic_json(config.status_path, saved)
    original = config.status_path.read_bytes()
    with pytest.raises(RuntimeError, match="uncertain"):
        watcher.resume_runtime(config, {}, watcher.now())
    assert config.status_path.read_bytes() == original


def test_observation_after_stat_commit_remains_inside_next_window(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _closed = setup_pool
    config = coordinator.configs[0]
    runtime = watcher.resume_runtime(config, {}, watcher.now())
    lock = watcher.acquire_lock(config.lock_path)
    assert lock is not None
    entry = pool.Entry(config, lock, "fake", watcher.now(), runtime, str(uuid.uuid4()))
    fingerprint = watcher.activity_feed.fingerprint
    boundary = "2026-11-07T12:00:00+00:00"
    end = "2026-11-07T12:00:02+00:00"
    later = "2026-11-07T12:00:03+00:00"
    runtime.last_signature_at = "2026-11-07T11:59:00+00:00"
    runtime.last_observation_started_at = runtime.last_signature_at

    def racing_identity(path: Path) -> tuple[int, ...]:
        old = fingerprint(path)
        path.write_text('{"human":true}', encoding="utf-8")
        return old

    monkeypatch.setattr(watcher.activity_feed, "fingerprint", racing_identity)
    monkeypatch.setattr(watcher, "now", Mock(side_effect=[boundary, end]))
    try:
        coordinator.observe(entry, (boundary, 0.0))
        assert runtime.last_observation_started_at == boundary
        assert runtime.last_signature_at == end
        runtime.pending_alert = None  # Previous interval has been acknowledged.
        monkeypatch.setattr(watcher.activity_feed, "fingerprint", fingerprint)
        monkeypatch.setattr(watcher, "now", Mock(return_value=later))
        coordinator.observe(entry)
        assert runtime.pending_alert is not None
        assert runtime.pending_alert.activity_started_at == boundary
        assert runtime.pending_alert.last_observed_at == later
    finally:
        watcher.release_lock(lock)


def test_initialization_unlock_failure_still_closes_current_and_prior_members(
    setup_pool: tuple[pool.Coordinator, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, closed = setup_pool
    resume = watcher.resume_runtime
    release = watcher.release_lock

    def resume_one(
        config: watcher.Config, previous: dict[str, object], started: str
    ) -> watcher.MonitorRuntime:
        if config == coordinator.configs[1]:
            raise RuntimeError("synthetic initialization refusal")
        return resume(config, previous, started)

    def release_one(handle: IO[bytes]) -> None:
        if str(handle.name) == str(coordinator.configs[1].lock_path):
            raise OSError("synthetic unlock refusal")
        release(handle)

    monkeypatch.setattr(watcher, "resume_runtime", resume_one)
    monkeypatch.setattr(watcher, "release_lock", release_one)
    with pytest.raises(OSError, match="unlock refusal"):
        coordinator.run()
    monkeypatch.setattr(watcher, "release_lock", release)
    assert_released(coordinator.configs)
    closed.assert_called_once()
