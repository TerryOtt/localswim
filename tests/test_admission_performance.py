"""Exact-byte live admission reuse without weakening disk-health observations."""

import os
import pathlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from localswim import api_endpoint, board_state
from tests.test_store import create_alpha, make_store


def test_large_unchanged_status_reads_skip_parse_audit_and_copy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, create_alpha)
    size = 20 * 1024 * 1024
    store.execute(1, lambda board: board.set_detail("alpha", "x" * size, "bot"))
    store.disk_status()
    assert path.stat().st_size >= size
    reads = 0
    original_read = pathlib.Path.read_bytes

    def counted_read(target: pathlib.Path) -> bytes:
        nonlocal reads
        if target == path:
            reads += 1
        return original_read(target)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unchanged health poll repeated parsing, audit replay or deepcopy")

    def poll(_index: int) -> tuple[str | None, float, str, float | None, str | None, int]:
        return store.disk_status()

    with monkeypatch.context() as warm:
        warm.setattr(pathlib.Path, "read_bytes", counted_read)
        warm.setattr(board_state, "load_bytes", forbidden)
        warm.setattr(board_state.Board, "verify", forbidden)
        warm.setattr(api_endpoint.copy, "deepcopy", forbidden)
        with ThreadPoolExecutor(max_workers=8) as pool:
            reports = list(pool.map(poll, range(32)))
    assert reads == 32
    assert all(report[3] is not None for report in reports)
    before = store.snapshot()
    original_version = api_endpoint.card_version(before, before.find("alpha"))
    metadata: dict[str, str] = {}
    result = api_endpoint.execute_http_command(
        store,
        2,
        ["cards", "alpha", "comment"],
        {"text": "Large-board edit"},
        "bot",
        expected_card=original_version,
        response_metadata=metadata,
    )
    assert result[1] == 3
    assert metadata["cardVersion"] != original_version
    with pytest.raises(api_endpoint.CardRevisionConflict):
        api_endpoint.execute_http_command(
            store,
            3,
            ["cards", "alpha", "comment"],
            {"text": "Stale intent"},
            "bot",
            expected_card=original_version,
        )
    final = store.snapshot()
    assert final.revision == 3
    assert len(final.find("alpha").comments) == 1
    assert len(final.find("alpha").detail) == size


def test_timestamp_preserving_change_refusal_and_restoration(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    original = path.read_bytes()
    stamp = path.stat()
    read_at = store.read_at
    damaged = original.replace(b'"schema": 4', b'"schema": 0')
    assert damaged != original and len(damaged) == len(original)
    path.write_bytes(damaged)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(board_state.BoardError):
        store.disk_status()
    assert store.read_at == read_at
    assert store._board.project == "Store"  # noqa: SLF001 -- retained accepted memory
    path.write_bytes(original)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert store.snapshot().project == "Store"
    assert store.admission_error is None


@pytest.mark.parametrize("invalid", [b"\xff", b'{"schema":4,"schema":4}', b"[" * 2000])
def test_invalid_byte_admission_retains_health_and_recovers(
    tmp_path: pathlib.Path, invalid: bytes
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    accepted = path.read_bytes()
    prior_read = store.read_at
    path.write_bytes(invalid)
    with pytest.raises(board_state.BoardError, match="disk admission failed"):
        store.disk_status()
    assert store.read_at == prior_read
    path.write_bytes(accepted)
    assert store.snapshot().project == "Store"


def test_command_failure_and_success_do_not_rebind_old_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    original = path.read_bytes()
    store.execute(0, create_alpha)
    assert store.snapshot().revision == 1
    path.write_bytes(original)
    restored = store.snapshot()
    assert restored.revision == 0
    assert restored.items == []
    snapshot = store.snapshot()
    snapshot.project = "Caller mutation"
    snapshot.policy.to_json().clear()
    assert store.snapshot().project == "Store"
    original_save = board_state.save

    def uncertain_save(
        board: board_state.Board,
        target: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        original_save(board, target, outcome=outcome)
        raise OSError("uncertain replacement")

    monkeypatch.setattr(board_state, "save", uncertain_save)
    with pytest.raises(OSError, match="uncertain replacement"):
        store.execute(0, create_alpha)
    assert store.snapshot().revision == 1


def test_external_policy_identity_requires_readmission(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    policy = make_store(path).policy
    store = api_endpoint.BoardStore(path, policy)
    parses = 0
    original = board_state.load_bytes

    def counted(
        data: bytes, target: pathlib.Path, active: board_state.TransitionPolicy | None = None
    ) -> board_state.Board:
        nonlocal parses
        parses += 1
        return original(data, target, active)

    monkeypatch.setattr(board_state, "load_bytes", counted)
    store.policy = board_state.TransitionPolicy.from_json(policy.to_json(), "second policy")
    store.snapshot()
    store.snapshot()
    assert parses == 1


def test_card_version_tracks_incident_history_without_unrelated_changes(
    tmp_path: pathlib.Path,
) -> None:
    store = make_store(tmp_path / "board.json")
    board = store.snapshot()
    for identity in ("target", "peer", "unrelated"):
        board.create(identity, identity, "backlog", "bot")
    target = board.find("target")
    peer = board.find("peer")
    original = api_endpoint.card_version(board, target)
    peer_original = api_endpoint.card_version(board, peer)
    board.comment("unrelated", "Independent edit", "bot")
    assert api_endpoint.card_version(board, target) == original
    board.link("target", "references", "peer", "bot")
    assert api_endpoint.card_version(board, target) != original
    assert api_endpoint.card_version(board, peer) != peer_original
    board.unlink("target", "references", "peer", "bot")
    assert api_endpoint.card_version(board, target) != original
    parent_before = api_endpoint.card_version(board, target)
    child_before = api_endpoint.card_version(board, peer)
    board.set_parent("peer", "target", "bot")
    assert api_endpoint.card_version(board, target) != parent_before
    assert api_endpoint.card_version(board, peer) != child_before
    board.set_parent("peer", None, "bot")
    assert api_endpoint.card_version(board, target) != parent_before


def test_response_card_version_belongs_to_committed_revision(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json")
    store.execute(0, create_alpha)
    monkeypatch.setattr(board_state, "now", lambda: "2026-11-07T12:00:00Z")
    expected = store.snapshot()
    expected.comment("alpha", "First edit", "bot")
    expected_version = api_endpoint.card_version(expected, expected.find("alpha"))
    original_publish = store._publish_candidate  # noqa: SLF001 -- deterministic interleaving

    def interleaved_publish(committed: board_state.Board) -> None:
        original_publish(committed)
        if committed.revision == 2:
            store.execute(2, lambda board: board.comment("alpha", "Later edit", "bot"))

    monkeypatch.setattr(store, "_publish_candidate", interleaved_publish)
    response = api_endpoint._http_command_response(  # noqa: SLF001 -- response transaction contract
        store, 1, ["cards", "alpha", "comment"], {"text": "First edit"}, "bot"
    )
    assert response["revision"] == 2
    assert response["cardVersion"] == expected_version
    latest = store.snapshot()
    assert latest.revision == 3
    assert response["cardVersion"] != api_endpoint.card_version(latest, latest.find("alpha"))
