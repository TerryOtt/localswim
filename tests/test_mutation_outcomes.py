"""Conservative mutation outcomes across storage replacement and CLI dispatch."""

import json
import pathlib
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO

import pytest

from localswim import api_endpoint, board_state
from tests.test_api import request_json
from tests.test_cli import run_cli, served_board

__all__ = ["served_board"]


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("client", ["api", "cli"])
def test_storage_failure_reports_unknown_without_replaying(
    served_board: pathlib.Path, monkeypatch: pytest.MonkeyPatch, phase: str, client: str
) -> None:
    original_save = board_state.save
    writes = 0

    def failed_save(
        board: board_state.Board,
        path: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        nonlocal writes
        writes += 1
        if phase == "after":
            original_save(board, path, outcome=outcome)
        raise OSError("private storage details must not reach clients")

    monkeypatch.setattr(board_state, "save", failed_save)
    if client == "api":
        descriptor = json.loads(board_state.service_descriptor_path(served_board).read_text())
        status, response = request_json(
            f"http://127.0.0.1:{descriptor['port']}" + api_endpoint.API_PREFIX + "/cards",
            token=api_endpoint.BROWSER_TOKEN,
            revision=0,
            body={"id": "once", "subject": "Once", "state": "backlog"},
        )
        assert status == 503
        assert response["code"] == "mutation_outcome_unknown"
        assert "inspect before retrying" in response["error"]
        assert "private" not in response["error"]
    else:
        result = run_cli(served_board, "card", "create", "once", "Once", "--state", "backlog")
        assert result.returncode != 0
        assert "mutation outcome is unknown" in result.stderr
        assert "inspect before retrying" in result.stderr
        assert "refused the command" not in result.stderr
        assert "private" not in result.stderr
        assert result.stdout == ""
    persisted = board_state.load(served_board)
    assert writes == 1
    assert persisted.revision == (1 if phase == "after" else 0)
    assert len(persisted.items) == (1 if phase == "after" else 0)


@pytest.mark.parametrize("status", [200, 409, 500, 503])
def test_cli_explicit_unknown_or_server_failure_never_replays(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0
    payload = json.dumps(
        {
            "error": "private failure detail",
            "code": "mutation_outcome_unknown" if status < 500 else "other_server_failure",
        }
    ).encode()

    def response(_request: urllib.request.Request, *, timeout: float) -> BytesIO:
        nonlocal calls
        del timeout
        calls += 1
        if status != 200:
            raise urllib.error.HTTPError(
                "http://127.0.0.1:12345/", status, "failure", Message(), BytesIO(payload)
            )
        return BytesIO(payload)

    monkeypatch.setattr(urllib.request, "urlopen", response)
    with pytest.raises(board_state.BoardError, match="mutation outcome is unknown") as failure:
        board_state._service_json(  # noqa: SLF001 -- shared CLI transport contract
            "http://127.0.0.1:12345/", "test-token", body={}, revision=0
        )
    assert calls == 1
    assert "private" not in str(failure.value)
