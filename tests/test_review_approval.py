"""Human review acceptance is one audited, atomic sole-writer command."""

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

from localswim import api_endpoint, board_state
from tests.test_api import API, RunningApi, request_json
from tests.test_api import api as api_fixture

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

api = api_fixture
APPROVAL = "Reviewed & approved, moving to Completed."


def ready(board: board_state.Board) -> None:
    """Build a valid review history through permitted transitions."""
    board.create("review", "Synthetic review", "backlog", "terry")
    board.move("review", "in_progress", "terry")
    board.move("review", "ready_for_review", "terry")


def seed(api: RunningApi, board: board_state.Board) -> None:
    ready(board)
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)


def test_human_acceptance_comments_before_transition_with_same_actor(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready(board)
    original = cast("Callable[[board_state.Board, str, str, str], str]", board_state.Board.move)
    calls: list[str] = []

    def observed_move(candidate: board_state.Board, ref: str, state: str, actor: str) -> str:
        item = candidate.find(ref)
        assert item.state == "ready_for_review"
        assert [(comment.text, comment.by) for comment in item.comments] == [(APPROVAL, "terry")]
        calls.append(state)
        return original(candidate, ref, state, actor)

    monkeypatch.setattr(board_state.Board, "move", observed_move)
    result = board.approve_review("#0001", "terry")
    item = board.find("review")
    assert calls == ["completed"]
    assert isinstance(result, str)
    assert result
    assert item.state == "completed"
    assert (item.history[-1].frm, item.history[-1].to, item.history[-1].by) == (
        "ready_for_review",
        "completed",
        "terry",
    )
    assert item.comments[-1].at <= item.history[-1].at
    assert board.verify() == []
    before = board.to_json()
    with pytest.raises(board_state.BoardError):
        board.approve_review("review", "terry")
    assert board.to_json() == before


@pytest.mark.parametrize("actor", ["bot", "unknown"])
def test_invalid_actor_refused_before_comment(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, actor: str
) -> None:
    ready(board)
    before = board.to_json()

    def forbidden_comment(*_args: object) -> str:
        pytest.fail("actor refusal must precede comment")

    monkeypatch.setattr(board_state.Board, "comment", forbidden_comment)
    with pytest.raises(board_state.BoardError):
        board.approve_review("review", actor)
    assert board.to_json() == before


@pytest.mark.parametrize("state", ["backlog", "in_progress", "completed"])
def test_wrong_state_refused_without_audit(board: board_state.Board, state: str) -> None:
    if state == "backlog":
        board.create("review", "Synthetic review", "backlog", "terry")
    else:
        ready(board)
    if state == "completed":
        board.move("review", state, "terry")
    elif state == "in_progress":
        board.move("review", "in_progress", "terry")
    before = board.to_json()
    with pytest.raises(board_state.BoardError):
        board.approve_review("review", "terry")
    assert board.to_json() == before


@pytest.mark.parametrize("mode", ["denied", "permissive_bot", "missing_source", "missing_target"])
def test_embedded_policy_and_actor_kind_are_both_required(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    ready(board)
    document = board.policy.to_json()
    edges = cast("dict[str, list[dict[str, str]]]", document["edges"])
    actor = "terry"
    if mode == "permissive_bot":
        edges["bot"].append({"from": "ready_for_review", "to": "completed"})
        actor = "bot"
    elif mode == "denied":
        edges["terry"] = [
            edge
            for edge in edges["terry"]
            if (edge["from"], edge["to"]) != ("ready_for_review", "completed")
        ]
    else:
        missing = "ready_for_review" if mode == "missing_source" else "completed"
        lanes = cast("list[dict[str, object]]", document["lanes"])
        document["lanes"] = cast(
            "board_state.JsonValue", [lane for lane in lanes if lane["id"] != missing]
        )
        for owner, entries in edges.items():
            edges[owner] = [edge for edge in entries if missing not in (edge["from"], edge["to"])]
    board.policy = board_state.TransitionPolicy.from_json(document, "approval test")
    before = board.to_json()

    def forbidden_comment(*_args: object) -> str:
        pytest.fail("policy refusal must precede comment")

    monkeypatch.setattr(board_state.Board, "comment", forbidden_comment)
    with pytest.raises(board_state.BoardError):
        board.approve_review("review", actor)
    assert board.to_json() == before


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_comment_failure_or_cancellation_restores_partial_comment(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    ready(board)
    before = board.to_json()
    original = cast("Callable[[board_state.Board, str, str, str], str]", board_state.Board.comment)

    def comment_then_fail(candidate: board_state.Board, ref: str, text: str, actor: str) -> str:
        original(candidate, ref, text, actor)
        raise failure("injected after comment")

    monkeypatch.setattr(board_state.Board, "comment", comment_then_fail)
    with pytest.raises(failure, match="injected"):
        board.approve_review("review", "terry")
    assert board.to_json() == before


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_direct_move_failure_or_cancellation_restores_comment_state_and_history(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    ready(board)
    before = board.to_json()
    original = cast("Callable[[board_state.Board, str, str, str], str]", board_state.Board.move)

    def move_then_fail(candidate: board_state.Board, ref: str, state: str, actor: str) -> str:
        original(candidate, ref, state, actor)
        raise failure("injected after move")

    monkeypatch.setattr(board_state.Board, "move", move_then_fail)
    with pytest.raises(failure, match="injected"):
        board.approve_review("review", "terry")
    assert board.to_json() == before


def test_store_save_failure_keeps_disk_and_published_state_unchanged(
    board: board_state.Board, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready(board)
    path = tmp_path / "approval.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        store.execute(0, lambda candidate: candidate.approve_review("review", "terry"))
    assert path.read_bytes() == before
    assert store.snapshot().to_json() == board.to_json()


def test_concurrent_same_revision_has_exactly_one_acceptance(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    ready(board)
    path = tmp_path / "approval.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)

    def accept() -> tuple[str, int]:
        return store.execute(0, lambda candidate: candidate.approve_review("review", "terry"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept) for _ in range(2)]
    assert sum(future.exception() is None for future in futures) == 1
    assert (
        sum(isinstance(future.exception(), api_endpoint.RevisionConflict) for future in futures)
        == 1
    )
    saved = board_state.load(path)
    assert saved.revision == 1
    assert [(comment.text, comment.by) for comment in saved.find("review").comments] == [
        (APPROVAL, "terry")
    ]
    assert saved.find("review").state == "completed"


def test_http_acceptance_authenticates_actor_and_rejects_retries(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    url = api.base + API + "/cards/review/approve"
    before = api.path.read_bytes()
    for token, revision, expected in [(None, 0, 401), (api_endpoint.BROWSER_TOKEN, None, 428)]:
        code, payload = request_json(url, token=token, revision=revision, body={})
        assert code == expected, payload
        assert api.path.read_bytes() == before
    code, payload = request_json(url, token=api_endpoint.CLI_TOKEN, revision=0, body={})
    assert code == 409, payload
    assert api.path.read_bytes() == before
    code, payload = request_json(url, token=api_endpoint.BROWSER_TOKEN, revision=0, body={})
    assert code == 200, payload
    assert payload["revision"] == 1
    saved = board_state.load(api.path)
    assert saved.find("review").comments[-1].by == "terry"
    assert saved.find("review").history[-1].by == "terry"
    committed = api.path.read_bytes()
    for revision, expected in [(0, 412), (1, 409)]:
        code, payload = request_json(
            url, token=api_endpoint.BROWSER_TOKEN, revision=revision, body={}
        )
        assert code == expected, payload
        assert api.path.read_bytes() == committed


@pytest.mark.parametrize("body", [{"actor": "terry"}, {"state": "completed"}, {"text": "override"}])
def test_approval_body_cannot_override_authenticated_command(
    api: RunningApi, board: board_state.Board, body: dict[str, object]
) -> None:
    seed(api, board)
    before = api.path.read_bytes()
    code, payload = request_json(
        api.base + API + "/cards/review/approve",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body=body,
    )
    assert code == 409, payload
    assert api.path.read_bytes() == before
