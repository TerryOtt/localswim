"""CLI refusal diagnostics retain their original single pre-dispatch check."""

import json
import urllib.request
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from localswim import api_endpoint, board_state, service_ownership
from tests.test_api import api

if TYPE_CHECKING:
    import pathlib

    from tests.test_api import RunningApi

__all__ = ["api"]

PRIVATE = "PRIVATE_PATH_TOKEN_RESPONSE_ERROR"


@pytest.mark.parametrize("operation", ["mutation", "batch", "shutdown"])
@pytest.mark.parametrize(
    ("failure", "phase", "code"),
    [
        ("descriptor", "descriptor_read", "descriptor_unreadable"),
        ("response", "response_headers", "response_headers_failed"),
        ("metadata", "metadata_decode", "metadata_invalid"),
        ("identity", "identity", "identity_mismatch"),
        ("http", "http_status", "http_status_unexpected"),
    ],
)
def test_cli_preflight_failure_is_safe_and_never_dispatches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
    phase: str,
    code: str,
) -> None:
    path = tmp_path / PRIVATE
    descriptor_file = board_state.service_descriptor_path(path)
    descriptor_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor_file.write_text("{}", encoding="utf-8")
    descriptor: board_state.JsonObject = {
        "schema": 1,
        "host": "127.0.0.1",
        "port": 55204,
        "board": str(path),
        "token": PRIVATE,
    }
    read = Mock(return_value=descriptor)
    if failure == "descriptor":
        read.side_effect = PermissionError(PRIVATE)
    monkeypatch.setattr(service_ownership, "read_service_metadata", read)
    response = Mock(status=503 if failure == "http" else 200)
    identity = PRIVATE if failure == "identity" else service_ownership.board_identity(path)
    response.read.return_value = (
        PRIVATE.encode()
        if failure == "metadata"
        else json.dumps({"boardIdentity": identity, "token": PRIVATE}).encode()
    )
    connection = Mock()
    connection.getresponse.return_value = response
    if failure == "response":
        connection.getresponse.side_effect = TimeoutError(PRIVATE)
    connect = Mock(return_value=connection)
    monkeypatch.setattr(service_ownership.http.client, "HTTPConnection", connect)
    dispatch = Mock(side_effect=AssertionError("preflight must stop dispatch"))
    monkeypatch.setattr(board_state, "_service_json", dispatch)

    def call() -> object:
        if operation == "mutation":
            return board_state.apply_remote_mutation(path, board_state.API_PREFIX + "/cards", {})
        if operation == "batch":
            return board_state.apply_remote_priority_batch(
                path,
                ["alpha"],
                "P3",
                expected_revision=0,
            )
        return board_state.shutdown_service(path)

    try:
        with pytest.raises(board_state.BoardError, match="nothing changed") as caught:
            call()

        message = str(caught.value)
        assert PRIVATE not in message
        assert str(path) not in message
        assert "outcome is unknown" not in message
        diagnostic = json.loads(message.split("; preflight=", 1)[1])
        assert diagnostic["healthy"] is False
        assert diagnostic["phase"] == phase
        assert diagnostic["code"] == code
        assert diagnostic["probe_mode"] == "cli_preflight"
        assert diagnostic["elapsed_ms"] >= 0
        assert set(diagnostic) <= {
            "healthy",
            "phase",
            "code",
            "probe_mode",
            "elapsed_ms",
            "port",
            "http_status",
            "error_type",
        }
        dispatch.assert_not_called()
        read.assert_called_once_with(path)
        assert descriptor_file.read_text(encoding="utf-8") == "{}"
        if failure == "descriptor":
            connect.assert_not_called()
            assert diagnostic["error_type"] == "permission"
            assert "port" not in diagnostic
        else:
            connect.assert_called_once_with("127.0.0.1", 55204, timeout=5)
            connection.request.assert_called_once_with(
                "GET",
                board_state.API_PREFIX + "/browser-session",
            )
            connection.getresponse.assert_called_once()
            connection.close.assert_called_once()
            assert diagnostic["port"] == 55204
            if failure == "response":
                assert diagnostic["error_type"] == "timeout"
            if failure == "http":
                assert diagnostic["http_status"] == 503
    finally:
        descriptor_file.unlink(missing_ok=True)


def test_real_snapshot_contention_reports_original_five_second_preflight(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    store.execute(
        0,
        lambda board: board.create(
            "synthetic",
            "Private contention fixture",
            "backlog",
            "bot",
            detail="x" * 2_000_000,
        ),
    )
    metadata: board_state.JsonObject = {
        "schema": 1,
        "host": "127.0.0.1",
        "port": api.server.server_address[1],
        "board": str(api.path),
        "token": PRIVATE,
    }
    read = Mock(return_value=metadata)
    monkeypatch.setattr(service_ownership, "read_service_metadata", read)
    dispatch = Mock(side_effect=AssertionError("preflight must stop dispatch"))
    monkeypatch.setattr(board_state, "_service_json", dispatch)
    before = api.path.read_bytes()
    with store._mutex:  # noqa: SLF001 -- deterministic occupied store fixture
        with pytest.raises(board_state.BoardError) as caught:
            board_state.apply_remote_mutation(
                api.path,
                board_state.API_PREFIX + "/cards/synthetic/comment",
                {"text": "x"},
            )

    diagnostic = json.loads(str(caught.value).split("; preflight=", 1)[1])
    assert diagnostic["probe_mode"] == "cli_preflight"
    assert diagnostic["phase"] == "response_headers"
    assert diagnostic["error_type"] == "timeout"
    assert diagnostic["elapsed_ms"] >= 4500
    read.assert_called_once_with(api.path)
    dispatch.assert_not_called()
    descriptor, recovered = service_ownership.probe_cli_preflight(api.path)
    assert descriptor is metadata
    assert recovered.healthy
    assert api.path.read_bytes() == before


def test_post_dispatch_timeout_stays_unknown_without_an_extra_probe(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "board.json"
    read = Mock(
        return_value={
            "schema": 1,
            "host": "127.0.0.1",
            "port": 55204,
            "board": str(path),
            "token": PRIVATE,
        }
    )
    monkeypatch.setattr(service_ownership, "read_service_metadata", read)
    response = Mock(status=200)
    response.read.return_value = json.dumps(
        {
            "boardIdentity": service_ownership.board_identity(path),
        }
    ).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        service_ownership.http.client,
        "HTTPConnection",
        Mock(return_value=connection),
    )
    methods: list[str] = []

    def request(req: urllib.request.Request, *, timeout: float) -> BytesIO:
        assert timeout > 0
        methods.append(req.get_method())
        if req.get_method() == "POST":
            raise TimeoutError("synthetic transport timeout")
        return BytesIO(b'{"revision":7}')

    monkeypatch.setattr(board_state.urllib.request, "urlopen", request)
    with pytest.raises(board_state.BoardError, match="mutation outcome is unknown") as caught:
        board_state.apply_remote_mutation(
            path,
            board_state.API_PREFIX + "/cards/alpha/comment",
            {"text": "x"},
        )
    assert "nothing changed" not in str(caught.value)
    assert "preflight=" not in str(caught.value)
    assert methods == ["GET", "POST"]
    read.assert_called_once_with(path)
    connection.request.assert_called_once_with("GET", board_state.API_PREFIX + "/browser-session")
