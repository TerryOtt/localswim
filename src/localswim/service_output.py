"""Keep captured service output independent of an absent pipe consumer.

Call before starting service threads. Interactive terminals, regular log files and
null devices retain their existing behavior. Redirected files deliberately remain
available after process exit; no runtime descriptor exposes their paths.
"""

import ctypes
import os
import pathlib
import stat
import sys
import tempfile
from typing import IO, TypedDict


def _set_windows_standard_handle(name: str, descriptor: int) -> None:
    """Keep native child inheritance aligned with the replaced CRT descriptor."""
    if os.name != "nt":
        return
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_handle = kernel32.SetStdHandle
    set_handle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
    set_handle.restype = wintypes.BOOL
    standard = -11 if name == "stdout" else -12
    if not set_handle(standard & 0xFFFFFFFF, msvcrt.get_osfhandle(descriptor)):
        raise ctypes.WinError(ctypes.get_last_error())


def _safe_destination(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_type = kernel32.GetFileType
        get_type.argtypes = (wintypes.HANDLE,)
        get_type.restype = wintypes.DWORD
        # fstat/PeekNamedPipe can itself wait behind a full synchronous pipe.
        # GetFileType inspects only the handle, without reading pipe metadata.
        return get_type(msvcrt.get_osfhandle(descriptor)) in (1, 2)  # disk, character
    mode = os.fstat(descriptor).st_mode
    return stat.S_ISREG(mode) or stat.S_ISCHR(mode) or os.isatty(descriptor)


def configure() -> dict[str, pathlib.Path]:
    """Replace unsafe captured output descriptors before any service work starts.

    Do not flush the old stream: its consumer may already have stopped reading.
    Replacing its descriptor also directs any existing Python buffer to the file.
    Windows mkdir mode 0700 (Python 3.13+) grants access only to the current user
    and administrators; POSIX uses the same owner-only mode. Individual files are
    created exclusively with mode 0600 inside that private directory.
    """
    directory: pathlib.Path | None = None
    redirected: dict[str, pathlib.Path] = {}
    for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        if stream is None:
            continue
        try:
            descriptor = stream.fileno()
            safe = _safe_destination(descriptor)
        except AttributeError, OSError, ValueError:
            # In-memory embedder/test streams have no OS backpressure to remove.
            continue
        if safe:
            continue
        if directory is None:
            directory = pathlib.Path(tempfile.mkdtemp(prefix="localswim-service-"))
        path = directory / f"{name}.log"
        output = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        try:
            os.dup2(output, descriptor, inheritable=True)
        finally:
            os.close(output)
        _set_windows_standard_handle(name, descriptor)
        redirected[name] = path
    return redirected


class ChildOutput(TypedDict):
    """Only the two standard output keyword arguments for subprocess.Popen."""

    stdout: IO[str] | None
    stderr: IO[str] | None


def child_output() -> ChildOutput:
    """Explicitly preserve output sinks for a Windows board replacement child."""
    return {"stdout": sys.stdout, "stderr": sys.stderr}
