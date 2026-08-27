"""Board behavior at the domain-model boundary."""

import re

import pytest

from localswim import board_state


def test_now_persists_microsecond_precision_with_local_offset() -> None:
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}",
        board_state.now(),
    )


def test_create_assigns_ticket_history_and_owner(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")

    item = board.find("alpha")
    assert item.ticket == 1
    assert item.owner == "bot"
    assert item.history[0].kind == "lane"
    assert item.history[0].frm is None
    assert board.next_ticket == 2


@pytest.mark.parametrize("reference", ["alpha", "1", "#1", "0001"])
def test_find_accepts_slug_and_ticket_spellings(board: board_state.Board, reference: str) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    assert board.find(reference).id == "alpha"


def test_create_refuses_duplicate_id(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    with pytest.raises(board_state.BoardError, match="duplicate id"):
        board.create("alpha", "Another", "backlog", "bot")


def test_create_refuses_forbidden_lane(board: board_state.Board) -> None:
    with pytest.raises(board_state.BoardError, match="may not create"):
        board.create("alpha", "Alpha", "completed", "bot")


def test_move_records_legal_transition(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "ready_for_work", "bot")
    board.move("alpha", "in_progress", "bot")

    item = board.find("alpha")
    assert item.state == "in_progress"
    assert item.history[-1].frm == "ready_for_work"
    assert item.history[-1].to == "in_progress"
    assert board.verify() == []


@pytest.mark.parametrize(
    ("actor", "source", "destination"),
    [
        ("bot", "ready_for_work", "in_progress"),
        ("bot", "in_progress", "ready_for_review"),
        ("bot", "blocked", "in_progress"),
        ("terry", "ready_for_work", "in_progress"),
        ("terry", "ready_for_review", "completed"),
    ],
)
def test_representative_allowed_edges(
    board: board_state.Board, actor: str, source: str, destination: str
) -> None:
    item = board_state.Item("alpha", "Alpha", source, ticket=1, owner="bot")
    board.items.append(item)
    board.move("alpha", destination, actor)
    assert item.state == destination
    assert board.verify() == []


@pytest.mark.parametrize(
    ("actor", "source", "destination"),
    [
        ("bot", "ready_for_review", "completed"),
        ("terry", "completed", "in_progress"),
        ("bot", "backlog", "completed"),
    ],
)
def test_representative_forbidden_edges(
    board: board_state.Board, actor: str, source: str, destination: str
) -> None:
    item = board_state.Item("alpha", "Alpha", source, ticket=1, owner="bot")
    board.items.append(item)
    with pytest.raises(board_state.BoardError):
        board.move("alpha", destination, actor)
    assert item.state == source
    assert item.history == []


def test_move_refuses_claude_signoff(board: board_state.Board) -> None:
    item = board_state.Item("alpha", "Alpha", "ready_for_review", ticket=1, owner="bot")
    board.items.append(item)
    with pytest.raises(board_state.BoardError, match="not to completed"):
        board.move("alpha", "completed", "bot")
    assert item.state == "ready_for_review"
    assert item.history == []


def test_assignment_and_priority_are_non_lane_history(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.assign("alpha", "terry", "bot")
    board.set_priority("alpha", "P1", "bot")

    item = board.find("alpha")
    assert [entry.kind for entry in item.history] == ["lane", "owner", "priority"]
    assert item.replayed_state() == "backlog"
    assert board.verify() == []


def test_comment_refuses_blank_text(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    with pytest.raises(board_state.BoardError, match="needs text"):
        board.comment("alpha", "  ", "terry")


def test_human_comment_creates_reference_but_bot_comment_does_not(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")

    board.comment("alpha", "See #0002", "terry")
    assert board.links_for("alpha") == [("references", "beta")]
    assert board.links_for("beta") == [("referenced_by", "alpha")]
    assert board.relationship_history == [
        board_state.RelationshipChange(
            at=board.find("alpha").comments[-1].at,
            by="terry",
            action="linked",
            frm="alpha",
            kind="references",
            to="beta",
        )
    ]

    board.links.clear()
    board.comment("alpha", "Explaining #0002", "bot")
    assert board.links == []
    assert len(board.relationship_history) == 1


def test_link_is_stored_once_and_inverse_is_derived(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.link("beta", "blocked_by", "alpha", "bot")

    assert len(board.links) == 1
    assert board.links_for("alpha") == [("blocks", "beta")]
    assert board.links_for("beta") == [("blocked_by", "alpha")]
    assert board.relationship_history[-1] == board_state.RelationshipChange(
        at=board.relationship_history[-1].at,
        by="bot",
        action="linked",
        frm="beta",
        kind="blocked_by",
        to="alpha",
    )

    board.unlink("beta", "blocked_by", "alpha", "bot")
    assert board.links == []
    assert board.relationship_history[-1] == board_state.RelationshipChange(
        at=board.relationship_history[-1].at,
        by="bot",
        action="unlinked",
        frm="beta",
        kind="blocked_by",
        to="alpha",
    )


def test_replace_link_changes_both_derived_directions_together(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.link("beta", "blocked_by", "alpha", "bot")

    result = board.replace_link("beta", "blocked_by", "relates_to", "alpha", "bot")

    assert "blocked_by -> relates_to" in result
    assert board.links_for("alpha") == [("relates_to", "beta")]
    assert board.links_for("beta") == [("relates_to", "alpha")]
    assert [change.action for change in board.relationship_history[-2:]] == [
        "unlinked",
        "linked",
    ]
    assert board.relationship_history[-2].at == board.relationship_history[-1].at


def test_failed_link_replacement_preserves_original_relationship(
    board: board_state.Board,
) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.link("alpha", "blocks", "beta", "bot")
    original_history = list(board.relationship_history)

    with pytest.raises(board_state.BoardError, match="unknown relationship"):
        board.replace_link("alpha", "blocks", "not_a_kind", "beta", "bot")

    assert board.links_for("alpha") == [("blocks", "beta")]
    assert board.links_for("beta") == [("blocked_by", "alpha")]
    assert board.relationship_history == original_history


def test_parent_cycle_is_refused_and_rolled_back(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.set_parent("beta", "alpha", "bot")
    original_history = list(board.parent_history)

    with pytest.raises(board_state.BoardError, match="parent cycle"):
        board.set_parent("alpha", "beta", "bot")
    assert board.find("alpha").parent is None
    assert board.find("beta").parent == "alpha"
    assert board.parent_history == original_history


def test_parent_changes_record_before_and_after_without_no_op_events(
    board: board_state.Board,
) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")

    board.set_parent("beta", "alpha", "bot")
    assert board.parent_history[-1] == board_state.ParentChange(
        at=board.parent_history[-1].at,
        by="bot",
        child="beta",
        frm=None,
        to="alpha",
    )

    board.set_parent("beta", "alpha", "bot")
    assert len(board.parent_history) == 1

    board.set_parent("beta", None, "bot")
    assert board.parent_history[-1] == board_state.ParentChange(
        at=board.parent_history[-1].at,
        by="bot",
        child="beta",
        frm="alpha",
        to=None,
    )
    assert board.verify() == []


def test_verify_detects_parent_history_drift(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.set_parent("beta", "alpha", "bot")
    board.find("beta").parent = None

    assert "something changed it without going through set_parent()" in board.verify()[0]


def test_lanes_sort_by_priority_then_monotonic_ticket(board: board_state.Board) -> None:
    newer = board_state.Item(
        "newer",
        "Newer",
        "backlog",
        ticket=2,
        priority="P2",
        owner="bot",
        history=[board_state.Change("2026-01-01T00:00:00+00:00", "backlog", "bot")],
    )
    older = board_state.Item(
        "older",
        "Older",
        "backlog",
        ticket=1,
        priority="P2",
        owner="bot",
        history=[board_state.Change("2026-01-02T00:00:00+00:00", "backlog", "bot")],
    )
    urgent = board_state.Item(
        "urgent",
        "Urgent",
        "backlog",
        ticket=3,
        priority="P1",
        owner="bot",
        history=[board_state.Change("2026-01-03T00:00:00+00:00", "backlog", "bot")],
    )
    board.items = [newer, older, urgent]

    backlog = next(lane for lane in board.lanes() if lane.state == "backlog")
    assert [item.id for item in backlog.items] == ["urgent", "older", "newer"]


def test_verify_detects_direct_state_write(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    board.find("alpha").state = "completed"
    assert any("history ends" in problem for problem in board.verify())


def test_verify_detects_broken_history(board: board_state.Board) -> None:
    item = board_state.Item(
        "alpha",
        "Alpha",
        "completed",
        ticket=1,
        owner="bot",
        history=[
            board_state.Change("2026-01-01T00:00:00+00:00", "backlog", "bot"),
            board_state.Change(
                "2026-01-02T00:00:00+00:00", "completed", "bot", frm="ready_for_review"
            ),
        ],
    )
    board.items.append(item)
    problems = board.verify()
    assert any("chain is broken" in problem for problem in problems)


def test_verify_detects_illegal_recorded_transition(board: board_state.Board) -> None:
    item = board_state.Item(
        "alpha",
        "Alpha",
        "completed",
        ticket=1,
        owner="bot",
        history=[
            board_state.Change("2026-01-01T00:00:00+00:00", "backlog", "bot"),
            board_state.Change("2026-01-02T00:00:00+00:00", "completed", "bot", frm="backlog"),
        ],
    )
    board.items.append(item)
    assert any("permission table forbids" in problem for problem in board.verify())
