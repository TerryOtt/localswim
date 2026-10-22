"""Shared gateway preferences use browser authentication and independent durable CAS."""

import http.client
import json
import sys
import threading
from typing import TYPE_CHECKING, cast

import pytest

from localswim import api_endpoint, workspace, workspace_config
from tests.test_api import request_json
from tests.test_workspace import RunningWorkspace, make_board
from tests.test_workspace import gateway as _gateway_fixture

gateway = _gateway_fixture

if TYPE_CHECKING:
    import pathlib
    import subprocess


def test_preferences_are_shared_durable_and_separate_from_board_revisions(
    gateway: RunningWorkspace,
) -> None:
    url = gateway.base + workspace.PREFERENCES_ROUTE
    status, initial = request_json(url)
    assert status == 200
    assert initial["pinnedBoards"] == []
    assert initial["pinsInitialized"] is False
    revision = initial["revision"]
    status, saved = request_json(
        url,
        token=gateway.instance.token,
        revision=revision,
        body={"action": "pin", "key": "alpha/board"},
    )
    assert status == 200
    assert saved["pinnedBoards"] == ["alpha/board"]
    assert saved["revision"] == revision + 1
    restarted = workspace.Workspace(gateway.root)
    assert restarted.settings.snapshot().to_json() == saved
    assert restarted.catalog()["preferences"] == saved
    assert [
        entry["key"] for entry in cast("list[dict[str, object]]", restarted.catalog()["boards"])
    ] == ["alpha/board", "beta/board"]
    assert not gateway.instance._children  # noqa: SLF001 -- proves preferences never start writers


def test_legacy_initialize_and_reordering_return_authoritative_preferences(
    gateway: RunningWorkspace,
) -> None:
    url = gateway.base + workspace.PREFERENCES_ROUTE
    revision = gateway.instance.settings.snapshot().revision
    commands: list[tuple[dict[str, object], list[str]]] = [
        ({"action": "initialize", "pinnedBoards": []}, []),
        (
            {"action": "initialize", "pinnedBoards": ["alpha/board", "beta/board"]},
            ["alpha/board", "beta/board"],
        ),
        ({"action": "initialize", "pinnedBoards": ["beta/board"]}, ["alpha/board", "beta/board"]),
        (
            {"action": "move", "key": "beta/board", "target": "alpha/board", "after": False},
            ["beta/board", "alpha/board"],
        ),
        ({"action": "unpin", "key": "beta/board"}, ["alpha/board"]),
    ]
    for command, expected in commands:
        status, snapshot = request_json(
            url, token=gateway.instance.token, revision=revision, body=command
        )
        assert status == 200
        assert snapshot["pinnedBoards"] == expected
        revision = snapshot["revision"]
    assert gateway.instance.settings.snapshot().pins_initialized is True


@pytest.mark.parametrize(
    ("token", "revision", "body", "expected"),
    [
        (None, 0, {"action": "pin", "key": "alpha/board"}, 401),
        ("wrong", 0, {"action": "pin", "key": "alpha/board"}, 401),
        ("valid", None, {"action": "pin", "key": "alpha/board"}, 428),
        ("valid", 999, {"action": "pin", "key": "alpha/board"}, 412),
        ("valid", 0, {"action": "pin", "key": "../outside"}, 400),
        ("valid", 0, {"action": "pin", "key": "alpha/board", "path": "other"}, 400),
        ("valid", 0, {"action": "autopush", "enabled": False}, 400),
    ],
)
def test_preferences_reject_invalid_mutations(
    gateway: RunningWorkspace,
    token: str | None,
    revision: int | None,
    body: dict[str, object],
    expected: int,
) -> None:
    before = gateway.instance.settings.path.read_bytes()
    status, _ = request_json(
        gateway.base + workspace.PREFERENCES_ROUTE,
        token=gateway.instance.token if token == "valid" else token,  # noqa: S105 -- fixture marker
        revision=revision,
        body=body,
    )
    assert status == expected
    assert gateway.instance.settings.path.read_bytes() == before


@pytest.mark.parametrize(
    ("revision", "body", "expected"),
    [
        ('"revision-nope"', b"{}", 400),
        ('"revision-0"', b'{"action":"pin","action":"unpin","key":"alpha/board"}', 400),
        ('"revision-0"', b"[]", 400),
        ('"revision-0"', b"{", 400),
        ('"revision-0"', b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}", 400),
        ('"revision-0"', b" " * (workspace.MAX_PREFERENCES_BYTES + 1), 413),
    ],
    ids=["bad-revision", "duplicate-field", "array", "malformed", "deep", "oversize"],
)
def test_preferences_bound_and_validate_json(
    gateway: RunningWorkspace, revision: str, body: bytes, expected: int
) -> None:
    before = gateway.instance.settings.path.read_bytes()
    connection = http.client.HTTPConnection("127.0.0.1", int(gateway.base.rsplit(":", 1)[1]))
    try:
        connection.request(
            "POST",
            workspace.PREFERENCES_ROUTE,
            body,
            {
                "Authorization": "Bearer " + gateway.instance.token,
                "If-Match": revision,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == expected
        response.read()
    finally:
        connection.close()
    assert gateway.instance.settings.path.read_bytes() == before
    status, result = request_json(
        gateway.base + workspace.PREFERENCES_ROUTE,
        token=gateway.instance.token,
        revision=0,
        body={"action": "pin", "key": "alpha/board"},
    )
    assert status == 200
    assert result["pinnedBoards"] == ["alpha/board"]


def test_same_root_gateways_have_one_revision_winner(gateway: RunningWorkspace) -> None:
    second = workspace.Workspace(gateway.root)
    server = workspace.WorkspaceServer((api_endpoint.HOST, 0), second)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        revision = second.settings.snapshot().revision
        status, _ = request_json(
            gateway.base + workspace.PREFERENCES_ROUTE,
            token=gateway.instance.token,
            revision=revision,
            body={"action": "pin", "key": "alpha/board"},
        )
        assert status == 200
        status, _ = request_json(
            f"http://127.0.0.1:{server.server_address[1]}" + workspace.PREFERENCES_ROUTE,
            token=second.token,
            revision=revision,
            body={"action": "pin", "key": "beta/board"},
        )
        assert status == 412
        assert second.settings.snapshot().pinned_boards == ("alpha/board",)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_persisted_autopush_is_inherited_only_by_new_board_services(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Board")
    workspace.Workspace(tmp_path, autopush=True)
    instance = workspace.Workspace(tmp_path)
    assert instance.autopush is True
    assert json.loads((tmp_path / workspace_config.CONFIG_FILENAME).read_text())["autopush"] is True
    launched: list[list[str]] = []
    endpoint = workspace.Endpoint(12345, "browser", "")

    def launch(arguments: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        launched.append(arguments)
        return cast("subprocess.Popen[bytes]", object())

    def existing(_path: pathlib.Path, **_kwargs: object) -> workspace.Endpoint:
        return endpoint

    def absent(_path: pathlib.Path, **_kwargs: object) -> None:
        return

    monkeypatch.setattr(workspace.subprocess, "Popen", launch)
    monkeypatch.setattr(instance, "_await_existing", existing)
    assert instance.endpoint(path) == endpoint
    assert launched == []
    monkeypatch.setattr(instance, "_await_existing", absent)
    monkeypatch.setattr(instance, "_existing", existing)
    assert instance.endpoint(path) == endpoint
    assert "--autopush" in launched[0]


@pytest.mark.parametrize("success", [False, True])
def test_gateway_shutdown_reports_sanitized_backup_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    success: bool,
) -> None:
    instance = workspace.Workspace(tmp_path)

    def stop() -> tuple[bool, str]:
        return success, "https://private:credential@example.invalid"

    monkeypatch.setattr(instance.publisher, "stop", stop)
    instance.stop_publishing()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        ""
        if success
        else "Gateway preferences backup failed during shutdown; local settings retained.\n"
    )


def _assert_preferences_unavailable(gateway: RunningWorkspace, revision: int) -> None:
    for route in (workspace.PREFERENCES_ROUTE, workspace.PREFIX):
        status, document = request_json(gateway.base + route)
        assert status == 503
        assert document == {"error": "preferences storage unavailable"}
    status, document = request_json(
        gateway.base + workspace.PREFERENCES_ROUTE,
        token=gateway.instance.token,
        revision=revision,
        body={"action": "pin", "key": "beta/board"},
    )
    assert status == 503
    assert document == {"error": "preferences storage unavailable"}


def _assert_preferences_recovered(
    gateway: RunningWorkspace, previous: workspace_config.GatewayPreferences
) -> None:
    for route in (workspace.PREFERENCES_ROUTE, workspace.PREFIX):
        status, document = request_json(gateway.base + route)
        assert status == 200
        assert (document if route == workspace.PREFERENCES_ROUTE else document["preferences"]) == (
            previous.to_json()
        )
    status, document = request_json(
        gateway.base + workspace.PREFERENCES_ROUTE,
        token=gateway.instance.token,
        revision=previous.revision,
        body={"action": "pin", "key": "beta/board"},
    )
    assert status == 200
    assert (
        document
        == workspace_config.GatewayPreferences(
            revision=previous.revision + 1,
            autopush=previous.autopush,
            pins_initialized=True,
            pinned_boards=(*previous.pinned_boards, "beta/board"),
        ).to_json()
    )


@pytest.mark.parametrize(
    "corrupt",
    [
        b'{"private-sentinel":',
        b"\xff",
        b'{"schema":1,"schema":1}',
        b"{}",
        b"[" * (sys.getrecursionlimit() * 2) + b"0" + b"]" * (sys.getrecursionlimit() * 2),
        b" " * (workspace_config.MAX_CONFIG_BYTES + 1),
    ],
    ids=["malformed", "encoding", "duplicate", "schema", "deep", "oversized"],
)
def test_corrupt_persisted_preferences_preserve_bytes_and_recover(
    gateway: RunningWorkspace, corrupt: bytes
) -> None:
    store = workspace_config.GatewayConfigStore(gateway.root, autopush=True)
    previous = store.apply(
        store.snapshot().revision,
        {"action": "pin", "key": "alpha/board"},
        {"alpha/board", "beta/board"},
    )
    valid = store.path.read_bytes()
    store.path.write_bytes(corrupt)
    _assert_preferences_unavailable(gateway, previous.revision)
    assert store.path.read_bytes() == corrupt
    store.path.write_bytes(valid)
    _assert_preferences_recovered(gateway, previous)


@pytest.mark.parametrize(
    "failure_type", [workspace_config.GatewayConfigUnavailableError, OSError, TimeoutError]
)
def test_persisted_preferences_boundary_failures_are_sanitized_and_recover(
    gateway: RunningWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    store = gateway.instance.settings
    previous = store.apply(0, {"action": "pin", "key": "alpha/board"}, {"alpha/board"})
    original = store.path.read_bytes()

    def fail() -> workspace_config.GatewayPreferences:
        raise failure_type("C:/private/path credential-sentinel saved-pin-sentinel")

    with monkeypatch.context() as failure:
        failure.setattr(store, "_read", fail)
        _assert_preferences_unavailable(gateway, previous.revision)
        assert store.path.read_bytes() == original
        status, _ = request_json(
            gateway.base + workspace.PREFERENCES_ROUTE,
            token="invalid",  # noqa: S106 -- deliberately invalid fixture credential
            revision=previous.revision,
            body={"action": "pin", "key": "beta/board"},
        )
        assert status == 401
    _assert_preferences_recovered(gateway, previous)


def test_unavailable_preferences_preserve_request_admission_order(
    gateway: RunningWorkspace,
) -> None:
    store = gateway.instance.settings
    valid = store.path.read_bytes()
    store.path.write_bytes(b"{")
    url = gateway.base + workspace.PREFERENCES_ROUTE
    status, _ = request_json(url, token=gateway.instance.token, body={"action": "erase"})
    assert status == 428
    status, document = request_json(
        url, token=gateway.instance.token, revision=0, body={"action": "erase"}
    )
    assert status == 503
    assert document == {"error": "preferences storage unavailable"}
    connection = http.client.HTTPConnection("127.0.0.1", int(gateway.base.rsplit(":", 1)[1]))
    try:
        connection.request(
            "POST",
            workspace.PREFERENCES_ROUTE,
            b"{",
            {"Authorization": "Bearer " + gateway.instance.token, "If-Match": '"revision-0"'},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "invalid preferences command"}
    finally:
        connection.close()
    assert store.path.read_bytes() == b"{"
    store.path.write_bytes(valid)
    status, _ = request_json(
        url, token=gateway.instance.token, revision=99, body={"action": "erase"}
    )
    assert status == 412
    status, _ = request_json(
        url, token=gateway.instance.token, revision=0, body={"action": "erase"}
    )
    assert status == 400
    assert store.path.read_bytes() == valid
