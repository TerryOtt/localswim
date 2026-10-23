"""Loopback REST contract tests using the real threaded handler."""

# ruff: noqa: SLF001 -- restart lifecycle tests intentionally inspect private state

import base64
import contextlib
import http.client
import json
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from localswim import api_endpoint, attachments, board_state
from tests.support import USERS

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator

API = api_endpoint.API_PREFIX


@pytest.mark.parametrize(
    ("raw", "rendered"),
    [
        ("`a * b ** c`", "<code>a * b ** c</code>"),
        ("`x < y > z` and `a -- b`", "<code>x &lt; y &gt; z</code> and <code>a -- b</code>"),
        ("**bold `a*b`** *soft*", "<strong>bold <code>a*b</code></strong> <em>soft</em>"),
        ("`unmatched **bold**", "`unmatched <strong>bold</strong>"),
        (
            "<script>**bold**</script> *soft*",
            "&lt;script&gt;<strong>bold</strong>&lt;/script&gt; <em>soft</em>",
        ),
        ("`<script>**x**</script>`", "<code>&lt;script&gt;**x**&lt;/script&gt;</code>"),
        ("## Heading `a*b`\nbody", '<b class="mdh">Heading <code>a*b</code></b>body'),
        ("\x00code0\x00 `literal*`", "\x00code0\x00 <code>literal*</code>"),
    ],
)
def test_inline_keeps_code_literal(raw: str, rendered: str) -> None:
    assert api_endpoint.inline(raw) == rendered


@dataclass(frozen=True)
class RunningApi:
    base: str
    path: pathlib.Path
    server: api_endpoint.BoardHttpServer


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
    server = api_endpoint.BoardHttpServer((api_endpoint.HOST, 0), api_endpoint.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningApi(f"http://127.0.0.1:{server.server_address[1]}", path, server)
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


def test_board_payload_keeps_literal_subject_and_raw_detail(api: RunningApi) -> None:
    raw = "`a * b ** c` & <literal>"
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"subject": "x < y > z", "state": "backlog", "detail": raw},
    )
    assert code == 200
    code, _ = request_json(
        api.base + API + "/cards/1/comment",
        token=api_endpoint.CLI_TOKEN,
        revision=1,
        body={"text": raw},
    )
    assert code == 200
    code, board = request_json(api.base + API + "/board")
    assert code == 200
    item = next(item for lane in board["lanes"] for item in lane["items"])
    assert item["subject"] == "x < y > z"
    assert item["detailRaw"] == raw
    assert item["detail"] == "<code>a * b ** c</code> &amp; &lt;literal&gt;"
    assert item["comments"][0]["text"] == item["detail"]


@pytest.fixture
def status_loads(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    """Count actual admission loads without substituting their validation."""
    original = board_state.load
    calls: list[pathlib.Path] = []

    def counted(
        path: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> board_state.Board:
        calls.append(path)
        return original(path, policy, install_context=install_context)

    monkeypatch.setattr(board_state, "load", counted)
    return calls


def break_status_disk(api: RunningApi, fault: str, patch: pytest.MonkeyPatch) -> None:
    """Damage only the synthetic snapshot or deterministically refuse its I/O."""
    original_stat = pathlib.Path.stat

    def failed_read(_path: pathlib.Path) -> board_state.JsonObject:
        if fault == "decoder_recursion":
            raise RecursionError("injected snapshot decoder nesting failure")
        raise OSError("injected snapshot read failure")

    def failed_stat(path: pathlib.Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == api.path:
            raise OSError("injected snapshot stat failure")
        return original_stat(path, follow_symlinks=follow_symlinks)

    if fault == "missing":
        api.path.unlink()
    elif fault in {"read", "decoder_recursion"}:
        patch.setattr(board_state, "_read_json", failed_read)
    elif fault == "stat":
        patch.setattr(pathlib.Path, "stat", failed_stat)
    elif fault == "malformed":
        api.path.write_text("{", encoding="utf-8")
    else:
        candidate = board_state.load(api.path, install_context=False)
        candidate.create("invalid", "Invalid audit", "backlog", "bot")
        candidate.find("invalid").archived = True
        board_state.save(candidate, api.path)
        assert candidate.verify()


@pytest.mark.parametrize(
    "fault", ["missing", "read", "stat", "malformed", "audit", "decoder_recursion"]
)
def test_status_contains_disk_failure_and_recovers(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    status_loads: list[pathlib.Path],
    fault: str,
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    accepted = store._board.to_json()
    restored = board_state.Board.from_json(accepted, "recovery", install_context=False)
    policy_doc = restored.policy.to_json()
    policy_doc["defaultPriority"] = "P4"
    restored.policy = board_state.TransitionPolicy.from_json(policy_doc, "recovery policy")
    restored.project = "Recovered disk"

    def refused_restart(_server: api_endpoint.BoardHttpServer) -> tuple[bool, str | None]:
        return False, "refused"

    monkeypatch.setattr(api_endpoint, "code_is_stale", lambda: True)
    monkeypatch.setattr(api_endpoint, "BUILD", "stale-build")
    monkeypatch.setattr(api_endpoint, "request_code_restart", refused_restart)
    monkeypatch.setattr(api_endpoint, "push_status", lambda: {"enabled": True, "error": "push"})
    with monkeypatch.context() as broken:
        break_status_disk(api, fault, broken)
        status_loads.clear()
        code, health = request_json(api.base + API + "/status")
    assert code == 200
    assert health["ok"] is False
    assert health["mtime"] == 0
    assert health["stamp"] == "unreadable"
    assert health["error"]
    assert health["build"] == "stale-build"
    assert health["codeStale"] is True
    assert health["restarting"] is False
    assert health["restartProblem"] == "refused"
    assert health["push"] == {"enabled": True, "error": "push"}
    assert status_loads == [api.path]
    assert store._board.to_json() == accepted
    if fault == "decoder_recursion":
        assert "snapshot decoder nesting failure" in str(store.admission_error)
        assert "retaining last accepted board and policy" in health["error"]

    board_state.save(restored, api.path)
    code, health = request_json(api.base + API + "/status")
    assert code == 200
    assert health["ok"] is True
    assert health["mtime"] == api.path.stat().st_mtime
    assert health["stamp"] != "unreadable"
    assert "error" not in health
    assert status_loads == [api.path, api.path]
    assert store._board.project == "Recovered disk"
    assert store.policy.default_priority == "P4"
    assert store.admission_error is None


def test_status_uses_one_candidate_for_policy_and_health(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    status_loads: list[pathlib.Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    candidate = board_state.Board.from_json(store._board.to_json(), "first", install_context=False)
    policy_doc = candidate.policy.to_json()
    policy_doc["defaultPriority"] = "P4"
    candidate.policy = board_state.TransitionPolicy.from_json(policy_doc, "first policy")
    board_state.save(candidate, api.path)
    original = board_state.load

    def replace_after_read(
        path: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> board_state.Board:
        loaded = original(path, policy, install_context=install_context)
        path.write_text("{", encoding="utf-8")
        return loaded

    monkeypatch.setattr(board_state, "load", replace_after_read)
    code, health = request_json(api.base + API + "/status")
    assert code == 200
    assert health["ok"] is True
    assert status_loads == [api.path]
    assert store.policy.default_priority == "P4"
    assert "embedded policy reloaded" in capsys.readouterr().out

    # The next request must reject the new disk bytes, not trust retained memory.
    code, health = request_json(api.base + API + "/status")
    assert code == 200
    assert health["ok"] is False
    assert status_loads == [api.path, api.path]
    assert store.policy.default_priority == "P4"
    assert "embedded policy reloaded" not in capsys.readouterr().out


@pytest.mark.parametrize("error_type", [OSError, RecursionError])
def test_status_contains_policy_reload_failure(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    status_loads: list[pathlib.Path],
    error_type: type[Exception],
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    store._external_policy = True
    accepted_policy = store.policy
    accepted_board = store._board.to_json()

    def refused_policy(
        _self: board_state.TransitionPolicy,
        _actors: set[str],
        _configured: frozenset[str],
    ) -> tuple[board_state.TransitionPolicy, str | None]:
        raise error_type("injected policy reload failure")

    with monkeypatch.context() as broken:
        broken.setattr(board_state.TransitionPolicy, "reload_if_changed", refused_policy)
        code, health = request_json(api.base + API + "/status")
    assert code == 200
    assert health["ok"] is False
    assert health["error"] == "injected policy reload failure"
    assert health["mtime"] == 0
    assert health["stamp"] == "unreadable"
    assert "build" in health
    assert "codeStale" in health
    assert "restarting" in health
    assert "push" in health
    assert status_loads == []
    assert store.policy is accepted_policy
    assert store._board.to_json() == accepted_board
    assert request_json(api.base + API + "/status")[1]["ok"] is True
    assert status_loads == [api.path]


def test_admission_does_not_normalize_unexpected_runtime_error(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    assert store.path == api.path
    accepted = store._board.to_json()

    def unexpected_failure(_path: pathlib.Path) -> board_state.JsonObject:
        raise RuntimeError("unexpected implementation failure")

    monkeypatch.setattr(board_state, "_read_json", unexpected_failure)
    with pytest.raises(RuntimeError, match="unexpected implementation failure"):
        store.snapshot()
    assert store._board.to_json() == accepted


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
    assert "let API_PREFIX = '/api/v001';" in page
    assert "display: flex; gap: 8px; align-items: flex-start; line-height: 1.15;" in page
    assert "function alignCardStarts()" in page
    assert "if (laneScroll) window.WorkspaceUI.restoreScroll(laneScroll);" in page
    assert '<span id="wordmark">localswim</span>' in page
    assert '<span id="title">API</span>' in page
    assert "#wordmark { color: #85B8FF; }" in page
    assert "#title { color: var(--barink); margin-left: 22px; }" in page
    assert "boardTitle = data.project;" in page
    assert "document.getElementById('title').textContent = boardTitle;" in page
    assert "/v1/" not in page
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def _contrast_luminance(color: str) -> float:
    """Apply the WCAG sRGB linearization and relative-luminance weights."""
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def _badge_color(value: str, variables: dict[str, str]) -> str:
    """Resolve the served badge stylesheet's literal or custom-property color."""
    variable = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
    color = variables[variable[1]] if variable else value
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", color), f"unsupported badge color {color!r}"
    return color


def test_priority_badges_have_enhanced_text_contrast(api: RunningApi) -> None:
    """Check the actual served palette rather than a separate expected-color table."""
    with urllib.request.urlopen(api.base + "/", timeout=5) as response:
        page = response.read().decode("utf-8")
    variables = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", page))
    base = re.search(r"\.pri\s*\{([^}]+)\}", page)
    assert base is not None
    base_properties = dict(re.findall(r"([a-z-]+):\s*([^;]+)", base[1]))
    badges = dict(re.findall(r"\.pri\.(P[0-5])\s*\{([^}]+)\}", page))
    assert set(badges) == {f"P{index}" for index in range(6)}
    for priority, declarations in badges.items():
        properties = dict(re.findall(r"([a-z-]+):\s*([^;]+)", declarations))
        foreground = _badge_color(properties.get("color", base_properties["color"]), variables)
        background = _badge_color(properties["background"], variables)
        luminances = sorted((_contrast_luminance(foreground), _contrast_luminance(background)))
        ratio = (luminances[1] + 0.05) / (luminances[0] + 0.05)
        assert ratio >= 7, f"{priority} contrast {ratio:.3f}:1 is below WCAG AAA's 7:1"
    assert float(base_properties["font-size"].removesuffix("px")) >= 12
    selector = re.search(r"#p-pri\s*\{([^}]+)\}", page)
    assert selector is not None
    selector_properties = dict(re.findall(r"([a-z-]+):\s*([^;]+)", selector[1]))
    assert float(selector_properties["font-size"].removesuffix("px")) >= 12


def test_dirty_build_id_includes_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def next_reply(
        args: list[str], **_kwargs: object
    ) -> api_endpoint.subprocess.CompletedProcess[str]:
        assert args[:2] == ["git", "-C"]
        reply = "deadbee\n" if "rev-parse" in args else " M api_endpoint.py\n"
        return api_endpoint.subprocess.CompletedProcess(args, 0, stdout=reply, stderr="")

    sources = {
        name: f"known source bytes: {name}\n".encode()
        for name in (
            "api_endpoint.py",
            "board_state.py",
            "attachments.py",
            "workspace.py",
            "workspace_config.py",
            "gateway_publish.py",
            "service_ownership.py",
            "workspace.js",
            "workspace.css",
        )
    }

    def source_bytes(path: pathlib.Path) -> bytes:
        return sources[path.name]

    monkeypatch.setattr(api_endpoint.subprocess, "run", next_reply)
    monkeypatch.setattr(pathlib.Path, "read_bytes", source_bytes)

    ident = api_endpoint.build_id()

    assert ident.startswith("deadbee-")
    assert ident.endswith("-dirty")
    assert len(ident.removeprefix("deadbee-").removesuffix("-dirty")) == 8
    assert api_endpoint.build_id() == ident

    for name in ("api_endpoint.py", "workspace.js", "workspace.css"):
        original = sources[name]
        sources[name] = original + b"changed bytes\n"
        changed = api_endpoint.build_id()
        assert re.fullmatch(r"deadbee-[0-9a-f]{8}-dirty", changed)
        assert changed != ident, name
        assert api_endpoint.build_id() == changed
        sources[name] = original
        assert api_endpoint.build_id() == ident

    def unreadable_source(_path: pathlib.Path) -> bytes:
        raise OSError("controlled unreadable source")

    monkeypatch.setattr(pathlib.Path, "read_bytes", unreadable_source)
    assert api_endpoint.build_id() == "deadbee-dirty"


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


@pytest.mark.parametrize("failure", ["stat", "read"])
@pytest.mark.parametrize("recovery", ["identical", "changed", "unreadable"])
def test_source_uncertainty_retries_at_unchanged_mtime(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    recovery: str,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"answer = 42\n")
    mtime = source.stat().st_mtime
    digest = api_endpoint.hashlib.sha256(source.read_bytes()).hexdigest()
    real_stat = pathlib.Path.stat
    real_read = pathlib.Path.read_bytes
    unavailable = True
    reads = 0

    def stat(path: pathlib.Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == source and unavailable and failure == "stat":
            raise OSError("synthetic stat failure")
        return real_stat(path, follow_symlinks=follow_symlinks)

    def read(path: pathlib.Path) -> bytes:
        nonlocal reads
        if path == source:
            reads += 1
            if unavailable and failure == "read":
                raise OSError("synthetic read failure")
            if recovery == "changed":
                return b"answer = 43\n"
        return real_read(path)

    monkeypatch.setattr(api_endpoint, "CODE_FILES", (source,))
    monkeypatch.setattr(api_endpoint, "_BOOT_CODE", ((mtime, digest),))
    monkeypatch.setattr(api_endpoint, "_seen_code_mtimes", [mtime if failure == "stat" else -1])
    monkeypatch.setattr(api_endpoint, "_code_differs", [False])
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    monkeypatch.setattr(pathlib.Path, "read_bytes", read)

    assert api_endpoint.code_is_stale()
    assert api_endpoint._seen_code_mtimes == [None]
    unavailable = recovery == "unreadable"
    assert api_endpoint.code_is_stale() is (recovery != "identical")
    if not unavailable:
        assert source.stat().st_mtime == mtime
        assert api_endpoint._seen_code_mtimes == [mtime]
        recovered_reads = reads
        assert api_endpoint.code_is_stale() is (recovery == "changed")
        assert reads == recovered_reads, "successful equal-mtime polling should remain cheap"
    else:
        assert api_endpoint._seen_code_mtimes == [None]


@pytest.mark.parametrize("flush_succeeds", [False, True])
def test_shutdown_overlap_abandons_only_unhanded_restart(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flush_succeeds: bool,
) -> None:
    queued: list[Callable[[], None]] = []
    worker_names: list[str] = []
    shutdowns: list[str] = []
    releases: list[bool] = []

    class FakeServer:
        def shutdown(self) -> None:
            shutdowns.append("shutdown")

    class FakeWorker:
        def __init__(self, target: Callable[[], None]) -> None:
            self.target = target

        def start(self) -> None:
            queued.append(self.target)

    def thread(*, target: Callable[[], None], name: str, daemon: bool) -> FakeWorker:
        assert daemon
        worker_names.append(name)
        return FakeWorker(target)

    def reserve(_server: object) -> bool:
        stack = contextlib.ExitStack()
        stack.callback(lambda: releases.append(True))
        api_endpoint._restart_gate_stack = stack
        return True

    def flush(_path: pathlib.Path) -> tuple[bool, str]:
        assert api_endpoint._shutdown_requested.is_set()
        queued.pop(0)()
        assert releases == [True]
        assert api_endpoint._restart_gate_stack is None
        assert not api_endpoint._restart_scheduled
        assert not api_endpoint._restart_requested.is_set()
        return flush_succeeds, "synthetic final flush"

    def sleep(_seconds: float) -> None:
        """Advance the captured restart directly to its stable-source check."""

    changed = tuple((mtime + 1, digest + "-changed") for mtime, digest in api_endpoint._BOOT_CODE)
    monkeypatch.setattr(api_endpoint, "_code_stamp", lambda: changed)
    monkeypatch.setattr(api_endpoint, "_source_startup_problem", lambda: None)
    monkeypatch.setattr(api_endpoint, "_reserve_board_restart", reserve)
    monkeypatch.setattr(api_endpoint, "flush_autopush_for_shutdown", flush)
    for name, value in {
        "_restart_scheduled": False,
        "_restart_requested": threading.Event(),
        "_shutdown_requested": threading.Event(),
        "_restart_refused_stamp": None,
        "_restart_problem": None,
        "_restart_gate_stack": None,
        "_restart_gate": None,
    }.items():
        monkeypatch.setattr(api_endpoint, name, value)
    monkeypatch.setattr(api_endpoint.time, "sleep", sleep)
    monkeypatch.setattr(api_endpoint.threading, "Thread", thread)
    server = cast("Any", FakeServer())

    assert api_endpoint.request_code_restart(server) == (True, None)
    assert api_endpoint.request_service_shutdown(server) == (
        flush_succeeds,
        "synthetic final flush",
    )
    if flush_succeeds:
        queued.pop(0)()
        assert api_endpoint._shutdown_requested.is_set()
        assert not api_endpoint._restart_requested.is_set()
        assert worker_names == ["code-restart", "service-shutdown"]
    else:
        assert not api_endpoint._shutdown_requested.is_set()
        assert api_endpoint.request_code_restart(server) == (True, None)
        assert api_endpoint.request_code_restart(server) == (True, None)
        assert len(queued) == 1
        queued.pop(0)()
        assert api_endpoint._restart_requested.is_set()
        assert api_endpoint.request_code_restart(server) == (True, None)
        assert not queued
        assert worker_names == ["code-restart", "code-restart"]
    assert shutdowns == ["shutdown"]
    api_endpoint._release_restart_gate()


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


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_create_uses_current_candidate_default(
    api: RunningApi, *, external: bool, explicit: bool
) -> None:
    original = board_state.load(api.path, install_context=False)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path, original.policy if external else None)
    raw = original.policy.to_json()
    raw["defaultPriority"] = "P4"
    original.policy = board_state.TransitionPolicy.from_json(raw, "edited embedded default")
    original.revision = 1
    board_state.save(original, api.path)
    body: dict[str, object] = {"subject": "Current default", "state": "backlog"}
    if explicit:
        body["priority"] = "P1"
    code, response = request_json(
        api.base + API + "/cards", token=api_endpoint.CLI_TOKEN, revision=1, body=body
    )
    assert code == 200
    assert response["revision"] == 2
    expected = "P1" if explicit else "P3" if external else "P4"
    assert board_state.load(api.path).find("current-default").priority == expected


@pytest.mark.parametrize("collisions", [0, 1, 2, 4])
def test_create_chooses_unused_deterministic_slug(api: RunningApi, collisions: int) -> None:
    board = board_state.load(api.path, install_context=False)
    ids = ["alpha", "alpha-0010", "alpha-0010-0002", "alpha-0010-0003"]
    for item_id in ids[:collisions]:
        board.create(item_id, "Existing", "backlog", "bot")
    board.next_ticket = 10
    board_state.save(board, api.path)
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"subject": "Alpha", "state": "backlog"},
    )
    assert code == 200
    assert response["revision"] == 1
    saved = board_state.load(api.path)
    expected = {0: "alpha", 1: "alpha-0010", 2: "alpha-0010-0002", 4: "alpha-0010-0004"}
    assert saved.find("10").id == expected[collisions]
    assert saved.next_ticket == 11
    assert len(saved.items) == collisions + 1
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=1,
        body={"id": expected[collisions], "subject": "Duplicate", "state": "backlog"},
    )
    assert code == 409
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "priority", ["normal", "urgent", "Normal", "p3", "P3", "INVALID", "NORMAL", "p6"]
)
def test_custom_priority_ids_match_exactly(api: RunningApi, *, batch: bool, priority: str) -> None:
    board = board_state.load(api.path, install_context=False)
    raw = board.policy.to_json()
    entries = cast("list[board_state.JsonObject]", raw["priorities"])
    for index, name in enumerate(["urgent", "normal", "Normal", "P3", "p3", "P5"]):
        entries[index]["id"] = name
    board.policy = board_state.TransitionPolicy.from_json(raw, "exact priority IDs")
    board.create("alpha", "Alpha", "backlog", "bot")
    board_state.save(board, api.path)
    before = api.path.read_bytes()
    route = "/cards/priorities" if batch else "/cards/alpha/priority"
    body: dict[str, object] = {"priority": priority}
    if batch:
        body["cards"] = ["alpha"]
    code, response = request_json(
        api.base + API + route, token=api_endpoint.CLI_TOKEN, revision=0, body=body
    )
    if priority in {"INVALID", "NORMAL", "p6"}:
        assert code == 409
        assert api.path.read_bytes() == before
    else:
        assert code == 200
        assert response["revision"] == 1
        assert board_state.load(api.path).find("alpha").priority == priority


@pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3", "P4", "P5", "p3"])
def test_single_priority_accepts_standard_ids_and_p3_convenience(
    api: RunningApi, priority: str
) -> None:
    board = board_state.load(api.path, install_context=False)
    board.create("alpha", "Alpha", "backlog", "bot")
    board_state.save(board, api.path)
    code, response = request_json(
        api.base + API + "/cards/alpha/priority",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"priority": priority},
    )
    assert code == 200
    assert response["priority"] == priority.upper()


def test_archive_routes_keep_data_and_enforce_credentials_and_revisions(api: RunningApi) -> None:
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"id": "focus", "subject": "Focus", "state": "backlog"},
    )
    assert code == 200
    before = api.path.read_bytes()
    route = api.base + API + "/cards/focus/archive"
    code, _ = request_json(route, revision=1, body={})
    assert code == 401
    assert api.path.read_bytes() == before
    code, _ = request_json(route, token=api_endpoint.CLI_TOKEN, revision=0, body={})
    assert code == 412
    assert api.path.read_bytes() == before
    code, out = request_json(route, token=api_endpoint.CLI_TOKEN, revision=1, body={})
    assert code == 200
    assert out["revision"] == 2
    saved = board_state.load(api.path)
    assert saved.find("focus").archived
    assert saved.archive_history[0].by == "bot"
    code, payload = request_json(api.base + API + "/board")
    assert code == 200
    assert payload["counts"]["backlog"] == 0
    assert payload["counts"]["open"] == 0
    hidden = next(item for lane in payload["lanes"] for item in lane["items"])
    assert hidden["id"] == "focus"
    assert hidden["archived"] is True

    unchanged = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards/missing/archive",
        token=api_endpoint.CLI_TOKEN,
        revision=2,
        body={},
    )
    assert code == 409
    delete = urllib.request.Request(api.base + API + "/cards/focus", method="DELETE")
    assert_http_error(delete, 501)
    assert api.path.read_bytes() == unchanged
    code, _ = request_json(
        api.base + API + "/cards/1/unarchive",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={},
    )
    assert code == 200
    saved = board_state.load(api.path)
    assert saved.revision == 3
    assert not saved.find("focus").archived
    assert saved.find("focus").state == "backlog"
    assert saved.archive_history[-1].by == "terry"
    assert saved.verify() == []


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


@pytest.mark.parametrize("credential", ["missing", "invalid", "browser"])
def test_shutdown_requires_cli_credential(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    flushed: list[pathlib.Path] = []
    dispatched: list[object] = []

    def flush(path: pathlib.Path) -> tuple[bool, str]:
        flushed.append(path)
        return True, "unexpected flush"

    def dispatch(server: object) -> tuple[bool, str]:
        dispatched.append(server)
        return True, "unexpected shutdown dispatch"

    monkeypatch.setattr(api_endpoint, "flush_autopush_for_shutdown", flush)
    monkeypatch.setattr(api_endpoint, "request_service_shutdown", dispatch)
    token = {
        "missing": None,
        "invalid": "arbitrary-invalid-shutdown-credential",
        "browser": api_endpoint.BROWSER_TOKEN,
    }[credential]
    code, response = request_json(api.base + API + "/shutdown", token=token, body={})

    assert code == 401
    assert "credential" in response["error"]
    assert flushed == []
    assert dispatched == []
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


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("0", 0),
        ('"0"', 0),
        ("revision-0", 0),
        ('"revision-0"', 0),
        ("١", 1),
        ('"revision-٠"', 0),
        ("²", None),
        ("9" * 5000, None),
        ("-1", None),
    ],
    ids=[
        "bare-zero",
        "quoted-zero",
        "prefixed-zero",
        "quoted-prefixed-zero",
        "arabic-indic-one",
        "quoted-prefixed-arabic-indic-zero",
        "superscript",
        "over-limit-decimal",
        "negative",
    ],
)
def test_expected_revision_decimal_conversion(header: str, expected: int | None) -> None:
    """Unicode decimal parsing does not imply HTTP header transport support."""
    handler = object.__new__(api_endpoint.Handler)
    handler.headers = http.client.HTTPMessage()
    handler.headers["If-Match"] = header
    assert handler._expected_revision() == expected


@pytest.mark.parametrize(
    "header",
    ["²", '"revision-²"', "9" * 5000, '"revision-' + "9" * 5000 + '"'],
    ids=["superscript", "quoted-prefixed-superscript", "over-limit", "quoted-prefixed-over-limit"],
)
def test_invalid_numeric_revision_returns_428_without_dispatch(
    api: RunningApi, header: str
) -> None:
    """5,000 digits exceed Python's default conversion limit, below HTTP's line limit."""
    before = api.path.read_bytes()
    body = json.dumps({"id": "alpha", "subject": "Alpha", "state": "backlog"}).encode()
    connection = http.client.HTTPConnection(api_endpoint.HOST, api.server.server_port, timeout=5)
    with (
        contextlib.closing(connection),
        patch.object(api_endpoint.Handler, "_read_json") as read_body,
        patch.object(api_endpoint, "_http_command_response") as dispatch,
    ):
        connection.putrequest("POST", API + "/cards")
        connection.putheader("Authorization", f"Bearer {api_endpoint.CLI_TOKEN}")
        connection.putheader("If-Match", header)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        # Refusal precedes body input. Defer the payload so closing the socket
        # does not leave unread request bytes that may trigger a Windows reset.
        connection.endheaders()
        with connection.getresponse() as response:
            assert response.status == 428
            assert json.loads(response.read()) == {
                "error": "If-Match must name the expected board revision"
            }
    read_body.assert_not_called()
    dispatch.assert_not_called()
    assert api.path.read_bytes() == before
    assert api_endpoint.STORE is not None
    assert api_endpoint.STORE.snapshot().revision == 0


@pytest.mark.parametrize("header", ["0", '"0"', "revision-0", '"revision-0"'])
def test_revision_header_forms_preserve_zero_and_cas(api: RunningApi, header: str) -> None:
    request = urllib.request.Request(
        api.base + API + "/cards",
        data=json.dumps({"id": "alpha", "subject": "Alpha", "state": "backlog"}).encode(),
        headers={
            "Authorization": f"Bearer {api_endpoint.CLI_TOKEN}",
            "If-Match": header,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())["revision"] == 1
    before = api.path.read_bytes()
    assert_http_error(request, 412)
    assert api.path.read_bytes() == before
    assert board_state.load(api.path).revision == 1


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


def test_blocking_order_and_priority_cascade_use_one_api_revision(api: RunningApi) -> None:
    for revision, (name, priority) in enumerate([("root", "P1"), ("middle", "P4"), ("leaf", "P5")]):
        code, _ = request_json(
            api.base + API + "/cards",
            token=api_endpoint.CLI_TOKEN,
            revision=revision,
            body={"id": name, "subject": name, "state": "backlog", "priority": priority},
        )
        assert code == 200
    for revision, (source, target) in enumerate([("middle", "leaf"), ("root", "middle")], start=3):
        code, _ = request_json(
            api.base + API + f"/cards/{source}/link",
            token=api_endpoint.CLI_TOKEN,
            revision=revision,
            body={"kind": "blocks", "other": target},
        )
        assert code == 200
    code, out = request_json(
        api.base + API + "/cards/root/priority",
        token=api_endpoint.BROWSER_TOKEN,
        revision=5,
        body={"priority": "P0"},
    )
    assert code == 200
    assert out["revision"] == 6
    saved = board_state.load(api.path)
    assert all(item.priority == "P0" for item in saved.items)
    assert all(item.history[-1].by == "terry" for item in saved.items)
    code, payload = request_json(api.base + API + "/board")
    assert code == 200
    lane = next(lane for lane in payload["lanes"] if lane["state"] == "backlog")
    assert [item["id"] for item in lane["items"]] == ["root", "middle", "leaf"]
    assert [item["blockingDepth"] for item in lane["items"]] == [2, 1, 0]
    assert payload["order"] == ["blockingDepthDesc", "priority", "ticket"]
    events = len(saved.find("leaf").history)
    code, out = request_json(
        api.base + API + "/cards/leaf/priority",
        token=api_endpoint.CLI_TOKEN,
        revision=6,
        body={"priority": "P5"},
    )
    assert code == 200
    assert "using P0" in out["result"]
    assert out["priority"] == "P0"
    assert board_state.load(api.path).find("leaf").priority == "P0"
    assert len(board_state.load(api.path).find("leaf").history) == events


@pytest.mark.parametrize("operation", ["add", "replace", "create"])
def test_browser_blocked_by_accepts_and_promotes_current_card_atomically(
    api: RunningApi, operation: str
) -> None:
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"id": "blocker", "subject": "Blocker", "state": "backlog", "priority": "P1"},
    )
    assert code == 200
    revision = 1
    body: dict[str, object]
    if operation == "create":
        path = "/cards"
        body = {
            "id": "current",
            "subject": "Current",
            "state": "backlog",
            "priority": "P5",
            "links": [{"kind": "blocked_by", "other": "blocker"}],
        }
    else:
        code, _ = request_json(
            api.base + API + "/cards",
            token=api_endpoint.CLI_TOKEN,
            revision=revision,
            body={"id": "current", "subject": "Current", "state": "backlog", "priority": "P5"},
        )
        assert code == 200
        revision += 1
        path = "/cards/current/link"
        body = {"kind": "blocked_by", "other": "blocker"}
        if operation == "replace":
            code, _ = request_json(
                api.base + API + path,
                token=api_endpoint.BROWSER_TOKEN,
                revision=revision,
                body={"kind": "references", "other": "blocker"},
            )
            assert code == 200
            revision += 1
            body["replaces"] = "references"
    code, out = request_json(
        api.base + API + path,
        token=api_endpoint.BROWSER_TOKEN,
        revision=revision,
        body=body,
    )
    assert code == 200
    assert out["revision"] == revision + 1
    saved = board_state.load(api.path)
    assert saved.revision == revision + 1
    assert saved.find_link("blocker", "blocks", "current") is not None
    current = saved.find("current")
    assert current.priority == "P1"
    assert len(current.history) == 2
    assert current.history[-1].by == "terry"
    assert current.history[-1].priority_frm == "P5"
    assert current.history[-1].priority_to == "P1"
    assert len(saved.find("blocker").history) == 1
    assert saved.verify() == []
    code, payload = request_json(api.base + API + "/board")
    assert code == 200
    cards = [item for lane in payload["lanes"] for item in lane["items"]]
    assert next(item for item in cards if item["id"] == "current")["priority"] == "P1"


@pytest.mark.parametrize(("blocker_priority", "current_priority"), [("P0", "P5"), ("P1", "P5")])
def test_failed_create_rolls_back_blocked_card_promotion(
    api: RunningApi, blocker_priority: str, current_priority: str
) -> None:
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={
            "id": "blocker",
            "subject": "Blocker",
            "state": "backlog",
            "priority": blocker_priority,
        },
    )
    assert code == 200
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=1,
        body={
            "id": "urgent",
            "subject": "Urgent",
            "state": "backlog",
            "priority": current_priority,
            "links": [
                {"kind": "blocked_by", "other": "blocker"},
                {"kind": "blocks", "other": "missing"},
            ],
        },
    )
    assert code == 409
    assert api.path.read_bytes() == before


def test_browser_root_priority_is_free_and_descendant_floor_is_ongoing(api: RunningApi) -> None:
    for revision, (name, priority) in enumerate([("root", "P5"), ("leaf", "P0")]):
        code, _ = request_json(
            api.base + API + "/cards",
            token=api_endpoint.BROWSER_TOKEN,
            revision=revision,
            body={"id": name, "subject": name, "state": "backlog", "priority": priority},
        )
        assert code == 200
    code, _ = request_json(
        api.base + API + "/cards/root/link",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"kind": "blocks", "other": "leaf"},
    )
    assert code == 200
    assert board_state.load(api.path).find("root").priority == "P5"
    for revision, (ref, requested, expected) in enumerate(
        [("root", "P3", "P3"), ("leaf", "P5", "P3"), ("root", "P1", "P1"), ("leaf", "P5", "P1")],
        start=3,
    ):
        code, out = request_json(
            api.base + API + f"/cards/{ref}/priority",
            token=api_endpoint.BROWSER_TOKEN,
            revision=revision,
            body={"priority": requested},
        )
        assert code == 200
        assert out["priority"] == expected
    saved = board_state.load(api.path)
    assert saved.find("root").priority == "P1"
    assert saved.find("leaf").priority == "P1"
    assert saved.revision == 7
    assert saved.verify() == []


def test_shutdown_releases_writer_gate_for_final_attachment_snapshot(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flush(_path: pathlib.Path) -> tuple[bool, str]:
        assert api_endpoint._shutdown_requested.is_set()
        acquired = api_endpoint._command_gate.acquire(blocking=False)
        assert acquired, "shutdown must not hold the gate needed by snapshot staging"
        api_endpoint._command_gate.release()
        return True, "snapshot flushed"

    monkeypatch.setattr(api_endpoint, "flush_autopush_for_shutdown", flush)
    code, _ = request_json(api.base + API + "/shutdown", token=api_endpoint.CLI_TOKEN, body={})
    assert code == 200


@pytest.mark.parametrize("mode", ["shutdown", "restart"])
@pytest.mark.parametrize("phase", ["idle", "headers", "body", "trickle_headers", "trickle_body"])
def test_incomplete_transport_drain_joins_workers(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch, mode: str, phase: str
) -> None:
    """The total drain deadline wins even while bytes keep arriving."""
    monkeypatch.setattr(api_endpoint, "HTTP_INCOMPLETE_DRAIN_S", 0.15)
    monkeypatch.setattr(api_endpoint, "HTTP_IDLE_TIMEOUT_S", 2.0)

    def flush(_path: pathlib.Path) -> tuple[bool, str]:
        return True, "disabled"

    monkeypatch.setattr(api_endpoint, "flush_autopush_for_shutdown", flush)
    entered, finished = threading.Event(), threading.Event()
    workers: list[threading.Thread] = []
    original = api_endpoint.Handler.handle

    def observe(handler: api_endpoint.Handler) -> None:
        workers.append(threading.current_thread())
        entered.set()
        try:
            original(handler)
        finally:
            finished.set()

    monkeypatch.setattr(api_endpoint.Handler, "handle", observe)
    before = api.path.read_bytes()
    with socket.create_connection((api_endpoint.HOST, api.server.server_port), timeout=2) as client:
        assert entered.wait(2)
        if "headers" in phase:
            client.sendall(b"POST /api/v001/cards HTTP/1.0\r\nX-Trickle: ")
        elif "body" in phase:
            client.sendall(
                b"POST /api/v001/cards HTTP/1.0\r\n"
                + f"Authorization: Bearer {api_endpoint.CLI_TOKEN}\r\n".encode()
                + b'If-Match: "revision-0"\r\nContent-Length: 10000\r\n\r\n{'
            )
        started = time.monotonic()
        stopper: threading.Thread | None = None
        try:
            if mode == "shutdown":
                assert api_endpoint.request_service_shutdown(api.server)[0]
            else:
                assert api_endpoint._reserve_board_restart(api.server)
                stopper = threading.Thread(target=api.server.shutdown)
                stopper.start()
            if phase.startswith("trickle"):
                while not finished.wait(0.015):
                    api.server.begin_drain()  # Repeated drain requests cannot reset the deadline.
                    with contextlib.suppress(OSError):
                        client.sendall(b" ")
                    assert time.monotonic() - started < 1.5
            assert finished.wait(1.5)
            assert time.monotonic() - started < 1.5
            for worker in workers:
                worker.join(timeout=1)
                assert not worker.is_alive()
            assert api.path.read_bytes() == before
        finally:
            if stopper is not None:
                stopper.join(timeout=2)
                assert not stopper.is_alive()
            api_endpoint._release_restart_gate()


def test_short_declared_body_never_admits_a_command(api: RunningApi) -> None:
    """A valid JSON prefix followed by EOF must not bypass Content-Length."""
    before = api.path.read_bytes()
    body = b'{"id":"short","subject":"short","state":"backlog"}'
    with socket.create_connection((api_endpoint.HOST, api.server.server_port), timeout=2) as client:
        client.sendall(
            b"POST /api/v001/cards HTTP/1.0\r\n"
            + f"Authorization: Bearer {api_endpoint.CLI_TOKEN}\r\n".encode()
            + f'If-Match: "revision-0"\r\nContent-Length: {len(body) + 1}\r\n\r\n'.encode()
            + body
        )
        client.shutdown(socket.SHUT_WR)
        assert client.recv(1024) == b""
    assert api.path.read_bytes() == before


def test_progressing_maximum_attachment_and_browser_reads(api: RunningApi) -> None:
    """A full 10 MiB attachment retains the existing request-size contract."""
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"id": "large", "subject": "large", "state": "backlog"},
    )
    assert code == 200
    blob = b"x" * attachments.MAX_BYTES
    body = json.dumps({"name": "large.bin", "data": base64.b64encode(blob).decode()}).encode()
    connection = http.client.HTTPConnection(api_endpoint.HOST, api.server.server_port, timeout=5)
    try:
        connection.putrequest("POST", API + "/cards/large/attachment")
        connection.putheader("Authorization", f"Bearer {api_endpoint.BROWSER_TOKEN}")
        connection.putheader("If-Match", '"revision-1"')
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        for offset in range(0, len(body), 65536):
            connection.send(body[offset : offset + 65536])
            if offset == 0:
                time.sleep(0.2)  # Progress may pause for longer than the 100 ms read poll.
        response = connection.getresponse()
        assert response.status == 200
        uploaded = json.loads(response.read())
    finally:
        connection.close()
    url = api.base + api_endpoint.attachment_url("large", uploaded["attachment"]["id"])
    with urllib.request.urlopen(url, timeout=5) as response:
        time.sleep(0.2)  # Response writes must restore the ordinary timeout after read admission.
        chunks: list[bytes] = []
        while chunk := response.read(65536):
            chunks.append(chunk)
        assert b"".join(chunks) == blob
    assert request_json(api.base + API + "/status")[1]["ok"] is True
    assert (
        request_json(api.base + API + "/browser-session")[1]["token"] == api_endpoint.BROWSER_TOKEN
    )


def test_idle_transport_timeout_without_drain(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary idle limit is separate from the short receive polling interval."""
    monkeypatch.setattr(api_endpoint, "HTTP_IDLE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(api_endpoint, "HTTP_READ_POLL_S", 0.02)
    workers: list[threading.Thread] = []
    original = api_endpoint.Handler.handle

    def observe(handler: api_endpoint.Handler) -> None:
        workers.append(threading.current_thread())
        original(handler)

    monkeypatch.setattr(api_endpoint.Handler, "handle", observe)
    before = api.path.read_bytes()
    started = time.monotonic()
    with socket.create_connection((api_endpoint.HOST, api.server.server_port), timeout=2) as client:
        assert client.recv(1024) == b""
    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 1.5
    assert api.server._drain_deadline is None
    assert len(workers) == 1
    workers[0].join(timeout=1)
    assert not workers[0].is_alive()
    assert api.path.read_bytes() == before


def test_connected_body_can_finish_within_restart_drain(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain deadline permits an existing progressing request to finish."""
    monkeypatch.setattr(api_endpoint, "HTTP_INCOMPLETE_DRAIN_S", 1.0)
    body = b'{"id":"finishing","subject":"finishing","state":"backlog"}'
    with socket.create_connection((api_endpoint.HOST, api.server.server_port), timeout=2) as client:
        client.sendall(
            b"POST /api/v001/cards HTTP/1.0\r\n"
            + f"Authorization: Bearer {api_endpoint.CLI_TOKEN}\r\n".encode()
            + f'If-Match: "revision-0"\r\nContent-Length: {len(body)}\r\n\r\n'.encode()
            + body[:1]
        )
        deadline = time.monotonic() + 1
        while not api.server._reading:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        api.server.begin_drain()
        client.sendall(body[1:])
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 200
        assert json.loads(response.read())["revision"] == 1
    assert board_state.load(api.path).find("finishing").subject == "finishing"


# Frozen #0177 adapter contracts: omitted, null, empty, whitespace, ordinary.
SCALAR_ROWS = [
    ("cards", "state", "backlog", (400, 409, 409, 409, 200)),
    ("cards", "subject", "Ordinary title", (400, 409, 409, 409, 200)),
    ("board/project", "project", "Ordinary project", (400, 409, 409, 409, 200)),
    ("cards/alpha/move", "to", "ready_for_work", (400, 409, 409, 409, 200)),
    ("cards/alpha/comment", "text", "Ordinary comment", (400, 409, 409, 409, 200)),
    ("cards/alpha/assign", "owner", "terry", (400, 409, 409, 409, 200)),
    ("cards/alpha/priority", "priority", "P1", (400, 409, 409, 409, 200)),
    ("cards/alpha/subject", "subject", "Ordinary title", (400, 409, 409, 409, 200)),
    ("cards/alpha/detail", "detail", "Ordinary detail", (400, 409, 409, 409, 200)),
    ("cards/alpha/link", "kind", "relates_to", (400, 409, 409, 409, 200)),
    ("cards/alpha/link", "other", "target", (400, 409, 409, 409, 200)),
    ("cards/alpha/attachment", "attachmentId", "uploaded", (400, 409, 409, 409, 200)),
    ("cards", "id", "exact-id", (200, 200, 200, 200, 200)),
    ("cards", "priority", "P1", (200, 200, 200, 409, 200)),
    ("cards", "detail", "Ordinary detail", (200, 200, 200, 200, 200)),
    ("cards", "owner", "terry", (200, 200, 200, 409, 200)),
    ("cards/alpha/parent", "parent", "target", (200, 200, 200, 409, 200)),
    ("cards/alpha/link", "remove", True, (200, 409, 409, 409, 200)),
]
SCALAR_MUTATORS = (
    "create",
    "move",
    "comment",
    "assign",
    "set_priority",
    "set_subject",
    "set_detail",
    "set_parent",
    "link",
    "unlink",
    "replace_link",
    "remove_attachment",
)


def assert_scalar_result(
    saved: board_state.Board, route: str, field: str, value: object, attachment_id: str
) -> None:
    """Assert the actual saved branch, including each distinct default/clear contract."""
    item = saved.find("alpha")
    if route == "cards":
        created = saved.find("4")
        assert created.id == ((value or "ordinary-title") if field == "id" else "ordinary-title")
        assert created.subject == (
            value.strip() if field == "subject" and isinstance(value, str) else "Ordinary title"
        )
        assert created.state == (value if field == "state" else "backlog")
        assert created.priority == ((value or "P3") if field == "priority" else "P3")
        assert created.detail == ((value or "") if field == "detail" else "")
        assert created.owner == ((value or "bot") if field == "owner" else "bot")
    elif route == "board/project":
        assert saved.project == "Ordinary project"
    elif field == "to":
        assert item.state == value
    elif field == "text":
        assert item.comments[-1].text == value
    elif field == "owner":
        assert item.owner == value
    elif field == "priority":
        assert item.priority == value
    elif field == "subject":
        assert item.subject == value
    elif field == "detail":
        assert item.detail == value
    elif field == "parent":
        assert item.parent == (value or None)
    elif field == "attachmentId":
        assert item.attachments == []
        assert item.attachment_history[-1].attachment_id == attachment_id
    elif field == "remove":
        assert (saved.find_link("alpha", "blocks", "target") is None) is (value is True)
    else:
        assert saved.find_link("alpha", "relates_to", "target") is not None


@pytest.mark.parametrize(("route", "field", "ordinary", "codes"), SCALAR_ROWS)
def test_http_scalar_contract_matrix(  # noqa: PLR0912, PLR0913, PLR0917 -- explicit frozen matrix
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    field: str,
    ordinary: object,
    codes: tuple[int, ...],
) -> None:
    board = board_state.load(api.path, install_context=False)
    for ref in ("alpha", "target", "oldparent"):
        board.create(ref, ref.title(), "backlog", "bot")
    board.set_parent("alpha", "oldparent", "bot")
    board.link("alpha", "blocks", "target", "bot")
    board_state.save(board, api.path)
    attachment_id = ""
    if route.endswith("/attachment"):
        store = api_endpoint.BoardStore(api.path)
        _, _, uploaded = store.upload(
            0, "alpha", "file.txt", base64.b64encode(b"file").decode(), "bot"
        )
        attachment_id = uploaded.id
        board = board_state.load(api.path, install_context=False)
        ordinary = attachment_id
    baseline_revision = board.revision
    cases: list[tuple[str, object, int, bool]] = [
        ("omitted", None, codes[0], False),
        ("null", None, codes[1], codes[1] == 409),
        ("empty", "", codes[2], field == "remove"),
        ("whitespace", " \t ", codes[3], field == "remove"),
        ("ordinary", ordinary, codes[4], False),
    ]
    cases.extend(
        ("wrong-type", wrong, 409, True)
        for wrong in (True, False, 0, 1, 1.5, [], ["private-scalar"], {}, {"private-scalar": 1})
        if field != "remove" or not isinstance(wrong, bool)
    )
    if field == "remove":
        cases.append(("false", False, 200, False))
        cases.append(("string-false", "false", 409, True))
    for mode, value, expected, no_dispatch in cases:
        board_state.save(board, api.path)
        api_endpoint.STORE = api_endpoint.BoardStore(api.path)
        before = api.path.read_bytes()
        body: dict[str, object] = (
            {"subject": "Ordinary title", "state": "backlog"} if route == "cards" else {}
        )
        if route.endswith("/link"):
            body.update(kind="blocks" if field == "remove" else "relates_to", other="target")
        if route.endswith("/attachment"):
            body["remove"] = True
        body.pop(field, None)
        if mode != "omitted":
            body[field] = value

        def refused_dispatch(*_args: object, **_kwargs: object) -> str:
            pytest.fail(f"{route}/{field} reached a domain mutator")

        with monkeypatch.context() as guarded:
            if no_dispatch:
                for method in SCALAR_MUTATORS:
                    guarded.setattr(board_state.Board, method, refused_dispatch)
            code, response = request_json(
                api.base + API + "/" + route,
                token=api_endpoint.CLI_TOKEN,
                revision=baseline_revision,
                body=body,
            )
        assert code == expected, (route, field, mode, response)
        if expected != 200:
            assert api.path.read_bytes() == before
            assert board_state.load(api.path).revision == baseline_revision
            if mode == "omitted":
                assert "missing field" in response["error"]
            if no_dispatch:
                assert field in response["error"]
                assert "private-scalar" not in response["error"]
        else:
            assert response["revision"] == baseline_revision + 1
            saved = board_state.load(api.path)
            assert saved.verify() == []
            assert_scalar_result(saved, route, field, value, attachment_id)


@pytest.mark.parametrize("remove", [False, True])
def test_link_replacement_keeps_precedence_and_checks_remove(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch, *, remove: bool
) -> None:
    board = board_state.load(api.path, install_context=False)
    board.create("alpha", "Alpha", "backlog", "bot")
    board.create("target", "Target", "backlog", "bot")
    board.link("alpha", "blocks", "target", "bot")
    board_state.save(board, api.path)
    before = api.path.read_bytes()
    body: dict[str, object] = {
        "kind": "relates_to",
        "other": "target",
        "replaces": "blocks",
        "remove": "false",
    }

    def no_replace(*_args: object, **_kwargs: object) -> str:
        pytest.fail("invalid remove reached replacement")

    with monkeypatch.context() as guarded:
        guarded.setattr(board_state.Board, "replace_link", no_replace)
        code, result = request_json(
            api.base + API + "/cards/alpha/link",
            token=api_endpoint.CLI_TOKEN,
            revision=0,
            body=body,
        )
    assert code == 409
    assert result["error"] == "remove must be a Boolean"
    assert api.path.read_bytes() == before
    body["remove"] = remove
    code, _ = request_json(
        api.base + API + "/cards/alpha/link", token=api_endpoint.CLI_TOKEN, revision=0, body=body
    )
    assert code == 200
    saved = board_state.load(api.path)
    assert saved.links_for("alpha") == [("relates_to", "target")]


@pytest.mark.parametrize("shape", ["array", "object"])
@pytest.mark.parametrize("permissions", [False, True])
def test_over_nested_json_is_sanitized_before_dispatch(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch, shape: str, *, permissions: bool
) -> None:
    prefix, suffix = ("[", "]") if shape == "array" else ('{"nested":', "}")
    raw = ('{"private-depth":' + prefix * 10000 + "0" + suffix * 10000 + "}").encode()
    assert len(raw) < attachments.MAX_REQUEST_BYTES
    before = api.path.read_bytes()

    def no_command(*_args: object, **_kwargs: object) -> str:
        pytest.fail("decoder refusal dispatched a command")

    original_loads = json.loads

    def depth_limited_decoder(
        value: str | bytes | bytearray,
        **kwargs: Any,  # noqa: ANN401 -- json.loads keyword forwarding
    ) -> Any:  # noqa: ANN401 -- preserve json.loads result typing
        # CPython 3.15's iterative decoder can accept this depth. Inject its documented
        # failure at the decoder boundary instead of imposing a new product depth limit.
        if value == raw:
            raise RecursionError("private-depth decoder sentinel")
        return original_loads(value, **kwargs)

    monkeypatch.setattr(json, "loads", depth_limited_decoder)
    monkeypatch.setattr(api_endpoint, "_http_command_response", no_command)
    monkeypatch.setattr(api_endpoint.BoardStore, "grant_permissions", no_command)
    route = "/board/permissions" if permissions else "/cards"
    bodies = [raw, b'{"private-shallow":']
    if permissions:
        bodies.append(b'{"actors":[],"actors":[],"create":[],"moves":[]}')
    for body in bodies:
        connection = http.client.HTTPConnection(
            "127.0.0.1", api.server.server_address[1], timeout=5
        )
        try:
            connection.request(
                "POST",
                API + route,
                body=body,
                headers={
                    "Authorization": f"Bearer {api_endpoint.CLI_TOKEN}",
                    "If-Match": '"revision-0"',
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            assert response.status == 400
            assert json.loads(response.read()) == {"error": "bad request body"}
        finally:
            connection.close()
        assert api.path.read_bytes() == before
        assert board_state.load(api.path).revision == 0


@pytest.mark.parametrize("permissions", [False, True])
def test_json_decoder_valid_and_ordinary_duplicate_key_controls(
    api: RunningApi, *, permissions: bool
) -> None:
    raw = (
        b'{"actors":["bot"],"create":["backlog"],"moves":[]}'
        if permissions
        else b'{"subject":"First title","subject":"Second title","state":"backlog"}'
    )
    connection = http.client.HTTPConnection("127.0.0.1", api.server.server_address[1], timeout=5)
    try:
        connection.request(
            "POST",
            API + ("/board/permissions" if permissions else "/cards"),
            body=raw,
            headers={
                "Authorization": f"Bearer {api_endpoint.CLI_TOKEN}",
                "If-Match": '"revision-0"',
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["revision"] == 1
    finally:
        connection.close()
    saved = board_state.load(api.path)
    assert saved.verify() == []
    if not permissions:
        assert saved.find("second-title").subject == "Second title"
