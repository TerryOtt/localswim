"""CLI selection, preview binding, and single-submit ticket-order transport."""

import copy
import http.client
import json
import urllib.request
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli
from tests.test_api import RunningApi
from tests.test_api import api as api_fixture
from tests.test_ticket_order_api import seed

if TYPE_CHECKING:
    import pathlib

    from click.testing import Result

api = api_fixture


def bind(api: RunningApi, board: board_state.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(api, board)
    descriptor: board_state.JsonObject = {
        "host": "127.0.0.1",
        "port": api.server.server_address[1],
        "token": api_endpoint.CLI_TOKEN,
    }

    def describe(_path: pathlib.Path) -> board_state.JsonObject:
        return descriptor

    monkeypatch.setattr(board_state, "_service_descriptor", describe)


def invoke(path: pathlib.Path, *arguments: str) -> Result:
    return CliRunner().invoke(cli.cli, [str(path), "board", "reorder-tickets", *arguments])


def preview() -> board_state.JsonObject:
    return {
        "result": "Preview ready",
        "revision": 7,
        "applied": False,
        "plan": {
            "lane": "backlog",
            "selectedCount": 2,
            "changedCount": 2,
            "cards": [
                {"id": "root", "subject": "Root", "oldTicket": 2, "newTicket": 1},
                {"id": "leaf", "subject": "Leaf", "oldTicket": 1, "newTicket": 2},
            ],
        },
    }


def test_default_whole_lane_preview_is_read_only(
    api: RunningApi, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(api, board, monkeypatch)
    before = api.path.read_bytes()
    result = invoke(api.path, "backlog", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["plan"]["selectedCount"] == 3
    assert payload["revision"] == 0 and payload["applied"] is False
    assert api.path.read_bytes() == before
    text = invoke(api.path, "backlog")
    assert text.exit_code == 0
    assert "No changes made" in text.output
    assert "--expected-revision" in text.output


def test_unordered_subset_apply_and_noop_reapply(
    api: RunningApi, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(api, board, monkeypatch)
    result = invoke(api.path, "backlog", "#0001", "root", "--apply", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True and payload["revision"] == 1
    assert [row["id"] for row in payload["plan"]["cards"]] == ["root", "leaf"]
    before = api.path.read_bytes()
    result = invoke(api.path, "backlog", "leaf", "root", "--apply", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is False and payload["revision"] == 1
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("entries", [[1, "root"], ["root", "#0001"]])
def test_cards_file_accepts_integers_and_references(
    api: RunningApi,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    entries: list[int | str],
) -> None:
    bind(api, board, monkeypatch)
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(entries), encoding="utf-8-sig")
    result = invoke(api.path, "backlog", "--cards-file", str(path), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["plan"]["selectedCount"] == 2


@pytest.mark.parametrize("entries", [[], {}, [True], [0], [-1], [1.5], [None], ["  "]])
def test_invalid_cards_file_is_rejected_before_transport(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, entries: object
) -> None:
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(entries), encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid selection reached service lookup")

    monkeypatch.setattr(board_state, "_service_descriptor", forbidden)
    result = invoke(tmp_path / "board.json", "backlog", "--cards-file", str(path))
    assert result.exit_code == 2
    assert "--cards-file" in result.output


def test_cards_file_size_and_mixed_input_limits(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "cards.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    result = invoke(tmp_path / "board.json", "backlog", "--cards-file", str(path))
    assert result.exit_code == 2 and "1 MiB" in result.output
    path.write_text('["root"]', encoding="utf-8")
    result = invoke(tmp_path / "board.json", "backlog", "root", "--cards-file", str(path))
    assert result.exit_code == 2 and "not both" in result.output


@pytest.mark.parametrize("references", [["leaf", "1"], ["missing"], ["bridge"]])
def test_canonical_duplicate_unknown_and_wrong_lane_are_atomic(
    api: RunningApi,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    references: list[str],
) -> None:
    bind(api, board, monkeypatch)
    before = api.path.read_bytes()
    result = invoke(api.path, "backlog", *references, "--apply")
    assert result.exit_code != 0
    assert "nothing changed" in result.output
    assert api.path.read_bytes() == before


def test_stale_expected_revision_never_applies(
    api: RunningApi, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(api, board, monkeypatch)
    before = api.path.read_bytes()
    result = invoke(api.path, "backlog", "--apply", "--expected-revision", "99")
    assert result.exit_code != 0
    assert "nothing changed" in result.output
    assert api.path.read_bytes() == before


def transport(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    preview_reply: bytes | Exception,
    apply_reply: bytes | Exception,
) -> tuple[Result, list[urllib.request.Request]]:
    descriptor: board_state.JsonObject = {
        "host": "127.0.0.1",
        "port": 12345,
        "token": "synthetic-ticket-order",
    }

    def describe(_path: pathlib.Path) -> board_state.JsonObject:
        return descriptor

    monkeypatch.setattr(board_state, "_service_descriptor", describe)
    requests: list[urllib.request.Request] = []

    class FailedRead(BytesIO):
        def __init__(self, failure: Exception) -> None:
            super().__init__()
            self.failure = failure

        def read(self, _size: int | None = -1, /) -> bytes:
            raise self.failure

    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert timeout > 0
        requests.append(request)
        reply = preview_reply if request.full_url.endswith("/preview") else apply_reply
        return FailedRead(reply) if isinstance(reply, Exception) else BytesIO(reply)

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    result = invoke(
        tmp_path / "board.json", "backlog", "1", "root", "--apply", "--expected-revision", "7"
    )
    return result, requests


def test_apply_uses_exact_preview_revision_and_stable_ids(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ack = preview()
    ack.update({"revision": 8, "applied": True})
    result, requests = transport(
        tmp_path,
        monkeypatch,
        preview_reply=json.dumps(preview()).encode(),
        apply_reply=json.dumps(ack).encode(),
    )
    assert result.exit_code == 0, result.output
    assert len(requests) == 2
    first, second = requests
    assert first.full_url.endswith("/preview") and second.full_url.endswith("/apply")
    assert first.get_header("If-match") == second.get_header("If-match") == '"revision-7"'
    assert isinstance(first.data, bytes) and isinstance(second.data, bytes)
    assert json.loads(first.data) == {"lane": "backlog", "cards": ["1", "root"]}
    assert json.loads(second.data) == {"lane": "backlog", "cards": ["root", "leaf"]}
    assert second.get_header("Authorization") == "Bearer synthetic-ticket-order"


@pytest.mark.parametrize(
    "reply", [b"[]", b"{", b"{}", TimeoutError("synthetic"), http.client.IncompleteRead(b"", 1)]
)
def test_lost_or_invalid_apply_ack_is_unknown_without_replay(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, reply: bytes | Exception
) -> None:
    result, requests = transport(
        tmp_path, monkeypatch, preview_reply=json.dumps(preview()).encode(), apply_reply=reply
    )
    assert result.exit_code != 0
    assert len(requests) == 2
    assert "inspect" in result.output and "retry" in result.output
    assert "unknown" in result.output or "unconfirmed" in result.output
    assert "nothing changed" not in result.output


@pytest.mark.parametrize("field", ["revision", "applied", "plan"])
def test_inconsistent_apply_ack_is_unconfirmed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    ack = copy.deepcopy(preview())
    ack.update({"revision": 8, "applied": True})
    ack[field] = {"revision": 9, "applied": False, "plan": {}}[field]
    result, requests = transport(
        tmp_path,
        monkeypatch,
        preview_reply=json.dumps(preview()).encode(),
        apply_reply=json.dumps(ack).encode(),
    )
    assert result.exit_code != 0 and "unconfirmed" in result.output
    assert len(requests) == 2


@pytest.mark.parametrize(
    "reply", [b"[]", b"{", TimeoutError("synthetic"), http.client.IncompleteRead(b"", 1)]
)
def test_lost_preview_is_read_only_and_never_applies(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, reply: bytes | Exception
) -> None:
    result, requests = transport(tmp_path, monkeypatch, preview_reply=reply, apply_reply=b"{}")
    assert result.exit_code != 0
    assert "nothing changed" in result.output
    assert len(requests) == 1 and requests[0].full_url.endswith("/preview")


def test_noop_preview_does_not_submit_apply(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_changes = preview()
    no_changes["plan"] = {"lane": "backlog", "selectedCount": 0, "changedCount": 0, "cards": []}
    result, requests = transport(
        tmp_path, monkeypatch, preview_reply=json.dumps(no_changes).encode(), apply_reply=b"{}"
    )
    assert result.exit_code == 0, result.output
    assert len(requests) == 1


def test_config_enable_disable_uses_live_writer(
    api: RunningApi, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(api, board, monkeypatch)
    for option, enabled, revision in (("--enabled", True, 1), ("--disabled", False, 2)):
        result = CliRunner().invoke(
            cli.cli, [str(api.path), "board", "set-reorder-button", "backlog", option]
        )
        assert result.exit_code == 0, result.output
        saved = board_state.load(api.path)
        assert saved.revision == revision
        assert ("backlog" in saved.policy.reorder_lanes) is enabled


def test_config_requires_explicit_enable_or_disable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_path: pathlib.Path) -> board_state.JsonObject:
        pytest.fail("missing explicit configuration choice reached service lookup")

    monkeypatch.setattr(board_state, "_service_descriptor", forbidden)
    result = CliRunner().invoke(
        cli.cli, [str(tmp_path / "board.json"), "board", "set-reorder-button", "backlog"]
    )
    assert result.exit_code == 2


def test_selection_count_bound_is_enforced_by_server(
    api: RunningApi,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    bind(api, board, monkeypatch)
    path = tmp_path / "many.json"
    path.write_text(json.dumps(["leaf"] * 10001), encoding="utf-8")
    before = api.path.read_bytes()
    result = invoke(api.path, "backlog", "--cards-file", str(path), "--apply")
    assert result.exit_code != 0
    assert api.path.read_bytes() == before
