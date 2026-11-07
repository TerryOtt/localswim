"""The monitor CLI consumes only bounded evidence with proven identity and range."""

import copy
import json
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from localswim import activity_feed, board_state, cli, monitor_activity_client, service_ownership

if TYPE_CHECKING:
    import pathlib

START = "2026-11-07T10:00:00+00:00"
END = "2026-11-07T10:00:15+00:00"
PRIVATE = "PRIVATE_CREDENTIAL_PROSE_ERROR"
STAMP = (1, 2, 3, 4, 5)


def envelope(path: pathlib.Path) -> board_state.JsonObject:
    """One safe covered event in the requested window."""
    return {
        "schema": 1,
        "boardIdentity": service_ownership.board_identity(path),
        "cursor": "a" * 32 + ":1",
        "coverage": {"complete": True, "code": "complete"},
        "range": {"since": START, "until": END},
        "fileIdentity": list(STAMP),
        "events": [{"ticket": 1, "id": "one", "kind": "commented", "at": START, "by": "bot"}],
    }


def install(
    _path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, data: bytes, *, status: int = 200
) -> tuple[Mock, Mock]:
    """Refuse any accidental board-content load or readiness preflight."""
    descriptor = Mock(return_value={"port": 12345, "token": PRIVATE, "monitorActivityVersion": 1})
    monkeypatch.setattr(service_ownership, "service_descriptor", descriptor)
    monkeypatch.setattr(board_state, "load", Mock(side_effect=AssertionError("no board load")))
    monkeypatch.setattr(
        service_ownership, "probe_cli_preflight", Mock(side_effect=AssertionError("no preflight"))
    )

    def fingerprint(_observed: pathlib.Path) -> tuple[int, ...]:
        return STAMP

    monkeypatch.setattr(activity_feed, "fingerprint", fingerprint)
    response = Mock(status=status)
    response.read.return_value = data
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        monitor_activity_client.http.client, "HTTPConnection", Mock(return_value=connection)
    )
    return descriptor, connection


def test_complete_monitor_activity_is_one_metadata_get(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    expected = envelope(path)
    descriptor, connection = install(path, monkeypatch, json.dumps(expected).encode())
    assert monitor_activity_client.read_monitor_activity(path, START, END) == expected
    descriptor.assert_called_once_with(path, require_cli_token=True)
    connection.getresponse.assert_called_once()
    method, route = connection.request.call_args.args
    assert method == "GET"
    assert route.startswith("/api/v001/monitor-activity?")
    assert PRIVATE not in route
    assert connection.request.call_args.kwargs["headers"] == {"Authorization": "Bearer " + PRIVATE}
    connection.close.assert_called_once()


def wire_data(value: board_state.JsonObject, failure: str) -> bytes:
    data = json.dumps(value).encode()
    if failure == "duplicate":
        return data.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
    if failure == "decode":
        return PRIVATE.encode()
    if failure == "oversize":
        return b" " * (activity_feed.MAX_RESPONSE_BYTES + 1)
    return data


@pytest.mark.parametrize(
    "failure",
    [
        "identity",
        "extra",
        "coverage",
        "empty",
        "range",
        "timestamp",
        "kind",
        "prose",
        "ticket",
        "stamp",
        "cursor",
        "duplicate",
        "decode",
        "oversize",
        "http",
        "close",
    ],
)
def test_unproven_or_private_feed_never_reaches_filter(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / PRIVATE
    value = envelope(path)
    event = cast("board_state.JsonObject", cast("list[board_state.JsonValue]", value["events"])[0])
    if failure == "identity":
        value["boardIdentity"] = "wrong"
    elif failure == "extra":
        value["token"] = PRIVATE
    elif failure == "coverage":
        value["coverage"] = {"complete": True, "code": "coverage_gap"}
    elif failure == "empty":
        value["events"] = []
    elif failure == "range":
        value["range"] = {"since": END, "until": END}
    elif failure == "timestamp":
        event["at"] = "2026-11-07T10:00:16+00:00"
    elif failure == "kind":
        event["kind"] = "unrecognized"
    elif failure == "prose":
        event["text"] = PRIVATE
    elif failure == "ticket":
        event["ticket"] = True
    elif failure == "stamp":
        value["fileIdentity"] = [9, 2, 3, 4, 5]
    elif failure == "cursor":
        value["cursor"] = PRIVATE
    data = wire_data(value, failure)
    _, connection = install(path, monkeypatch, data, status=503 if failure == "http" else 200)
    if failure == "close":
        connection.close.side_effect = OSError(PRIVATE)
    with pytest.raises(board_state.BoardError) as caught:
        monitor_activity_client.read_monitor_activity(path, START, END)
    assert PRIVATE not in str(caught.value)
    assert str(path) not in str(caught.value)
    assert "coverage is unproven" in str(caught.value)
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_incomplete_metadata_is_explicit_and_never_claims_coverage(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    value = envelope(path)
    value["coverage"] = {"complete": False, "code": "startup_gap"}
    value["fileIdentity"] = None
    install(path, monkeypatch, json.dumps(value).encode())
    assert monitor_activity_client.read_monitor_activity(path, START, END) == value


def test_monitor_cli_does_not_load_the_board(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    value = envelope(path)
    read = Mock(return_value=copy.deepcopy(value))
    monkeypatch.setattr(monitor_activity_client, "read_monitor_activity", read)
    monkeypatch.setattr(board_state, "load", Mock(side_effect=AssertionError("no board load")))
    result = CliRunner().invoke(
        cli.cli, [str(path), "activity", "monitor-between", START, END, "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == value
    read.assert_called_once_with(path.resolve(), START, END, None)


def test_equivalent_range_offsets_are_validated_as_instants(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    value = envelope(path)
    value["range"] = {"since": "2026-11-07T05:00:00-05:00", "until": "2026-11-07T05:00:15-05:00"}
    install(path, monkeypatch, json.dumps(value).encode())
    assert monitor_activity_client.read_monitor_activity(path, START, END) == value
