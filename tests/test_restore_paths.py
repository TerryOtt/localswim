"""Native Windows boundary proofs, using only the configured scratch directory."""

import ctypes
import hashlib
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from localswim import restore_paths
from localswim.restore_paths import CheckedPath, PathBoundaryError, PathIdentity, check_role_paths

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


def test_unsupported_platform_refuses_before_native_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_native() -> None:
        pytest.fail("Unsupported platform must not initialize Windows APIs")

    monkeypatch.setattr(restore_paths.os, "name", "posix")
    monkeypatch.setattr(restore_paths, "_Windows", unexpected_native)
    with pytest.raises(PathBoundaryError, match="require Windows"):
        CheckedPath.capture(tmp_path, allow_missing=True)


def test_bounded_sealed_read_and_closed_proof(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_bytes(b"abc")
    with CheckedPath.capture(source) as proof:
        assert proof.exists
        assert proof.read_bytes(3) == b"abc"
        assert proof.sealed_digest(3) == hashlib.sha256(b"abc").hexdigest()
        with pytest.raises(PathBoundaryError, match="exceeds"):
            proof.read_bytes(2)
    with pytest.raises(PathBoundaryError, match="closed"):
        proof.revalidate()


@pytest.mark.parametrize("suffix", ["..\\escape", "file:stream", "NUL", "tail.", "tail "])
def test_ambiguous_spelling_refuses(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(PathBoundaryError):
        CheckedPath.capture(tmp_path / suffix, allow_missing=True)


def test_directory_is_not_a_readable_file(tmp_path: Path) -> None:
    with (
        CheckedPath.capture(tmp_path) as proof,
        pytest.raises(PathBoundaryError, match="regular"),
    ):
        proof.read_bytes(100)


def test_hardlinks_refuse(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"bytes")
    alias = tmp_path / "alias"
    os.link(original, alias)
    for path in (original, alias):
        with pytest.raises(PathBoundaryError, match="linked"):
            CheckedPath.capture(path)


def test_actual_inside_tree_junction_refuses(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (target / "bytes").write_bytes(b"bytes")
    junction = tmp_path / "alias"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    try:
        with pytest.raises(PathBoundaryError, match="Reparse"):
            CheckedPath.capture(junction / "bytes")
    finally:
        junction.rmdir()


def test_leaf_and_ancestor_replacement_are_pinned(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    leaf = parent / "snapshot"
    leaf.write_bytes(b"original")
    with CheckedPath.capture(leaf) as proof:
        with pytest.raises(PermissionError):
            leaf.rename(parent / "moved")
        with pytest.raises(PermissionError):
            parent.rename(tmp_path / "moved_parent")
        with pytest.raises(PermissionError):
            leaf.write_bytes(b"changed")
        assert proof.read_bytes(8) == b"original"
    leaf.rename(parent / "moved")


def test_deterministic_component_identity_replacement_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = tmp_path / "snapshot"
    leaf.write_bytes(b"original")
    with CheckedPath.capture(leaf) as proof:
        original = proof._native.information  # noqa: SLF001
        ancestor_handle = proof._handles[-2][1]  # noqa: SLF001

        def replaced(handle: int) -> PathIdentity:
            info = original(handle)
            return replace(info, index=info.index + 1) if handle == ancestor_handle else info

        monkeypatch.setattr(proof._native, "information", replaced)  # noqa: SLF001
        with pytest.raises(PathBoundaryError, match="identity"):
            proof.read_bytes(8)


def test_post_read_mutation_stamp_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = tmp_path / "snapshot"
    leaf.write_bytes(b"original")
    with CheckedPath.capture(leaf) as proof:
        original = proof._native.information  # noqa: SLF001
        leaf_handle = proof._handles[-1][1]  # noqa: SLF001
        leaf_checks = 0

        def drifted(handle: int) -> PathIdentity:
            nonlocal leaf_checks
            info = original(handle)
            if handle == leaf_handle:
                leaf_checks += 1
                if leaf_checks > 1:
                    return replace(info, modified=info.modified + 1)
            return info

        monkeypatch.setattr(proof._native, "information", drifted)  # noqa: SLF001
        with pytest.raises(PathBoundaryError, match="identity"):
            proof.read_bytes(8)


def test_missing_suffix_rechecks_and_role_overlap(tmp_path: Path) -> None:
    with CheckedPath.capture(tmp_path / "future" / "stage", allow_missing=True) as future:
        assert not future.exists
        future.revalidate()
        (tmp_path / "future").mkdir()
        with pytest.raises(PathBoundaryError, match=r"now exists|identity"):
            future.revalidate()
    with (
        CheckedPath.capture(tmp_path / "one", allow_missing=True) as one,
        CheckedPath.capture(tmp_path / "two", allow_missing=True) as two,
        CheckedPath.capture(tmp_path / "one" / "stage", allow_missing=True) as nested,
    ):
        check_role_paths(source=one, target=two)
        with pytest.raises(PathBoundaryError, match="overlap"):
            check_role_paths(source=one, staging=nested)


def test_short_name_alias_refuses_when_available(tmp_path: Path) -> None:
    path = tmp_path / "long filename with spaces.snapshot"
    path.write_bytes(b"bytes")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GetShortPathNameW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    buffer = ctypes.create_unicode_buffer(32768)
    assert dll.GetShortPathNameW(str(path), buffer, len(buffer))
    if buffer.value.lower() == str(path).lower():
        pytest.skip("8.3 aliases are disabled on scratch volume")
    with pytest.raises(PathBoundaryError, match="Aliased"):
        CheckedPath.capture(Path(buffer.value))


def test_failed_context_entry_closes_all_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"original")
    proof = CheckedPath.capture(source)

    def cancelled() -> None:
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(proof, "revalidate", cancelled)
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        proof.__enter__()
    source.write_bytes(b"released")
    source.rename(tmp_path / "renamed")


def test_capture_failure_closes_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"original")
    native = restore_paths._Windows()  # noqa: SLF001
    original = native.information
    calls = 0

    def cancelled(handle: int) -> PathIdentity:
        nonlocal calls
        calls += 1
        if calls == len(source.parents) + 1:
            raise KeyboardInterrupt("capture cancelled")
        return original(handle)

    monkeypatch.setattr(native, "information", cancelled)
    monkeypatch.setattr(restore_paths, "_Windows", lambda: native)
    with pytest.raises(KeyboardInterrupt, match="capture cancelled"):
        CheckedPath.capture(source)
    source.write_bytes(b"released")
    tmp_path.rename(tmp_path.with_name(tmp_path.name + "_released"))


def test_creation_session_owned_nested_sibling_transitions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    with restore_paths.CreationSession() as session:
        original = session.capture(source, directory=False)
        stage = session.capture(tmp_path / "stage", allow_missing=True, directory=True)
        checkpoint = session.capture(tmp_path / "checkpoint", allow_missing=True, directory=True)
        check_role_paths(source=original, staging=stage, checkpoint=checkpoint)
        session.create_dir(stage.path)
        assert stage.exists
        assert stage.identity is not None
        session.create_dir(checkpoint.path)
        nested = session.ensure_dir(stage.path / "attachments" / "card")
        created = session.create_file(nested.path / "bytes", b"candidate")
        assert created.read_bytes(9) == b"candidate"
        assert original.read_bytes(6) == b"source"
        session.create_file(checkpoint.path / "complete", b"")
        session.revalidate()
    assert source.read_bytes() == b"source"


def test_native_created_file_and_directory_stay_pinned(tmp_path: Path) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        directory = session.create_dir(tmp_path / "owned")
        file = session.create_file(directory.path / "bytes", b"owned")
        with pytest.raises(PermissionError):
            file.path.rename(directory.path / "moved")
        with pytest.raises(PermissionError):
            file.path.write_bytes(b"external")
        with pytest.raises(PermissionError):
            directory.path.rename(tmp_path / "moved")
        assert file.read_bytes(5) == b"owned"
    file.path.write_bytes(b"released")
    directory.path.rename(tmp_path / "released")
    with pytest.raises(PathBoundaryError, match="closed"):
        file.revalidate()


def test_created_ownership_never_reopens_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        original_open = session._native.open  # noqa: SLF001
        created = tmp_path / "owned"

        def forbid_reopen(path: Path) -> int:
            assert path != created
            return original_open(path)

        monkeypatch.setattr(session._native, "open", forbid_reopen)  # noqa: SLF001
        proof = session.create_file(created, b"same handle")
        assert proof.read_bytes(11) == b"same handle"


@pytest.mark.parametrize("directory", [False, True])
def test_exclusive_creation_preserves_unregistered_existing_destination(
    tmp_path: Path,
    *,
    directory: bool,
) -> None:
    existing = tmp_path / "existing"
    if directory:
        existing.mkdir()
    else:
        existing.write_bytes(b"existing")
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        if directory:
            with pytest.raises(PathBoundaryError, match="Exclusive"):
                session.create_dir(existing)
        else:
            with pytest.raises(PathBoundaryError, match="Exclusive"):
                session.create_file(existing, b"replacement")
    assert existing.is_dir() if directory else existing.read_bytes() == b"existing"


def test_session_refuses_unknown_missing_adoption(tmp_path: Path) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path / "unknown", allow_missing=True)
        (tmp_path / "unknown").mkdir()
        with pytest.raises(PathBoundaryError, match=r"identity|Unowned"):
            session.create_dir(tmp_path / "other")
    assert not (tmp_path / "other").exists()


def test_owned_creation_refuses_unrelated_ancestor_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.ensure_dir(tmp_path / "private")
        native = session._native  # noqa: SLF001
        original_information = native.information
        ancestor = session._nodes[session._key(tmp_path)].handle  # noqa: SLF001
        original_create = native.create_child
        created = False

        def create(parent: int, name: str, *, directory: bool) -> int:
            nonlocal created
            handle = original_create(parent, name, directory=directory)
            created = True
            return handle

        def changed(handle: int) -> PathIdentity:
            info = original_information(handle)
            return (
                replace(info, modified=info.modified + 1)
                if created and handle == ancestor
                else info
            )

        monkeypatch.setattr(native, "create_child", create)
        monkeypatch.setattr(native, "information", changed)
        with pytest.raises(PathBoundaryError, match="identity"):
            session.create_file(tmp_path / "private" / "partial", b"never written")
    assert (tmp_path / "private" / "partial").read_bytes() == b""


@pytest.mark.parametrize("phase", ["write", "flush", "readback", "interrupt"])
def test_owned_io_failure_closes_without_deleting_partial_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    session = restore_paths.CreationSession()
    session.capture(tmp_path, directory=True)
    native = session._native  # noqa: SLF001

    def write_failed(handle: int, data: bytes) -> None:
        original_write(handle, data)
        if phase == "interrupt":
            raise KeyboardInterrupt("injected cancellation")
        raise PathBoundaryError("injected write failure")

    def flush_failed(_handle: int) -> None:
        raise PathBoundaryError("injected flush failure")

    def read_failed(_handle: int, _limit: int) -> bytes:
        return b"wrong"

    original_write = native.write_chunk
    if phase in {"write", "interrupt"}:
        monkeypatch.setattr(native, "write_chunk", write_failed)
    elif phase == "flush":
        monkeypatch.setattr(native, "flush", flush_failed)
    else:
        monkeypatch.setattr(native, "read_chunk", read_failed)
    try:
        with pytest.raises((PathBoundaryError, KeyboardInterrupt)):
            session.create_file(tmp_path / "partial", b"preserved")
        with pytest.raises(PathBoundaryError, match="failed"):
            session.revalidate()
    finally:
        session.close()
    assert (tmp_path / "partial").read_bytes() == b"preserved"
    (tmp_path / "partial").rename(tmp_path / "orphan")


def test_streaming_copy_is_bounded_and_same_handle_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"chunked" * 20000
    source = tmp_path / "source"
    source.write_bytes(data)
    with restore_paths.CreationSession() as session:
        proof = session.capture(source, directory=False)
        original = session._native.read_chunk  # noqa: SLF001
        limits: list[int] = []

        def bounded(handle: int, limit: int) -> bytes:
            limits.append(limit)
            return original(handle, limit)

        monkeypatch.setattr(session._native, "read_chunk", bounded)  # noqa: SLF001
        copied = session.copy_file(
            proof,
            tmp_path / "copy",
            max_bytes=4 * 1024 * 1024 * 1024,
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )
        assert copied.read_bytes(len(data)) == data
        assert limits
        assert max(limits) <= 65536


def test_session_directory_role_and_foreign_proof_refusal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    with restore_paths.CreationSession() as session, pytest.raises(PathBoundaryError, match="type"):
        session.capture(source, directory=True)
    with (
        restore_paths.CreationSession() as session,
        CheckedPath.capture(source) as foreign,
        pytest.raises(PathBoundaryError, match="this session"),
    ):
        session.read(foreign, 6)


def test_session_hardlinks_and_junctions_refuse(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    file = target / "bytes"
    file.write_bytes(b"bytes")
    os.link(file, target / "hardlink")
    with (
        restore_paths.CreationSession() as session,
        pytest.raises(PathBoundaryError, match="linked"),
    ):
        session.capture(file)
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    try:
        with (
            restore_paths.CreationSession() as session,
            pytest.raises(
                PathBoundaryError,
                match="Reparse",
            ),
        ):
            session.capture(junction)
    finally:
        junction.rmdir()


def test_session_unsupported_platform_refuses_before_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected() -> None:
        pytest.fail("Unsupported platform initialized native creation")

    monkeypatch.setattr(restore_paths.os, "name", "posix")
    monkeypatch.setattr(restore_paths, "_CreatingWindows", unexpected)
    with pytest.raises(PathBoundaryError, match="requires Windows"):
        restore_paths.CreationSession()


def test_failed_view_context_never_closes_session_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        proof = session.create_file(tmp_path / "owned", b"owned")

        def cancelled() -> None:
            raise KeyboardInterrupt("view cancelled")

        monkeypatch.setattr(proof, "revalidate", cancelled)
        with pytest.raises(KeyboardInterrupt, match="view cancelled"):
            proof.__enter__()
        session.revalidate()
        with pytest.raises(PermissionError):
            proof.path.rename(tmp_path / "replacement")
        assert session.capture(proof.path).read_bytes(5) == b"owned"


def test_native_create_cancellation_closes_returned_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        native = session._native  # noqa: SLF001
        original = native.ntdll.NtCreateFile

        def cancelled(*arguments: object) -> int:
            original(*arguments)
            raise KeyboardInterrupt("native cancelled")

        monkeypatch.setattr(native.ntdll, "NtCreateFile", cancelled)
        with pytest.raises(KeyboardInterrupt, match="native cancelled"):
            session.create_dir(tmp_path / "orphan")
    (tmp_path / "orphan").rename(tmp_path / "released")


def test_native_short_write_refuses_and_preserves_created_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)

        def short_write(
            _handle: int,
            _buffer: object,
            _length: int,
            count: object,
            _overlapped: object,
        ) -> int:
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong)).contents.value = 0  # pyright: ignore[reportArgumentType]
            return 1

        monkeypatch.setattr(session._native.dll, "WriteFile", short_write)  # noqa: SLF001
        with pytest.raises(PathBoundaryError, match="short"):
            session.create_file(tmp_path / "partial", b"expected")
    assert (tmp_path / "partial").read_bytes() == b""


def test_owned_parent_identity_change_refuses_without_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_paths.CreationSession() as session:
        session.capture(tmp_path, directory=True)
        native = session._native  # noqa: SLF001
        parent_handle = session._nodes[session._key(tmp_path)].handle  # noqa: SLF001
        original_information = native.information
        original_create = native.create_child
        created = False

        def create(parent: int, name: str, *, directory: bool) -> int:
            nonlocal created
            handle = original_create(parent, name, directory=directory)
            created = True
            return handle

        def changed(handle: int) -> PathIdentity:
            info = original_information(handle)
            return (
                replace(info, index=info.index + 1) if created and handle == parent_handle else info
            )

        monkeypatch.setattr(native, "create_child", create)
        monkeypatch.setattr(native, "information", changed)
        with pytest.raises(PathBoundaryError, match="identity"):
            session.create_dir(tmp_path / "orphan")
    (tmp_path / "orphan").rename(tmp_path / "released")


def test_session_refuses_post_read_source_stamp_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"original")
    with restore_paths.CreationSession() as session:
        proof = session.capture(source, directory=False)
        handle = proof._handles[-1][1]  # noqa: SLF001
        original = session._native.information  # noqa: SLF001
        checks = 0

        def changed(candidate: int) -> PathIdentity:
            nonlocal checks
            info = original(candidate)
            if candidate == handle:
                checks += 1
                if checks > 1:
                    return replace(info, modified=info.modified + 1)
            return info

        monkeypatch.setattr(session._native, "information", changed)  # noqa: SLF001
        with pytest.raises(PathBoundaryError, match="identity"):
            proof.read_bytes(8)
