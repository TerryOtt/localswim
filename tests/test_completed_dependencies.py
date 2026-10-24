"""Completed history stays outside priority propagation, while links retain depth."""

import json
import socket
from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, board_state

if TYPE_CHECKING:
    import pathlib


def complete(board: board_state.Board, item_id: str) -> None:
    """Complete an existing ready card through its audited workflow."""
    board.move(item_id, "in_progress", "bot")
    board.move(item_id, "ready_for_review", "bot")
    board.move(item_id, "completed", "terry")


def reopening_policy(board: board_state.Board) -> board_state.Board:
    """Allow Terry to reopen a card without changing the default policy fixture."""
    raw = board.to_json()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    edges = policy["edges"]
    assert isinstance(edges, dict)
    terry = edges["terry"]
    assert isinstance(terry, list)
    terry.append({"from": "completed", "to": "ready_for_work"})
    return board_state.Board.from_json(raw, "reopening policy")


@pytest.mark.parametrize("relationship", ["blocks", "blocked_by"])
@pytest.mark.parametrize("archived", [False, True])
def test_completed_links_preserve_card_and_allow_unfinished_priority_edits(
    board: board_state.Board, relationship: str, *, archived: bool
) -> None:
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    board.set_archived("done", archived=archived, by="bot")
    board.create("open", "Remaining", "backlog", "bot", priority="P3")
    accepted = board.find("done").to_json()
    if relationship == "blocks":
        board.link("done", relationship, "open", "bot")
    else:
        board.link("open", relationship, "done", "bot")
    board.set_priority("open", "P5", "terry")
    assert board.find("open").priority == "P5"
    assert board.find("done").to_json() == accepted
    assert board.find_link("done", "blocks", "open") is not None
    assert board_state.blocking_order(board).depths == {"done": 1, "open": 0}
    assert board.verify() == []


def test_completed_card_does_not_receive_promotions_or_transmit_them(
    board: board_state.Board,
) -> None:
    board.create("root", "Urgent", "backlog", "bot", priority="P0")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P3")
    accepted = board.find("done").to_json()
    board.link("root", "blocks", "done", "terry")
    board.link("done", "blocks", "leaf", "terry")
    assert board.find("done").to_json() == accepted
    assert board.find("leaf").priority == "P3"
    board.set_priority("done", "P5", "terry")
    assert board.find("done").priority == "P5"
    assert board.find("done").history[-1].priority_frm == "P2"
    assert board.find("done").history[-1].by == "terry"
    assert board.find("leaf").priority == "P3"
    assert board_state.blocking_order(board).depths == {"root": 2, "done": 1, "leaf": 0}
    assert board.verify() == []


def test_mixed_cycle_keeps_full_graph_depth_without_crossing_completed(
    board: board_state.Board,
) -> None:
    for name, priority in [("root", "P4"), ("a", "P3"), ("b", "P0"), ("leaf", "P5")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    accepted = board.find("done").to_json()
    for source, target in [("root", "a"), ("a", "done"), ("done", "b"), ("b", "a"), ("a", "leaf")]:
        board.link(source, "blocks", target, "bot")
    assert board_state.blocking_order(board).depths == {
        "root": 2,
        "a": 1,
        "b": 1,
        "done": 1,
        "leaf": 0,
    }
    assert [board.find(name).priority for name in ("root", "a", "done", "b", "leaf")] == [
        "P4",
        "P0",
        "P2",
        "P0",
        "P0",
    ]
    assert board.find("done").to_json() == accepted
    board.set_priority("b", "P5", "terry")
    assert board.find("b").priority == "P5"
    assert board.find("a").priority == "P0"
    assert len(board.links) == 5
    assert [item.id for item in board.lanes()[0].items] == ["a", "leaf", "root", "b"]
    assert board.verify() == []


def test_archived_unfinished_blocker_still_promotes_open_work(board: board_state.Board) -> None:
    board.create("root", "Archived prerequisite", "backlog", "bot", priority="P1")
    board.set_archived("root", archived=True, by="bot")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P3")
    board.link("root", "blocks", "leaf", "terry")
    assert board.find("leaf").priority == "P1"
    assert board.find("leaf").history[-1].by == "terry"
    assert board_state.blocking_order(board).depths == {"root": 1, "leaf": 0}
    assert board.verify() == []


def test_completion_does_not_demote_existing_descendants(board: board_state.Board) -> None:
    board.create("root", "Prerequisite", "ready_for_work", "bot", priority="P1")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P3")
    board.link("root", "blocks", "leaf", "bot")
    leaf = board.find("leaf").to_json()
    complete(board, "root")
    assert board.find("leaf").to_json() == leaf
    assert not board.enforce_dependency_priorities("bot")
    assert board.find("leaf").to_json() == leaf
    assert board.verify() == []


def test_completed_identity_ignores_label_and_other_lanes_without_outbound_edges(
    board: board_state.Board,
) -> None:
    raw = board.to_json()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    lanes = policy["lanes"]
    assert isinstance(lanes, list)
    for lane in lanes:
        assert isinstance(lane, dict)
        if lane["id"] == "completed":
            lane["label"] = "Accepted"
        if lane["id"] == "backlog":
            lane["label"] = "Completed"
    edges = policy["edges"]
    assert isinstance(edges, dict)
    for actor, allowed in edges.items():
        assert isinstance(allowed, list)
        retained: list[board_state.JsonValue] = []
        for edge in allowed:
            assert isinstance(edge, dict)
            if edge["from"] != "backlog":
                retained.append(edge)
        edges[actor] = retained
    board = board_state.Board.from_json(raw, "renamed lanes and terminal backlog")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P0")
    complete(board, "done")
    board.create("root", "Unfinished", "backlog", "bot", priority="P1")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P3")
    board.link("done", "blocks", "root", "bot")
    board.link("root", "blocks", "leaf", "bot")
    assert board.find("root").priority == "P1"
    assert board.find("leaf").priority == "P1"
    assert board.verify() == []


def test_startup_reconciliation_preserves_completed_objects_and_is_idempotent(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    for name, priority in [("done", "P2"), ("bridge", "P5")]:
        board.create(name, name, "ready_for_work", "bot", priority=priority)
        complete(board, name)
    board.set_archived("bridge", archived=True, by="bot")
    for name, priority in [("root", "P0"), ("child", "P3"), ("leaf", "P3")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    board.links.extend(
        [
            board_state.Link("root", "blocks", "done"),
            board_state.Link("done", "blocks", "child"),
            board_state.Link("root", "blocks", "bridge"),
            board_state.Link("bridge", "blocks", "child"),
            board_state.Link("root", "blocks", "leaf"),
        ]
    )
    accepted = {name: board.find(name).to_json() for name in ("done", "bridge")}
    board.revision = 7
    path = tmp_path / "historical.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    repaired = store.snapshot()
    assert repaired.revision == 8
    assert repaired.find("child").priority == "P3"
    assert repaired.find("leaf").priority == "P0"
    assert repaired.find("leaf").history[-1].by == "bot"
    assert {name: repaired.find(name).to_json() for name in accepted} == accepted
    assert repaired.links == board.links
    assert board_state.blocking_order(repaired).depths == {
        "root": 2,
        "done": 1,
        "bridge": 1,
        "child": 0,
        "leaf": 0,
    }
    assert repaired.verify() == []
    before = path.read_bytes()
    assert store.snapshot().revision == 8
    assert api_endpoint.BoardStore(path).snapshot().revision == 8
    assert path.read_bytes() == before


def test_reopening_resumes_audited_inheritance_in_one_transaction(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    board = reopening_policy(board)
    board.create("root", "Prerequisite", "backlog", "bot", priority="P1")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P3")
    complete(board, "done")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P5")
    board.link("root", "blocks", "done", "bot")
    board.link("done", "blocks", "leaf", "bot")
    before = {name: len(board.find(name).history) for name in ("done", "leaf")}
    path = tmp_path / "reopen.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    _, revision = store.execute(
        0, lambda candidate: candidate.move("done", "ready_for_work", "terry")
    )
    saved = store.snapshot()
    assert revision == saved.revision == 1
    assert saved.find("done").state == "ready_for_work"
    assert saved.find("done").priority == saved.find("leaf").priority == "P1"
    assert len(saved.find("done").history) == before["done"] + 2
    assert len(saved.find("leaf").history) == before["leaf"] + 1
    for name in ("done", "leaf"):
        assert saved.find(name).history[-1].by == "terry"
        assert saved.find(name).history[-1].priority_to == "P1"
    assert saved.verify() == []


def test_reopening_and_cascade_roll_back_when_snapshot_save_fails(
    board: board_state.Board, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = reopening_policy(board)
    board.create("root", "Prerequisite", "backlog", "bot", priority="P0")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P3")
    complete(board, "done")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P5")
    board.link("root", "blocks", "done", "bot")
    board.link("done", "blocks", "leaf", "bot")
    path = tmp_path / "rollback.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()

    def refuse_save(candidate: board_state.Board, target: pathlib.Path) -> None:
        assert candidate.find("done").state == "ready_for_work"
        assert candidate.find("done").priority == candidate.find("leaf").priority == "P0"
        assert target == path
        raise OSError("injected save failure")

    monkeypatch.setattr(board_state, "save", refuse_save)
    with pytest.raises(OSError, match="injected save failure"):
        store.execute(0, lambda candidate: candidate.move("done", "ready_for_work", "terry"))
    assert path.read_bytes() == before
    assert store.snapshot().to_json() == board.to_json()


@pytest.mark.parametrize("raises", [False, True])
def test_direct_reopening_restores_all_cards_when_verification_fails(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, *, raises: bool
) -> None:
    board = reopening_policy(board)
    board.create("root", "Prerequisite", "backlog", "bot", priority="P0")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P3")
    complete(board, "done")
    for name in ("a", "b", "branch"):
        board.create(name, name, "backlog", "bot", priority="P5")
    for source, target in [
        ("root", "done"),
        ("done", "a"),
        ("a", "b"),
        ("b", "a"),
        ("done", "branch"),
    ]:
        board.link(source, "blocks", target, "bot")
    before = board.to_json()

    def refuse_verification(candidate: board_state.Board) -> list[str]:
        assert candidate.find("done").state == "ready_for_work"
        assert all(candidate.find(name).priority == "P0" for name in ("done", "a", "b", "branch"))
        if raises:
            raise board_state.BoardError("injected verification failure")
        return ["done: injected verification failure"]

    monkeypatch.setattr(board_state.Board, "verify", refuse_verification)
    with pytest.raises(board_state.BoardError, match="injected verification failure"):
        board.move("done", "ready_for_work", "terry")
    assert board.to_json() == before


def test_offline_policy_embedding_preserves_completed_history(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    board.create("root", "Urgent", "backlog", "bot", priority="P0")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    for name in ("child", "leaf"):
        board.create(name, name, "backlog", "bot", priority="P3")
    board.links.extend(
        [
            board_state.Link("root", "blocks", "done"),
            board_state.Link("done", "blocks", "child"),
            board_state.Link("root", "blocks", "leaf"),
        ]
    )
    accepted = board.find("done").to_json()
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        board.port = candidate.getsockname()[1]
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(board.policy.to_json()), encoding="utf-8")
    raw = board.to_json()
    raw["schema"] = board_state.EMBEDDED_POLICY_PREVIOUS_SCHEMA
    del raw["policy"]
    path = tmp_path / "historical.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    board_state.embed_policy(path, policy)
    migrated = board_state.load(path)
    assert migrated.find("done").to_json() == accepted
    assert migrated.find("child").priority == "P3"
    assert migrated.find("leaf").priority == "P0"
    assert migrated.find("leaf").history[-1].by == "bot"
    assert migrated.links == board.links
    assert migrated.revision == 1
    assert migrated.verify() == []


def test_replacement_snapshot_with_only_completed_floors_needs_no_repair(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    board.create("leaf", "Remaining", "backlog", "bot", priority="P3")
    path = tmp_path / "replacement.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    replacement = store.snapshot()
    replacement.links.append(board_state.Link("done", "blocks", "leaf"))
    replacement.revision = 1
    board_state.save(replacement, path)
    before = path.read_bytes()
    assert store.snapshot().to_json() == replacement.to_json()
    assert store.snapshot().revision == 1
    assert path.read_bytes() == before
