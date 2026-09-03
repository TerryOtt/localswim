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
    api_endpoint._shutdown_requested.clear()
    server = api_endpoint.http.server.ThreadingHTTPServer(
        (api_endpoint.HOST, 0), api_endpoint.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningApi(f"http://127.0.0.1:{server.server_address[1]}", path)
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api_endpoint._shutdown_requested.clear()
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
    assert "function alignCardStarts()" in page
    assert "alignCardStarts();\n  playFlip(before);" in page
    assert '<span id="wordmark">localswim</span>' in page
    assert '<span id="title">API</span>' in page
    assert "#wordmark { color: #85B8FF; }" in page
    assert "#title { color: var(--barink); margin-left: 22px; }" in page
    assert "boardTitle = data.project;" in page
    assert "document.getElementById('title').textContent = boardTitle;" in page
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


def test_dirty_build_refreshes_after_clean_commit_when_loaded_sources_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = iter(("cafebabe", "cafebabe"))
    calls = 0

    def next_build() -> str:
        nonlocal calls
        calls += 1
        return next(candidates)

    monkeypatch.setattr(api_endpoint, "BUILD", "deadbee-12345678-dirty")
    monkeypatch.setattr(api_endpoint, "_next_build_refresh_at", 0.0)
    monkeypatch.setattr(api_endpoint.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(api_endpoint, "build_id", next_build)
    monkeypatch.setattr(api_endpoint, "_BOOT_CODE", ((1.0, "alpha"), (1.0, "beta")))
    monkeypatch.setattr(api_endpoint, "_code_stamp", lambda: ((2.0, "alpha"), (2.0, "beta")))

    refreshed = api_endpoint.refresh_build_id_if_clean()

    assert refreshed == "cafebabe"
    assert api_endpoint.BUILD == "cafebabe"
    assert calls == 2


def test_dirty_build_refresh_refuses_source_bytes_the_process_did_not_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_endpoint, "BUILD", "deadbee-12345678-dirty")
    monkeypatch.setattr(api_endpoint, "_next_build_refresh_at", 0.0)
    monkeypatch.setattr(api_endpoint.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(api_endpoint, "build_id", lambda: "cafebabe")
    monkeypatch.setattr(api_endpoint, "_BOOT_CODE", ((1.0, "alpha"), (1.0, "beta")))
    monkeypatch.setattr(
        api_endpoint,
        "_code_stamp",
        lambda: ((2.0, "alpha-changed"), (2.0, "beta")),
    )

    refreshed = api_endpoint.refresh_build_id_if_clean()

    assert refreshed == "deadbee-12345678-dirty"


def test_dirty_build_refresh_refuses_disagreeing_git_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = iter(("cafebabe", "facefeed"))
    monkeypatch.setattr(api_endpoint, "BUILD", "deadbee-12345678-dirty")
    monkeypatch.setattr(api_endpoint, "_next_build_refresh_at", 0.0)
    monkeypatch.setattr(api_endpoint.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(api_endpoint, "build_id", lambda: next(candidates))
    monkeypatch.setattr(api_endpoint, "_BOOT_CODE", ((1.0, "alpha"), (1.0, "beta")))
    monkeypatch.setattr(api_endpoint, "_code_stamp", lambda: ((2.0, "alpha"), (2.0, "beta")))

    refreshed = api_endpoint.refresh_build_id_if_clean()

    assert refreshed == "deadbee-12345678-dirty"


def test_dirty_build_refresh_throttles_git_checks_while_checkout_stays_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    times = iter((10.0, 11.0))

    def still_dirty() -> str:
        nonlocal calls
        calls += 1
        return "deadbee-12345678-dirty"

    monkeypatch.setattr(api_endpoint, "BUILD", "deadbee-12345678-dirty")
    monkeypatch.setattr(api_endpoint, "_next_build_refresh_at", 0.0)
    monkeypatch.setattr(api_endpoint.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(api_endpoint, "build_id", still_dirty)

    first = api_endpoint.refresh_build_id_if_clean()
    second = api_endpoint.refresh_build_id_if_clean()

    assert first == "deadbee-12345678-dirty"
    assert second == "deadbee-12345678-dirty"
    assert calls == 1


def test_status_uses_refreshed_clean_build_id(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_endpoint, "code_is_stale", lambda: False)
    monkeypatch.setattr(api_endpoint, "refresh_build_id_if_clean", lambda: "cafebabe")

    code, health = request_json(api.base + API + "/status")

    assert code == 200
    assert health["build"] == "cafebabe"


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


def test_create_can_atomically_add_relationships(api: RunningApi) -> None:
    first_code, _first = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Target", "state": "backlog"},
    )
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={
            "subject": "New card",
            "state": "backlog",
            "links": [{"kind": "blocks", "other": "target"}],
        },
    )

    saved = board_state.load(api.path)
    assert first_code == 200
    assert code == 200
    assert response["revision"] == 2
    assert saved.links_for("new-card") == [("blocks", "target")]
    assert saved.links_for("target") == [("blocked_by", "new-card")]
    assert saved.relationship_history[-1].by == "terry"


def test_failed_create_relationship_rolls_back_card_and_every_link(api: RunningApi) -> None:
    first_code, _first = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Target", "state": "backlog"},
    )
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={
            "subject": "New card",
            "state": "backlog",
            "links": [
                {"kind": "blocks", "other": "target"},
                {"kind": "relates_to", "other": "missing"},
            ],
        },
    )

    saved = board_state.load(api.path)
    assert first_code == 200
    assert code == 409
    assert "no item" in response["error"]
    assert saved.revision == 1
    assert [item.id for item in saved.items] == ["target"]
    assert saved.links == []
    assert saved.relationship_history == []


def test_new_card_dialog_exposes_repeatable_relationship_controls(api: RunningApi) -> None:
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert 'id="mk-relations"' in page
    assert 'id="mk-rel-add"' in page
    assert "links: relationshipSpecs(document.getElementById('mk-relations'))" in page


def test_existing_relationship_can_be_added_retyped_and_removed(api: RunningApi) -> None:
    request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Alpha", "state": "backlog"},
    )
    request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={"subject": "Beta", "state": "backlog"},
    )
    add_code, add_response = request_json(
        api.base + API + "/cards/alpha/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"kind": "blocks", "other": "beta"},
    )
    replace_code, replace_response = request_json(
        api.base + API + "/cards/alpha/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=3,
        body={"kind": "relates_to", "other": "beta", "replaces": "blocks"},
    )
    replaced = board_state.load(api.path)
    payload_code, payload = request_json(api.base + API + "/board")
    remove_code, remove_response = request_json(
        api.base + API + "/cards/alpha/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=4,
        body={"kind": "relates_to", "other": "beta", "remove": True},
    )

    assert (add_code, add_response["revision"]) == (200, 3)
    assert (replace_code, replace_response["revision"]) == (200, 4)
    assert replaced.links_for("alpha") == [("relates_to", "beta")]
    assert replaced.links_for("beta") == [("relates_to", "alpha")]
    alpha_payload = next(
        item for lane in payload["lanes"] for item in lane["items"] if item["id"] == "alpha"
    )
    assert payload_code == 200
    assert alpha_payload["links"][0]["id"] == "beta"
    assert [change.action for change in replaced.relationship_history[-2:]] == [
        "unlinked",
        "linked",
    ]
    assert (remove_code, remove_response["revision"]) == (200, 5)
    assert board_state.load(api.path).links == []


def test_failed_http_link_replacement_keeps_original_type(api: RunningApi) -> None:
    request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=0,
        body={"subject": "Alpha", "state": "backlog"},
    )
    request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={"subject": "Beta", "state": "backlog"},
    )
    request_json(
        api.base + API + "/cards/alpha/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"kind": "blocks", "other": "beta"},
    )

    code, _response = request_json(
        api.base + API + "/cards/alpha/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=3,
        body={"kind": "not_a_kind", "other": "beta", "replaces": "blocks"},
    )

    saved = board_state.load(api.path)
    assert code == 409
    assert saved.revision == 3
    assert saved.links_for("alpha") == [("blocks", "beta")]
    assert [change.action for change in saved.relationship_history] == ["linked"]


def test_card_drawer_exposes_relationship_add_change_and_remove_controls(
    api: RunningApi,
) -> None:
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert 'id="p-rel-add"' in page
    assert 'aria-label="Add relationship" title="Add relationship">+</button>' in page
    assert "#p-rel-h { display: flex; align-items: center; gap: 5px; }" in page
    assert "return items.sort((left, right) => (" in page
    assert "Number.parseInt(left.ticket.slice(1), 10)" in page
    assert "Number.parseInt(right.ticket.slice(1), 10)" in page
    assert "replaces: ref.kind" in page
    assert "remove: true" in page


def test_card_drawer_puts_comment_entry_before_comment_history(api: RunningApi) -> None:
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert page.index('id="say"') < page.index('id="p-comments"')


def test_card_drawer_renders_newest_comments_first(api: RunningApi) -> None:
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert "for (const c of [...it.comments].reverse())" in page


def test_missing_credential_returns_401(api: RunningApi) -> None:
    code, response = request_json(
        api.base + API + "/cards", revision=0, body={"subject": "Alpha", "state": "backlog"}
    )
    assert code == 401
    assert "credential" in response["error"]


def test_shutdown_requires_cli_credential(api: RunningApi) -> None:
    code, response = request_json(api.base + API + "/shutdown", body={})

    assert code == 401
    assert "credential" in response["error"]
    assert not api_endpoint._shutdown_requested.is_set()


def test_authenticated_shutdown_flushes_and_stops(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def successful_flush(_path: pathlib.Path) -> tuple[bool, str]:
        return True, "no board change to commit"

    monkeypatch.setattr(
        api_endpoint,
        "flush_autopush_for_shutdown",
        successful_flush,
    )

    code, response = request_json(
        api.base + API + "/shutdown",
        token=api_endpoint.CLI_TOKEN,
        body={},
    )

    assert code == 200
    assert response == {
        "result": "board service shutdown scheduled",
        "push": "no board change to commit",
    }
    assert api_endpoint._shutdown_requested.is_set()


def test_failed_shutdown_flush_leaves_service_running(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_flush(_path: pathlib.Path) -> tuple[bool, str]:
        return False, "push failed"

    monkeypatch.setattr(
        api_endpoint,
        "flush_autopush_for_shutdown",
        failed_flush,
    )

    code, response = request_json(
        api.base + API + "/shutdown",
        token=api_endpoint.CLI_TOKEN,
        body={},
    )

    assert code == 503
    assert response == {"error": "final autopush failed: push failed"}
    assert not api_endpoint._shutdown_requested.is_set()


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
