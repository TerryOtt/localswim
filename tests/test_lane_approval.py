"""One-click lane approval keeps human authorization and whole-board atomicity."""

import copy
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli
from tests.test_api import API, RunningApi, request_json
from tests.test_api import api as api_fixture

if TYPE_CHECKING:
    import pathlib

api = api_fixture
LANE = "ready_for_review"


def ready(board: board_state.Board, *, message: str | None = None) -> None:
    for name in ("first", "second", "archived"):
        board.create(name, name.title(), "backlog", "terry", detail="Preserve detail")
        board.move(name, "in_progress", "terry")
        board.move(name, LANE, "terry")
        board.comment(name, "Preserve comment", "bot")
    board.set_archived("archived", archived=True, by="terry")
    board.configure_lane_approval(LANE, True, message, "bot")


def seed(api: RunningApi, board: board_state.Board) -> None:
    ready(board)
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)


def tokens(board: board_state.Board) -> dict[str, str]:
    return {
        item.id: api_endpoint.card_version(board, item)
        for item in board.items
        if item.state == LANE and not item.archived
    }


def test_all_active_cards_approved_with_standard_audits(board: board_state.Board) -> None:
    ready(board)
    original = copy.deepcopy(board)
    assert board.approve_lane(LANE, "terry") == 2
    for item in board.items:
        old = original.find(item.id)
        assert item.detail == old.detail
        assert item.ticket == old.ticket
        if item.archived:
            assert item.to_json() == old.to_json()
        else:
            assert item.state == "completed"
            assert item.comments[:-1] == old.comments
            assert item.history[:-1] == old.history
            assert item.comments[-1].text == board_state.REVIEW_APPROVAL_MESSAGE
            assert item.comments[-1].by == item.history[-1].by == "terry"
            assert item.comments[-1].at <= item.history[-1].at
            assert item.history[-1].frm == LANE
            assert item.history[-1].to == "completed"
    assert board.verify() == []


def test_custom_message_uses_normal_reference_semantics_and_toggle_preserves_it(
    board: board_state.Board,
) -> None:
    ready(board, message="Approved with #0003 checked.")
    board.configure_lane_approval(LANE, False, None, "bot")
    board.configure_lane_approval(LANE, True, None, "bot")
    assert board.policy.lane_approval(LANE) == (True, "Approved with #0003 checked.")
    assert board.approve_lane(LANE, "terry") == 2
    assert len(board.links) == 2
    assert board.find("first").comments[-1].text == "Approved with #0003 checked."
    assert board.verify() == []


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_later_card_failure_rolls_back_all_cards_and_reference_audits(
    board: board_state.Board, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    ready(board, message="Approved #0003.")
    before = board.to_json()
    original = board_state.Board._links_from_text  # noqa: SLF001 -- inject a mid-batch failure

    def fail_second(
        self: board_state.Board, item: board_state.Item, text: str, by: str, at: str
    ) -> tuple[list[str], list[str]]:
        result = original(self, item, text, by, at)
        if item.id == "second":
            raise failure("synthetic later-card failure")
        return result

    monkeypatch.setattr(board_state.Board, "_links_from_text", fail_second)
    with pytest.raises(failure):
        board.approve_lane(LANE, "terry")
    assert board.to_json() == before


@pytest.mark.parametrize("mode", ["disabled", "bot", "unknown", "wrong_lane", "denied"])
def test_domain_authority_and_lane_refusals_preserve_snapshot(
    board: board_state.Board, mode: str
) -> None:
    ready(board)
    actor = "terry"
    lane = LANE
    if mode == "disabled":
        board.configure_lane_approval(LANE, False, None, "bot")
    elif mode in {"bot", "unknown"}:
        actor = mode
    elif mode == "wrong_lane":
        lane = "backlog"
    else:
        document = board.policy.to_json()
        edges = cast("dict[str, list[dict[str, str]]]", document["edges"])
        edges["terry"] = [row for row in edges["terry"] if row != {"from": LANE, "to": "completed"}]
        # Rows may also contain a description.
        edges["terry"] = [
            row for row in edges["terry"] if (row["from"], row["to"]) != (LANE, "completed")
        ]
        board.policy = board_state.TransitionPolicy.from_json(document, "test")
    before = board.to_json()
    with pytest.raises(board_state.BoardError):
        board.approve_lane(lane, actor)
    assert board.to_json() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("approveAll", "true"),
        ("approveAll", 1),
        ("approvalMessage", ""),
        ("approvalMessage", " " * 3),
        ("approvalMessage", 1),
        ("approvalMessage", "x" * 4001),
    ],
)
def test_policy_rejects_invalid_approval_metadata(
    board: board_state.Board, field: str, value: board_state.JsonValue
) -> None:
    document = board.policy.to_json()
    lane = next(
        row for row in cast("list[board_state.JsonObject]", document["lanes"]) if row["id"] == LANE
    )
    lane[field] = value
    with pytest.raises(board_state.BoardError):
        board_state.TransitionPolicy.from_json(document, "test")


def test_store_empty_lane_is_byte_and_revision_stable(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    board.configure_lane_approval(LANE, True, None, "bot")
    path = tmp_path / "board.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()
    assert store.approve_lane(0, LANE, "terry", tokens(board))["approvedCount"] == 0
    assert path.read_bytes() == before
    assert store.snapshot().revision == 0


def test_concurrent_approval_has_exactly_one_commit(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    ready(board)
    path = tmp_path / "board.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.approve_lane, 0, LANE, "terry", tokens(board)) for _ in range(2)
        ]
    assert sum(f.exception() is None for f in futures) == 1
    assert sum(isinstance(f.exception(), api_endpoint.RevisionConflict) for f in futures) == 1
    saved = board_state.load(path)
    assert saved.revision == 1
    assert len(saved.find("first").comments) == 2
    assert len(saved.find("second").comments) == 2


def test_save_failure_keeps_published_board_and_disk(
    board: board_state.Board, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready(board)
    path = tmp_path / "board.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic save failure")

    monkeypatch.setattr(board_state, "save", fail)
    with pytest.raises(OSError, match="synthetic save failure"):
        store.approve_lane(0, LANE, "terry", tokens(board))
    assert path.read_bytes() == before
    assert store.snapshot().to_json() == board.to_json()


def test_http_credentials_revision_and_strict_body(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    url = api.base + API + "/board/lane-approval/apply"
    before = api.path.read_bytes()
    cases: list[tuple[str | None, int | None, dict[str, object], int]] = [
        (None, 0, {"lane": LANE, "cards": tokens(board)}, 401),
        (api_endpoint.CLI_TOKEN, 0, {"lane": LANE, "cards": tokens(board)}, 403),
        (api_endpoint.BROWSER_TOKEN, None, {"lane": LANE, "cards": tokens(board)}, 428),
        (api_endpoint.BROWSER_TOKEN, 1, {"lane": LANE, "cards": tokens(board)}, 412),
        (api_endpoint.BROWSER_TOKEN, 0, {"lane": LANE, "cards": ["first"]}, 409),
        (api_endpoint.BROWSER_TOKEN, 0, {"lane": "backlog", "cards": tokens(board)}, 409),
    ]
    for token, revision, body, expected in cases:
        code, out = request_json(url, token=token, revision=revision, body=body)
        assert code == expected, out
        assert api.path.read_bytes() == before
    code, out = request_json(
        url,
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"lane": LANE, "cards": tokens(board)},
    )
    assert code == 200, out
    assert out["approvedCount"] == 2 and out["revision"] == 1
    saved = api.path.read_bytes()
    code, _ = request_json(
        url,
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"lane": LANE, "cards": tokens(board)},
    )
    assert code == 412 and api.path.read_bytes() == saved


def test_config_is_cli_only_and_preserves_omitted_message(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    url = api.base + API + "/board/lane-approval/config"
    body: dict[str, object] = {"lane": LANE, "enabled": False, "message": "Custom approval"}
    code, _ = request_json(url, token=api_endpoint.BROWSER_TOKEN, revision=0, body=body)
    assert code == 403
    code, out = request_json(url, token=api_endpoint.CLI_TOKEN, revision=0, body=body)
    assert code == 200, out
    code, out = request_json(
        url, token=api_endpoint.CLI_TOKEN, revision=1, body={"lane": LANE, "enabled": True}
    )
    assert code == 200, out
    assert board_state.load(api.path).policy.lane_approval(LANE) == (True, "Custom approval")


@pytest.mark.parametrize("change", ["content", "arrival"])
def test_rendered_cards_fence_even_when_client_uses_newer_board_revision(
    api: RunningApi, board: board_state.Board, change: str
) -> None:
    seed(api, board)
    rendered = tokens(board)
    store = api_endpoint.STORE
    assert store is not None

    def mutate(candidate: board_state.Board) -> str:
        if change == "content":
            return candidate.comment("first", "Unseen new content", "bot")
        candidate.create("arrival", "Unseen arrival", "backlog", "terry")
        candidate.move("arrival", "in_progress", "terry")
        return candidate.move("arrival", LANE, "terry")

    store.execute(0, mutate)
    before = api.path.read_bytes()
    code, out = request_json(
        api.base + API + "/board/lane-approval/apply",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={"lane": LANE, "cards": rendered},
    )
    assert code == 412, out
    assert out["code"] == "card_revision_conflict"
    assert api.path.read_bytes() == before


def test_cli_configuration_uses_live_service_and_preserves_omitted_message(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def mutate(_path: pathlib.Path, route: str, body: dict[str, object]) -> str:
        calls.append((route, body))
        return "Configured"

    monkeypatch.setattr(board_state, "apply_remote_mutation", mutate)
    base = [str(tmp_path / "board.json"), "board", "set-approval-button", LANE]
    assert CliRunner().invoke(cli.cli, [*base, "--enabled"]).exit_code == 0
    assert calls == [(API + "/board/lane-approval/config", {"lane": LANE, "enabled": True})]
    assert (
        CliRunner().invoke(cli.cli, [*base, "--disabled", "--message", "New text"]).exit_code == 0
    )
    assert calls[-1][1] == {"lane": LANE, "enabled": False, "message": "New text"}
    assert CliRunner().invoke(cli.cli, [*base, "--enabled", "--message", " "]).exit_code != 0
    assert len(calls) == 2
