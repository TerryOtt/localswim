"""Owner settings use bounded authenticated metadata, never board admission or replay."""

import json
import pathlib
from typing import cast
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from localswim import board_state, cli, service_ownership

TARGET = "0f351a87-f7ea-4da4-9a89-47a96a64abcb"
PRIVATE = "PRIVATE_TOKEN_PROSE_ERROR"


def settings(path: pathlib.Path) -> board_state.JsonObject:
    return {
        "schema": 1,
        "boardIdentity": service_ownership.board_identity(path),
        "threadId": TARGET,
        "enabled": False,
    }


def install(
    monkeypatch: pytest.MonkeyPatch, replies: list[tuple[int, bytes]]
) -> tuple[Mock, list[Mock]]:
    """Provide isolated HTTP peers and reject accidental full-board preflight."""
    monkeypatch.setattr(
        service_ownership,
        "service_descriptor",
        Mock(return_value={"port": 12345, "token": PRIVATE}),
    )
    monkeypatch.setattr(board_state, "load", Mock(side_effect=AssertionError("board read")))
    monkeypatch.setattr(
        service_ownership, "probe_cli_preflight", Mock(side_effect=AssertionError("admission"))
    )
    connections: list[Mock] = []
    for status, content in replies:
        response = Mock(status=status)
        response.read.return_value = content
        connection = Mock()
        connection.getresponse.return_value = response
        connections.append(connection)
    factory = Mock(side_effect=connections)
    monkeypatch.setattr(board_state.http.client, "HTTPConnection", factory)
    return factory, connections


def test_settings_identity_before_single_configuration_post(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "absent.json"
    current = settings(path)
    enabled = {**current, "enabled": True}
    factory, connections = install(
        monkeypatch, [(200, json.dumps(value).encode()) for value in (current, enabled)]
    )
    result = board_state.configure_remote_notifications(path, {"action": "enable"})
    assert result == enabled
    assert factory.call_count == 2
    assert [item.request.call_args.args[0] for item in connections] == ["GET", "POST"]
    for item in connections:
        assert item.request.call_args.args[1] == "/api/v001/notifications"
        assert item.request.call_args.kwargs["headers"]["Authorization"] == f"Bearer {PRIVATE}"
        item.getresponse.return_value.read.assert_called_once_with(4097)
        item.close.assert_called_once()
    assert json.loads(connections[1].request.call_args.kwargs["body"]) == {"action": "enable"}


@pytest.mark.parametrize(
    "failure",
    ["identity", "schema", "extra", "target", "duplicate", "oversize", "deep", "redirect"],
)
def test_bad_metadata_prevents_configuration_write(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "absent.json"
    value = settings(path)
    if failure == "identity":
        value["boardIdentity"] = "b" * 64
    elif failure == "schema":
        value["schema"] = True
    elif failure == "extra":
        value["private"] = PRIVATE
    elif failure == "target":
        value["threadId"] = PRIVATE
    content = json.dumps(value).encode()
    if failure == "duplicate":
        content = content.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
    elif failure == "oversize":
        content = b" " * 4097
    elif failure == "deep":
        content = b"[" * 1500 + b"]" * 1500
    factory, connections = install(monkeypatch, [(302 if failure == "redirect" else 200, content)])
    with pytest.raises(board_state.BoardError) as caught:
        board_state.configure_remote_notifications(path, {"action": "enable"})
    assert PRIVATE not in str(caught.value)
    assert factory.call_count == 1
    assert connections[0].request.call_args.args[0] == "GET"
    connections[0].close.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "malformed", "identity", "server", "read"])
def test_uncertain_configuration_is_never_replayed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "absent.json"
    value = settings(path)
    content = json.dumps({**value, "boardIdentity": "foreign"} if failure == "identity" else value)
    if failure == "malformed":
        content = PRIVATE
    factory, connections = install(
        monkeypatch,
        [
            (200, json.dumps(value).encode()),
            (503 if failure == "server" else 200, content.encode()),
        ],
    )
    if failure == "timeout":
        connections[1].getresponse.side_effect = TimeoutError(PRIVATE)
    elif failure == "read":
        connections[1].getresponse.return_value.read.side_effect = OSError(PRIVATE)
    with pytest.raises(board_state.BoardError, match="inspect status before retrying") as caught:
        board_state.configure_remote_notifications(path, {"action": "enable"})
    assert PRIVATE not in str(caught.value)
    assert factory.call_count == 2
    connections[1].request.assert_called_once()
    connections[1].close.assert_called_once()


@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        (["status", "--json"], None),
        (["target", TARGET.upper()], {"action": "target", "threadId": TARGET}),
        (["enable"], {"action": "enable"}),
        (["disable"], {"action": "disable"}),
    ],
)
def test_cli_settings_never_load_board(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    body: board_state.JsonObject | None,
) -> None:
    path = tmp_path / "absent.json"
    value = settings(path)
    configure = Mock(return_value=value)
    monkeypatch.setattr(board_state, "configure_remote_notifications", configure)
    monkeypatch.setattr(board_state, "load", Mock(side_effect=AssertionError("board read")))
    result = CliRunner().invoke(cli.cli, [str(path), "notifications", *arguments])
    assert result.exit_code == 0, result.output
    configure.assert_called_once_with(path.resolve(), body)
    if arguments[0] == "status":
        assert cast("board_state.JsonObject", json.loads(result.output)) == value


def test_cli_invalid_target_makes_no_request(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure = Mock()
    monkeypatch.setattr(board_state, "configure_remote_notifications", configure)
    result = CliRunner().invoke(
        cli.cli, [str(tmp_path / "absent.json"), "notifications", "target", "not-a-uuid"]
    )
    assert result.exit_code != 0
    configure.assert_not_called()
