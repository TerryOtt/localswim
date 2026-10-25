"""Persistent repository publisher locks handle first use and every cleanup path."""

import errno
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
lazy import pathlib

import pytest

from localswim import api_endpoint


@pytest.fixture
def lock_path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Isolate the OS lock from Git operations; publication tests exercise real Git."""

    def common_directory(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        assert args == ["rev-parse", "--git-common-dir"]
        assert cwd == tmp_path
        return subprocess.CompletedProcess(args, 0, str(tmp_path), "")

    monkeypatch.setattr(api_endpoint, "_git", common_directory)
    return tmp_path / "localswim-autopush.lock"


def assert_closed(fd: int) -> None:
    with pytest.raises(OSError, match="Bad file descriptor") as caught:
        os.lseek(fd, 0, os.SEEK_SET)
    assert caught.value.errno == errno.EBADF


@pytest.mark.skipif(os.name != "nt", reason="Windows rejects writes into another byte lock")
def test_simultaneous_first_use_never_writes_through_a_held_windows_lock(
    lock_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force both old fstat calls to see zero, then delay writer two until lock one is held."""
    opened = threading.Barrier(2, timeout=5)
    inspected = threading.Barrier(2, timeout=5)
    first_held = threading.Event()
    second_attempted = threading.Event()
    entry_order: list[str] = []
    empty_observations: list[int] = []
    original_try = api_endpoint.try_process_lock

    def open_lock(path: pathlib.Path, flags: int, mode: int) -> int:
        fd = os.open(path, flags, mode)
        try:
            opened.wait()
        except threading.BrokenBarrierError:
            os.close(fd)
            raise
        return fd

    def inspect_lock(fd: int) -> os.stat_result:
        snapshot = os.fstat(fd)
        empty_observations.append(snapshot.st_size)
        inspected.wait()
        return snapshot

    def initialize_lock(fd: int, data: bytes) -> int:
        # This seam is exercised only by the unsafe implementation's negative control.
        if threading.current_thread().name == "second":
            assert first_held.wait(5)
            try:
                return os.write(fd, data)
            finally:
                second_attempted.set()
        return os.write(fd, data)

    def acquire_lock(fd: int) -> bool:
        if threading.current_thread().name == "second":
            assert first_held.wait(5)
        acquired = original_try(fd)
        if not acquired:
            second_attempted.set()
        return acquired

    # Patch only this module's OS binding, keeping pytest/threading file I/O untouched.
    monkeypatch.setattr(
        api_endpoint,
        "os",
        SimpleNamespace(
            open=open_lock,
            fstat=inspect_lock,
            write=initialize_lock,
            close=os.close,
            lseek=os.lseek,
            name=os.name,
            SEEK_SET=os.SEEK_SET,
            O_CREAT=os.O_CREAT,
            O_RDWR=os.O_RDWR,
        ),
    )
    monkeypatch.setattr(api_endpoint, "try_process_lock", acquire_lock)

    def contender(name: str) -> None:
        threading.current_thread().name = name
        with api_endpoint.repository_push_locked(lock_path.parent):
            entry_order.append(name)
            if name == "first":
                first_held.set()
                assert second_attempted.wait(5)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(contender, name) for name in ("first", "second")]
        failures = [future.exception(timeout=10) for future in futures]

    assert failures == [None, None]
    assert entry_order == ["first", "second"]
    assert empty_observations in ([], [0, 0])
    assert lock_path.read_bytes() == b""


def test_lock_timeout_preserves_polling_closes_descriptor_and_can_reacquire(
    lock_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors: list[int] = []
    sleeps: list[float] = []
    ticks = iter([1.0, 1.04, 1.08, 1.12])

    def refuse(fd: int) -> bool:
        descriptors.append(fd)
        return False

    with monkeypatch.context() as patch:
        patch.setattr(api_endpoint, "try_process_lock", refuse)
        patch.setattr(
            api_endpoint,
            "time",
            SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
        )
        patch.setattr(api_endpoint, "_PUSH_LOCK_WAIT_S", 0.1)
        patch.setattr(api_endpoint, "_PUSH_LOCK_POLL_S", 0.04)
        with (
            pytest.raises(
                TimeoutError, match="timed out waiting for repository autopush lock"
            ) as caught,
            api_endpoint.repository_push_locked(lock_path.parent),
        ):
            pytest.fail("timed-out contender entered")

    assert str(lock_path) in str(caught.value)
    assert len(descriptors) == 3
    assert sleeps == [0.04, 0.04]
    assert_closed(descriptors[0])
    with api_endpoint.repository_push_locked(lock_path.parent):
        assert lock_path.stat().st_size == 0
    assert lock_path.read_bytes() == b""


@pytest.mark.parametrize("failure_at", ["acquire", "body", "release"])
@pytest.mark.parametrize("contents", [b"", b"persistent lock\n"])
def test_lock_errors_preserve_cause_close_descriptor_and_allow_reacquisition(
    lock_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure_at: str, contents: bytes
) -> None:
    lock_path.write_bytes(contents)
    failure = OSError(errno.EIO, f"injected {failure_at} failure")
    descriptors: list[int] = []
    releases: list[int] = []
    original_try = api_endpoint.try_process_lock
    original_release = api_endpoint.release_process_lock

    def acquire(fd: int) -> bool:
        descriptors.append(fd)
        if failure_at == "acquire":
            raise failure
        return original_try(fd)

    def release(fd: int) -> None:
        releases.append(fd)
        if failure_at == "release":
            raise failure
        original_release(fd)

    def fail_during_lock() -> None:
        with api_endpoint.repository_push_locked(lock_path.parent):
            assert failure_at != "acquire"
            if failure_at == "body":
                raise failure

    with monkeypatch.context() as patch:
        patch.setattr(api_endpoint, "try_process_lock", acquire)
        patch.setattr(api_endpoint, "release_process_lock", release)
        with pytest.raises(OSError, match=f"injected {failure_at} failure") as caught:
            fail_during_lock()

    assert caught.value is failure
    assert len(descriptors) == 1
    assert releases == ([] if failure_at == "acquire" else descriptors)
    assert_closed(descriptors[0])
    with api_endpoint.repository_push_locked(lock_path.parent):
        assert lock_path.stat().st_size == len(contents)
    assert lock_path.read_bytes() == contents


def test_real_process_timeout_cannot_enter_or_release_the_holder(lock_path: pathlib.Path) -> None:
    """The timed-out process closes only its descriptor; the original holder stays exclusive."""
    child = """
import pathlib
import subprocess
import sys
from localswim import api_endpoint

root = pathlib.Path(sys.argv[1])
api_endpoint._git = lambda args, cwd: subprocess.CompletedProcess(args, 0, str(root), "")
api_endpoint._PUSH_LOCK_WAIT_S = 0.1
api_endpoint._PUSH_LOCK_POLL_S = 0.01
try:
    with api_endpoint.repository_push_locked(root):
        raise AssertionError("contender entered while holder was active")
except TimeoutError:
    pass
"""

    def contender() -> None:
        result = subprocess.run(
            [sys.executable, "-c", child, str(lock_path.parent)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    with api_endpoint.repository_push_locked(lock_path.parent):
        contender()
        contender()
        assert lock_path.stat().st_size == 0
    with api_endpoint.repository_push_locked(lock_path.parent):
        assert lock_path.stat().st_size == 0
    assert lock_path.read_bytes() == b""
