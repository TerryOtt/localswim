"""Deterministic work counts for metadata-bound workspace discovery caches."""

# ruff: noqa: SLF001 -- exercise cache and discovery boundaries deterministically

import json
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from localswim import board_state, workspace
from tests.test_workspace import make_board


@pytest.mark.parametrize("mebibytes", [2, 20])
def test_many_large_boards_reuse_parses_across_sidebar_intervals(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, mebibytes: int
) -> None:
    paths = [tmp_path / f"board-{index:02}.json" for index in range(12)]
    for path in paths:
        make_board(path, path.stem)
        # JSON whitespace reaches a representative snapshot size without making
        # synthetic history validation dominate this deterministic work test.
        with path.open("ab") as stream:
            stream.write(b" " * (mebibytes * 1024 * 1024 - path.stat().st_size))
    instance = workspace.Workspace(tmp_path)
    reads: list[pathlib.Path] = []
    loads: list[pathlib.Path] = []
    original_read, original_load = workspace._read_candidate, board_state.load
    clock = [100.0]
    monkeypatch.setattr(workspace.time, "monotonic", lambda: clock[0])

    def read(path: pathlib.Path) -> workspace._Candidate | None:
        reads.append(path)
        return original_read(path)

    def load(path: pathlib.Path, *, install_context: bool = True) -> board_state.Board:
        loads.append(path)
        return original_load(path, install_context=install_context)

    monkeypatch.setattr(workspace, "_read_candidate", read)
    monkeypatch.setattr(board_state, "load", load)
    expected = instance.catalog()["boards"]

    def catalog(_: int) -> board_state.JsonValue:
        return instance.catalog()["boards"]

    for _ in range(3):
        clock[0] += 10
        with ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(catalog, range(8)))
        assert snapshots == [expected] * 8
    assert sorted(reads) == paths
    assert sorted(loads) == paths
    # A selected route never walks or recognizes the other eleven snapshots.
    monkeypatch.setattr(instance, "_paths", lambda: pytest.fail("selected route scanned root"))
    endpoint = workspace.Endpoint(12345, "synthetic-browser-capability", "")

    def reuse(_: workspace._Candidate) -> workspace.Endpoint:
        return endpoint

    monkeypatch.setattr(instance, "_endpoint_under_gate", reuse)
    for _ in range(4):
        assert instance.resolve_endpoint("board-00") == (paths[0], endpoint)
    assert len(reads) == len(loads) == len(paths)


def test_preserved_metadata_edit_refreshes_summary_and_invalid_recognition(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(workspace.time, "monotonic", lambda: clock[0])
    assert instance.catalog()["boards"][0]["project"] == "Before"  # type: ignore[index]
    identity = path.stat()
    blob = path.read_bytes().replace(b'"Before"', b'"After!"')
    path.write_bytes(blob)
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    clock[0] += 10
    assert instance.catalog()["boards"][0]["project"] == "After!"  # type: ignore[index]
    # Same length, same restored mtime, now an unrecognized schema.
    blob = blob.replace(b'"schema": 4', b'"schema": 1', 1)
    path.write_bytes(blob)
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    clock[0] += 10
    assert instance.catalog()["boards"] == []
    make_board(path, "Restored")
    assert instance.catalog()["boards"][0]["project"] == "Restored"  # type: ignore[index]
    path.unlink()
    assert instance.catalog()["boards"] == []
    make_board(tmp_path / "added.json", "New")
    assert instance.catalog()["boards"][0]["project"] == "New"  # type: ignore[index]


def test_cached_catalog_is_detached_and_recognized_errors_recover(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Valid")
    instance = workspace.Workspace(tmp_path)
    first: Any = instance.catalog()
    first["boards"][0]["counts"]["review"] = 1000
    assert instance.catalog()["boards"][0]["counts"]["review"] == 0  # type: ignore[index]
    raw = json.loads(path.read_text())
    raw["policy"] = {}
    path.write_text(json.dumps(raw))
    assert "error" in instance.catalog()["boards"][0]  # type: ignore[operator,index]
    make_board(path, "Recovered")
    assert "error" not in instance.catalog()["boards"][0]  # type: ignore[operator,index]


@pytest.mark.parametrize("key", ["../board", ".private/board", "attachments/board", "a\\board"])
def test_cached_recognition_never_grants_unsafe_key(tmp_path: pathlib.Path, key: str) -> None:
    make_board(tmp_path / "board.json", "Valid")
    instance = workspace.Workspace(tmp_path)
    instance.catalog()
    with pytest.raises(ValueError, match="unknown workspace board"):
        instance.board_path(key)


def test_uppercase_json_suffix_retains_discovered_key(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.JSON"
    make_board(path, "Valid")
    instance = workspace.Workspace(tmp_path)
    assert instance.board_path("board") == path.resolve()


def test_session_cache_requires_fresh_matching_descriptor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Valid")
    instance = workspace.Workspace(tmp_path)
    metadata: board_state.JsonObject = {"port": 12345, "token": "first-process-cli-token"}
    calls: list[int] = []

    def descriptor(_: pathlib.Path) -> board_state.JsonObject:
        return dict(metadata)

    def session(_: pathlib.Path, port: int) -> workspace.Endpoint:
        calls.append(port)
        return workspace.Endpoint(port, str(metadata["token"]) + "-browser", "")

    monkeypatch.setattr(workspace.service_ownership, "service_descriptor", descriptor)
    monkeypatch.setattr(instance, "_browser_endpoint", session)

    def busy(_: pathlib.Path) -> bool:
        return True

    monkeypatch.setattr(workspace.service_ownership, "writer_busy", busy)
    first = instance._existing(path)
    assert instance._existing(path) == first
    assert calls == [12345]
    metadata["token"] = "replacement-process-cli-token"  # noqa: S105 -- synthetic identity
    second = instance._existing(path)
    assert second != first
    assert calls == [12345, 12345]

    def missing(_: pathlib.Path) -> board_state.JsonObject:
        raise OSError("descriptor missing during replacement")

    monkeypatch.setattr(workspace.service_ownership, "service_descriptor", missing)
    assert instance._existing(path) is None
    assert path not in instance._sessions


def test_attachment_named_board_is_not_attachment_directory(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "attachments.json"
    make_board(path, "Ordinary board")
    assert workspace.Workspace(tmp_path).board_path("attachments") == path.resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
def test_case_alias_cannot_enter_excluded_attachment_directory(tmp_path: pathlib.Path) -> None:
    make_board(tmp_path / "attachments" / "board.json", "Excluded")
    with pytest.raises(ValueError, match="unknown workspace board"):
        workspace.Workspace(tmp_path).board_path("ATTACHMENTS/board")


def test_unchanged_foreign_json_is_not_reparsed_each_sidebar_interval(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "foreign.json"
    path.write_bytes(b"{}" + b" " * (2 * 1024 * 1024))
    instance = workspace.Workspace(tmp_path)
    clock = [100.0]
    reads: list[pathlib.Path] = []
    original = workspace._read_candidate

    def read(target: pathlib.Path) -> workspace._Candidate | None:
        reads.append(target)
        return original(target)

    monkeypatch.setattr(workspace.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(workspace, "_read_candidate", read)
    for _ in range(4):
        assert instance.catalog()["boards"] == []
        clock[0] += 10
    assert reads == [path]
