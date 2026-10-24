"""Atomic priority normalization through the model, sole writer, and HTTP boundary."""

import hashlib
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, cast

import pytest

from localswim import api_endpoint, board_state, write_protocol
from tests.support import token as fixture_token
from tests.test_api import API, RunningApi, request_json
from tests.test_api import api as api_fixture
from tests.test_completed_dependencies import complete

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Mapping

api = api_fixture
SYNTHETIC_BEARER = "test-only-priority-batch"


def add_cards(board: board_state.Board, *names: str, priority: str = "P2") -> None:
    for name in names:
        board.create(name, name, "backlog", "bot", priority=priority)


def seed_api(api: RunningApi, board: board_state.Board) -> None:
    """Install only synthetic test state before any HTTP mutation."""
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)


def batch(
    api: RunningApi,
    body: dict[str, object],
    *,
    revision: int = 0,
    token: str | None = None,
) -> tuple[int, dict[str, object]]:
    code, result = request_json(
        api.base + API + "/cards/priorities",
        token=token or api_endpoint.CLI_TOKEN,
        revision=revision,
        body=body,
    )
    return code, result


@pytest.mark.parametrize("cycle", [False, True])
def test_combined_lowering_handles_chains_and_unfinished_cycles(
    board: board_state.Board,
    *,
    cycle: bool,
) -> None:
    add_cards(board, "a", "b", "leaf")
    board.link("a", "blocks", "b", "bot")
    board.link("b", "blocks", "leaf", "bot")
    if cycle:
        board.link("b", "blocks", "a", "bot")
    graph = [link.to_json() for link in board.links]
    depths = board_state.blocking_order(board).depths
    lengths = {item.id: len(item.history) for item in board.items}

    board.set_priorities(["b", "a", "leaf"], "P3", "terry")

    assert {item.priority for item in board.items} == {"P3"}
    assert [link.to_json() for link in board.links] == graph
    assert board_state.blocking_order(board).depths == depths
    for item in board.items:
        assert len(item.history) == lengths[item.id] + 1
        assert item.history[-1].priority_frm == "P2"
        assert item.history[-1].priority_to == "P3"
        assert item.history[-1].by == "terry"
    before = board.to_json()
    board.set_priorities(["a", "b", "leaf"], "P3", "terry")
    assert board.to_json() == before
    assert board.verify() == []


@pytest.mark.parametrize("cycle", [False, True])
def test_external_unfinished_floor_clamps_entire_selection(
    board: board_state.Board,
    *,
    cycle: bool,
) -> None:
    add_cards(board, "outside", priority="P1")
    add_cards(board, "a", "b", priority="P2")
    board.link("outside", "blocks", "a", "bot")
    board.link("a", "blocks", "b", "bot")
    if cycle:
        board.link("b", "blocks", "a", "bot")
    before = board.to_json()

    result = board.set_priorities(["a", "b"], "P3", "bot")

    assert "P1" in result
    assert "P3" in result
    assert board.to_json() == before


def test_completed_intermediate_object_hash_and_full_graph_are_preserved(
    board: board_state.Board,
) -> None:
    add_cards(board, "root", priority="P0")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    add_cards(board, "leaf", priority="P2")
    board.link("root", "blocks", "done", "bot")
    board.link("done", "blocks", "leaf", "bot")
    accepted = hashlib.sha256(
        json.dumps(board.find("done").to_json(), sort_keys=True).encode()
    ).digest()
    depths = board_state.blocking_order(board).depths
    graph = [link.to_json() for link in board.links]

    board.set_priorities(["root", "leaf"], "P3", "bot")

    assert board.find("leaf").priority == "P3"
    assert (
        hashlib.sha256(json.dumps(board.find("done").to_json(), sort_keys=True).encode()).digest()
        == accepted
    )
    assert board_state.blocking_order(board).depths == depths
    assert [link.to_json() for link in board.links] == graph
    assert board.verify() == []


@pytest.mark.parametrize("archived", [False, True])
def test_completed_selection_rejected_before_any_changes(
    board: board_state.Board,
    *,
    archived: bool,
) -> None:
    add_cards(board, "open")
    board.create("done", "Accepted", "ready_for_work", "bot", priority="P2")
    complete(board, "done")
    board.set_archived("done", archived=archived, by="bot")
    before = board.to_json()
    with pytest.raises(board_state.BoardError, match=r"[Cc]ompleted"):
        board.set_priorities(["open", "done"], "P3", "bot")
    assert board.to_json() == before


def test_explicit_archived_unfinished_selection_is_allowed(board: board_state.Board) -> None:
    add_cards(board, "hidden", "visible")
    board.set_archived("hidden", archived=True, by="bot")
    board.link("hidden", "blocks", "visible", "bot")
    board.set_priorities(["hidden", "visible"], "P3", "bot")
    assert all(item.priority == "P3" for item in board.items)
    assert board.find("hidden").archived
    assert board.verify() == []


@pytest.mark.parametrize("references", [["a", "1"], ["a", "missing"], ["a", ""], []])
def test_model_invalid_selection_is_atomic(
    board: board_state.Board,
    references: list[str],
) -> None:
    add_cards(board, "a")
    before = board.to_json()
    with pytest.raises(board_state.BoardError):
        board.set_priorities(references, "P3", "bot")
    assert board.to_json() == before


@pytest.mark.parametrize(("priority", "actor"), [("P99", "bot"), ("P3", "unknown")])
def test_model_rejects_unknown_priority_or_actor(
    board: board_state.Board,
    priority: str,
    actor: str,
) -> None:
    add_cards(board, "a")
    before = board.to_json()
    with pytest.raises(board_state.BoardError):
        board.set_priorities(["a"], priority, actor)
    assert board.to_json() == before


def test_selection_bound_is_checked_before_card_lookup(
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_find(_board: board_state.Board, _ref: str) -> board_state.Item:
        pytest.fail("oversized batch reached lookup")

    monkeypatch.setattr(board_state.Board, "find", forbidden_find)
    with pytest.raises(board_state.BoardError, match="1000"):
        board.set_priorities(["absent"] * 1001, "P3", "bot")


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_direct_application_exception_restores_priorities_and_history(
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    add_cards(board, "a", "b")
    before = board.to_json()
    original = cast(
        "Callable[[board_state.Board, Mapping[str, str], str], tuple[str, ...]]",
        vars(board_state.Board)["_apply_priority_targets"],
    )

    def apply_then_fail(
        candidate: board_state.Board,
        targets: Mapping[str, str],
        by: str,
    ) -> tuple[str, ...]:
        partial = dict(targets)
        partial["b"] = candidate.find("b").priority
        original(candidate, partial, by)
        assert candidate.find("a").priority == "P3"
        assert candidate.find("b").priority == "P2"
        raise failure("injected audit failure")

    monkeypatch.setattr(board_state.Board, "_apply_priority_targets", apply_then_fail)
    with pytest.raises(failure, match="injected"):
        board.set_priorities(["a", "b"], "P3", "bot")
    assert board.to_json() == before


def test_api_reports_canonical_selection_and_every_cascade_in_ticket_order(
    api: RunningApi,
    board: board_state.Board,
) -> None:
    add_cards(board, "root", "middle", "leaf", priority="P4")
    board.link("root", "blocks", "middle", "bot")
    board.link("middle", "blocks", "leaf", "bot")
    seed_api(api, board)

    code, response = batch(api, {"cards": ["#0002", "root"], "priority": "P1"})

    assert code == 200
    assert set(response) == {
        "result",
        "revision",
        "priorities",
        "changes",
        "generation",
        "writeProtocol",
        "writeToken",
    }
    assert response["revision"] == 1
    assert response["priorities"] == {"root": "P1", "middle": "P1"}
    assert response["changes"] == [
        {"id": name, "from": "P4", "to": "P1"} for name in ["root", "middle", "leaf"]
    ]
    saved = board_state.load(api.path)
    assert saved.revision == 1
    assert all(item.history[-1].by == "bot" for item in saved.items)
    histories = [item.to_json() for item in saved.items]
    code, response = batch(api, {"cards": ["root", "middle"], "priority": "P1"}, revision=1)
    assert code == 200
    assert response["revision"] == 2
    assert response["changes"] == []
    assert [item.to_json() for item in board_state.load(api.path).items] == histories


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"cards": [], "priority": "P3"},
        {"cards": "a", "priority": "P3"},
        {"cards": [True], "priority": "P3"},
        {"cards": [""], "priority": "P3"},
        {"cards": ["a"], "priority": True},
        {"cards": ["a"], "priority": "P99"},
        {"cards": ["a", "1"], "priority": "P3"},
        {"cards": ["a", "missing"], "priority": "P3"},
        {"cards": ["a"], "priority": "P3", "actor": "terry"},
    ],
)
def test_api_bad_input_leaves_snapshot_byte_identical(
    api: RunningApi,
    board: board_state.Board,
    body: dict[str, object],
) -> None:
    add_cards(board, "a")
    seed_api(api, board)
    before = api.path.read_bytes()
    code, _ = batch(api, body)
    assert code in {400, 409}
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("token", [None, "invalid"])
def test_api_requires_valid_bearer(api: RunningApi, token: str | None) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards/priorities",
        token=token,
        revision=0,
        body={"cards": ["a"], "priority": "P3"},
    )
    assert code in {401, 403}
    assert api.path.read_bytes() == before


def test_api_stale_revision_cannot_change_concurrently_completed_selection(
    api: RunningApi,
    board: board_state.Board,
) -> None:
    board.create("a", "A", "ready_for_work", "bot", priority="P2")
    seed_api(api, board)
    assert api_endpoint.STORE is not None

    def finish(candidate: board_state.Board) -> str:
        complete(candidate, "a")
        return "completed"

    api_endpoint.STORE.execute(fixture_token(0), finish)
    before = api.path.read_bytes()
    code, _ = batch(api, {"cards": ["a"], "priority": "P3"})
    assert code == 412
    assert api.path.read_bytes() == before
    code, _ = batch(api, {"cards": ["a"], "priority": "P3"}, revision=1)
    assert code == 409
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["verify", "save"])
def test_store_failure_does_not_publish_candidate(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    add_cards(board, "a", "b")
    path = tmp_path / "rollback.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()
    if failure == "verify":

        def fail_verify(_board: board_state.Board) -> list[str]:
            return ["injected verify failure"]

        monkeypatch.setattr(board_state.Board, "verify", fail_verify)
        error = board_state.BoardError
    else:

        def fail_save(
            _board: board_state.Board,
            _path: pathlib.Path,
            *,
            outcome: board_state.SaveOutcome | None = None,  # noqa: ARG001 -- save call shape
        ) -> None:
            raise OSError("injected save failure")

        monkeypatch.setattr(board_state, "save", fail_save)
        error = OSError
    with pytest.raises(error, match="injected"):
        api_endpoint.execute_priority_batch(
            store, fixture_token(0), {"cards": ["a", "b"], "priority": "P3"}, "bot"
        )
    assert path.read_bytes() == before
    monkeypatch.undo()
    assert store.snapshot().to_json() == board.to_json()


def test_response_captured_before_another_writer_commit(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_cards(board, "a")
    path = tmp_path / "race.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    original = api_endpoint.BoardStore.execute

    def execute_then_interleave(
        current: api_endpoint.BoardStore,
        expected: write_protocol.WriteToken,
        command: Callable[[board_state.Board], str],
    ) -> tuple[str, int]:
        result = original(current, expected, command)
        original(
            current,
            fixture_token(result[1]),
            lambda candidate: candidate.set_priority("a", "P0", "terry"),
        )
        return result

    monkeypatch.setattr(api_endpoint.BoardStore, "execute", execute_then_interleave)
    response = api_endpoint.execute_priority_batch(
        store, fixture_token(0), {"cards": ["a"], "priority": "P3"}, "bot"
    )
    assert response["revision"] == 1
    assert response["priorities"] == {"a": "P3"}
    assert response["changes"] == [{"id": "a", "from": "P2", "to": "P3"}]
    assert board_state.load(path).revision == 2
    assert board_state.load(path).find("a").priority == "P0"


def test_named_policy_priority_keeps_exact_case(
    api: RunningApi,
    board: board_state.Board,
) -> None:
    raw = board.policy.to_json()
    entries = cast("list[board_state.JsonObject]", raw["priorities"])
    entries[3]["id"] = "normalCase"
    raw["defaultPriority"] = "normalCase"
    board.policy = board_state.TransitionPolicy.from_json(raw, "named priority test")
    add_cards(board, "a")
    seed_api(api, board)
    code, response = batch(api, {"cards": ["a"], "priority": "normalCase"})
    assert code == 200
    assert response["priorities"] == {"a": "normalCase"}
    before = api.path.read_bytes()
    code, _ = batch(api, {"cards": ["a"], "priority": "NORMALCASE"}, revision=1)
    assert code == 409
    assert api.path.read_bytes() == before


def mock_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    def descriptor(_path: pathlib.Path) -> board_state.JsonObject:
        return {"schema": 1, "host": "127.0.0.1", "port": 8792, "token": SYNTHETIC_BEARER}

    monkeypatch.setattr(board_state, "_service_descriptor", descriptor)


def batch_reply() -> board_state.JsonObject:
    return {"result": "changed", "revision": 8, "priorities": {"a": "normalCase"}, "changes": []}


@pytest.mark.parametrize("expected", [None, 0, 7])
def test_remote_batch_makes_exactly_one_post_and_only_fetches_omitted_revision(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    expected: int | None,
) -> None:
    mock_descriptor(monkeypatch)
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    def request(
        url: str,
        token: str,
        *,
        body: dict[str, object] | None = None,
        write_token: str | None = None,
        _sanitize_errors: bool = False,
    ) -> board_state.JsonObject:
        assert token == SYNTHETIC_BEARER
        calls.append((url, body, write_token))
        return (
            {
                "writeProtocol": 2,
                "generation": fixture_token(7).generation,
                "revision": 7,
                "writeToken": fixture_token(7).format(),
            }
            if body is None
            else batch_reply()
        )

    monkeypatch.setattr(board_state, "_service_json", request)
    response = board_state.apply_remote_priority_batch(
        tmp_path / "unused.json",
        ["#1"],
        "normalCase",
        expected_revision=expected,
        expected_generation=None if expected is None else fixture_token(0).generation,
    )
    assert response == batch_reply()
    post = (
        "http://127.0.0.1:8792" + API + "/cards/priorities",
        {"cards": ["#1"], "priority": "normalCase"},
        fixture_token(7 if expected is None else expected).format(),
    )
    assert calls == (
        [("http://127.0.0.1:8792" + API + "/board", None, None), post]
        if expected is None
        else [post]
    )


@pytest.mark.parametrize("revision", [-1, True, False])
def test_invalid_explicit_revision_rejected_before_service_access(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: int,
) -> None:
    def forbidden_descriptor(_path: pathlib.Path) -> board_state.JsonObject:
        pytest.fail("invalid revision reached service descriptor")

    monkeypatch.setattr(board_state, "_service_descriptor", forbidden_descriptor)
    with pytest.raises(board_state.BoardError, match="nonnegative integer"):
        board_state.apply_remote_priority_batch(
            tmp_path / "unused.json",
            ["a"],
            "P3",
            expected_revision=revision,
            expected_generation=fixture_token(0).generation,
        )


@pytest.mark.parametrize(
    "bad_reply",
    [
        {},
        {"result": True},
        {"revision": True},
        {"revision": -1},
        {"priorities": {}},
        {"priorities": {"a": ""}},
        {"changes": "bad"},
        {"changes": [{"id": "a", "from": "P2", "to": "P2"}]},
        {"changes": [{"id": "a", "from": "P2", "to": "P3"}]},
        {"changes": [{"id": "a", "from": "P2", "to": "normalCase", "extra": "x"}]},
        {
            "changes": [
                {"id": "other", "from": "P2", "to": "P3"},
                {"id": "other", "from": "P2", "to": "P3"},
            ]
        },
    ],
)
def test_client_rejects_malformed_structured_reply_without_retry(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_reply: board_state.JsonObject,
) -> None:
    mock_descriptor(monkeypatch)
    calls = 0

    def request(
        _url: str,
        _token: str,
        *,
        body: dict[str, object],
        write_token: str,
    ) -> board_state.JsonObject:
        nonlocal calls
        calls += 1
        assert body == {"cards": ["a"], "priority": "normalCase"}
        assert write_token == fixture_token(7).format()
        return bad_reply if not bad_reply else batch_reply() | bad_reply

    monkeypatch.setattr(board_state, "_service_json", request)
    with pytest.raises(board_state.BoardError, match="outcome is unknown"):
        board_state.apply_remote_priority_batch(
            tmp_path / "unused.json",
            ["a"],
            "normalCase",
            expected_revision=7,
            expected_generation=fixture_token(0).generation,
        )
    assert calls == 1


def test_explicit_revision_refusal_is_not_refreshed_or_retried(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_descriptor(monkeypatch)
    calls = 0

    def refused(
        _url: str,
        _token: str,
        *,
        body: dict[str, object],
        write_token: str,
    ) -> board_state.JsonObject:
        nonlocal calls
        calls += 1
        assert write_token == fixture_token(0).format()
        assert body["cards"] == ["a"]
        raise board_state.BoardError("board revision is 1, not 0")

    monkeypatch.setattr(board_state, "_service_json", refused)
    with pytest.raises(board_state.BoardError, match="revision is 1"):
        board_state.apply_remote_priority_batch(
            tmp_path / "unused.json",
            ["a"],
            "P3",
            expected_revision=0,
            expected_generation=fixture_token(0).generation,
        )
    assert calls == 1


def test_mutation_timeout_reports_unknown_outcome_without_retry(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_descriptor(monkeypatch)
    calls = 0

    def timeout(request: urllib.request.Request, *, timeout: float) -> None:
        nonlocal calls
        calls += 1
        assert timeout > 0
        assert request.method == "POST"
        assert request.get_header("Authorization") == f"Bearer {SYNTHETIC_BEARER}"
        assert request.get_header("If-match") == fixture_token(0).format()
        assert isinstance(request.data, bytes)
        assert json.loads(request.data) == {"cards": ["a"], "priority": "P3"}
        raise urllib.error.URLError(TimeoutError("synthetic timeout"))

    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    with pytest.raises(board_state.BoardError, match="mutation outcome is unknown") as caught:
        board_state.apply_remote_priority_batch(
            tmp_path / "unused.json",
            ["a"],
            "P3",
            expected_revision=0,
            expected_generation=fixture_token(0).generation,
        )
    assert "nothing changed" not in str(caught.value)
    assert calls == 1
