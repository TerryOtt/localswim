"""Real HTTP diagnostics under bounded, synthetic board-store contention."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from localswim import api_endpoint, service_ownership
from tests.test_api import api, request_json

if TYPE_CHECKING:
    from tests.test_api import RunningApi

__all__ = ["api"]


@pytest.mark.parametrize("modern", [False, True])
def test_snapshot_contention_keeps_readiness_strict_and_selects_lifecycle(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    *,
    modern: bool,
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
                **({"monitorIdentityVersion": 1} if modern else {}),
            }
        ),
    )
    before = api.path.read_bytes()

    # Hold the production mutex while the HTTP handler requests its snapshot.
    with store._mutex:  # noqa: SLF001 -- reproduce an occupied store critical section
        failed = service_ownership.probe_service_health(api.path)
        lifecycle = service_ownership.probe_monitor_lifecycle(api.path)

    assert not failed.healthy
    assert failed.phase == "response_headers"
    assert failed.code == "response_headers_failed"
    assert failed.error_type == "timeout"
    assert failed.elapsed_ms >= 800
    assert failed.probe_mode == "readiness"
    assert lifecycle.healthy is modern
    assert lifecycle.probe_mode == ("lifecycle_identity" if modern else "legacy_readiness")
    recovered = service_ownership.probe_service_health(api.path)
    assert recovered.healthy
    assert recovered.http_status == 200
    assert api.path.read_bytes() == before


def test_lifecycle_identity_does_not_certify_invalid_disk_readiness(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_ownership,
        "read_service_metadata",
        Mock(
            return_value={
                "schema": 1,
                "host": "127.0.0.1",
                "port": api.server.server_address[1],
                "board": str(api.path),
                "monitorIdentityVersion": 1,
            }
        ),
    )
    original = api.path.read_bytes()
    api.path.write_text("{", encoding="utf-8")
    try:
        lifecycle = service_ownership.probe_monitor_lifecycle(api.path)
        readiness = service_ownership.probe_service_health(api.path)
        status, payload = request_json(api.base + api_endpoint.API_PREFIX + "/status")
        assert lifecycle.healthy
        assert lifecycle.probe_mode == "lifecycle_identity"
        assert not readiness.healthy
        assert status == 200
        assert payload["ok"] is False
    finally:
        api.path.write_bytes(original)
    assert service_ownership.service_is_healthy(api.path)


def test_identity_is_cached_and_contains_no_session_data(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = service_ownership.board_identity(api.path)
    snapshot = Mock(side_effect=AssertionError("identity must not read the store"))
    monkeypatch.setattr(api_endpoint, "live_snapshot", snapshot)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", api.path.with_name("other-board.json"))

    status, payload = request_json(api.base + api_endpoint.API_PREFIX + "/monitor-identity")

    assert status == 200
    assert payload == {"schema": 1, "boardIdentity": expected}
    snapshot.assert_not_called()


def test_published_capability_and_transport_drain(
    api: RunningApi,
) -> None:
    api_endpoint.publish_service(api.path, api.server.server_address[1])
    try:
        descriptor = service_ownership.service_descriptor(api.path)
        assert descriptor["monitorIdentityVersion"] == 1
        assert service_ownership.probe_monitor_lifecycle(api.path).healthy
        api.server.begin_drain()
        assert not service_ownership.probe_monitor_lifecycle(api.path).healthy
    finally:
        api_endpoint.remove_service(api.path)
