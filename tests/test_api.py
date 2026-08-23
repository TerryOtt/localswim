"""Loopback REST contract tests using the real threaded handler."""

# ruff: noqa: SLF001 -- restart lifecycle tests intentionally inspect private state

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state
from tests.support import USERS

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

API = api_endpoint.API_PREFIX


@dataclass(frozen=True)
class RunningApi:
    base: str
    path: pathlib.Path


@pytest.fixture
def api(tmp_path: pathlib.Path) -> Iterator[RunningApi]:
    """Run the production handler against an isolated board."""
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="API", users=USERS, browser_user="terry", cli_user="bot", default_owner="bot"
    )
    board_state.save(board, path)
    api_endpoint.BOARD_PATH = path
    api_endpoint.STORE = api_endpoint.BoardStore(path)
    server = api_endpoint.http.server.ThreadingHTTPServer(
        (api_endpoint.HOST, 0), api_endpoint.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningApi(f"http://127.0.0.1:{server.server_address[1]}", path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api_endpoint.STORE = None


def request_json(
    url: str,
    *,
    token: str | None = None,
    revision: int | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return status and decoded JSON for both success and HTTP refusal."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if revision is not None:
        headers["If-Match"] = f'"revision-{revision}"'
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if body is not None else "GET"
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())
        except ConnectionAbortedError:
            if attempt:
                raise
            time.sleep(0.02)
    raise AssertionError("request retry fell through")


def assert_http_error(request: urllib.request.Request, expected: int) -> None:
    """Assert an HTTP refusal, retrying one transient Windows socket abort."""
    for attempt in range(2):
        try:
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
        except ConnectionAbortedError:
            if attempt:
                raise
            time.sleep(0.02)
        else:
            with caught.value:
                assert caught.value.code == expected
            return
    raise AssertionError("request retry fell through")


def test_board_and_status_reads(api: RunningApi) -> None:
    status_code, board = request_json(api.base + API + "/board")
    health_code, health = request_json(api.base + API + "/status")
    assert status_code == 200
    assert board["revision"] == 0
    assert health_code == 200
    assert health["ok"] is True
    assert health["restarting"] is False


def test_page_contains_guarded_auto_reload_and_state_restore(api: RunningApi) -> None:
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert "saveStaleReloadState();" in page
    assert "restoreStaleReloadState();" in page
    assert "window.location.replace(next.toString());" in page
    assert "sessionStorage.setItem(STALE_RELOAD_STATE_KEY" in page
    assert "saved.editing === 'detail'" in page
    assert "if (saved.make)" in page
    assert "AUTO-RELOAD FAILED" in page
    assert "const API_PREFIX = '/api/v001';" in page
    assert "display: flex; gap: 8px; align-items: flex-start; line-height: 1.15;" in page
    assert "/v1/" not in page
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_dirty_build_id_includes_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [
            api_endpoint.subprocess.CompletedProcess([], 0, stdout="deadbee\n", stderr=""),
            api_endpoint.subprocess.CompletedProcess(
                [], 0, stdout=" M api_endpoint.py\n", stderr=""
            ),
        ]
    )

    def next_reply(
        *_args: object, **_kwargs: object
    ) -> api_endpoint.subprocess.CompletedProcess[str]:
        return next(replies)

    monkeypatch.setattr(api_endpoint.subprocess, "run", next_reply)

    ident = api_endpoint.build_id()

    assert ident.startswith("deadbee-")
    assert ident.endswith("-dirty")
    assert len(ident.removeprefix("deadbee-").removesuffix("-dirty")) == 8


def test_valid_source_change_schedules_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = threading.Event()

    class FakeServer:
        def shutdown(self) -> None:
            stopped.set()

    changed = tuple((mtime + 1, digest + "-changed") for mtime, digest in api_endpoint._BOOT_CODE)
    monkeypatch.setattr(api_endpoint, "_code_stamp", lambda: changed)
    monkeypatch.setattr(api_endpoint, "_source_startup_problem", lambda: None)
    monkeypatch.setattr(api_endpoint, "RESTART_DEBOUNCE_S", 0)
    api_endpoint._restart_requested.clear()
    api_endpoint._restart_scheduled = False
    api_endpoint._restart_refused_stamp = None
    api_endpoint._restart_problem = None

    scheduled, problem = api_endpoint.request_code_restart(cast("Any", FakeServer()))

    assert scheduled is True
    assert problem is None
    assert stopped.wait(1)
    assert api_endpoint._restart_requested.is_set()
    api_endpoint._restart_requested.clear()
    api_endpoint._restart_scheduled = False


def test_invalid_source_change_keeps_server_and_reports_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = threading.Event()

    class FakeServer:
        def shutdown(self) -> None:
            stopped.set()

    changed = tuple((mtime + 1, digest + "-broken") for mtime, digest in api_endpoint._BOOT_CODE)
    message = "api_endpoint.py line 12: invalid syntax"
    monkeypatch.setattr(api_endpoint, "_code_stamp", lambda: changed)
    monkeypatch.setattr(api_endpoint, "_source_startup_problem", lambda: message)
    monkeypatch.setattr(api_endpoint, "RESTART_DEBOUNCE_S", 0)
    api_endpoint._restart_requested.clear()
    api_endpoint._restart_scheduled = False
    api_endpoint._restart_refused_stamp = None
    api_endpoint._restart_problem = None

    first = api_endpoint.request_code_restart(cast("Any", FakeServer()))
    deadline = time.monotonic() + 1
    while api_endpoint._restart_scheduled and time.monotonic() < deadline:
        time.sleep(0.01)
    second = api_endpoint.request_code_restart(cast("Any", FakeServer()))

    assert first == (True, None)
    assert second == (False, message)
    assert not stopped.is_set()
    assert not api_endpoint._restart_requested.is_set()
    api_endpoint._restart_refused_stamp = None
    api_endpoint._restart_problem = None


def test_source_syntax_problem_names_file_and_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = tmp_path / "valid.py"
    invalid = tmp_path / "invalid.py"
    valid.write_text("answer = 42\n", encoding="utf-8")
    invalid.write_text("if True print('no')\n", encoding="utf-8")
    monkeypatch.setattr(api_endpoint, "CODE_FILES", (valid, invalid))

    problem = api_endpoint._source_syntax_problem()

    assert problem is not None
    assert problem.startswith("invalid.py line 1:")


def test_source_startup_problem_reports_child_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_endpoint, "_source_syntax_problem", lambda: None)
    failed = api_endpoint.subprocess.CompletedProcess(
        [], 1, stdout="", stderr="Traceback\nNameError: broken startup\n"
    )

    def fail_run(
        *_args: object, **_kwargs: object
    ) -> api_endpoint.subprocess.CompletedProcess[str]:
        return failed

    monkeypatch.setattr(api_endpoint.subprocess, "run", fail_run)

    problem = api_endpoint._source_startup_problem()

    assert problem == "startup check failed: NameError: broken startup"


def test_requested_restart_reexecs_the_module_with_original_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []

    def record_exec(executable: str, args: list[str]) -> None:
        calls.append((executable, args))

    monkeypatch.setattr(api_endpoint.os, "execv", record_exec)
    api_endpoint._restart_requested.set()

    try:
        api_endpoint._reexec_if_requested()
    finally:
        api_endpoint._restart_requested.clear()

    assert calls == [
        (
            api_endpoint.sys.executable,
            [
                api_endpoint.sys.executable,
                "-m",
                "localswim.api_endpoint",
                *api_endpoint.sys.argv[1:],
            ],
        )
    ]


def test_authenticated_create_uses_browser_actor(api: RunningApi) -> None:
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Alpha", "state": "backlog"},
    )
    saved = board_state.load(api.path)
    assert code == 200
    assert response["revision"] == 1
    assert saved.find("1").history[0].by == "terry"


def test_missing_credential_returns_401(api: RunningApi) -> None:
    code, response = request_json(
        api.base + API + "/cards", revision=0, body={"subject": "Alpha", "state": "backlog"}
    )
    assert code == 401
    assert "credential" in response["error"]


def test_missing_revision_returns_428(api: RunningApi) -> None:
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        body={"subject": "Alpha", "state": "backlog"},
    )
    assert code == 428
    assert "If-Match" in response["error"]


def test_stale_revision_returns_412_without_write(api: RunningApi) -> None:
    request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Alpha", "state": "backlog"},
    )
    code, response = request_json(
        api.base + API + "/cards/1/comment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"text": "stale"},
    )
    saved = board_state.load(api.path)
    assert code == 412
    assert "refresh" in response["error"]
    assert saved.revision == 1
    assert saved.find("1").comments == []


def test_domain_refusal_returns_409(api: RunningApi) -> None:
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"id": "alpha", "subject": "Alpha", "state": "completed"},
    )
    assert code == 409
    assert "may not create" in response["error"]
    assert board_state.load(api.path).revision == 0


def test_malformed_json_returns_400(api: RunningApi) -> None:
    request = urllib.request.Request(
        api.base + API + "/cards",
        data=b"{",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_endpoint.BROWSER_TOKEN}",
            "Content-Type": "application/json",
            "If-Match": '"revision-0"',
        },
    )
    assert_http_error(request, 400)
    assert board_state.load(api.path).revision == 0


def test_unknown_route_returns_404(api: RunningApi) -> None:
    request = urllib.request.Request(api.base + API + "/unknown", data=b"{}", method="POST")
    assert_http_error(request, 404)


@pytest.mark.parametrize("route", ["/v1/board", "/data"])
def test_retired_read_routes_return_404(api: RunningApi, route: str) -> None:
    assert_http_error(urllib.request.Request(api.base + route), 404)


@pytest.mark.parametrize("route", ["/v1/status", "/mtime"])
def test_retired_status_routes_redirect_stale_tabs(
    api: RunningApi,
    route: str,
) -> None:
    with urllib.request.urlopen(api.base + route, timeout=5) as response:
        health = json.loads(response.read())
        assert response.url == api.base + API + "/status"
        assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert health["ok"] is True


def test_retired_mutation_route_returns_404(api: RunningApi) -> None:
    request = urllib.request.Request(
        api.base + "/v1/cards",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_endpoint.BROWSER_TOKEN}",
            "If-Match": '"revision-0"',
        },
    )
    assert_http_error(request, 404)
