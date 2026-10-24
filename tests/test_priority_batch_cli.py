"""Atomic priority selection through the public CLI boundary."""

import http.client
import json
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli, service_ownership
from tests.support import token as fixture_token
from tests.test_cli import assert_cli, run_cli, served_board

__all__ = ["served_board"]

if TYPE_CHECKING:
    import pathlib

    from click.testing import Result


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

    captured = store.snapshot()
    store.execute(fixture_token(captured.revision, captured.generation), complete)
    before = served_board.read_bytes()
    result = run_cli(served_board, "card", "set-priorities", "P3", "alpha", "finished")
    assert result.returncode != 0
    assert "HTTP 409" in result.stderr
    assert served_board.read_bytes() == before


def test_cli_batch_stale_revision_never_refreshes(served_board: pathlib.Path) -> None:
    captured = board_state.load(served_board)
    stale = captured.revision
    create_card(served_board, "alpha")
    before = served_board.read_bytes()
    result = run_cli(
        served_board,
        "card",
        "set-priorities",
        "P3",
        "alpha",
        "--expected-generation",
        captured.generation,
        "--expected-revision",
        str(stale),
    )
    assert result.returncode != 0
    assert "HTTP 412" in result.stderr
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
        expected_generation: str | None = None,
    ) -> board_state.JsonObject:
        assert expected_generation == "11111111-1111-4111-8111-111111111111"
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
            "--expected-generation",
            "11111111-1111-4111-8111-111111111111",
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
    [
        [],
        [
            "alpha",
            "--expected-generation",
            "11111111-1111-4111-8111-111111111111",
            "--expected-revision",
            "-1",
        ],
        [
            "alpha",
            "--expected-generation",
            "11111111-1111-4111-8111-111111111111",
            "--expected-revision",
            "1.5",
        ],
    ],
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
    for text in (
        "atomic",
        "Completed",
        "--expected-generation",
        "--expected-revision",
        "--json",
        "unknown outcome",
    ):
        assert text in help_result.stdout


def test_cli_batch_missing_service_is_a_clear_error(tmp_path: pathlib.Path) -> None:
    result = run_cli(tmp_path / "absent.json", "card", "set-priorities", "P3", "alpha")
    assert result.returncode != 0
    assert "service" in result.stderr.lower()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("structured", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    "reply",
    [
        b"[]",
        b"[1]",
        b"null",
        b'"reply"',
        b"23",
        b"1.5",
        b"true",
        b"false",
        b"{",
        None,
        http.client.IncompleteRead(b"private partial body", 100),
        http.client.BadStatusLine("private framing detail"),
    ],
    ids=[
        "empty-array",
        "array",
        "null",
        "string",
        "integer",
        "float",
        "true",
        "false",
        "malformed",
        "timeout",
        "incomplete-read",
        "bad-status-line",
    ],
)
def test_cli_batch_transport_uncertain_reply_never_replays(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: bytes | Exception | None,
    *,
    structured: bool,
) -> None:
    result, requests = invoke_batch_transport(tmp_path, monkeypatch, reply, structured=structured)
    assert_batch_post(requests)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "mutation outcome is unknown" in result.stderr
    assert "inspect before retrying" in result.stderr
    assert "nothing changed" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "private" not in result.stderr
    assert len(result.stderr) < 250


@pytest.mark.parametrize("structured", [False, True], ids=["text", "json"])
def test_cli_batch_transport_valid_object_reports_success(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, structured: bool
) -> None:
    message = "Selected 1 card(s); 1 actual priority change(s)"
    payload = {
        "result": message,
        "revision": 8,
        "priorities": {"alpha": "P3"},
        "changes": [{"id": "alpha", "from": "P2", "to": "P3"}],
    }
    result, requests = invoke_batch_transport(
        tmp_path, monkeypatch, json.dumps(payload).encode(), structured=structured
    )
    assert_batch_post(requests)
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if structured:
        assert json.loads(result.stdout) == payload
    else:
        assert message in result.stdout


def invoke_batch_transport(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: bytes | Exception | None,
    *,
    structured: bool,
) -> tuple[Result, list[urllib.request.Request]]:
    path = tmp_path / "absent.json"
    board_state.service_descriptor_path(path).write_text(
        json.dumps(
            {
                "schema": 1,
                "host": "127.0.0.1",
                "port": 12345,
                "board": str(path.resolve()),
                "token": "synthetic",
            }
        ),
        encoding="utf-8",
    )
    requests: list[urllib.request.Request] = []
    identity_probes: list[str] = []
    closed: list[bool] = []
    session = json.dumps(
        {
            "boardIdentity": service_ownership.board_identity(path),
            "token": "browser-only-capability",
            "userCSS": "",
            "writeProtocol": 2,
            "generation": "11111111-1111-4111-8111-111111111111",
        }
    ).encode()

    class MetadataResponse:
        status = 200

        @staticmethod
        def read(amount: int) -> bytes:
            assert amount == service_ownership.MAX_METADATA_BYTES + 1
            return session

    class MetadataConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            assert (host, port) == ("127.0.0.1", 12345)
            assert timeout > 0

        @staticmethod
        def request(method: str, route: str) -> None:
            assert method == "GET"
            assert route == board_state.API_PREFIX + "/browser-session"
            identity_probes.append(route)

        @staticmethod
        def getresponse() -> MetadataResponse:
            return MetadataResponse()

        @staticmethod
        def close() -> None:
            closed.append(True)

    class FailedRead(BytesIO):
        def read(self, _size: int | None = -1, /) -> bytes:
            assert isinstance(reply, Exception)
            raise reply

    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        requests.append(request)
        assert timeout > 0
        assert request.get_method() == "POST"
        if reply is None:
            raise urllib.error.URLError(TimeoutError("synthetic timeout"))
        if isinstance(reply, urllib.error.HTTPError):
            raise reply
        if isinstance(reply, Exception):
            return FailedRead()
        return BytesIO(reply)

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    monkeypatch.setattr(board_state.http.client, "HTTPConnection", MetadataConnection)
    arguments = [
        str(path),
        "card",
        "set-priorities",
        "P3",
        "alpha",
        "--expected-generation",
        "11111111-1111-4111-8111-111111111111",
        "--expected-revision",
        "7",
    ]
    if structured:
        arguments.append("--json")
    result = CliRunner().invoke(cli.cli, arguments)
    assert identity_probes == [board_state.API_PREFIX + "/browser-session"]
    assert closed == [True]
    return result, requests


def assert_batch_post(requests: list[urllib.request.Request]) -> None:
    mutations = [request for request in requests if request.get_method() != "GET"]
    assert len(mutations) == 1
    request = mutations[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://127.0.0.1:12345/api/v001/cards/priorities"
    assert request.get_header("Authorization") == "Bearer synthetic"
    assert (
        request.get_header("If-match")
        == '"generation-11111111-1111-4111-8111-111111111111-revision-7"'
    )
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"cards": ["alpha"], "priority": "P3"}


@pytest.mark.parametrize("reply", [b"[]", b"null", b"23", b"true"])
def test_service_json_nonobject_get_preserves_error(
    monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert request.get_method() == "GET"
        assert timeout > 0
        return BytesIO(reply)

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(
        board_state.BoardError, match=r"^board service returned a non-object response$"
    ):
        board_state._service_json(  # noqa: SLF001 -- preserve the shared GET response boundary
            "http://127.0.0.1:12345/api/v001/board", "synthetic"
        )


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"private partial body", 100),
        http.client.BadStatusLine("private framing detail"),
    ],
)
def test_service_json_get_read_failure_is_controlled(
    monkeypatch: pytest.MonkeyPatch, failure: http.client.HTTPException
) -> None:
    requests: list[str] = []

    class FailedRead(BytesIO):
        def read(self, _size: int | None = -1, /) -> bytes:
            raise failure

    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert timeout > 0
        requests.append(request.get_method())
        return FailedRead()

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(board_state.BoardError) as caught:
        board_state._service_json(  # noqa: SLF001 -- shared read-only transport boundary
            "http://127.0.0.1:12345/api/v001/board", "synthetic"
        )
    assert str(caught.value) == "could not read board service response; nothing changed"
    assert requests == ["GET"]


@pytest.mark.parametrize("structured", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("status", [403, 412])
@pytest.mark.parametrize("body_kind", ["incomplete", "framing", "timeout", "json", "valid"])
def test_cli_batch_known_refusal_survives_unreadable_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body_kind: str,
    *,
    structured: bool,
) -> None:
    class RefusalBody(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            if body_kind == "incomplete":
                raise http.client.IncompleteRead(b"private partial body", 100)
            if body_kind == "framing":
                raise http.client.BadStatusLine("private framing detail")
            if body_kind == "timeout":
                raise TimeoutError("private timeout detail")
            return super().read(size)

    payload = b'{"error":"known refusal"}' if body_kind == "valid" else b"{"
    response = RefusalBody(payload)
    refusal = urllib.error.HTTPError(
        "http://127.0.0.1:12345/api/v001/cards/priorities", status, "refused", Message(), response
    )
    result, requests = invoke_batch_transport(tmp_path, monkeypatch, refusal, structured=structured)
    assert_batch_post(requests)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "board service refused the command" in result.stderr
    assert f"HTTP {status}" in result.stderr
    assert "known refusal" not in result.stderr
    assert "unknown" not in result.stderr
    assert "private" not in result.stderr
    assert "Traceback" not in result.stderr
    assert len(result.stderr) < 150
    assert response.closed
