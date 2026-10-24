"""Inert complete-token adapters retain originating state across credential changes."""

# ruff: noqa: SLF001 -- these private preparations deliberately have no public selector

import argparse
import http.server
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from localswim import board_state, cli, service_ownership, workspace
from localswim.write_protocol import WriteToken

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

GENERATION = "12345678-1234-4234-8234-123456789abc"
OTHER_GENERATION = "abcdefab-1234-4234-8234-123456789abc"
CAPTURED = WriteToken(GENERATION, 7).format()
API = board_state.API_PREFIX


@dataclass
class Transport:
    path: pathlib.Path
    port: int = 0
    credential: str = "cli-capability"
    identity: str = ""
    generation: str = GENERATION
    refusal: int | None = None
    posts: list[tuple[str, str | None, str | None]] = field(
        default_factory=list[tuple[str, str | None, str | None]]
    )
    gets: list[str] = field(default_factory=list[str])

    def metadata(self) -> board_state.JsonObject:
        return {
            "writeProtocol": 2,
            "generation": GENERATION,
            "revision": 7,
            "writeToken": CAPTURED,
        }

    def descriptor(self) -> None:
        board_state.service_descriptor_path(self.path).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "host": "127.0.0.1",
                    "port": self.port,
                    "board": str(self.path.resolve()),
                    "token": self.credential,
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def transport(tmp_path: pathlib.Path) -> Iterator[Transport]:
    state = Transport(tmp_path / "synthetic.json")
    state.identity = service_ownership.board_identity(state.path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, value: object, status: int = 200) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            state.gets.append(self.path)
            if self.path == API + "/browser-session":
                self.reply(
                    {
                        **state.metadata(),
                        "boardIdentity": state.identity,
                        "token": "browser-capability",
                        "userCSS": "",
                    }
                )
            else:
                self.reply(state.metadata())

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            state.posts.append(
                (self.path, self.headers.get("If-Match"), self.headers.get("Authorization"))
            )
            if state.refusal is not None:
                self.reply({"error": "private-capability"}, state.refusal)
            elif self.headers.get("If-Match") != WriteToken(state.generation, 7).format():
                self.reply({"error": "state_changed"}, 412)
            else:
                self.reply(
                    {
                        "result": "accepted",
                        "revision": 8,
                        "priorities": {"alpha": "P3"},
                        "changes": [],
                    }
                )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- base API
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = server.server_port
    state.descriptor()
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "arguments",
    [
        ["card", "set-priorities", "P3", "alpha"],
        ["board", "set-project", "new name"],
        ["card", "comment", "alpha", "text"],
    ],
)
def test_prepared_cli_captures_one_board_read(transport: Transport, arguments: list[str]) -> None:
    result = CliRunner().invoke(cli.cli, [str(transport.path), *arguments])
    assert result.exit_code == 0, result.output
    assert transport.gets == [API + "/browser-session", API + "/board"]
    assert len(transport.posts) == 1
    assert transport.posts[0][1:] == (CAPTURED, "Bearer cli-capability")


def test_prepared_explicit_pair_never_fetches_board(transport: Transport) -> None:
    transport.credential = "rotated-cli-capability"
    transport.generation = OTHER_GENERATION
    transport.descriptor()
    result = CliRunner().invoke(
        cli.cli,
        [
            str(transport.path),
            "card",
            "set-priorities",
            "P3",
            "alpha",
            "--expected-generation",
            GENERATION,
            "--expected-revision",
            "7",
        ],
    )
    assert result.exit_code != 0
    assert "HTTP 412" in result.stderr
    assert transport.gets == [API + "/browser-session"]
    assert transport.posts == [
        (API + "/cards/priorities", CAPTURED, "Bearer rotated-cli-capability")
    ]


@pytest.mark.parametrize(
    "options",
    [
        ["--expected-revision", "7"],
        ["--expected-generation", GENERATION],
        ["--expected-generation", "bad", "--expected-revision", "7"],
    ],
)
def test_prepared_bad_pair_refuses_before_any_network(
    transport: Transport, options: list[str]
) -> None:
    result = CliRunner().invoke(
        cli.cli,
        [str(transport.path), "card", "set-priorities", "P3", "alpha", *options],
    )
    assert result.exit_code != 0
    assert transport.gets == transport.posts == []


def test_prepared_wrong_board_refuses_before_snapshot_or_post(transport: Transport) -> None:
    transport.identity = "foreign-board"
    with pytest.raises(board_state.BoardError, match="identity"):
        board_state.apply_remote_mutation(transport.path, API + "/board/project", {"project": "x"})
    assert transport.gets == [API + "/browser-session"]
    assert transport.posts == []


@pytest.mark.parametrize("status", [401, 412, 500])
def test_prepared_refusal_is_sanitized_and_never_replayed(
    transport: Transport, status: int
) -> None:
    transport.refusal = status
    with pytest.raises(board_state.BoardError, match="inspect before retrying") as error:
        board_state.apply_remote_mutation(transport.path, API + "/board/project", {"project": "x"})
    assert "private" not in str(error.value)
    assert len(transport.posts) == 1
    assert transport.gets == [API + "/browser-session", API + "/board"]


def test_prepared_legacy_mapping_retains_complete_token(transport: Transport) -> None:
    args = argparse.Namespace(create=None, set_project="new name")
    assert board_state._remote_apply(transport.path, args) == "accepted"
    assert transport.posts == [(API + "/board/project", CAPTURED, "Bearer cli-capability")]


def test_prepared_construction_does_not_change_public_cli() -> None:
    commands = dict(cli.cli.commands)
    cards = dict(cli.card_commands.commands)
    options = tuple(cli.card_set_priorities.params)
    private = cli.cli
    private_help = CliRunner().invoke(private, ["board.json", "card", "set-priorities", "--help"])
    public_help = CliRunner().invoke(cli.cli, ["board.json", "card", "set-priorities", "--help"])
    assert "--expected-generation" in private_help.output
    assert "--expected-generation" in public_help.output
    assert cli.cli.commands == commands
    assert cli.card_commands.commands == cards
    assert tuple(cli.card_set_priorities.params) == options
    assert private.commands["card"] is cli.card_commands


def test_prepared_workspace_validates_bounded_board_session(
    tmp_path: pathlib.Path, transport: Transport
) -> None:
    prepared = workspace.Workspace(tmp_path)
    public = workspace.Workspace(tmp_path)
    try:
        endpoint = prepared._browser_endpoint(transport.path, transport.port)
        assert endpoint is not None
        assert endpoint.generation == GENERATION
        assert endpoint.write_protocol == 2
        session = endpoint.browser_session(transport.path, prepared.token)
        assert session["generation"] == GENERATION
        assert session["token"] == prepared.token
        assert "revision" not in session
        assert "writeToken" not in session
        assert prepared.settings.snapshot().revision == 0
        prepared.settings.apply(0, {"action": "pin", "key": "board"}, ["board"])
        assert prepared.settings.snapshot().revision == 1
        assert endpoint.browser_session(transport.path, prepared.token)["generation"] == GENERATION
        shipped = public._browser_endpoint(transport.path, transport.port)
        assert shipped is not None
        assert shipped.generation == GENERATION
        assert shipped.browser_session(transport.path, public.token)["generation"] == GENERATION
        transport.identity = "foreign"
        assert prepared._browser_endpoint(transport.path, transport.port) is None
    finally:
        prepared.close()
        public.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("writeProtocol", True),
        ("writeProtocol", 1),
        ("generation", OTHER_GENERATION),
        ("revision", True),
        ("revision", 8),
        ("writeToken", '"revision-7"'),
    ],
)
def test_prepared_rejects_inconsistent_snapshot(field: str, value: board_state.JsonValue) -> None:
    snapshot: board_state.JsonObject = {
        "writeProtocol": 2,
        "generation": GENERATION,
        "revision": 7,
        "writeToken": CAPTURED,
    }
    snapshot[field] = value
    with pytest.raises(board_state.BoardError):
        board_state._snapshot_precondition(snapshot)


def test_prepared_gateway_rotates_only_authentication_and_never_replays(
    tmp_path: pathlib.Path, transport: Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = workspace.Workspace(tmp_path)
    resolutions: list[str] = []
    transport.generation = OTHER_GENERATION

    def resolve(key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
        resolutions.append(key)
        return transport.path, workspace.Endpoint(
            transport.port, "fresh-browser-capability", "", OTHER_GENERATION, 2
        )

    monkeypatch.setattr(instance, "resolve_endpoint", resolve)
    server = workspace.WorkspaceServer(("127.0.0.1", 0), instance)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{workspace.BOARD_PREFIX}board/board/project",
            data=b'{"project":"x"}',
            headers={"Authorization": "Bearer " + instance.token, "If-Match": CAPTURED},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        assert error.value.code == 412
        error.value.close()
        assert resolutions == ["board"]
        assert transport.posts == [
            (API + "/board/project", CAPTURED, "Bearer fresh-browser-capability")
        ]
        assert instance.settings.snapshot().revision == 0
        assert transport.gets == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        instance.close()


@pytest.mark.parametrize("reply", [b"{", b"[]", None])
def test_prepared_unknown_post_outcome_is_sanitized_without_replay(
    monkeypatch: pytest.MonkeyPatch, reply: bytes | None
) -> None:
    requests: list[urllib.request.Request] = []

    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert timeout > 0
        requests.append(request)
        if reply is None:
            raise urllib.error.URLError(TimeoutError("private-capability"))
        return BytesIO(reply)

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(board_state.BoardError, match="outcome is unknown") as error:
        board_state._service_json(
            "http://127.0.0.1:1" + API + "/board/project",
            "synthetic-capability",
            body={"project": "x"},
            write_token=CAPTURED,
        )
    assert len(requests) == 1
    assert requests[0].get_header("If-match") == CAPTURED
    assert "inspect before retrying" in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("writeProtocol", 1),
        ("generation", OTHER_GENERATION),
        ("revision", 8),
        ("writeToken", '"revision-7"'),
    ],
)
def test_prepared_session_cannot_synthesize_a_partial_token(
    tmp_path: pathlib.Path, field: str, value: board_state.JsonValue
) -> None:
    instance = workspace.Workspace(tmp_path)
    session: board_state.JsonObject = {
        "writeProtocol": 2,
        "generation": GENERATION,
        "revision": 7,
        "writeToken": CAPTURED,
    }
    session[field] = value
    try:
        with pytest.raises(ValueError, match=r"write (metadata|token)"):
            instance._session_endpoint(1, "browser-capability", "", session)
    finally:
        instance.close()


def test_prepared_read_error_does_not_expose_upstream_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    def respond(request: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert timeout > 0
        requests.append(request)
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "private-capability",
            Message(),
            BytesIO(b'{"error":"private-capability"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(board_state.BoardError, match="HTTP 500") as error:
        board_state._service_snapshot("http://127.0.0.1:1", "synthetic-capability")
    assert len(requests) == 1
    assert "private" not in str(error.value)
