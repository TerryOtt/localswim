"""Publish only the durable gateway preferences through the shared repository lock."""

import subprocess
import threading
import time
from typing import TYPE_CHECKING

from . import api_endpoint

if TYPE_CHECKING:
    import pathlib

    from .workspace_config import GatewayConfigStore

QUIET_SECONDS = 5.0
RETRY_SECONDS = 30.0
TICK_SECONDS = 1.0
GIT_TIMEOUT_SECONDS = 120


def _git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 -- fixed Git executable, separate shell-free argv
            ["git", *arguments],  # noqa: S607 -- supported PATH Git discovery
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        # Git diagnostics may echo remote credentials. Status never publishes stderr.
        return subprocess.CompletedProcess(arguments, 1, "", "")


def _unavailable(path: pathlib.Path) -> str:
    cwd = path.parent
    if _git(["rev-parse", "--show-toplevel"], cwd).returncode:
        return "gateway directory is not a Git repository"
    if _git(["check-ignore", "--quiet", "--", path.name], cwd).returncode == 0:
        return "gateway configuration is ignored by Git"
    if not _git(["remote"], cwd).stdout.strip():
        return "gateway repository has no remote"
    return ""


class GatewayPublisher:
    """Debounce settings snapshots; construction never starts a network worker.

    Publication takes the repository lock before the store lock. Mutations take only
    the store lock. Release the store before pushing so network waits cannot block UI
    settings changes, while board publishers remain serialized through the whole push.
    """

    def __init__(self, store: GatewayConfigStore) -> None:
        self.store = store
        self._mutex = threading.Lock()
        self._operation = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._status: dict[str, object] = {"state": "off", "at": 0.0, "detail": "not started"}

    def _set_status(self, state: str, detail: str) -> None:
        with self._mutex:
            self._status = {"state": state, "at": time.time(), "detail": detail}

    def status(self) -> dict[str, object]:
        """Return a detached status snapshot without exposing Git diagnostics."""
        with self._mutex:
            return dict(self._status)

    def start(self) -> None:
        """Start one watcher and arm startup reconciliation for an enabled store."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="gateway-autopush", daemon=True)
        self._worker.start()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            info = self.store.path.stat()
        except OSError:
            return None
        else:
            return info.st_mtime_ns, info.st_size

    def _commit(self) -> tuple[bool, str]:
        """Validate and capture exactly one file while settings writers are paused."""
        with self.store.locked():
            if not self.store.snapshot().autopush:
                return False, "disabled"
            path = self.store.path
            args = ["--", path.name]
            status = _git(["status", "--porcelain=v1", "--untracked-files=all", *args], path.parent)
            if status.returncode:
                return False, "gateway Git status failed"
            if not status.stdout.strip():
                return False, ""
            if _git(["add", *args], path.parent).returncode:
                return False, "gateway Git add failed"
            if _git(
                ["commit", "-m", "Gateway: automatic preferences snapshot", *args], path.parent
            ).returncode:
                return False, "gateway Git commit failed"
            return True, ""

    def flush(self) -> tuple[bool, str]:
        """Publish current settings synchronously, including a previous unpushed commit."""
        with self._operation:
            try:
                return self._flush()
            except OSError, ValueError:
                detail = "gateway configuration validation or repository lock failed"
                self._set_status("failed", detail)
                return False, detail

    def _flush(self) -> tuple[bool, str]:
        if not self.store.snapshot().autopush:
            self._set_status("off", "gateway autopush is disabled")
            return True, "gateway autopush is disabled"
        unavailable = _unavailable(self.store.path)
        if unavailable:
            self._set_status("off", unavailable)
            return True, unavailable
        self._set_status("pending", "publishing gateway preferences")
        with api_endpoint.repository_push_locked(self.store.path.parent):
            committed, problem = self._commit()
            if problem == "disabled":
                self._set_status("off", "gateway autopush is disabled")
                return True, "gateway autopush is disabled"
            if problem:
                self._set_status("failed", problem)
                return False, problem
            if _git(["push"], self.store.path.parent).returncode:
                detail = "gateway push failed; local snapshot retained for retry"
                self._set_status("failed", detail)
                return False, detail
        detail = (
            "gateway preferences committed and pushed" if committed else "repository synchronized"
        )
        self._set_status("ok", detail)
        return True, detail

    def _run(self) -> None:
        seen = self._stamp()
        due: float | None = time.monotonic() + QUIET_SECONDS
        self._set_status("pending", "checking gateway autopush settings")
        while not self._stop.is_set():
            try:
                enabled = self.store.snapshot().autopush
            except OSError, ValueError:
                enabled = True  # flush publishes the explicit validation failure after quiet time
            stamp = self._stamp()
            now = time.monotonic()
            if stamp != seen:
                seen = stamp
                due = now + QUIET_SECONDS
                if enabled:
                    self._set_status("pending", "gateway preferences changed; waiting for quiet")
            if not enabled:
                self._set_status("off", "gateway autopush is disabled")
                due = now + QUIET_SECONDS
            elif due is not None and now >= due:
                ok, _ = self.flush()
                due = None if ok else time.monotonic() + RETRY_SECONDS
            if self._stop.wait(TICK_SECONDS):
                break

    def stop(self) -> tuple[bool, str]:
        """Stop the watcher and flush a final snapshot before lifecycle shutdown."""
        self._stop.set()
        if self._worker is not None:
            self._worker.join()
            self._worker = None
        return self.flush()
