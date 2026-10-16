"""Atomic priority selection through the public CLI boundary."""

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli
from tests.test_cli import assert_cli, run_cli, served_board

__all__ = ["served_board"]

if TYPE_CHECKING:
    import pathlib


def create_card(path: pathlib.Path, reference: str, priority: str = "P2") -> None:
    assert_cli(
        path,
        "card",
        "create",
        reference,
        reference,
        "--state",
        "ready_for_work",
        "--priority",
        priority,
    )


def test_cli_batch_lowers_an_entire_cycle(served_board: pathlib.Path) -> None:
    create_card(served_board, "alpha")
    create_card(served_board, "beta")
    assert_cli(served_board, "card", "link", "alpha", "blocks", "beta")
    assert_cli(served_board, "card", "link", "beta", "blocks", "alpha")
    result = run_cli(served_board, "card", "set-priorities", "P3", "#0002", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["priorities"] == {"alpha": "P3", "beta": "P3"}
    assert payload["changes"] == [
        {"id": "alpha", "from": "P2", "to": "P3"},
        {"id": "beta", "from": "P2", "to": "P3"},
    ]
    board = board_state.load(served_board)
    assert board.find("alpha").priority == board.find("beta").priority == "P3"
    assert board.find("alpha").history[-1].by == "bot"
    assert payload["revision"] == board.revision


def test_cli_batch_reports_effective_clamps(served_board: pathlib.Path) -> None:
    create_card(served_board, "external", "P1")
    create_card(served_board, "selected")
    assert_cli(served_board, "card", "link", "external", "blocks", "selected")
    result = run_cli(served_board, "card", "set-priorities", "P3", "selected")
    assert result.returncode == 0, result.stderr
    assert "Selected 1 card(s); 0 actual priority change(s)" in result.stdout
    assert "Clamped selected: requested P3, effective P1" in result.stdout
    structured = run_cli(served_board, "card", "set-priorities", "P3", "selected", "--json")
    payload = json.loads(structured.stdout)
    assert payload["priorities"] == {"selected": "P1"}
    assert payload["changes"] == []


def test_cli_batch_json_includes_cascades(served_board: pathlib.Path) -> None:
    create_card(served_board, "first", "P3")
    create_card(served_board, "second", "P3")
    assert_cli(served_board, "card", "link", "first", "blocks", "second")
    result = run_cli(served_board, "card", "set-priorities", "P1", "first", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["priorities"] == {"first": "P1"}
    assert payload["changes"] == [
        {"id": "first", "from": "P3", "to": "P1"},
        {"id": "second", "from": "P3", "to": "P1"},
    ]


@pytest.mark.parametrize(
    ("priority", "references"),
    [("P3", ("alpha", "#0001")), ("P3", ("alpha", "missing")), ("absent", ("alpha",))],
)
def test_cli_batch_invalid_selection_is_atomic(
    served_board: pathlib.Path, priority: str, references: tuple[str, ...]
) -> None:
    create_card(served_board, "alpha")
    before = served_board.read_bytes()
    result = run_cli(served_board, "card", "set-priorities", priority, *references, "--json")
    assert result.returncode != 0
    assert "Error:" in result.stderr
    assert result.stdout == ""
    assert served_board.read_bytes() == before


def test_cli_batch_completed_selection_is_atomic(served_board: pathlib.Path) -> None:
    create_card(served_board, "alpha")
    create_card(served_board, "finished")
    store = api_endpoint.STORE
    assert store is not None

    def complete(board: board_state.Board) -> str:
        board.move("finished", "in_progress", "terry")
        board.move("finished", "ready_for_review", "terry")
        return board.move("finished", "completed", "terry")

    store.execute(store.snapshot().revision, complete)
    before = served_board.read_bytes()
    result = run_cli(served_board, "card", "set-priorities", "P3", "alpha", "finished")
    assert result.returncode != 0
    assert "completed" in result.stderr.lower()
    assert served_board.read_bytes() == before


def test_cli_batch_stale_revision_never_refreshes(served_board: pathlib.Path) -> None:
    stale = board_state.load(served_board).revision
    create_card(served_board, "alpha")
    before = served_board.read_bytes()
    result = run_cli(
        served_board, "card", "set-priorities", "P3", "alpha", "--expected-revision", str(stale)
    )
    assert result.returncode != 0
    assert "revision" in result.stderr
    assert served_board.read_bytes() == before


def test_cli_batch_noop_has_no_audit_change(served_board: pathlib.Path) -> None:
    create_card(served_board, "alpha", "P3")
    before = board_state.load(served_board).find("alpha").to_json()
    result = run_cli(served_board, "card", "set-priorities", "P3", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changes"] == []
    assert board_state.load(served_board).find("alpha").to_json() == before


def test_cli_batch_preserves_named_priority_and_utf8_result(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "absent.json"
    calls: list[tuple[pathlib.Path, tuple[str, ...], str, int | None]] = []
    reply: board_state.JsonObject = {
        "result": "Changed café",
        "revision": 8,
        "priorities": {"café": "normalCase"},
        "changes": [{"id": "café", "from": "urgent", "to": "normalCase"}],
    }

    def submit(
        board: pathlib.Path,
        references: tuple[str, ...],
        priority: str,
        *,
        expected_revision: int | None = None,
    ) -> board_state.JsonObject:
        calls.append((board, references, priority, expected_revision))
        return reply

    monkeypatch.setattr(board_state, "apply_remote_priority_batch", submit)
    result = CliRunner().invoke(
        cli.cli,
        [
            str(path),
            "card",
            "set-priorities",
            "normalCase",
            "café",
            "--expected-revision",
            "7",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [(path.resolve(), ("café",), "normalCase", 7)]
    assert "café" in result.output
    assert json.loads(result.output) == reply


@pytest.mark.parametrize(
    "arguments",
    [[], ["alpha", "--expected-revision", "-1"], ["alpha", "--expected-revision", "1.5"]],
)
def test_cli_batch_rejects_missing_references_and_invalid_revision(
    tmp_path: pathlib.Path, arguments: list[str]
) -> None:
    result = run_cli(tmp_path / "absent.json", "card", "set-priorities", "P3", *arguments)
    assert result.returncode == 2
    assert "Error:" in result.stderr


def test_cli_batch_reference_limit_and_help(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "absent.json"
    result = CliRunner().invoke(
        cli.cli, [str(path), "card", "set-priorities", "P3", *[f"card-{n}" for n in range(1001)]]
    )
    assert result.exit_code == 2
    assert "at most 1000" in result.output
    help_result = run_cli(path, "card", "set-priorities", "--help")
    assert help_result.returncode == 0
    for text in ("atomic", "Completed", "--expected-revision", "--json", "unknown outcome"):
        assert text in help_result.stdout


def test_cli_batch_missing_service_is_a_clear_error(tmp_path: pathlib.Path) -> None:
    result = run_cli(tmp_path / "absent.json", "card", "set-priorities", "P3", "alpha")
    assert result.returncode != 0
    assert "service" in result.stderr.lower()
    assert "Traceback" not in result.stderr
