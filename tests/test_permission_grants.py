"""Additive exact-ID model grants and their live CLI command boundary."""

from copy import deepcopy
from typing import TYPE_CHECKING, cast

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli
from tests.support import token as fixture_token

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Sequence


def test_grants_preserve_cards_and_other_policy_fields(board: board_state.Board) -> None:
    board.create("synthetic", "Synthetic card", "backlog", "bot")
    board.comment("synthetic", "Synthetic comment", "terry")
    board.set_archived("synthetic", archived=True, by="bot")
    before = board.to_json()
    expected_policy = deepcopy(board.policy.to_json())
    lanes = cast("list[board_state.JsonObject]", expected_policy["lanes"])
    target = next(lane for lane in lanes if lane["id"] == "in_progress")
    target["create"] = ["bot", "terry"]
    edges = cast("dict[str, list[board_state.JsonValue]]", expected_policy["edges"])
    edges["bot"].append({"from": "backlog", "to": "completed"})
    edges["terry"].append({"from": "backlog", "to": "completed"})

    board.grant_permissions(["bot", "terry"], ["in_progress"], [("backlog", "completed")], "bot")

    after = board.to_json()
    after.pop("policy")
    before.pop("policy")
    assert after == before
    assert board.policy.to_json() == expected_policy
    assert board.verify() == []


@pytest.mark.parametrize("actor", ["bot", "terry"])
@pytest.mark.parametrize("kind", ["create", "move", "combined"])
def test_single_actor_grants_change_only_requested_permissions(
    board: board_state.Board, actor: str, kind: str
) -> None:
    before = deepcopy(board.to_json())
    expected_policy = deepcopy(board.policy.to_json())
    create = ["in_progress"] if kind in {"create", "combined"} else []
    moves = [("backlog", "completed")] if kind in {"move", "combined"} else []
    if create:
        lanes = cast("list[board_state.JsonObject]", expected_policy["lanes"])
        target = next(lane for lane in lanes if lane["id"] == "in_progress")
        assert target["create"] == []
        target["create"] = [actor]
    if moves:
        edges = cast("dict[str, list[board_state.JsonValue]]", expected_policy["edges"])
        edges[actor].append({"from": "backlog", "to": "completed"})
    before["policy"] = expected_policy

    board.grant_permissions([actor], create, moves, "bot")

    assert board.to_json() == before
    assert board.verify() == []


def test_duplicate_and_existing_grants_are_idempotent(board: board_state.Board) -> None:
    actors = ["bot", "bot"]
    create = ["in_progress", "in_progress"]
    moves = [("backlog", "completed"), ("backlog", "completed")]
    board.grant_permissions(actors, create, moves, "bot")
    before = board.to_json()
    board.grant_permissions(actors, create, moves, "terry")
    assert board.to_json() == before
    assert board.policy.check_edges() == []
    assert board_state.Board.from_json(board.to_json(), "synthetic").to_json() == before


@pytest.mark.parametrize(
    ("actors", "create", "moves", "by", "message"),
    [
        ([], ["in_progress"], [], "bot", "at least one actor"),
        (["bot"], [], [], "bot", "at least one create or move"),
        (["bot", "unknown"], ["in_progress"], [], "bot", "unknown actor"),
        (["Bot"], ["in_progress"], [], "bot", "unknown actor"),
        ([" bot"], ["in_progress"], [], "bot", "unknown actor"),
        ([""], ["in_progress"], [], "bot", "unknown actor"),
        (["bot"], ["in_progress", "unknown"], [], "bot", "unknown lane"),
        (["bot"], ["In progress"], [], "bot", "unknown lane"),
        (["bot"], [""], [], "bot", "unknown lane"),
        (["bot"], ["in_progress"], [("backlog", "unknown")], "bot", "unknown lane"),
        (["bot"], ["in_progress"], [("unknown", "completed")], "bot", "unknown lane"),
        (["bot"], ["in_progress"], [("backlog", "backlog")], "bot", "itself"),
        (["bot"], ["in_progress"], [], "unknown", "unknown actor"),
    ],
)
def test_invalid_batch_keeps_original_policy_and_snapshot(  # noqa: PLR0913 -- table covers each field
    board: board_state.Board,
    actors: Sequence[str],
    create: Sequence[str],
    moves: Sequence[tuple[str, str]],
    by: str,
    *,
    message: str,
) -> None:
    before = board.to_json()
    policy = board.policy
    with pytest.raises(board_state.BoardError, match=message):
        board.grant_permissions(actors, create, moves, by)
    assert board.policy is policy
    assert board.to_json() == before


def test_failed_policy_validation_keeps_original_snapshot(board: board_state.Board) -> None:
    board.users += (board_state.User("extra", "Extra", board_state.BOT, "#123456"),)
    before = board.to_json()
    policy = board.policy
    with pytest.raises(board_state.BoardError, match="exactly 2"):
        board.grant_permissions(["extra"], ["in_progress"], [("backlog", "completed")], "bot")
    assert board.policy is policy
    assert board.to_json() == before


def test_failed_transaction_does_not_publish_grants(
    board: board_state.Board, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "synthetic.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()

    def refuse_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("synthetic save failure")

    monkeypatch.setattr(board_state, "save", refuse_save)
    with pytest.raises(OSError, match="synthetic save failure"):
        store.execute(
            fixture_token(0),
            lambda candidate: candidate.grant_permissions(
                ["bot"], ["in_progress"], [("backlog", "completed")], "bot"
            ),
        )
    assert path.read_bytes() == before
    assert store.snapshot().to_json() == board.to_json()


def test_cli_posts_exact_grant_body(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "synthetic.json"
    calls: list[tuple[pathlib.Path, str, dict[str, object]]] = []

    def mutate(board: pathlib.Path, route: str, body: dict[str, object]) -> str:
        calls.append((board, route, body))
        return "Granted permissions"

    monkeypatch.setattr(board_state, "apply_remote_mutation", mutate)
    result = CliRunner().invoke(
        cli.cli,
        [
            str(path),
            "board",
            "grant-permissions",
            "--actor",
            "bot",
            "--actor",
            "terry",
            "--create-in",
            "in_progress",
            "--create-in",
            "blocked",
            "--move",
            "backlog",
            "completed",
            "--move",
            "ready_for_work",
            "backlog",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            path.resolve(),
            board_state.API_PREFIX + "/board/permissions",
            {
                "actors": ["bot", "terry"],
                "create": ["in_progress", "blocked"],
                "moves": [
                    {"from": "backlog", "to": "completed"},
                    {"from": "ready_for_work", "to": "backlog"},
                ],
            },
        )
    ]


@pytest.mark.parametrize(
    "arguments",
    [[], ["--actor", "bot"], ["--create-in", "backlog"], ["--actor", "bot", "--move", "backlog"]],
)
def test_cli_refuses_incomplete_grants(tmp_path: pathlib.Path, arguments: list[str]) -> None:
    result = CliRunner().invoke(
        cli.cli, [str(tmp_path / "synthetic.json"), "board", "grant-permissions", *arguments]
    )
    assert result.exit_code == 2
