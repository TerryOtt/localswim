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
