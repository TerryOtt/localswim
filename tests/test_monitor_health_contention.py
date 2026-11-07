"""Real HTTP diagnostics under bounded, synthetic board-store contention."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from localswim import api_endpoint, service_ownership
from tests.test_api import api

if TYPE_CHECKING:
    from tests.test_api import RunningApi

__all__ = ["api"]


def test_snapshot_contention_reports_timeout_and_recovers(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    store.execute(
        0,
        lambda board: board.create(
            "synthetic",
            "Synthetic contention fixture",
            "backlog",
            "bot",
            detail="x" * 2_000_000,
        ),
    )
    monkeypatch.setattr(
        service_ownership,
        "read_service_metadata",
        Mock(
            return_value={
                "schema": 1,
                "host": "127.0.0.1",
                "port": api.server.server_address[1],
                "board": str(api.path),
            }
        ),
    )
    before = api.path.read_bytes()

    # Hold the production mutex while the HTTP handler requests its snapshot.
    with store._mutex:  # noqa: SLF001 -- reproduce an occupied store critical section
        failed = service_ownership.probe_service_health(api.path)

    assert not failed.healthy
    assert failed.phase == "response_headers"
    assert failed.code == "response_headers_failed"
    assert failed.error_type == "timeout"
    assert failed.elapsed_ms >= 800
    recovered = service_ownership.probe_service_health(api.path)
    assert recovered.healthy
    assert recovered.http_status == 200
    assert api.path.read_bytes() == before
