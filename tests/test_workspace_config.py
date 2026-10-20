"""Gateway configuration durability, validation, and transaction isolation."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from localswim import workspace_config

if TYPE_CHECKING:
    import pathlib

    from localswim import board_state

KEYS = {"alpha/board", "beta", "gamma"}


def test_defaults_durable_autopush_and_immutable_snapshot(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    assert store.snapshot().to_json() == {
        "schema": 1,
        "revision": 0,
        "autopush": False,
        "pinsInitialized": False,
        "pinnedBoards": [],
    }
    enabled = workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    assert enabled.snapshot().autopush
    assert enabled.snapshot().revision == 1
    reopened = workspace_config.GatewayConfigStore(tmp_path)
    assert reopened.snapshot() == enabled.snapshot()
    document = reopened.snapshot().to_json()
    document["pinnedBoards"] = ["beta"]
    assert reopened.snapshot().pinned_boards == ()


def test_order_idempotence_cas_and_absent_pins(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    snapshot = store.apply(0, {"action": "pin", "key": "alpha/board"}, KEYS)
    snapshot = store.apply(snapshot.revision, {"action": "pin", "key": "beta"}, KEYS)
    snapshot = store.apply(snapshot.revision, {"action": "pin", "key": "beta"}, KEYS)
    assert snapshot.revision == 2
    assert store.snapshot().pinned_boards == ("alpha/board", "beta")
    snapshot = store.apply(
        2, {"action": "move", "key": "beta", "target": "alpha/board", "after": False}, KEYS
    )
    assert snapshot.pinned_boards == ("beta", "alpha/board")
    assert (
        store.apply(
            3, {"action": "move", "key": "beta", "target": "alpha/board", "after": False}, KEYS
        ).revision
        == 3
    )
    assert store.snapshot().pinned_boards == ("beta", "alpha/board")
    with pytest.raises(workspace_config.ConfigRevisionConflict):
        store.apply(0, {"action": "unpin", "key": "beta"}, KEYS)
    snapshot = store.apply(3, {"action": "unpin", "key": "alpha/board"}, {"beta"})
    assert snapshot.pinned_boards == ("beta",)
    assert snapshot.autopush
    assert workspace_config.GatewayConfigStore(tmp_path).snapshot() == snapshot


def test_legacy_initialization_only_first_nonempty_and_explicit_empty(
    tmp_path: pathlib.Path,
) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    assert not store.apply(0, {"action": "initialize", "pinnedBoards": []}, KEYS).pins_initialized
    first = store.apply(0, {"action": "initialize", "pinnedBoards": ["beta", "alpha/board"]}, KEYS)
    assert first.pinned_boards == ("beta", "alpha/board")
    assert store.apply(1, {"action": "initialize", "pinnedBoards": ["gamma"]}, KEYS) == first
    snapshot = store.apply(1, {"action": "unpin", "key": "beta"}, KEYS)
    snapshot = store.apply(snapshot.revision, {"action": "unpin", "key": "alpha/board"}, KEYS)
    assert snapshot.pins_initialized
    assert not snapshot.pinned_boards
    assert (
        store.apply(snapshot.revision, {"action": "initialize", "pinnedBoards": ["gamma"]}, KEYS)
        == snapshot
    )


def test_explicit_unpin_marks_empty_preference_initialized(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    result = store.apply(0, {"action": "unpin", "key": "beta"}, KEYS)
    assert result.pins_initialized
    assert result.revision == 1
    assert store.apply(1, {"action": "unpin", "key": "beta"}, KEYS) == result


@pytest.mark.parametrize(
    "change",
    [
        {"schema": True},
        {"schema": 2},
        {"revision": True},
        {"revision": -1},
        {"autopush": 1},
        {"pinsInitialized": 0},
        {"extra": "field"},
        {"pinnedBoards": ["beta", "beta"]},
        {"pinnedBoards": ["../beta"]},
        {"pinnedBoards": ["beta"]},
        {"pinnedBoards": "beta"},
    ],
)
def test_invalid_file_never_reset(tmp_path: pathlib.Path, change: board_state.JsonObject) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    document = store.snapshot().to_json()
    document.update(change)
    original = json.dumps(document)
    store.path.write_text(original, encoding="utf-8")
    with pytest.raises(workspace_config.GatewayConfigError):
        workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    assert store.path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("raw", ['{"schema":1,"schema":1}', "{", "[]"])
def test_invalid_json_never_reset(tmp_path: pathlib.Path, raw: str) -> None:
    path = tmp_path / workspace_config.CONFIG_FILENAME
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(workspace_config.GatewayConfigError):
        workspace_config.GatewayConfigStore(tmp_path)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "command",
    [
        {"action": "pin", "key": "../beta"},
        {"action": "pin", "key": "unknown"},
        {"action": "pin", "key": "beta", "extra": True},
        {"action": "erase"},
        {"action": "initialize", "pinnedBoards": ["unknown"]},
        {"action": "move", "key": "beta", "target": "gamma", "after": 1},
        {"action": "unpin", "key": "unknown"},
    ],
)
def test_invalid_command_preserves_bytes(
    tmp_path: pathlib.Path, command: board_state.JsonObject
) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    original = store.path.read_bytes()
    with pytest.raises(workspace_config.GatewayConfigError):
        store.apply(0, command, KEYS)
    assert store.path.read_bytes() == original


def test_multiple_store_instances_compare_and_swap(tmp_path: pathlib.Path) -> None:
    first = workspace_config.GatewayConfigStore(tmp_path)
    second = workspace_config.GatewayConfigStore(tmp_path)

    def pin(store: workspace_config.GatewayConfigStore, key: str) -> bool:
        try:
            store.apply(0, {"action": "pin", "key": key}, KEYS)
        except workspace_config.ConfigRevisionConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pin, (first, second), ("beta", "gamma")))
    assert sorted(results) == [False, True]
    assert first.snapshot().revision == 1
    assert len(first.snapshot().pinned_boards) == 1


def test_nested_lock_and_other_process_exclusion(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    source = (
        "import pathlib,sys\n"
        "from localswim import workspace_config as config\n"
        "config.LOCK_WAIT_SECONDS=0.1\n"
        "try:\n config.GatewayConfigStore(pathlib.Path(sys.argv[1]))\n"
        "except TimeoutError:\n print('locked')\n"
    )
    with store.locked():
        with store.locked():
            assert store.snapshot().revision == 0
        result = subprocess.run(
            [sys.executable, "-c", source, str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    assert result.stdout.strip() == "locked"


def test_atomic_failure_retains_existing_and_cleans_temp(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    original = store.path.read_bytes()

    def fail(_descriptor: int) -> None:
        raise OSError("simulated flush failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="simulated"):
        store.apply(0, {"action": "pin", "key": "beta"}, KEYS)
    assert store.path.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        f".{workspace_config.CONFIG_FILENAME}.lock",
        workspace_config.CONFIG_FILENAME,
    ]


def test_replace_failure_preserves_preferences_and_next_write_recovers(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    previous = store.apply(0, {"action": "pin", "key": "beta"}, KEYS)
    original = store.path.read_bytes()
    expected = workspace_config.GatewayPreferences(
        revision=previous.revision + 1,
        autopush=True,
        pins_initialized=True,
        pinned_boards=("beta", "gamma"),
    )
    prepared: list[pathlib.Path] = []

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        temporary = tmp_path / os.fspath(source)
        assert tmp_path / os.fspath(target) == store.path
        assert temporary.parent == tmp_path
        assert temporary.name.startswith(f".{workspace_config.CONFIG_FILENAME}.")
        assert temporary != store.path
        assert json.loads(temporary.read_bytes()) == expected.to_json()
        prepared.append(temporary)
        raise OSError("simulated final replacement failure")

    with monkeypatch.context() as failure:
        failure.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="simulated final replacement failure"):
            store.apply(previous.revision, {"action": "pin", "key": "gamma"}, KEYS)

    assert len(prepared) == 1
    assert store.path.read_bytes() == original
    assert store.snapshot() == previous
    assert workspace_config.GatewayConfigStore(tmp_path).snapshot() == previous
    assert not prepared[0].exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        f".{workspace_config.CONFIG_FILENAME}.lock",
        workspace_config.CONFIG_FILENAME,
    ]

    recovered = store.apply(previous.revision, {"action": "pin", "key": "gamma"}, KEYS)
    assert recovered == expected
    assert store.snapshot() == expected
    assert store.path.read_bytes() != original
    assert workspace_config.GatewayConfigStore(tmp_path).snapshot() == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        f".{workspace_config.CONFIG_FILENAME}.lock",
        workspace_config.CONFIG_FILENAME,
    ]


def test_symlink_configuration_rejected_without_touching_target(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    path = tmp_path / workspace_config.CONFIG_FILENAME
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links require local Windows privileges")
    with pytest.raises(workspace_config.GatewayConfigError):
        workspace_config.GatewayConfigStore(tmp_path)
    assert target.read_text(encoding="utf-8") == "private"


def test_move_after_self_and_missing_board_retention(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    current = store.apply(
        0, {"action": "initialize", "pinnedBoards": ["beta", "alpha/board", "gamma"]}, KEYS
    )
    current = store.apply(
        current.revision,
        {"action": "move", "key": "beta", "target": "gamma", "after": True},
        {"beta", "gamma"},
    )
    assert current.pinned_boards == ("alpha/board", "gamma", "beta")
    assert (
        store.apply(
            current.revision,
            {"action": "move", "key": "beta", "target": "beta", "after": True},
            {"beta", "gamma"},
        )
        == current
    )
    assert store.snapshot().pinned_boards == current.pinned_boards


def test_reject_oversized_file_and_command_without_reset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path)
    original = store.path.read_bytes()
    monkeypatch.setattr(workspace_config, "MAX_CONFIG_BYTES", len(original))
    with pytest.raises(workspace_config.GatewayConfigError, match="size limit"):
        store.apply(0, {"action": "pin", "key": "alpha/board"}, KEYS)
    assert store.path.read_bytes() == original
    store.path.write_bytes(original + b" " * 100)
    with pytest.raises(workspace_config.GatewayConfigError, match="size limit"):
        store.snapshot()


def test_invalid_revision_and_board_snapshot_untouched(tmp_path: pathlib.Path) -> None:
    board = tmp_path / "beta.json"
    board.write_text('{"board":"sentinel"}', encoding="utf-8")
    original = board.read_bytes()
    store = workspace_config.GatewayConfigStore(tmp_path)
    for revision in (True, -1):
        with pytest.raises(workspace_config.GatewayConfigError, match="revision"):
            store.apply(revision, {"action": "pin", "key": "beta"}, KEYS)
    store.apply(0, {"action": "pin", "key": "beta"}, KEYS)
    assert board.read_bytes() == original


def test_directory_configuration_rejected(tmp_path: pathlib.Path) -> None:
    (tmp_path / workspace_config.CONFIG_FILENAME).mkdir()
    with pytest.raises(workspace_config.GatewayConfigError, match="regular file"):
        workspace_config.GatewayConfigStore(tmp_path)


def test_deep_json_preserves_bytes_and_recovers_after_repair(tmp_path: pathlib.Path) -> None:
    store = workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    valid = store.path.read_bytes()
    depth = sys.getrecursionlimit() * 2
    malformed = ("[" * depth + "0" + "]" * depth).encode("utf-8")
    assert len(malformed) < workspace_config.MAX_CONFIG_BYTES
    store.path.write_bytes(malformed)
    with pytest.raises(workspace_config.GatewayConfigError, match="invalid gateway configuration"):
        store.snapshot()
    with pytest.raises(workspace_config.GatewayConfigError, match="invalid gateway configuration"):
        workspace_config.GatewayConfigStore(tmp_path, autopush=True)
    assert store.path.read_bytes() == malformed
    store.path.write_bytes(valid)
    assert store.snapshot().autopush
    result = store.apply(0, {"action": "pin", "key": "beta"}, KEYS)
    assert result.pinned_boards == ("beta",)
    assert result.revision == 1
