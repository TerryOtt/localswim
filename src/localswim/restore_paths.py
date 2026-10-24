"""Read-only, pinned local Windows path proofs for isolated restore preparation.

Only ordinary canonical drive-letter paths on local fixed disks are supported.
Proofs own handles: callers must close them (prefer an ExitStack). No write or
creation primitive is exposed. A missing suffix is a condition to recheck, not
permission to create it; a later phase must capture a fresh proof after creation.
Nonfollowing handles and denied delete/write sharing pin object identities;
directory timestamps are only an additional conservative drift check. Unrelated
sibling activity may therefore cause refusal and requires a fresh preflight.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from typing import Self
lazy from pathlib import Path


class PathBoundaryError(ValueError):
    """A path cannot be proven safe under the supported local contract."""


class _Information(ctypes.Structure):
    _fields_ = (
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    )


@dataclass(frozen=True)
class PathIdentity:
    """Immutable identity and mutation stamp, independent of path spelling."""

    volume: int
    index: int
    attributes: int
    size: int
    modified: int
    links: int


class _Windows:
    def __init__(self) -> None:
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dll.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self.dll.CreateFileW.restype = wintypes.HANDLE
        self.dll.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.dll.GetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Information),
        )
        self.dll.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self.dll.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        self.dll.ReadFile.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        self.dll.SetFilePointerEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.c_void_p,
            wintypes.DWORD,
        )

    def open(self, path: Path) -> int:
        # OPEN_REPARSE_POINT | BACKUP_SEMANTICS; deny write/delete sharing.
        handle = self.dll.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        if not isinstance(handle, int):
            raise PathBoundaryError("Invalid native handle")
        return handle

    def information(self, handle: int) -> PathIdentity:
        info = _Information()
        if not self.dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return PathIdentity(
            info.volume,
            info.index_high << 32 | info.index_low,
            info.attributes,
            info.size_high << 32 | info.size_low,
            info.write.dwHighDateTime << 32 | info.write.dwLowDateTime,
            info.links,
        )

    def canonical(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        count = self.dll.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise PathBoundaryError("Cannot establish canonical path")
        return buffer.value.removeprefix("\\\\?\\")

    def close(self, handle: int) -> None:
        if not self.dll.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _validate_spelling(path: Path) -> Path:
    text = str(path)
    if os.name != "nt":
        raise PathBoundaryError("Restore filesystem proofs currently require Windows")
    if not re.fullmatch(r"[A-Za-z]:\\[^:]*", text):
        raise PathBoundaryError("An ordinary absolute local drive path is required")
    if any(
        part in {".", ".."}
        or part.endswith((".", " "))
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)
        for part in path.parts[1:]
    ):
        raise PathBoundaryError("Ambiguous or traversing path component")
    return path


class CheckedPath:
    """A live proof pinned against ancestor and leaf replacement or mutation."""

    def __init__(self, path: Path, native: _Windows) -> None:
        self.path = path
        self.exists = False
        self.identity: PathIdentity | None = None
        self._native = native
        self._handles: list[tuple[Path, int, PathIdentity]] = []
        self._missing: Path | None = None
        self._closed = False

    @classmethod
    def capture(cls, path: Path, *, allow_missing: bool = False) -> Self:
        path = _validate_spelling(path)
        native = _Windows()
        if native.dll.GetDriveTypeW(path.anchor) != 3:  # noqa: PLR2004 -- DRIVE_FIXED
            raise PathBoundaryError("Only local fixed disks are supported")
        proof = cls(path, native)
        try:
            proof._capture(allow_missing=allow_missing)
        except BaseException:
            proof._cleanup()
            raise
        return proof

    def _capture(self, *, allow_missing: bool) -> None:
        components = [*reversed(self.path.parents), self.path]
        for component in components:
            try:
                handle = self._native.open(component)
            except OSError as error:
                if allow_missing and error.winerror in {2, 3}:
                    self._missing = component
                    return
                raise PathBoundaryError("Cannot pin path component") from error
            try:
                info = self._native.information(handle)
                if info.attributes & 0x400:
                    raise PathBoundaryError("Reparse components are forbidden")  # noqa: TRY301
                if os.path.normcase(self._native.canonical(handle)) != os.path.normcase(
                    str(component)
                ):
                    raise PathBoundaryError("Aliased path spelling is forbidden")  # noqa: TRY301
                directory = bool(info.attributes & 0x10)
                if component != self.path and not directory:
                    raise PathBoundaryError("Ancestor is not a directory")  # noqa: TRY301
                if not directory and (info.attributes & 0x40 or info.links != 1):
                    raise PathBoundaryError("Nonregular or multiply linked file")  # noqa: TRY301
            except BaseException:
                with suppress(BaseException):
                    self._native.close(handle)
                raise
            self._handles.append((component, handle, info))
        self.exists = True
        self.identity = self._handles[-1][2]

    def revalidate(self) -> None:
        """Recheck every held object and spelling, including a missing suffix."""
        if self._closed:
            raise PathBoundaryError("Path proof is closed")
        for path, handle, expected in self._handles:
            if self._native.information(handle) != expected:
                raise PathBoundaryError("Path component identity or type changed")
            if os.path.normcase(self._native.canonical(handle)) != os.path.normcase(str(path)):
                raise PathBoundaryError("Path component spelling changed")
        if self._missing is not None:
            try:
                handle = self._native.open(self._missing)
            except OSError as error:
                if error.winerror not in {2, 3}:
                    raise PathBoundaryError("Missing path cannot be revalidated") from error
            else:
                with suppress(BaseException):
                    self._native.close(handle)
                raise PathBoundaryError("Previously missing path now exists")

    def read_bytes(self, max_bytes: int) -> bytes:
        """Read at most cap+1 bytes, refusing directories, growth and overflow."""
        if isinstance(max_bytes, bool) or not 0 <= max_bytes <= 64 * 1024 * 1024:
            raise PathBoundaryError("Invalid bounded read cap")
        self.revalidate()
        if not self.exists or self.identity is None or self.identity.attributes & 0x10:
            raise PathBoundaryError("A regular existing file is required")
        if self.identity.size > max_bytes:
            raise PathBoundaryError("File exceeds bounded read cap")
        handle = self._handles[-1][1]
        if not self._native.dll.SetFilePointerEx(handle, 0, None, 0):
            raise PathBoundaryError("Cannot position bounded read")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            buffer = ctypes.create_string_buffer(min(65536, remaining))
            count = wintypes.DWORD()
            if not self._native.dll.ReadFile(
                handle,
                buffer,
                len(buffer),
                ctypes.byref(count),
                None,
            ):
                raise PathBoundaryError("Bounded file read failed")
            if not count.value:
                break
            chunks.append(buffer.raw[: count.value])
            remaining -= count.value
        data = b"".join(chunks)
        self.revalidate()
        if len(data) > max_bytes or len(data) != self.identity.size:
            raise PathBoundaryError("File changed or exceeds bounded read cap")
        return data

    def sealed_digest(self, max_bytes: int) -> str:
        """Seal bytes against this proof's immutable identity stamp."""
        return hashlib.sha256(self.read_bytes(max_bytes)).hexdigest()

    def _cleanup(self) -> None:
        # Cleanup must never replace a primary exception, including cancellation.
        for _, handle, _ in reversed(self._handles):
            with suppress(BaseException):
                self._native.close(handle)
        self._handles.clear()
        self._closed = True

    def close(self) -> None:
        self._cleanup()

    def __enter__(self) -> Self:
        try:
            self.revalidate()
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


def check_role_paths(**roles: CheckedPath) -> None:
    """Reject equal/nested role roots; aliases were rejected during capture."""
    paths: list[Path] = []
    for proof in roles.values():
        proof.revalidate()
        for previous in paths:
            if proof.path.is_relative_to(previous) or previous.is_relative_to(proof.path):
                raise PathBoundaryError("Restore role paths overlap")
        paths.append(proof.path)
