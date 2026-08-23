"""Executable documentation examples."""

import pathlib

from localswim import board_state

TERRY_LANES = [
    "backlog",
    "ready_for_work",
    "in_progress",
    "blocked",
    "needs_terry_action",
    "ready_for_review",
    "completed",
]


def test_example_board_is_valid_and_empty() -> None:
    example = pathlib.Path(__file__).resolve().parents[1] / "examples" / "board.example.json"

    board = board_state.load(example)

    assert board.project == "Example project"
    assert board.port == 8792
    assert board.items == []
    assert board.browser_user == "terry"
    assert board.cli_user == "bot"


def test_initialization_examples_regenerate_checked_board(tmp_path: pathlib.Path) -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    generated = tmp_path / "board.json"

    board_state.initialize_board(
        generated,
        root / "examples" / "board-description.example.json",
        root / "examples" / "permissions.example.json",
    )

    checked = board_state.load(root / "examples" / "board.example.json")
    assert board_state.load(generated).to_json() == checked.to_json()


def test_terry_workflow_board_preserves_personal_lane_and_priority_model() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    board = board_state.load(root / "examples" / "board.terry-workflow.json")

    assert board.project == "Terry workflow example"
    assert board.port == 8793
    assert board.items == []
    assert [lane_id for lane_id, _label in board.policy.lanes] == TERRY_LANES
    assert [
        (priority, board.policy.priority_label[priority]) for priority in board.policy.priorities
    ] == [
        ("P0", "On fire"),
        ("P1", "Urgent"),
        ("P2", "High"),
        ("P3", "Normal"),
        ("P4", "Low"),
        ("P5", "Only if idle"),
    ]
    assert board.policy.default_priority == "P3"
    assert board.policy.edges_for("terry") == frozenset(
        [
            ("backlog", "ready_for_work"),
            ("backlog", "in_progress"),
            ("backlog", "needs_terry_action"),
            ("ready_for_work", "backlog"),
            ("ready_for_work", "in_progress"),
            ("ready_for_work", "needs_terry_action"),
            ("in_progress", "ready_for_work"),
            ("in_progress", "blocked"),
            ("in_progress", "needs_terry_action"),
            ("in_progress", "ready_for_review"),
            ("blocked", "ready_for_work"),
            ("blocked", "in_progress"),
            ("blocked", "needs_terry_action"),
            ("needs_terry_action", "backlog"),
            ("needs_terry_action", "ready_for_work"),
            ("needs_terry_action", "in_progress"),
            ("needs_terry_action", "ready_for_review"),
            ("ready_for_review", "backlog"),
            ("ready_for_review", "ready_for_work"),
            ("ready_for_review", "in_progress"),
            ("ready_for_review", "completed"),
        ]
    )
    assert board.policy.edges_for("bot") == frozenset(
        [
            ("backlog", "ready_for_work"),
            ("backlog", "needs_terry_action"),
            ("ready_for_work", "backlog"),
            ("ready_for_work", "in_progress"),
            ("ready_for_work", "blocked"),
            ("ready_for_work", "needs_terry_action"),
            ("in_progress", "backlog"),
            ("in_progress", "ready_for_work"),
            ("in_progress", "blocked"),
            ("in_progress", "needs_terry_action"),
            ("in_progress", "ready_for_review"),
            ("blocked", "backlog"),
            ("blocked", "ready_for_work"),
            ("blocked", "in_progress"),
            ("blocked", "needs_terry_action"),
            ("blocked", "ready_for_review"),
            ("needs_terry_action", "backlog"),
            ("needs_terry_action", "ready_for_work"),
            ("needs_terry_action", "in_progress"),
            ("needs_terry_action", "blocked"),
            ("needs_terry_action", "ready_for_review"),
            ("ready_for_review", "backlog"),
            ("ready_for_review", "ready_for_work"),
            ("ready_for_review", "in_progress"),
            ("ready_for_review", "blocked"),
            ("ready_for_review", "needs_terry_action"),
        ]
    )


def test_terry_workflow_inputs_regenerate_checked_board(tmp_path: pathlib.Path) -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    generated = tmp_path / "board.json"

    board_state.initialize_board(
        generated,
        root / "examples" / "board-description.terry-workflow.json",
        root / "examples" / "permissions.terry-workflow.json",
    )

    checked = board_state.load(root / "examples" / "board.terry-workflow.json")
    assert board_state.load(generated).to_json() == checked.to_json()
