"""Queue one next-turn Codex alert after a burst with uncertain or human activity.

The optional MIT-licensed ``watchfiles`` package supplies filesystem notifications.
This command never reads or logs board content. It records only file signatures
and timestamps in explicitly configured runtime paths, coalesces related writes across a
quiet window, then queues a bounded ``localswim-cli`` activity query for the target
thread's next turn.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import IO, Any


MAX_PORT = 65535
MAX_SERVICE_MISSES = 5
DEFAULT_QUIET_SECONDS = 15.0
MAX_QUIET_SECONDS = 300.0
ACTOR_FILTER_VERSION = 2


@dataclass(frozen=True)
class Config:
    """Validated monitor inputs."""

    board: Path
    board_label: str
    thread_id: str
    port: int
    status_path: Path
    target_path: Path
    lock_path: Path
    quiet_seconds: float
    automation_actor: str = "bot"
    reload: bool = False


@dataclass(frozen=True)
class PendingAlert:
    """Sanitized metadata for board writes waiting on the quiet window."""

    activity_started_at: str
    first_observed_at: str
    last_observed_at: str
    last_write_monotonic: float
    write_count: int


@dataclass
class MonitorRuntime:
    """Mutable sanitized state for one running monitor."""

    last_signature: tuple[int, int]
    last_signature_at: str
    last_alert_at: str | None = None
    pending_alert: PendingAlert | None = None
    service_misses: int = 0
    stopping: bool = False


def note_board_write(
    pending: PendingAlert | None,
    *,
    activity_started_at: str,
    observed_at: str,
    observed_monotonic: float,
) -> PendingAlert:
    """Start or extend one trailing-edge debounce and activity-query window."""

    if pending is None:
        return PendingAlert(
            activity_started_at,
            observed_at,
            observed_at,
            observed_monotonic,
            1,
        )
    return PendingAlert(
        pending.activity_started_at,
        pending.first_observed_at,
        observed_at,
        observed_monotonic,
        pending.write_count + 1,
    )


def quiet_window_elapsed(
    pending: PendingAlert,
    *,
    current_monotonic: float,
    quiet_seconds: float,
) -> bool:
    """Report whether a pending burst has seen the configured quiet period."""

    return current_monotonic - pending.last_write_monotonic >= quiet_seconds


def now() -> str:
    """Return an unambiguous local timestamp."""

    return datetime.now(tz=UTC).astimezone().isoformat()


def activity_query_bound(stamp: str) -> str:
    """Return one monitor cursor at localswim's persisted event precision."""

    instant = datetime.fromisoformat(stamp)
    if instant.tzinfo is None:
        raise ValueError("an activity query bound needs a UTC offset")
    return instant.isoformat(timespec="microseconds")


def write_atomic_json(path: Path, value: dict[str, object]) -> None:
    """Replace one transient status document without exposing a partial write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    """Read one monitor-owned transient document."""

    value = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return cast("dict[str, Any]", value)


def stop_requested(path: Path, pid: int) -> bool:
    """Return whether the monitor target asks this exact process to stop."""
    try:
        target = read_json(path)
    except OSError, TypeError, ValueError:
        return False
    return target.get("schema") == 1 and target.get("stopPid") == pid


def board_signature(path: Path) -> tuple[int, int]:
    """Wait briefly for an atomically replaced board path, then stat it."""

    for _attempt in range(20):
        try:
            stat = path.stat()
        except FileNotFoundError:
            time.sleep(0.025)
        else:
            return stat.st_mtime_ns, stat.st_size
    raise RuntimeError(f"board did not become readable after a filesystem event: {path}")


def try_lock(descriptor: int) -> bool:
    """Use the same platform-specific OS lock conventions as writer ownership."""
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- Windows-only lock implementation

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
    else:
        import fcntl  # noqa: PLC0415 -- POSIX-only lock implementation

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def acquire_lock(path: Path) -> IO[bytes] | None:
    """Own a persistent portable lock file, or report an existing monitor."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        acquired = try_lock(handle.fileno())
    except BaseException:
        handle.close()
        raise
    if not acquired:
        handle.close()
        return None
    return handle


def release_lock(handle: IO[bytes]) -> None:
    """Release the byte-range lock held for the monitor lifetime."""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- Windows-only lock implementation

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415 -- POSIX-only lock implementation

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def service_is_healthy(port: int) -> bool:
    """Check the loopback service without reading private board content."""

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v001/status", timeout=1
        ) as response:
            status = cast("object", json.load(response))
    except OSError, TimeoutError, ValueError:
        return False
    return isinstance(status, dict) and cast("dict[str, object]", status).get("ok") is True


def handle_idle_cycle(
    config: Config,
    codex: str,
    runtime: MonitorRuntime,
    publish_status: Callable[[str], None],
) -> None:
    """Update health and flush any alert whose quiet window has elapsed."""
    if service_is_healthy(config.port):
        runtime.service_misses = 0
    else:
        runtime.service_misses += 1
    if runtime.pending_alert is not None and quiet_window_elapsed(
        runtime.pending_alert,
        current_monotonic=time.monotonic(),
        quiet_seconds=config.quiet_seconds,
    ):
        alerted_at = flush_pending_alert(config, codex, runtime.pending_alert)
        if alerted_at is not None:
            runtime.last_alert_at = alerted_at
        runtime.pending_alert = None
        publish_status("running")
    if runtime.service_misses >= MAX_SERVICE_MISSES and runtime.pending_alert is None:
        message = "localswim was unavailable for five consecutive checks"
        raise RuntimeError(message)


def bot_only_activity(config: Config, pending: PendingAlert) -> bool:
    """Suppress nonempty automation-only activity, independent of filesystem counts."""
    cli = shutil.which("localswim-cli")
    if cli is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603 -- resolved executable, fixed CLI operation
            [
                cli,
                str(config.board),
                "activity",
                "between",
                "--json",
                activity_query_bound(pending.activity_started_at),
                activity_query_bound(pending.last_observed_at),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            return False
        events = cast("object", json.loads(completed.stdout))
    except OSError, subprocess.TimeoutExpired, ValueError:
        return False
    if not isinstance(events, list):
        return False
    activity = cast("list[object]", events)
    # Filesystem signatures and durable activity events are not one-to-one.
    # A nonempty complete CLI result owns actor attribution, not write_count.
    if not activity:
        return False
    return all(
        isinstance(event, dict)
        and cast("dict[str, object]", event).get("by") == config.automation_actor
        for event in activity
    )


def queue_alert(config: Config, codex: str, pending: PendingAlert) -> None:
    """Queue a sanitized next-turn alert to the registered Codex thread."""

    target = read_json(config.target_path)
    if target.get("schema") != 1 or not isinstance(target.get("threadId"), str):
        raise ValueError("the monitor target file is invalid")
    try:
        thread_id = str(uuid.UUID(target["threadId"]))
    except ValueError as exc:
        raise ValueError("the monitor target thread ID is invalid") from exc

    write_summary = (
        "1 board write was observed"
        if pending.write_count == 1
        else f"{pending.write_count} board writes were coalesced"
    )
    activity_start = activity_query_bound(pending.activity_started_at)
    activity_end = activity_query_bound(pending.last_observed_at)
    message = (
        f"{config.board_label} next-turn board-write alert. {write_summary} after a "
        f"{config.quiet_seconds:g}-second quiet window. This notification was queued "
        "for the next Codex turn; it does not acknowledge delivery to the turn that "
        "was active when the write occurred. First inspect only sanitized activity metadata "
        "through localswim-cli. If every recorded event is by the configured automation "
        f"actor {config.automation_actor}, take no further action. Otherwise inspect "
        "cards as needed. Do not edit the JSON directly. Use activity between "
        f"{activity_start} {activity_end}; those bounds enclose the change interval "
        "at the board event log's precision. "
        f"First observed at {pending.first_observed_at}; last observed at "
        f"{pending.last_observed_at}."
    )
    completed = subprocess.run(  # noqa: S603 -- resolved executable, fixed arguments
        [codex, "queue", "--thread", thread_id, "--message", message],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"codex queue failed with exit status {completed.returncode}")


def flush_pending_alert(config: Config, codex: str, pending: PendingAlert) -> str | None:
    """Queue uncertain activity; return None for proven automation-only events."""
    if bot_only_activity(config, pending):
        print(f"Suppressed {pending.write_count} automation-only board write(s).", flush=True)
        return None

    queue_alert(config, codex, pending)
    alerted_at = now()
    print(
        f"Queued one next-turn alert for {pending.write_count} board write(s) ending at "
        f"{pending.last_observed_at}.",
        flush=True,
    )
    return alerted_at


def parse_args(argv: Sequence[str] | None = None) -> Config:
    """Parse and validate process inputs."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-path", required=True, type=Path)
    parser.add_argument(
        "--board-label",
        default="localswim",
        help="sanitized board name used in queued alerts",
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--status-path",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--target-path",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--quiet-seconds",
        type=float,
        default=DEFAULT_QUIET_SECONDS,
        help="seconds without another board write before one coalesced alert",
    )
    parser.add_argument("--automation-actor", default="bot")
    parser.add_argument(
        "--reload", action="store_true", help="replace an existing monitor gracefully"
    )
    args = parser.parse_args(argv)
    board = args.board_path.resolve(strict=True)
    runtime_paths = [
        args.status_path.resolve(),
        args.target_path.resolve(),
        args.lock_path.resolve(),
    ]
    if not board.is_file():
        parser.error("--board-path must name a file")
    identities = [os.path.normcase(str(path)) for path in [board, *runtime_paths]]
    if len(set(identities)) != len(identities):
        parser.error("board, status, target and lock paths must all be distinct")
    board_label = args.board_label.strip()
    if not board_label:
        parser.error("--board-label must not be empty")
    if not args.automation_actor.strip():
        parser.error("--automation-actor must not be empty")
    thread_id = str(uuid.UUID(args.thread_id))
    if not 1 <= args.port <= MAX_PORT:
        parser.error(f"--port must be between 1 and {MAX_PORT}")
    if not 0 < args.quiet_seconds <= MAX_QUIET_SECONDS:
        parser.error(f"--quiet-seconds must be greater than 0 and at most {MAX_QUIET_SECONDS:g}")
    return Config(
        board,
        board_label,
        thread_id,
        args.port,
        runtime_paths[0],
        runtime_paths[1],
        runtime_paths[2],
        args.quiet_seconds,
        args.automation_actor.strip(),
        args.reload,
    )


def reload_requested(path: Path, pid: int) -> bool:
    """Request replacement only for the exact process named by the launcher."""
    try:
        target = read_json(path)
    except OSError, TypeError, ValueError:
        return False
    return target.get("schema") == 1 and target.get("reloadPid") == pid


def request_target(config: Config) -> dict[str, Any]:
    """Retarget and optionally request a cooperative handoff from the current owner."""
    previous: dict[str, Any] = {}
    if config.reload:
        with contextlib.suppress(OSError, TypeError, ValueError):
            previous = read_json(config.status_path)
    target: dict[str, object] = {
        "schema": 1,
        "threadId": config.thread_id,
        "updatedAt": now(),
    }
    if config.reload and isinstance(previous.get("pid"), int):
        target["reloadPid"] = previous["pid"]
    write_atomic_json(config.target_path, target)
    return previous


def resume_runtime(config: Config, previous: dict[str, Any], started_at: str) -> MonitorRuntime:
    """Retain the prior activity cursor and detect writes during lock handoff."""
    signature = board_signature(config.board)
    runtime = MonitorRuntime(signature, started_at)
    observed_at = now()
    if config.reload:
        try:
            final = read_json(config.status_path)
        except OSError, TypeError, ValueError:
            final = previous
        cursor = final.get("lastSignatureAt")
        if isinstance(cursor, str):
            activity_query_bound(cursor)
            runtime.last_signature_at = cursor
            if final.get("lastSignature") != list(signature):
                runtime.pending_alert = note_board_write(
                    None,
                    activity_started_at=cursor,
                    observed_at=observed_at,
                    observed_monotonic=time.monotonic(),
                )
                runtime.last_signature_at = observed_at
        restore_pending(runtime, final)
    return runtime


def restore_pending(runtime: MonitorRuntime, final: dict[str, Any]) -> None:
    """Retain unflushed activity if the retiring process failed while draining."""
    count = final.get("pendingWriteCount")
    start = final.get("pendingActivityStartedAt")
    first = final.get("pendingFirstObservedAt")
    last = final.get("pendingLastObservedAt")
    if (
        isinstance(count, int)
        and count > 0
        and isinstance(start, str)
        and isinstance(first, str)
        and isinstance(last, str)
    ):
        activity_query_bound(start)
        activity_query_bound(first)
        activity_query_bound(last)
        gap = runtime.pending_alert
        runtime.pending_alert = PendingAlert(
            start,
            first,
            last if gap is None else gap.last_observed_at,
            time.monotonic(),
            count + (0 if gap is None else gap.write_count),
        )


def watch_board(config: Config) -> Iterator[set[object]]:
    """Load filesystem watching only when the optional command is invoked."""
    try:
        backend = importlib.import_module("watchfiles")
    except ImportError as exc:
        raise RuntimeError("install localswim[monitor] to use localswim-monitor") from exc

    def only_board(_change: object, changed_path: str) -> bool:
        return os.path.normcase(str(Path(changed_path).resolve())) == os.path.normcase(
            str(config.board)
        )

    return cast(
        "Iterator[set[object]]",
        backend.watch(
            config.board.parent,
            watch_filter=only_board,
            debounce=100,
            step=20,
            recursive=False,
            rust_timeout=1000,
            yield_on_timeout=True,
        ),
    )


def claim_monitor(config: Config) -> tuple[IO[bytes] | None, dict[str, Any]]:
    """Retarget one owner and wait for its cooperative replacement when requested."""
    previous = request_target(config)
    lock = acquire_lock(config.lock_path)
    if lock is None and config.reload:
        deadline = time.monotonic() + MAX_QUIET_SECONDS + 30
        while lock is None and time.monotonic() < deadline:
            time.sleep(0.1)
            lock = acquire_lock(config.lock_path)
        if lock is None:
            write_atomic_json(
                config.target_path,
                {
                    "schema": 1,
                    "threadId": config.thread_id,
                    "updatedAt": now(),
                },
            )
            raise RuntimeError("monitor reload timed out; replacement did not acquire ownership")
    return lock, previous


def prepare_runtime(
    config: Config, previous: dict[str, Any], lock: IO[bytes]
) -> tuple[str, str, MonitorRuntime]:
    """Prepare the runtime, releasing the lifetime lock if initialization fails."""
    codex = shutil.which("codex")
    if codex is None:
        release_lock(lock)
        raise RuntimeError("codex executable is not on PATH")
    try:
        started_at = now()
        return codex, started_at, resume_runtime(config, previous, started_at)
    except BaseException:
        release_lock(lock)
        raise


def reconcile_board_signature(config: Config, runtime: MonitorRuntime) -> bool:
    """Debounce a changed signature, including writes before native subscription.

    Idle yields must reconcile before quiet-window flushing or reload readiness.
    Notifications may arrive after reconciliation; unchanged signatures never
    extend the burst or count the same observation twice.
    """
    signature = board_signature(config.board)
    if signature == runtime.last_signature:
        return False
    runtime.last_signature = signature
    observed_at = now()
    runtime.pending_alert = note_board_write(
        runtime.pending_alert,
        activity_started_at=runtime.last_signature_at,
        observed_at=observed_at,
        observed_monotonic=time.monotonic(),
    )
    runtime.last_signature_at = observed_at
    print(
        f"Debouncing board write {runtime.pending_alert.write_count} at {observed_at}.",
        flush=True,
    )
    return True


def monitor(config: Config) -> int:
    """Retarget an existing watcher or monitor until localswim stops."""

    lock, previous = claim_monitor(config)
    if lock is None:
        print(f"Retargeted the existing {config.board_label} monitor.", flush=True)
        return 0

    codex, started_at, runtime = prepare_runtime(config, previous, lock)

    def status(state: str, **extra: object) -> None:
        write_atomic_json(
            config.status_path,
            {
                "schema": 1,
                "state": state,
                "pid": os.getpid(),
                "board": str(config.board),
                "startedAt": started_at,
                "lastSignature": list(runtime.last_signature),
                "lastSignatureAt": runtime.last_signature_at,
                "lastAlertAt": runtime.last_alert_at,
                "quietSeconds": config.quiet_seconds,
                "actorFilterVersion": ACTOR_FILTER_VERSION,
                "automationActor": config.automation_actor,
                "reloadSupported": True,
                "pendingWriteCount": (
                    0 if runtime.pending_alert is None else runtime.pending_alert.write_count
                ),
                "pendingFirstObservedAt": (
                    None
                    if runtime.pending_alert is None
                    else runtime.pending_alert.first_observed_at
                ),
                "pendingActivityStartedAt": (
                    None
                    if runtime.pending_alert is None
                    else runtime.pending_alert.activity_started_at
                ),
                "pendingLastObservedAt": (
                    None
                    if runtime.pending_alert is None
                    else runtime.pending_alert.last_observed_at
                ),
                **extra,
            },
        )

    try:
        status("running")
        print(f"Watching {config.board} for Codex thread alerts.", flush=True)
        for changes in watch_board(config):
            if stop_requested(config.target_path, os.getpid()):
                if service_is_healthy(config.port):
                    if not runtime.stopping:
                        status("stopping")
                    runtime.stopping = True
                    continue
                status("stopped")
                print(f"Stopped the {config.board_label} monitor cleanly.", flush=True)
                return 0
            if runtime.stopping:
                runtime.stopping = False
                status("running")
            if changes:
                time.sleep(0.05)
            if reconcile_board_signature(config, runtime):
                status("running")
            if not changes:
                handle_idle_cycle(config, codex, runtime, status)
                if (
                    reload_requested(config.target_path, os.getpid())
                    and runtime.pending_alert is None
                ):
                    status("reload-ready")
                    return 0
    except Exception as exc:
        status("failed", detail=str(exc))
        raise
    finally:
        release_lock(lock)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the monitor with a concise failure on stderr."""

    try:
        return monitor(parse_args(argv))
    except Exception as exc:  # noqa: BLE001 -- final CLI boundary reports all monitor failures
        print(f"localswim monitor failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
