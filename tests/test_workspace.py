"""Workspace discovery, routing, and isolated live-board writer contracts."""

import base64
import contextlib
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state, workspace
from tests.support import USERS
from tests.test_api import request_json
from tests.test_attachments import PNG
from tests.test_cli import run_cli

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator, Iterator

API = board_state.API_PREFIX


def make_board(path: pathlib.Path, project: str, *, alternate: bool = False) -> None:
    """Create an isolated persisted board with distinguishable users and labels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    users = USERS
    policy = board_state.TransitionPolicy.load()
    if alternate:
        users = (
            board_state.User("terry", "Alternate human", board_state.HUMAN, "#992244"),
            board_state.User("bot", "Alternate bot", board_state.BOT, "#229944"),
        )
        document = policy.to_json()
        lanes = cast("list[board_state.JsonValue]", document["lanes"])
        cast("board_state.JsonObject", lanes[0])["label"] = "Alternate backlog"
        policy = board_state.TransitionPolicy.from_json(document, str(path))
    board = board_state.Board(
        project=project,
        users=users,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
        policy=policy,
    )
    board_state.save(board, path)


def board_route(base: str, key: str, suffix: str = "board") -> str:
    """Encode a relative board key as exactly one routing segment."""
    return base + API + "/workspace/boards/" + urllib.parse.quote(key, safe="") + "/" + suffix


@dataclass(frozen=True)
class RunningWorkspace:
    """One gateway over two independent scratch boards."""

    base: str
    root: pathlib.Path
    first: pathlib.Path
    second: pathlib.Path
    instance: workspace.Workspace


@pytest.fixture
def gateway(tmp_path: pathlib.Path) -> Iterator[RunningWorkspace]:
    """Bind an ephemeral gateway; all board children belong to this fixture."""
    root = tmp_path / "boards"
    first = root / "alpha" / "board.json"
    second = root / "beta" / "board.json"
    make_board(first, "Alpha")
    make_board(second, "Beta", alternate=True)
    instance = workspace.Workspace(root)
    server = workspace.WorkspaceServer((api_endpoint.HOST, 0), instance)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningWorkspace(
            f"http://127.0.0.1:{server.server_address[1]}", root, first, second, instance
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        instance.close()


def catalog_boards(instance: workspace.Workspace) -> list[dict[str, Any]]:
    """Narrow the JSON catalog at the test boundary."""
    return cast("list[dict[str, Any]]", instance.catalog()["boards"])


def test_discovery_excludes_configuration_and_attachment_files(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "boards"
    good = root / "project" / "board.json"
    make_board(good, "Real board")
    (root / "config.json").write_text('{"port":8794}', encoding="utf-8")
    make_board(root / "attachments" / "0001" / "copied-board.json", "Attachment content")
    (root / "project" / "permissions.json").write_text('{"schema":4}', encoding="utf-8")
    instance = workspace.Workspace(root)
    try:
        rows = catalog_boards(instance)
        assert [row["key"] for row in rows] == ["project/board"]
        assert rows[0]["project"] == "Real board"
        assert rows[0]["counts"] == {"blocked": 0, "needsHuman": 0, "review": 0}
    finally:
        instance.close()


def test_stable_keys_survive_label_changes_and_new_boards(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "boards"
    path = root / "shared" / "board.json"
    make_board(path, "Before rename")
    instance = workspace.Workspace(root)
    try:
        before = catalog_boards(instance)[0]["key"]
        board = board_state.load(path)
        board.project = "After rename"
        board_state.save(board, path)
        make_board(root / "shared" / "another.json", "Another board")
        rows = catalog_boards(instance)
        renamed = next(row for row in rows if row["project"] == "After rename")
        assert renamed["key"] == before == "shared/board"
        assert instance.board_path(before) == path.resolve()
    finally:
        instance.close()


@pytest.mark.parametrize("key", ["missing", "../outside", "/absolute", "alpha/../beta/board"])
def test_unknown_or_escaping_keys_never_fall_back(gateway: RunningWorkspace, key: str) -> None:
    with pytest.raises(ValueError, match=r"board|key|path|unknown"):
        gateway.instance.board_path(key)
    status, _ = request_json(board_route(gateway.base, key))
    assert status == 404
    assert not board_state.service_descriptor_path(gateway.first).exists()
    assert not board_state.service_descriptor_path(gateway.second).exists()


def test_attention_counts_include_only_nonarchived_cards(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Counts")
    board = board_state.load(path)
    for ref, lane in (
        ("blocked-one", "blocked"),
        ("blocked-two", "blocked"),
        ("hidden", "blocked"),
        ("human", "needs_terry_action"),
        ("review", "ready_for_review"),
    ):
        board.create(ref, ref, "backlog", "terry")
        board.move(ref, "in_progress", "terry")
        board.move(ref, lane, "terry")
    board.set_archived("hidden", archived=True, by="terry")
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    try:
        assert catalog_boards(instance)[0]["counts"] == {
            "blocked": 2,
            "needsHuman": 1,
            "review": 1,
        }
    finally:
        instance.close()


def test_cold_deep_link_serves_app_without_loading_wrong_board(gateway: RunningWorkspace) -> None:
    for key in ("alpha/board", "beta/board"):
        with urllib.request.urlopen(
            gateway.base + "/board/" + urllib.parse.quote(key, safe=""), timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert "workspace" in response.read().decode("utf-8").lower()
    assert board_state.load(gateway.first).revision == 0
    assert board_state.load(gateway.second).revision == 0


def test_proxy_mutations_keep_human_attribution_and_revision_isolation(
    gateway: RunningWorkspace,
) -> None:
    session_code, session = request_json(gateway.base + API + "/browser-session")
    assert session_code == 200
    token = str(session["token"])
    code, first = request_json(board_route(gateway.base, "alpha/board"))
    assert code == 200
    code, second = request_json(board_route(gateway.base, "beta/board"))
    assert code == 200
    assert first["project"] == "Alpha"
    assert second["project"] == "Beta"
    code, out = request_json(
        board_route(gateway.base, "beta/board", "cards"),
        token=token,
        revision=0,
        body={"id": "human-card", "subject": "Human work", "state": "backlog"},
    )
    assert code == 200, out
    assert out["revision"] == 1
    assert board_state.load(gateway.second).find("human-card").history[0].by == "terry"
    assert board_state.load(gateway.first).revision == 0
    code, _ = request_json(
        board_route(gateway.base, "beta/board", "cards/human-card/comment"),
        token=token,
        revision=0,
        body={"text": "Stale write"},
    )
    assert code == 412
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "cards"),
        revision=0,
        body={"id": "uncredentialed", "subject": "Refused", "state": "backlog"},
    )
    assert code == 401
    assert board_state.load(gateway.second).find("human-card").comments == []
    assert board_state.load(gateway.first).items == []


def test_board_session_styles_and_policy_do_not_leak_across_projects(
    gateway: RunningWorkspace,
) -> None:
    for key, expected_color, expected_label in (
        ("alpha/board", "#2266aa", "Backlog"),
        ("beta/board", "#992244", "Alternate backlog"),
        ("alpha/board", "#2266aa", "Backlog"),
    ):
        code, session = request_json(board_route(gateway.base, key, "browser-session"))
        assert code == 200
        assert expected_color in session["userCSS"]
        code, payload = request_json(board_route(gateway.base, key))
        assert code == 200
        assert payload["lanes"][0]["label"] == expected_label


def test_attachment_proxy_rebases_urls_and_preserves_download_headers(
    gateway: RunningWorkspace,
) -> None:
    _, session = request_json(gateway.base + API + "/browser-session")
    token = str(session["token"])
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "cards"),
        token=token,
        revision=0,
        body={"id": "picture", "subject": API + "/cards/picture", "state": "backlog"},
    )
    assert code == 200
    code, out = request_json(
        board_route(gateway.base, "alpha/board", "cards/picture/attachment"),
        token=token,
        revision=1,
        body={"name": "evidence.png", "data": base64.b64encode(PNG).decode("ascii")},
    )
    assert code == 200, out
    _, payload = request_json(board_route(gateway.base, "alpha/board"))
    assert payload["lanes"][0]["items"][0]["subject"] == API + "/cards/picture"
    attachment = payload["lanes"][0]["items"][0]["attachments"][0]
    expected = board_route(
        "", "alpha/board", "cards/picture/attachments/" + str(out["attachment"]["id"])
    )
    assert attachment["url"] == expected
    with urllib.request.urlopen(gateway.base + attachment["url"], timeout=5) as response:
        assert response.read() == PNG
        assert response.headers["Content-Disposition"].startswith("inline;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    with urllib.request.urlopen(
        gateway.base + attachment["url"] + "?download=1", timeout=5
    ) as response:
        assert response.read() == PNG
        assert response.headers["Content-Disposition"].startswith("attachment;")
    assert board_state.load(gateway.second).revision == 0


@contextlib.contextmanager
def existing_writer(path: pathlib.Path) -> Generator[dict[str, Any]]:
    """Run one real board process without affecting any interactive services."""
    child = subprocess.Popen(
        [sys.executable, "-m", "localswim.api_endpoint", str(path), "--port", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    descriptor_path = board_state.service_descriptor_path(path)
    try:
        deadline = time.monotonic() + 10
        while not descriptor_path.exists():
            assert child.poll() is None, "isolated board process exited during startup"
            assert time.monotonic() < deadline, "board startup timed out"
            time.sleep(0.02)
        yield cast("dict[str, Any]", json.loads(descriptor_path.read_text(encoding="utf-8")))
    finally:
        with contextlib.suppress(board_state.BoardError, OSError):
            board_state.shutdown_service(path)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_gateway_reuses_existing_writer_and_cli_keeps_bot_attribution(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200
        assert payload["project"] == "Alpha"
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current == descriptor
        result = run_cli(
            gateway.first, "card", "create", "bot-card", "Bot work", "--state", "backlog"
        )
        assert result.returncode == 0, result.stderr
        assert board_state.load(gateway.first).find("bot-card").history[0].by == "bot"
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200
        assert payload["revision"] == 1


def test_wrong_service_descriptor_cannot_cross_route_board_data(gateway: RunningWorkspace) -> None:
    with existing_writer(gateway.second) as second:
        fake = dict(second)
        fake["board"] = str(gateway.first.resolve())
        board_state.service_descriptor_path(gateway.first).write_text(
            json.dumps(fake), encoding="utf-8"
        )
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200, payload
        assert payload["project"] == "Alpha"
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current["pid"] != second["pid"]
        assert current["port"] != second["port"]
        assert board_state.load(gateway.first).revision == 0
        assert board_state.load(gateway.second).revision == 0


def test_invalid_board_is_visible_as_error_without_spawning_a_writer(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "damaged.json"
    make_board(path, "Damaged board")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["items"] = "invalid cards"
    path.write_text(json.dumps(raw), encoding="utf-8")
    instance = workspace.Workspace(tmp_path)
    try:
        rows = catalog_boards(instance)
        assert len(rows) == 1
        assert rows[0]["key"] == "damaged"
        assert rows[0]["error"]
        assert not board_state.service_descriptor_path(path).exists()
    finally:
        instance.close()


def test_browser_sessions_identify_board_and_never_expose_cli_capability(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        base = f"http://127.0.0.1:{descriptor['port']}"
        code, upstream = request_json(base + API + "/browser-session")
        assert code == 200
        assert upstream["boardIdentity"] == workspace.path_identity(gateway.first)
        assert upstream["token"] != descriptor["token"]
        code, scoped = request_json(board_route(gateway.base, "alpha/board", "browser-session"))
        assert code == 200
        assert scoped["boardIdentity"] == upstream["boardIdentity"]
        assert scoped["userCSS"] == upstream["userCSS"]
        assert scoped["token"] == gateway.instance.token
        assert scoped["token"] != upstream["token"]


def test_close_stops_owned_children_but_preserves_existing_writers(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        assert request_json(board_route(gateway.base, "alpha/board"))[0] == 200
        assert request_json(board_route(gateway.base, "beta/board"))[0] == 200
        owned_descriptor = board_state.service_descriptor_path(gateway.second)
        assert owned_descriptor.exists()
        gateway.instance.close()
        assert not owned_descriptor.exists()
        original_descriptor = board_state.service_descriptor_path(gateway.first)
        assert json.loads(original_descriptor.read_text(encoding="utf-8")) == descriptor
        base = f"http://127.0.0.1:{descriptor['port']}"
        assert request_json(base + API + "/board")[0] == 200
