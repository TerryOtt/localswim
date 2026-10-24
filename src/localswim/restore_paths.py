"""Read-only, pinned local Windows path proofs for isolated restore preparation.

Only ordinary canonical drive-letter paths on local fixed disks are supported.
Proofs own handles: callers must close them (prefer an ExitStack). No write or
creation primitive is exposed by CheckedPath. CreationSession supplies separate,
session-owned exclusive creation proofs for the later preparation phase.
Nonfollowing handles and denied delete/write sharing pin object identities;
directory timestamps are only an additional conservative drift check. Unrelated
sibling activity may therefore cause refusal and requires a fresh preflight.
"""

# These companion proof/registry classes share handle internals within this module.
# ruff: noqa: SLF001
# pyright: reportPrivateUsage=false

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from typing import Self
lazy from collections.abc import Iterable
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
        if isinstance(max_bytes, bool) or not 0 <= max_bytes <= 96 * 1024 * 1024:
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


class _UnicodeString(ctypes.Structure):
    _fields_ = (
        ("length", wintypes.USHORT),
        ("maximum", wintypes.USHORT),
        ("buffer", wintypes.LPWSTR),
    )


class _ObjectAttributes(ctypes.Structure):
    _fields_ = (
        ("length", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security", ctypes.c_void_p),
        ("quality", ctypes.c_void_p),
    )


class _IoStatus(ctypes.Structure):
    _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))


class _CreatingWindows(_Windows):
    """Bindings initialized only inside an explicitly Windows-only session."""

    def __init__(self) -> None:
        super().__init__()
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.ntdll.NtCreateFile.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatus),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        )
        self.ntdll.NtCreateFile.restype = wintypes.LONG
        self.dll.WriteFile.argtypes = self.dll.ReadFile.argtypes
        self.dll.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        self.dll.GetVolumeInformationByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            wintypes.DWORD,
        )

    def require_ntfs(self, handle: int) -> None:
        filesystem = ctypes.create_unicode_buffer(32)
        if (
            not self.dll.GetVolumeInformationByHandleW(
                handle,
                None,
                0,
                None,
                None,
                None,
                filesystem,
                len(filesystem),
            )
            or filesystem.value != "NTFS"
        ):
            raise PathBoundaryError("Creation requires a local NTFS volume")

    def create_child(self, parent: int, name: str, *, directory: bool) -> int:
        # FILE_CREATE returns the new object's handle in the same native operation.
        # RootDirectory is the pinned parent; no pathname reopen confers ownership.
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if not 0 < length <= 510:  # noqa: PLR2004 -- 255 UTF-16 code units on NTFS
            raise PathBoundaryError("Native child name exceeds the NTFS component bound")
        unicode = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(unicode),
            0x40,
            None,
            None,
        )
        handle = wintypes.HANDLE()
        status = _IoStatus()
        transferred = False
        try:
            result = self.ntdll.NtCreateFile(
                ctypes.byref(handle),
                0x80100000 if directory else 0xC0100000,
                ctypes.byref(attributes),
                ctypes.byref(status),
                None,
                0x80,
                1,
                2,
                0x00200020 | (1 if directory else 0x40),
                None,
                0,
            )
            if result < 0:
                raise PathBoundaryError(
                    f"Exclusive native creation refused (NTSTATUS {result:#x})",
                )
            if not isinstance(handle.value, int) or status.information != 2:  # noqa: PLR2004
                raise PathBoundaryError(
                    "Native creation did not establish exclusive ownership",
                )
            transferred = True
            return handle.value
        finally:
            if not transferred and isinstance(handle.value, int):
                with suppress(BaseException):
                    self.close(handle.value)

    def seek_start(self, handle: int) -> None:
        if not self.dll.SetFilePointerEx(handle, 0, None, 0):
            raise PathBoundaryError("Cannot position owned handle")

    def read_chunk(self, handle: int, limit: int) -> bytes:
        buffer = ctypes.create_string_buffer(limit)
        count = wintypes.DWORD()
        if not self.dll.ReadFile(handle, buffer, limit, ctypes.byref(count), None):
            raise PathBoundaryError("Native file read failed")
        return buffer.raw[: count.value]

    def write_chunk(self, handle: int, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data)
        count = wintypes.DWORD()
        if not self.dll.WriteFile(handle, buffer, len(data), ctypes.byref(count), None):
            raise PathBoundaryError("Owned file write failed")
        if count.value != len(data):
            raise PathBoundaryError("Owned file write was short")

    def flush(self, handle: int) -> None:
        if not self.dll.FlushFileBuffers(handle):
            raise PathBoundaryError("Owned file flush failed")


@dataclass
class _SessionNode:
    path: Path
    handle: int
    identity: PathIdentity


class _SessionPath(CheckedPath):
    """A session view; closing a view never releases the session's ownership."""

    def __init__(self, path: Path, session: CreationSession, *, directory: bool | None) -> None:
        super().__init__(path, session._native)
        self._session = session
        self._directory = directory

    def revalidate(self) -> None:
        if self._closed:
            raise PathBoundaryError("Path proof is closed")
        self._session.revalidate()

    def close(self) -> None:
        self._closed = True

    def _cleanup(self) -> None:
        # CheckedPath context entry invokes this on validation failure. A view
        # must never close the registry's shared native handles.
        self.close()


class CreationSession:
    """Fresh proof registry and append-only native ownership, never byte cleanup.

    Every read/create validates the complete registry. Only a successful owned
    child creation can advance that direct parent's size/write stamp, across all
    registered views. Owned writes advance only their own leaf. Other stamps
    remain strict. Close releases handles, including partial creations, without
    deleting anything. File flushes support application-crash preparation; this
    API makes no directory-flush or physical power-loss durability claim.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise PathBoundaryError("Restore creation currently requires Windows")
        self._native = _CreatingWindows()
        self._nodes: dict[str, _SessionNode] = {}
        self._proofs: list[_SessionPath] = []
        self._closed = False
        self._failed = False

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    @staticmethod
    def _strong(info: PathIdentity) -> tuple[int, int, int, int]:
        return info.volume, info.index, info.attributes, info.links

    def _validate_node(self, node: _SessionNode, *, stamps: bool = True) -> PathIdentity:
        info = self._native.information(node.handle)
        if info != node.identity if stamps else self._strong(info) != self._strong(node.identity):
            raise PathBoundaryError("Session path component identity or type changed")
        if info.attributes & 0x400:
            raise PathBoundaryError("Reparse components are forbidden")
        if not info.attributes & 0x10 and (info.attributes & 0x40 or info.links != 1):
            raise PathBoundaryError("Nonregular or multiply linked file")
        if self._key(Path(self._native.canonical(node.handle))) != self._key(node.path):
            raise PathBoundaryError("Aliased path spelling is forbidden")
        return info

    def _sync(self) -> None:
        for proof in self._proofs:
            proof._handles = []
            proof._missing = None
            for path in [*reversed(proof.path.parents), proof.path]:
                node = self._nodes.get(self._key(path))
                if node is None:
                    proof._missing = path
                    break
                proof._handles.append((path, node.handle, node.identity))
            proof.exists = proof._missing is None
            proof.identity = proof._handles[-1][2] if proof.exists else None
            if (
                proof.exists
                and proof._directory is not None
                and proof.identity is not None
                and bool(proof.identity.attributes & 0x10) != proof._directory
            ):
                raise PathBoundaryError("Path role has an unexpected directory/file type")

    def revalidate(self) -> None:
        if self._closed or self._failed:
            raise PathBoundaryError("Creation session is closed or failed")
        for node in self._nodes.values():
            self._validate_node(node)
        for proof in self._proofs:
            if proof._missing is None:
                continue
            try:
                handle = self._native.open(proof._missing)
            except OSError as error:
                if error.winerror not in {2, 3}:
                    raise PathBoundaryError("Missing session path cannot be revalidated") from error
            else:
                with suppress(BaseException):
                    self._native.close(handle)
                raise PathBoundaryError("Unowned previously missing path now exists")

    def capture(
        self,
        path: Path,
        *,
        allow_missing: bool = False,
        directory: bool | None = None,
    ) -> CheckedPath:
        self.revalidate()
        path = _validate_spelling(path)
        if self._native.dll.GetDriveTypeW(path.anchor) != 3:  # noqa: PLR2004
            raise PathBoundaryError("Only local fixed disks are supported")
        for component in [*reversed(path.parents), path]:
            key = self._key(component)
            if key in self._nodes:
                node = self._nodes[key]
            else:
                try:
                    handle = self._native.open(component)
                except OSError as error:
                    if allow_missing and error.winerror in {2, 3}:
                        break
                    raise PathBoundaryError("Cannot pin session path component") from error
                try:
                    node = _SessionNode(component, handle, self._native.information(handle))
                    self._validate_node(node)
                    self._native.require_ntfs(handle)
                except BaseException:
                    with suppress(BaseException):
                        self._native.close(handle)
                    raise
                self._nodes[key] = node
            if component != path and not node.identity.attributes & 0x10:
                raise PathBoundaryError("Ancestor is not a directory")
        proof = _SessionPath(path, self, directory=directory)
        self._proofs.append(proof)
        self._sync()
        self.revalidate()
        return proof

    def read(self, proof: CheckedPath, max_bytes: int) -> bytes:
        self._require_proof(proof)
        return proof.read_bytes(max_bytes)

    def _require_proof(self, proof: CheckedPath) -> None:
        if not isinstance(proof, _SessionPath) or proof._session is not self or proof._closed:
            raise PathBoundaryError("A live proof from this session is required")
        self.revalidate()

    def _create(self, path: Path, *, directory: bool) -> _SessionNode:
        path = _validate_spelling(path)
        self.revalidate()
        parent = self._nodes.get(self._key(path.parent))
        if parent is None or not parent.identity.attributes & 0x10:
            raise PathBoundaryError("Creation requires a registered existing directory parent")
        if self._key(path) in self._nodes:
            raise PathBoundaryError("Exclusive destination already exists")
        handle: int | None = None
        try:
            handle = self._native.create_child(parent.handle, path.name, directory=directory)
            child = _SessionNode(path, handle, self._native.information(handle))
            self._validate_node(child)
            if bool(child.identity.attributes & 0x10) != directory:
                raise PathBoundaryError("Created object has unexpected type")  # noqa: TRY301
            parent_after = self._validate_node(parent, stamps=False)
            self._nodes[self._key(path)] = child
            handle = None  # The registry now owns cleanup even if a later check fails.
            parent.identity = parent_after
            self._sync()
            self.revalidate()
        except BaseException:
            self._failed = True
            if handle is not None:
                with suppress(BaseException):
                    self._native.close(handle)
            raise
        return child

    def create_dir(self, path: Path) -> CheckedPath:
        self._create(path, directory=True)
        return self.capture(path, directory=True)

    def ensure_dir(self, path: Path) -> CheckedPath:
        proof = self.capture(path, allow_missing=True, directory=True)
        if proof.exists:
            return proof
        for component in [*reversed(path.parents), path]:
            if self._key(component) not in self._nodes:
                self.create_dir(component)
        return self.capture(path, directory=True)

    def _write_verified(
        self,
        node: _SessionNode,
        chunks: Iterable[bytes],
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> CheckedPath:
        # Internal callers supply a bounded bytes iterator. No external callback runs here.
        try:
            total = 0
            digest = hashlib.sha256()
            for chunk in chunks:
                if len(chunk) > 65536:  # noqa: PLR2004
                    raise PathBoundaryError("Invalid bounded write chunk")  # noqa: TRY301
                total += len(chunk)
                if total > expected_size:
                    raise PathBoundaryError("Copy exceeds expected byte size")  # noqa: TRY301
                self._native.write_chunk(node.handle, chunk)
                digest.update(chunk)
            if total != expected_size or digest.hexdigest() != expected_sha256:
                raise PathBoundaryError("Copy byte size or digest mismatch")  # noqa: TRY301
            self._native.flush(node.handle)
            after = self._validate_node(node, stamps=False)
            if after.size != expected_size:
                raise PathBoundaryError("Owned file size mismatch")  # noqa: TRY301
            node.identity = after
            self._sync()
            self.revalidate()
            self._native.seek_start(node.handle)
            digest = hashlib.sha256()
            total = 0
            while chunk := self._native.read_chunk(
                node.handle, min(65536, expected_size - total + 1)
            ):
                total += len(chunk)
                if total > expected_size:
                    raise PathBoundaryError("Owned readback exceeds expected size")  # noqa: TRY301
                digest.update(chunk)
            if total != expected_size or digest.hexdigest() != expected_sha256:
                raise PathBoundaryError("Owned same-handle readback mismatch")  # noqa: TRY301
            self.revalidate()
            return self.capture(node.path, directory=False)
        except BaseException:
            self._failed = True
            raise

    def create_file(self, path: Path, data: bytes) -> CheckedPath:
        if type(data) is not bytes or len(data) > 96 * 1024 * 1024:
            raise PathBoundaryError("Invalid bounded file bytes")
        node = self._create(path, directory=False)
        return self._write_verified(
            node,
            (data[offset : offset + 65536] for offset in range(0, len(data), 65536)),
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )

    def copy_file(
        self,
        source: CheckedPath,
        path: Path,
        *,
        max_bytes: int,
        expected_size: int,
        expected_sha256: str,
    ) -> CheckedPath:
        self._require_proof(source)
        if (
            type(max_bytes) is not int
            or type(expected_size) is not int
            or not 0 <= expected_size <= max_bytes <= 4 * 1024 * 1024 * 1024
            or not re.fullmatch("[0-9a-f]{64}", expected_sha256)
        ):
            raise PathBoundaryError("Invalid bounded copy contract")
        if not source.exists or source.identity is None or source.identity.attributes & 0x10:
            raise PathBoundaryError("Copy requires a regular source file")
        if source.identity.size != expected_size:
            raise PathBoundaryError("Copy source size differs from expectation")
        handle = source._handles[-1][1]
        self._native.seek_start(handle)
        node = self._create(path, directory=False)
        return self._write_verified(
            node,
            iter(lambda: self._native.read_chunk(handle, 65536), b""),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def close(self) -> None:
        for node in reversed(list(self._nodes.values())):
            with suppress(BaseException):
                self._native.close(node.handle)
        self._nodes.clear()
        self._closed = True
        for proof in self._proofs:
            proof._closed = True

    def __enter__(self) -> Self:
        try:
            self.revalidate()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
