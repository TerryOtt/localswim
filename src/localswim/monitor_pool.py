"""Bounded shared native subscriptions and serial per-board monitoring."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import monitor as legacy

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import IO

RECONCILE_SECONDS = 30.0
CONTROL_SECONDS = 0.25
NATIVE_BACKENDS = (
    "ReadDirectoryChangesWatcher",
    "INotifyWatcher",
    "FsEventWatcher",
    "KqueueWatcher",
)


def canonical(path: Path) -> Path:
    """Use canonical platform path identities without reading board content."""
    return Path(os.path.normcase(str(path.resolve())))


class NativeSubscription:
    """One notify object watches each distinct parent exactly once, nonrecursively.

    watchfiles 1.3.0 RustNotify's repr exposes Recommended versus Poll and the
    concrete recommended watcher. Unknown representations fail closed.
    """

    def __init__(self, boards: Sequence[Path], *, allow_polling: bool = False) -> None:
        self.boards = frozenset(canonical(path) for path in boards)
        self.directories = tuple(sorted({path.parent for path in self.boards}))
        self.stop = threading.Event()
        try:
            backend = importlib.import_module("watchfiles._rust_notify")
        except ImportError as exc:
            raise RuntimeError("install localswim[monitor] to use native monitoring") from exc
        self.watcher = backend.RustNotify(
            [str(path) for path in self.directories], False, False, 30000, False, False
        )
        description = repr(self.watcher)
        if description.startswith("RustNotify(Recommended(") and any(
            name in description for name in NATIVE_BACKENDS
        ):
            self.mode = "native"
        elif description.startswith("RustNotify(Poll(") and allow_polling:
            self.mode = "polling"
        else:
            self.watcher.close()
            raise RuntimeError(
                "native monitor backend unavailable or unrecognized; polling requires explicit opt-in"
            )

    def poll(self) -> set[Path]:
        """Return only registered board paths, waiting at most 250ms when idle."""
        changes = self.watcher.watch(100, 20, 250, self.stop)
        if isinstance(changes, str):
            return set()
        return {
            path
            for _kind, raw_path in changes
            if (path := canonical(Path(raw_path))) in self.boards
        }

    def close(self) -> None:
        """Stop after the intake caller exits its bounded poll."""
        self.stop.set()
        self.watcher.close()


class DirtyBoards:
    """Coalesce observations into one latest stamp per registered board."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self.paths = frozenset(canonical(path) for path in paths)
        self.pending: dict[Path, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def mark(self, paths: Sequence[Path] | set[Path]) -> None:
        with self.lock:
            stamp = legacy.now(), time.monotonic()
            for path in paths:
                key = canonical(path)
                if key in self.paths:
                    self.pending[key] = stamp

    def take(self) -> dict[Path, tuple[str, float]]:
        with self.lock:
            pending, self.pending = self.pending, {}
        return pending


@dataclass
class Entry:
    """One board's original ownership and recovery state."""

    config: legacy.Config
    lock: IO[bytes]
    codex: str
    started_at: str
    runtime: legacy.MonitorRuntime
    owner_id: str
    next_reconcile: float = 0.0

    def status(self, state: str, **extra: object) -> None:
        legacy.publish_runtime_status(
            self.config,
            self.runtime,
            self.started_at,
            self.owner_id,
            state,
            backendMode=self.backend_mode,
            coordinatorPid=os.getpid(),
            **extra,
        )

    backend_mode: str = "native"
    control_changed: bool = True
    reload_pending: bool = False
    stop_pending: bool = False
    drain_pending: bool = False
    drain_failed: bool = False


def validate_configs(configs: Sequence[legacy.Config]) -> None:
    """Reject all cross-board aliases before any target or ownership mutation."""
    if not 1 <= len(configs) <= 256:
        raise ValueError("between 1 and 256 monitor boards are required")
    paths: set[Path] = set()
    file_ids: set[tuple[int, int]] = set()
    for config in configs:
        group = [config.board, config.status_path, config.target_path, config.lock_path]
        group.append(legacy.request_lock_path(config.target_path, group))
        for raw_path in group:
            path = canonical(raw_path)
            if path in paths:
                raise ValueError("pooled board and runtime paths must all be distinct")
            paths.add(path)
            if path.exists():
                identity = path.stat()
                key = identity.st_dev, identity.st_ino
                if key in file_ids:
                    raise ValueError("pooled paths must not be hardlink aliases")
                file_ids.add(key)


class Coordinator:
    """Run one serial worker with independently coalesced board work.

    Native intake never parses boards or queues alerts. Its one slot per board
    retains activity arriving during a slow query or sink. No worker backlog can
    grow with write count. Lifecycle and repair stats run every thirty seconds.
    """

    def __init__(self, configs: Sequence[legacy.Config], *, allow_polling: bool = False) -> None:
        validate_configs(configs)
        self.configs = tuple(configs)
        self.allow_polling = allow_polling
        self.dirty = DirtyBoards(
            [path for config in configs for path in (config.board, config.target_path)]
        )
        self.entries: dict[Path, Entry] = {}
        self.control_keys = {
            canonical(config.board): canonical(config.target_path) for config in configs
        }
        self.failed = False
        self.draining: list[Entry] = []
        self.stop = threading.Event()
        self.requested_stop = threading.Event()
        self.intake_error: BaseException | None = None

    def intake(self, subscription: NativeSubscription) -> None:
        try:
            while not self.stop.is_set():
                self.dirty.mark(subscription.poll())
        except Exception as exc:  # noqa: BLE001 -- transport thread reports sanitized failure
            self.intake_error = exc
            self.stop.set()

    def observe(self, entry: Entry, stamp: tuple[str, float] | None = None) -> None:
        runtime = entry.runtime
        next_cursor = legacy.now()
        signature = legacy.board_signature(entry.config.board)
        identity = legacy.activity_feed.fingerprint(entry.config.board)
        if (
            signature == runtime.last_signature
            and identity == runtime.last_file_identity
            and stamp is None
        ):
            return
        # Bind the cursor to the actual stat, never an older intake notification.
        observed_at, observed_clock = legacy.now(), time.monotonic()
        runtime.pending_alert = legacy.note_board_write(
            runtime.pending_alert,
            activity_started_at=runtime.last_observation_started_at or runtime.last_signature_at,
            observed_at=observed_at,
            observed_monotonic=observed_clock,
        )
        runtime.last_signature = signature
        runtime.last_file_identity = identity
        runtime.last_signature_at = observed_at
        runtime.last_observation_started_at = next_cursor
        entry.status("running")

    def cycle(self) -> None:
        dirty = self.dirty.take()
        for path, entry in list(self.entries.items()):
            try:
                self.cycle_entry(path, entry, dirty)
            except Exception:  # noqa: BLE001 -- isolate one member's service or delivery failure
                try:
                    with contextlib.suppress(OSError):
                        entry.status(
                            "failed", detail="board monitor stopped; pending interval preserved"
                        )
                finally:
                    self.entries.pop(path, None)
                    self.release_entry(entry)
                    self.failed = True
        for entry in self.draining:
            self.finish_drain(entry)
            self.release_entry(entry)
        self.draining.clear()

    def cycle_entry(self, path: Path, entry: Entry, dirty: dict[Path, tuple[str, float]]) -> None:
        config, runtime = entry.config, entry.runtime
        clock = time.monotonic()
        if path in dirty:
            self.observe(entry, dirty[path])
        reconcile = clock >= entry.next_reconcile
        if reconcile:
            self.observe(entry)
            entry.next_reconcile = clock + RECONCILE_SECONDS
        if self.control_keys[path] in dirty or reconcile or entry.control_changed:
            entry.stop_pending = legacy.stop_requested(
                config.target_path, os.getpid(), entry.owner_id
            )
            entry.reload_pending = legacy.reload_requested(
                config.target_path, os.getpid(), entry.owner_id
            )
            entry.drain_pending = legacy.drain_requested(
                config.target_path, os.getpid(), entry.owner_id
            )
            entry.control_changed = False
        if entry.drain_pending:
            self.drain_entry(entry)
            self.entries.pop(path, None)
            self.draining.append(entry)
            return
        if entry.stop_pending:
            if reconcile:
                runtime.last_health = legacy.service_ownership.probe_monitor_lifecycle(config.board)
                runtime.health_checked_at = legacy.now()
            if runtime.last_health is not None and not runtime.last_health.healthy:
                entry.status("stopped")
                self.release_entry(entry)
                del self.entries[path]
                return
            if not runtime.stopping:
                runtime.stopping = True
                entry.status("stopping")
            return
        runtime.stopping = False
        if reconcile:
            legacy.handle_idle_cycle(config, entry.codex, runtime, entry.status)
        elif runtime.pending_alert is not None and legacy.quiet_window_elapsed(
            runtime.pending_alert, current_monotonic=clock, quiet_seconds=config.quiet_seconds
        ):
            alerted = legacy.flush_pending_alert(config, entry.codex, runtime.pending_alert)
            if alerted is not None:
                runtime.last_alert_at = alerted
            runtime.pending_alert = None
            entry.status("running")
        if entry.reload_pending and runtime.pending_alert is None:
            # Stat once at the actual handoff boundary, not on every idle tick.
            self.observe(entry)
            if runtime.pending_alert is None:
                entry.status("reload-ready")
                self.release_entry(entry)
                del self.entries[path]

    def request_stop(self) -> None:
        """Request an all-member cooperative drain at the next worker boundary."""
        self.requested_stop.set()

    def drain_entry(self, entry: Entry) -> None:
        """Observe the shutdown boundary and deliver or retain its complete interval."""
        try:
            self.observe(entry)
            runtime = entry.runtime
            if runtime.pending_alert is not None:
                alerted = legacy.flush_pending_alert(
                    entry.config, entry.codex, runtime.pending_alert
                )
                if alerted is not None:
                    runtime.last_alert_at = alerted
                runtime.pending_alert = None
            entry.status("stopped")
        except Exception:  # noqa: BLE001 -- every other member must still drain
            self.failed = True
            entry.drain_failed = True
            with contextlib.suppress(OSError):
                entry.status(
                    "failed", detail="cooperative drain failed; pending interval preserved"
                )

    def finish_drain(self, entry: Entry) -> None:
        """Retain writes arriving during any sink in a bounded final observation pass."""
        try:
            self.observe(entry)
            entry.status("failed" if entry.drain_failed else "stopped")
        except Exception:  # noqa: BLE001 -- continue every other final observation
            self.failed = True
            with contextlib.suppress(OSError):
                entry.status(
                    "failed", detail="final observation failed; recovery metadata retained"
                )

    def release_entry(self, entry: Entry) -> None:
        """Cleanup failure cannot prevent other members releasing ownership."""
        try:
            legacy.release_lock(entry.lock)
        except Exception:  # noqa: BLE001 -- cleanup remains isolated per member
            self.failed = True
        finally:
            try:
                entry.lock.close()
            except Exception:  # noqa: BLE001 -- continue releasing every other member
                self.failed = True

    def run(self) -> int:
        for config in self.configs:
            config.target_path.parent.mkdir(parents=True, exist_ok=True)
        subscription = NativeSubscription(
            [path for config in self.configs for path in (config.board, config.target_path)],
            allow_polling=self.allow_polling,
        )
        thread = threading.Thread(
            target=self.intake, args=(subscription,), name="localswim-native-intake"
        )
        try:
            thread.start()
            for config in self.configs:
                lock, previous = legacy.claim_monitor(config)
                if lock is None:
                    continue
                codex, started, runtime = legacy.prepare_runtime(config, previous, lock)
                entry = Entry(
                    config,
                    lock,
                    codex,
                    started,
                    runtime,
                    str(uuid.uuid4()),
                    backend_mode=subscription.mode,
                )
                self.entries[canonical(config.board)] = entry
                entry.status("running")
            while self.entries and not self.stop.is_set() and not self.requested_stop.is_set():
                self.cycle()
                self.requested_stop.wait(CONTROL_SECONDS)
            if self.requested_stop.is_set():
                self.stop.set()
                thread.join()
                # Reconcile every board, including dirty events received during a sink call.
                for entry in self.entries.values():
                    self.drain_entry(entry)
                for entry in self.entries.values():
                    self.finish_drain(entry)
            if self.intake_error is not None:
                raise RuntimeError("native monitor subscription failed") from self.intake_error
        except BaseException:
            for entry in self.entries.values():
                with contextlib.suppress(OSError):
                    entry.status(
                        "failed", detail="pooled monitor stopped; pending interval preserved"
                    )
            raise
        finally:
            self.stop.set()
            if thread.is_alive():
                thread.join()
            try:
                subscription.close()
            finally:
                for entry in [*self.entries.values(), *self.draining]:
                    self.release_entry(entry)
                self.entries.clear()
                self.draining.clear()
        return 1 if self.failed else 0


def run_pool(configs: Sequence[legacy.Config], *, allow_polling: bool = False) -> int:
    """Run only explicitly registered boards; no discovery or launch side effects."""
    return Coordinator(configs, allow_polling=allow_polling).run()


def request_pool_stop(configs: Sequence[legacy.Config]) -> int:
    """Request exact-owner drains without retargeting, starting or force-killing."""
    validate_configs(configs)
    for config in configs:
        with legacy.target_transaction(config):
            owner = legacy.published_owner(config)
            if not owner or not isinstance(owner.get("ownerId"), str):
                continue
            target = legacy.read_json(config.target_path)
            if target.get("schema") != 1:
                raise ValueError("invalid monitor target; stop request refused")
            target.update({"drainPid": owner["pid"], "drainOwnerId": owner["ownerId"]})
            legacy.write_atomic_json(config.target_path, target)
    return 0


def _manifest_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("monitor manifest has duplicate fields")
        value[key] = item
    return value


def _manifest_member(arguments: list[str]) -> legacy.Config:
    try:
        return legacy.parse_args(arguments)
    except SystemExit as error:
        raise ValueError("manifest member arguments were not accepted") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Read an explicit bounded manifest of existing single-board argument lists."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--allow-polling", action="store_true")
    parser.add_argument(
        "--stop", action="store_true", help="cooperatively drain only manifest members"
    )
    args = parser.parse_args(argv)
    try:
        with args.manifest.open("rb") as source:
            encoded = source.read(1024 * 1024 + 1)
        if len(encoded) > 1024 * 1024:
            raise ValueError("monitor manifest exceeds 1 MiB")
        value = cast("object", json.loads(encoded, object_pairs_hook=_manifest_object))
        if (
            not isinstance(value, dict)
            or set(cast("dict[str, object]", value)) != {"schema", "boards"}
            or type(cast("dict[str, object]", value).get("schema")) is not int
            or cast("dict[str, object]", value)["schema"] != 1
        ):
            raise ValueError("monitor manifest must use schema 1")
        boards = cast("dict[str, object]", value).get("boards")
        if not isinstance(boards, list) or not 1 <= len(cast("list[object]", boards)) <= 256:
            raise ValueError("monitor manifest needs between 1 and 256 boards")
        configs: list[legacy.Config] = []
        for board in cast("list[object]", boards):
            if not isinstance(board, list) or not all(
                isinstance(arg, str) for arg in cast("list[object]", board)
            ):
                raise ValueError("manifest boards must be single-board argument arrays")
            if any(arg in {"--help", "-h"} for arg in cast("list[str]", board)):
                raise ValueError(
                    "manifest members must contain monitor arguments, not help requests"
                )
            configs.append(_manifest_member(cast("list[str]", board)))
        validate_configs(configs)
        if args.stop:
            return request_pool_stop(configs)
        legacy.service_output.configure()
        return run_pool(configs, allow_polling=args.allow_polling)
    except Exception as exc:  # noqa: BLE001 -- concise process boundary
        print(f"localswim monitor pool failed: {exc}", file=sys.stderr, flush=True)
        return 1
