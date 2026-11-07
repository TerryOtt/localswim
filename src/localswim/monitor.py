"""Queue one next-turn Codex alert after a burst with uncertain or human activity.

The optional MIT-licensed ``watchfiles`` package supplies filesystem notifications.
This command never reads or logs board content. It records only file signatures
and timestamps in explicitly configured runtime paths, coalesces related writes across a
quiet window, then queries bounded ``localswim-cli`` activity metadata and queues a
compact date range and changed card numbers for the target thread's next turn.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import activity_feed, service_output, service_ownership

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence
    from typing import IO, Any


MAX_PORT = 65535
MAX_SERVICE_MISSES = 5
DEFAULT_QUIET_SECONDS = 15.0
MAX_QUIET_SECONDS = 300.0
ACTOR_FILTER_VERSION = 2
SIGNATURE_PARTS = 2
RECONCILE_SECONDS = 30.0


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
    allow_polling: bool = False


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
    last_health: service_ownership.ServiceHealth | None = None
    health_checked_at: str | None = None
    last_file_identity: tuple[int, ...] | None = None
    last_observation_started_at: str | None = None


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """Read one monitor-owned transient document."""

    with path.open("rb") as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError("monitor runtime metadata exceeds 64 KiB")
    value = cast("object", json.loads(encoded))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return cast("dict[str, Any]", value)


def stop_requested(path: Path, pid: int, owner_id: str | None = None) -> bool:
    """Return whether the monitor target asks this exact process to stop."""
    try:
        target = read_json(path)
    except OSError, TypeError, ValueError:
        return False
    return (
        target.get("schema") == 1
        and target.get("stopPid") == pid
        and (owner_id is None or target.get("stopOwnerId") == owner_id)
    )


def drain_requested(path: Path, pid: int, owner_id: str) -> bool:
    """Match a cooperative explicit drain to one exact monitor generation."""
    try:
        target = read_json(path)
    except OSError, TypeError, ValueError:
        return False
    return (
        target.get("schema") == 1
        and target.get("drainPid") == pid
        and target.get("drainOwnerId") == owner_id
    )


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
        import msvcrt  # Windows-only lock implementation

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
    else:
        import fcntl  # POSIX-only lock implementation

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
        import msvcrt  # Windows-only lock implementation

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # POSIX-only lock implementation

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def service_is_healthy(target: int | Path) -> bool:
    """Check board identity for lifecycle; retain legacy port-only probes."""
    if isinstance(target, Path):
        return service_ownership.service_is_healthy(target)
    port = target

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
    previous = runtime.last_health
    recovering = runtime.service_misses > 0
    health = service_ownership.probe_monitor_lifecycle(config.board)
    runtime.last_health = health
    runtime.health_checked_at = now()
    if health.healthy:
        runtime.service_misses = 0
    else:
        runtime.service_misses += 1
    changed = previous is None or (
        previous.healthy,
        previous.phase,
        previous.code,
        previous.port,
        previous.http_status,
        previous.error_type,
        previous.probe_mode,
    ) != (
        health.healthy,
        health.phase,
        health.code,
        health.port,
        health.http_status,
        health.error_type,
        health.probe_mode,
    )
    if not health.healthy or recovering:
        with contextlib.suppress(OSError):
            print(
                "Monitor health check: "
                + json.dumps(
                    {
                        **health.to_json(),
                        "checkedAt": runtime.health_checked_at,
                        "consecutiveFailures": runtime.service_misses,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    if changed or not health.healthy or recovering:
        with contextlib.suppress(OSError):
            publish_status("running")
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


def read_activity(config: Config, pending: PendingAlert) -> list[object] | None:
    """Read sanitized activity once; preserve uncertainty on query failure."""
    cli = shutil.which("localswim-cli")
    if cli is None:
        return None
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(  # noqa: S603 -- resolved executable, fixed metadata command
                [
                    cli,
                    str(config.board),
                    "activity",
                    "monitor-between",
                    "--json",
                    activity_query_bound(pending.activity_started_at),
                    activity_query_bound(pending.last_observed_at),
                ],
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0 or output.tell() > activity_feed.MAX_RESPONSE_BYTES + 1:
                return None
            output.seek(0)
            envelope = cast("object", json.loads(output.read(activity_feed.MAX_RESPONSE_BYTES + 1)))
    except OSError, subprocess.TimeoutExpired, TypeError, ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    metadata = cast("dict[str, object]", envelope)
    coverage = metadata.get("coverage")
    events = metadata.get("events")
    if metadata.get("schema") != 1 or not isinstance(coverage, dict):
        return None
    if cast("dict[str, object]", coverage).get("complete") is not True:
        return None
    if (
        not isinstance(events, list)
        or len(cast("list[object]", events)) > activity_feed.LIMIT_EVENTS
    ):
        return None
    return cast("list[object]", events)


def bot_only_events(activity: list[object] | None, actor: str) -> bool:
    """Suppress only nonempty activity entirely attributed to the automation actor."""
    # Filesystem signatures and durable activity events are not one-to-one.
    # A nonempty complete CLI result owns actor attribution, not write_count.
    if not activity:
        return False
    return all(
        isinstance(event, dict) and cast("dict[str, object]", event).get("by") == actor
        for event in activity
    )


def bot_only_activity(config: Config, pending: PendingAlert) -> bool:
    """Suppress nonempty automation-only activity, independent of filesystem counts."""
    return bot_only_events(read_activity(config, pending), config.automation_actor)


def queue_alert(
    config: Config, codex: str, pending: PendingAlert, activity: list[object] | None = None
) -> None:
    """Queue a sanitized next-turn alert to the registered Codex thread."""

    target = read_json(config.target_path)
    if target.get("schema") != 1 or not isinstance(target.get("threadId"), str):
        raise ValueError("the monitor target file is invalid")
    try:
        thread_id = str(uuid.UUID(target["threadId"]))
    except ValueError as exc:
        raise ValueError("the monitor target thread ID is invalid") from exc

    tickets: set[int] = set()
    incomplete = not activity
    for event in activity or []:
        if not isinstance(event, dict):
            incomplete = True
            continue
        metadata = cast("dict[str, object]", event)
        for key in ("ticket", "otherTicket", "parentFromTicket", "parentToTicket"):
            ticket = metadata.get(key)
            if key != "ticket" and ticket is None:
                continue
            if type(ticket) is int and 0 < ticket <= 999999999:
                tickets.add(ticket)
            else:
                incomplete = True
    if len(tickets) > 128:
        incomplete = True
    lines = [f"- #{ticket:04d}" for ticket in sorted(tickets)[:128]]
    if incomplete:
        lines.append("- Card numbers unavailable; inspect activity metadata.")
    activity_start = activity_query_bound(pending.activity_started_at)
    activity_end = activity_query_bound(pending.last_observed_at)
    message = f"{config.board_label}: {activity_start} → {activity_end}\n\n" + "\n".join(lines)
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
    activity = read_activity(config, pending)
    if bot_only_events(activity, config.automation_actor):
        print(f"Suppressed {pending.write_count} automation-only board write(s).", flush=True)
        return None

    queue_alert(config, codex, pending, activity)
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
    parser.add_argument(
        "--allow-polling", action="store_true", help="explicitly accept a polling backend"
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
    try:
        request_lock_path(runtime_paths[1], [board, *runtime_paths])
    except ValueError as exc:
        parser.error(str(exc))
    board_label = args.board_label.strip()
    if not board_label or len(board_label) > 200:
        parser.error("--board-label must contain between 1 and 200 characters")
    if not args.automation_actor.strip() or len(args.automation_actor) > 200:
        parser.error("--automation-actor must contain between 1 and 200 characters")
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
        args.allow_polling,
    )


def request_lock_path(target: Path, protected: Sequence[Path]) -> Path:
    """Resolve the derived lock and refuse aliases to protected runtime files."""
    path = target.with_name(f"{target.name}.lock").resolve()
    if os.path.normcase(str(path)) in {
        os.path.normcase(str(protected_path.resolve())) for protected_path in protected
    }:
        raise ValueError(
            "board, status, target, lock and target request lock paths must all be distinct"
        )
    return path


def reload_requested(path: Path, pid: int, owner_id: str | None = None) -> bool:
    """Request replacement only for the exact process named by the launcher."""
    try:
        target = read_json(path)
    except OSError, TypeError, ValueError:
        return False
    return (
        target.get("schema") == 1
        and target.get("reloadPid") == pid
        and (owner_id is None or target.get("reloadOwnerId") == owner_id)
    )


@contextlib.contextmanager
def target_transaction(config: Config) -> Generator[None]:
    """Serialize launcher read/modify/write operations without replacing any lock."""
    path = request_lock_path(
        config.target_path, [config.board, config.status_path, config.target_path, config.lock_path]
    )
    lock = acquire_lock(path)
    deadline = 0.0 if lock is not None else time.monotonic() + 30
    while lock is None:
        if time.monotonic() >= deadline:
            raise RuntimeError("monitor target request lock timed out")
        time.sleep(0.1)
        lock = acquire_lock(path)
    try:
        yield
    finally:
        release_lock(lock)


def published_owner(config: Config) -> dict[str, Any]:
    """Discover only a same-board owner with a complete non-PID identity."""
    try:
        previous = read_json(config.status_path)
        board = previous.get("board")
        if (
            type(previous.get("schema")) is int
            and previous["schema"] == 1
            and type(previous.get("pid")) is int
            and previous["pid"] > 0
            and isinstance(board, str)
            and os.path.normcase(str(Path(board).resolve()))
            == os.path.normcase(str(config.board.resolve()))
            and previous.get("state") in {"running", "stopping"}
        ):
            owner = previous.get("ownerId")
            if isinstance(owner, str) and str(uuid.UUID(owner)) == owner:
                return previous
            if owner is None and previous.get("reloadSupported") is True:
                started = previous.get("startedAt")
                if isinstance(started, str):
                    activity_query_bound(started)
                    return previous
    except OSError, TypeError, ValueError:
        pass
    return {}


def request_target(config: Config) -> tuple[str, dict[str, Any]]:
    """Retarget and optionally request a cooperative handoff from the current owner."""
    request_id = str(uuid.uuid4())
    target: dict[str, object] = {
        "schema": 1,
        "threadId": config.thread_id,
        "updatedAt": now(),
        "requestId": request_id,
    }
    with target_transaction(config):
        previous = published_owner(config) if config.reload else {}
        if previous:
            target.update(reload_fields(previous))
        write_atomic_json(config.target_path, target)
    return request_id, previous


def reload_fields(previous: dict[str, Any]) -> dict[str, object]:
    """Keep pre-UUID owners upgradeable; new owners require their published UUID."""
    return {"reloadPid": previous["pid"], "reloadOwnerId": previous.get("ownerId")}


def refresh_reload(config: Config, request_id: str, *, cancel: bool = False) -> dict[str, Any]:
    """Update only this launcher's request while holding the target transaction lock."""
    target = read_json(config.target_path)
    if target.get("requestId") != request_id or "stopPid" in target:
        raise RuntimeError("monitor reload superseded by a newer target request")
    previous = published_owner(config)
    if cancel:
        target.pop("reloadPid", None)
        target.pop("reloadOwnerId", None)
    elif previous and "reloadPid" not in target:
        target.update(reload_fields(previous))
    else:
        return previous
    write_atomic_json(config.target_path, target)
    return previous


def resume_runtime(config: Config, previous: dict[str, Any], started_at: str) -> MonitorRuntime:
    """Recover activity after ownership acquisition, before replacing status.

    Recovery is at least once: a crash after queue acknowledgement but before
    status replacement can replay the saved interval. It is not an exactly-once
    delivery or power-loss durability guarantee.
    """
    observation_started_at = now()
    signature = board_signature(config.board)
    runtime = MonitorRuntime(signature, started_at)
    runtime.last_observation_started_at = observation_started_at
    runtime.last_file_identity = activity_feed.fingerprint(config.board)
    observed_at = now()
    runtime.last_signature_at = observed_at
    if not config.reload:
        final = orphaned_status(config)
    else:
        try:
            final = read_json(config.status_path)
        except OSError, TypeError, ValueError:
            final = previous
    cursor = final.get("lastSignatureAt")
    if isinstance(cursor, str):
        activity_query_bound(cursor)
        runtime.last_signature_at = cursor
        lower_bound = final.get("lastObservationStartedAt", cursor)
        runtime.last_observation_started_at = lower_bound
        saved_identity = final.get("lastFileIdentity")
        if final.get("lastSignature") != list(signature) or (
            saved_identity is not None and saved_identity != list(runtime.last_file_identity)
        ):
            runtime.pending_alert = note_board_write(
                None,
                activity_started_at=lower_bound,
                observed_at=observed_at,
                observed_monotonic=time.monotonic(),
            )
            runtime.last_signature_at = observed_at
            runtime.last_observation_started_at = observation_started_at
    restore_pending(runtime, final)
    if not config.reload:
        runtime.last_alert_at = final.get("lastAlertAt")
    return runtime


def orphaned_status(config: Config) -> dict[str, Any]:
    """Validate ordinary-start metadata under the lifetime lock; protect uncertainty."""
    try:
        final = read_json(config.status_path)
    except FileNotFoundError:
        return {}
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("saved monitor status is unreadable; preserved for recovery") from exc
    try:
        validate_orphaned_status(config, final)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("saved monitor status is uncertain; preserved for recovery") from exc
    return final


def validate_orphaned_status(config: Config, final: dict[str, Any]) -> None:
    """Require a complete same-canonical-board cursor and pending interval."""
    board = final.get("board")
    if (
        type(final.get("schema")) is not int
        or final["schema"] != 1
        or not isinstance(board, str)
        or os.path.normcase(str(Path(board).resolve()))
        != os.path.normcase(str(config.board.resolve()))
    ):
        raise ValueError("unsupported or foreign-board monitor status")
    signature = cast("object", final.get("lastSignature"))
    if not isinstance(signature, list):
        raise TypeError("invalid saved board signature")
    parts = cast("list[object]", signature)
    if len(parts) != SIGNATURE_PARTS or any(type(part) is not int or part < 0 for part in parts):
        raise ValueError("invalid saved board signature")
    identity = final.get("lastFileIdentity")
    if "lastFileIdentity" in final:
        if not isinstance(identity, list):
            raise ValueError("invalid saved file identity")
        identity_parts = cast("list[object]", identity)
        if len(identity_parts) != 5 or any(
            type(part) is not int or part < 0 for part in identity_parts
        ):
            raise ValueError("invalid saved file identity")
    cursor_value = final.get("lastSignatureAt")
    if not isinstance(cursor_value, str):
        raise TypeError("invalid saved activity cursor")
    cursor = datetime.fromisoformat(activity_query_bound(cursor_value))
    validate_saved_pending(final, cursor)
    lower_bound = final.get("lastObservationStartedAt", cursor_value)
    if not isinstance(lower_bound, str):
        raise ValueError("invalid saved observation boundary")
    lower = datetime.fromisoformat(activity_query_bound(lower_bound))
    if lower > cursor:
        raise ValueError("observation boundary exceeds activity cursor")
    if final.get("pendingWriteCount"):
        pending_start = datetime.fromisoformat(
            activity_query_bound(final["pendingActivityStartedAt"])
        )
        if lower < pending_start:
            raise ValueError("observation boundary precedes pending interval")
    alerted_at = final.get("lastAlertAt")
    if alerted_at is not None:
        if not isinstance(alerted_at, str):
            raise ValueError("invalid saved alert timestamp")
        activity_query_bound(alerted_at)


def validate_saved_pending(final: dict[str, Any], cursor: datetime) -> None:
    """Refuse partial or reversed pending intervals rather than silently clearing them."""
    count = final.get("pendingWriteCount")
    if type(count) is not int or count < 0:
        raise ValueError("invalid saved pending count")
    if not all(
        field in final
        for field in (
            "pendingActivityStartedAt",
            "pendingFirstObservedAt",
            "pendingLastObservedAt",
        )
    ):
        raise ValueError("incomplete saved pending metadata")
    stamps = [
        final.get("pendingActivityStartedAt"),
        final.get("pendingFirstObservedAt"),
        final.get("pendingLastObservedAt"),
    ]
    if count:
        bounds: list[datetime] = []
        for stamp in stamps:
            if not isinstance(stamp, str):
                raise TypeError("incomplete saved pending interval")
            bounds.append(datetime.fromisoformat(activity_query_bound(stamp)))
        if not bounds[0] <= bounds[1] <= bounds[2] == cursor:
            raise ValueError("inconsistent saved activity interval")
    elif any(stamp is not None for stamp in stamps):
        raise ValueError("saved pending timestamps have no pending count")


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
    """Verify the native backend before watching board and control replacements."""
    from .monitor_pool import NativeSubscription

    subscription = NativeSubscription(
        [config.board, config.target_path], allow_polling=config.allow_polling
    )

    def batches() -> Iterator[set[object]]:
        try:
            while True:
                yield cast("set[object]", subscription.poll())
        finally:
            subscription.close()

    return batches()


def claim_monitor(config: Config) -> tuple[IO[bytes] | None, dict[str, Any]]:
    """Retarget one owner and wait for its cooperative replacement when requested."""
    request_id, previous = request_target(config)
    with target_transaction(config):
        if config.reload:
            previous = refresh_reload(config, request_id) or previous
        lock = acquire_lock(config.lock_path)
    if lock is None and config.reload:
        deadline = time.monotonic() + MAX_QUIET_SECONDS + 30
        while lock is None and time.monotonic() < deadline:
            time.sleep(0.1)
            with target_transaction(config):
                previous = refresh_reload(config, request_id) or previous
                lock = acquire_lock(config.lock_path)
        if lock is None:
            with target_transaction(config):
                refresh_reload(config, request_id, cancel=True)
            raise RuntimeError("monitor reload timed out; replacement did not acquire ownership")
    return lock, previous


def prepare_runtime(
    config: Config, previous: dict[str, Any], lock: IO[bytes]
) -> tuple[str, str, MonitorRuntime]:
    """Prepare the runtime, releasing the lifetime lock if initialization fails."""
    codex = shutil.which("codex")
    if codex is None:
        try:
            release_lock(lock)
        finally:
            lock.close()
        raise RuntimeError("codex executable is not on PATH")
    try:
        started_at = now()
        return codex, started_at, resume_runtime(config, previous, started_at)
    except BaseException:
        try:
            release_lock(lock)
        finally:
            lock.close()
        raise


def reconcile_board_signature(config: Config, runtime: MonitorRuntime) -> bool:
    """Debounce a changed signature, including writes before native subscription.

    Idle yields must reconcile before quiet-window flushing or reload readiness.
    Notifications may arrive after reconciliation; unchanged signatures never
    extend the burst or count the same observation twice.
    """
    observation_started_at = now()
    signature = board_signature(config.board)
    if signature == runtime.last_signature:
        return False
    runtime.last_signature = signature
    runtime.last_file_identity = activity_feed.fingerprint(config.board)
    observed_at = now()
    runtime.pending_alert = note_board_write(
        runtime.pending_alert,
        activity_started_at=runtime.last_observation_started_at or runtime.last_signature_at,
        observed_at=observed_at,
        observed_monotonic=time.monotonic(),
    )
    runtime.last_signature_at = observed_at
    runtime.last_observation_started_at = observation_started_at
    print(
        f"Debouncing board write {runtime.pending_alert.write_count} at {observed_at}.",
        flush=True,
    )
    return True


def publish_runtime_status(
    config: Config,
    runtime: MonitorRuntime,
    started_at: str,
    owner_id: str,
    state: str,
    **extra: object,
) -> None:
    """Publish compatible recovery metadata for either monitor execution mode."""
    write_atomic_json(
        config.status_path,
        {
            "schema": 1,
            "state": state,
            "pid": os.getpid(),
            "ownerId": owner_id,
            "board": str(config.board),
            "startedAt": started_at,
            "lastSignature": list(runtime.last_signature),
            **(
                {"lastFileIdentity": list(runtime.last_file_identity)}
                if runtime.last_file_identity is not None
                else {}
            ),
            **(
                {"lastObservationStartedAt": runtime.last_observation_started_at}
                if runtime.last_observation_started_at is not None
                else {}
            ),
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
                None if runtime.pending_alert is None else runtime.pending_alert.first_observed_at
            ),
            "pendingActivityStartedAt": (
                None if runtime.pending_alert is None else runtime.pending_alert.activity_started_at
            ),
            "pendingLastObservedAt": (
                None if runtime.pending_alert is None else runtime.pending_alert.last_observed_at
            ),
            **(
                {
                    "lastHealthCheck": {
                        **runtime.last_health.to_json(),
                        "checkedAt": runtime.health_checked_at,
                    },
                    "consecutiveHealthFailures": runtime.service_misses,
                }
                if runtime.last_health is not None
                else {}
            ),
            **extra,
        },
    )


def monitor(config: Config) -> int:
    """Retarget an existing watcher or monitor until localswim stops."""

    lock, previous = claim_monitor(config)
    if lock is None:
        print(f"Retargeted the existing {config.board_label} monitor.", flush=True)
        return 0

    codex, started_at, runtime = prepare_runtime(config, previous, lock)
    owner_id = str(uuid.uuid4())

    def status(state: str, **extra: object) -> None:
        publish_runtime_status(config, runtime, started_at, owner_id, state, **extra)

    try:
        status("running")
        print(f"Watching {config.board} for Codex thread alerts.", flush=True)
        for changes in watch_board(config):
            if stop_requested(config.target_path, os.getpid()):
                health = service_ownership.probe_monitor_lifecycle(config.board)
                runtime.last_health = health
                runtime.health_checked_at = now()
                if health.healthy:
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
                    reload_requested(config.target_path, os.getpid(), owner_id)
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
        from .monitor_pool import run_pool

        config = parse_args(argv)
        from .monitor_pool import validate_configs

        validate_configs([config])
        service_output.configure()
        return run_pool([config], allow_polling=config.allow_polling)
    except Exception as exc:  # noqa: BLE001 -- final CLI boundary reports all monitor failures
        print(f"localswim monitor failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
