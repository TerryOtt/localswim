"""Workspace discovery, routing, and isolated live-board writer contracts."""

import base64
import contextlib
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, attachments, board_state, service_ownership, workspace
from tests.support import USERS
from tests.test_api import request_json
from tests.test_attachments import PNG
from tests.test_cli import run_cli

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Generator, Iterator

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


def test_discovery_isolates_nested_and_invalid_candidates(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Control")
    depth = sys.getrecursionlimit() * 2
    (tmp_path / "nested.json").write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
    (tmp_path / "invalid.json").write_bytes(b"\xff")
    (tmp_path / "syntax.json").write_text("{", encoding="utf-8")
    (tmp_path / "unrelated.json").write_text('{"schema":4}', encoding="utf-8")
    make_board(tmp_path / ".hidden" / "board.json", "Hidden")
    make_board(tmp_path / ".git" / "board.json", "Git")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "synthetic", "")

    def endpoint(_candidate: workspace._Candidate) -> workspace.Endpoint:
        return expected

    monkeypatch.setattr(instance, "_endpoint_under_gate", endpoint)
    assert [row["project"] for row in catalog_boards(instance)] == ["Control"]
    assert instance.board_path("board") == path.resolve()
    assert instance.resolve_endpoint("board") == (path.resolve(), expected)
    result = instance.update_preferences(0, {"action": "pin", "key": "board"})
    assert result.pinned_boards == ("board",)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_discovery_candidate_exact_byte_boundary(tmp_path: pathlib.Path, extra: int) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Boundary")
    original = path.read_bytes()
    ceiling = workspace.MAX_CANDIDATE_BYTES
    path.write_bytes(original + b" " * (ceiling + extra - len(original)))
    rows = catalog_boards(workspace.Workspace(tmp_path))
    assert len(rows) == (0 if extra > 0 else 1)
    if rows:
        assert rows[0]["project"] == "Boundary"


def test_discovery_growth_read_is_limited_to_ceiling_plus_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 200)
    monkeypatch.setattr(workspace, "MAX_CANDIDATE_BYTES", 100)
    original_stat = os.fstat
    reads: list[int] = []
    original_fdopen = os.fdopen

    def small_stat(fd: int) -> os.stat_result:
        values = list(original_stat(fd))
        values[6] = 1
        return os.stat_result(values)

    @contextlib.contextmanager
    def recording_open(fd: int, mode: str) -> Generator[Any]:
        with original_fdopen(fd, mode) as stream:

            class Reader:
                def fileno(self) -> int:
                    return stream.fileno()

                def read(self, size: int) -> bytes:
                    reads.append(size)
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(os, "fstat", small_stat)
    monkeypatch.setattr(os, "fdopen", recording_open)
    assert workspace._read_candidate(path) is None  # noqa: SLF001 -- reader boundary probe
    assert reads == [101]


def test_proxy_resolution_scans_and_reads_each_candidate_once(
    gateway: RunningWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[None] = []
    reads: list[pathlib.Path] = []
    original_paths = gateway.instance._paths  # noqa: SLF001 -- deterministic scan counter
    original_reader = workspace._read_candidate  # noqa: SLF001 -- candidate read counter

    def scan() -> dict[str, workspace._Candidate]:
        scans.append(None)
        return original_paths()

    def read(path: pathlib.Path) -> workspace._Candidate | None:
        reads.append(path)
        return original_reader(path)

    monkeypatch.setattr(gateway.instance, "_paths", scan)
    monkeypatch.setattr(workspace, "_read_candidate", read)
    status, _ = request_json(board_route(gateway.base, "alpha/board", "browser-session"))
    assert status == 200
    assert len(scans) == 1
    assert sorted(reads) == sorted([gateway.first, gateway.second])


@pytest.mark.parametrize("change", ["remove", "replace", "directory"])
def test_request_resolution_refuses_changed_candidates(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic filesystem race

    def scan_then_change() -> dict[str, workspace._Candidate]:
        candidates = original_paths()
        path.unlink()
        if change == "replace":
            make_board(path, "Replacement with a different size")
        elif change == "directory":
            path.mkdir()
        return candidates

    monkeypatch.setattr(instance, "_paths", scan_then_change)
    with pytest.raises((ValueError, OSError)):
        instance.resolve_endpoint("board")
    assert not instance._children  # noqa: SLF001 -- refusal must precede child launch


@pytest.mark.parametrize("change", ["none", "remove", "replace", "symlink"])
def test_launch_gate_rechecks_candidate_before_reuse_or_launch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = tmp_path / "root"
    path = root / "board.json"
    make_board(path, "Inside")
    outside = tmp_path / "outside.json"
    make_board(outside, "Outside")
    instance = workspace.Workspace(root)
    scans = 0
    reuse_calls = 0
    original_paths = instance._paths  # noqa: SLF001 -- no second scan after gate wait
    expected = workspace.Endpoint(12345, "synthetic", "")

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        return original_paths()

    @contextlib.contextmanager
    def delayed_gate(target: pathlib.Path) -> Generator[object]:
        if change != "none":
            target.unlink()
            if change == "replace":
                target.write_text("{}", encoding="utf-8")
            elif change == "symlink":
                try:
                    target.symlink_to(outside)
                except OSError:
                    pytest.skip("creating symlinks requires unavailable OS privilege")
        yield object()

    def reuse(_path: pathlib.Path) -> workspace.Endpoint:
        nonlocal reuse_calls
        reuse_calls += 1
        return expected

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("changed candidate reached process launch")

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(instance, "_launch_locked", delayed_gate)
    monkeypatch.setattr(instance, "_existing", reuse)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    if change == "none":
        assert instance.resolve_endpoint("board") == (path.resolve(), expected)
        assert reuse_calls == 1
    else:
        with pytest.raises((OSError, ValueError)):
            instance.resolve_endpoint("board")
        assert reuse_calls == 0
    assert scans == 1


@pytest.mark.parametrize("boundary", ["busy", "upgrade", "ready", "before_launch"])
def test_endpoint_wait_paths_retain_candidate_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    attempts = 0

    def replace() -> None:
        path.unlink()
        path.write_text("{}", encoding="utf-8")

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        nonlocal attempts
        attempts += 1
        if boundary == "upgrade":
            replace()
            raise workspace.ServiceUpgradePendingError("synthetic upgrade")
        if boundary == "ready":
            replace()
            return workspace.Endpoint(12345, "synthetic", "")
        return None

    def busy(_path: pathlib.Path) -> bool:
        replace()
        return boundary == "busy"

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("changed candidate reached process launch")

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0)
    with pytest.raises((OSError, ValueError)):
        instance.resolve_endpoint("board")
    assert attempts == 1


def test_fresh_requests_observe_removal_and_replacement(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    assert catalog_boards(instance)[0]["project"] == "Before"
    path.unlink()
    assert catalog_boards(instance) == []
    with pytest.raises(ValueError, match="unknown"):
        instance.board_path("board")
    make_board(path, "After")
    assert catalog_boards(instance)[0]["project"] == "After"


def test_catalog_contains_recursion_in_recognized_board_validation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Recognized")

    def too_deep(_path: pathlib.Path) -> board_state.Board:
        raise RecursionError("synthetic validation recursion")

    monkeypatch.setattr(board_state, "load", too_deep)
    instance = workspace.Workspace(tmp_path)
    row = catalog_boards(instance)[0]
    assert row["key"] == "board"
    assert "error" in row
    with pytest.raises(ValueError, match="nesting"):
        instance.resolve_endpoint("board")


def test_nonregular_candidates_are_not_opened(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fifo.json"
    path.touch()
    original_stat = type(path).stat

    def nonregular(target: pathlib.Path, *, follow_symlinks: bool = True) -> os.stat_result:
        values = list(original_stat(target, follow_symlinks=follow_symlinks))
        if target == path:
            values[0] = stat.S_IFIFO
        return os.stat_result(values)

    monkeypatch.setattr(type(path), "stat", nonregular)

    def fail_open(*_args: object) -> int:
        pytest.fail("nonregular file opened")

    monkeypatch.setattr(os, "open", fail_open)
    assert workspace._read_candidate(path) is None  # noqa: SLF001 -- reader boundary probe


def test_direct_endpoint_rejects_undiscovered_and_outside_paths(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    make_board(outside, "Outside")
    unrelated = root / "unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")
    instance = workspace.Workspace(root)
    for path in (outside, unrelated):
        with pytest.raises(ValueError, match="outside"):
            instance.endpoint(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction behavior")
def test_discovery_excludes_directory_junction_escape(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "board.json"
    make_board(outside, "Outside")
    link = root / "junction"
    command = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside.parent)],
        capture_output=True,
        check=False,
    )
    if command.returncode:
        pytest.skip("creating directory junctions is unavailable")
    assert link.is_junction()
    assert catalog_boards(workspace.Workspace(root)) == []


@pytest.mark.parametrize("directory", [False, True])
def test_discovery_omits_privileged_symlink_escapes(
    tmp_path: pathlib.Path, *, directory: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "board.json"
    make_board(outside, "Outside")
    link = root / ("linked" if directory else "linked.json")
    try:
        link.symlink_to(outside.parent if directory else outside, target_is_directory=directory)
    except OSError:
        pytest.skip("creating symlinks requires unavailable OS privilege")
    assert catalog_boards(workspace.Workspace(root)) == []


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


@pytest.mark.parametrize(
    "event",
    [
        "created",
        "moved",
        "assigned",
        "prioritized",
        "commented",
        "linked",
        "unlinked",
        "parented",
        "unparented",
        "subject",
        "detail",
        "archived",
        "unarchived",
        "attached",
        "attachment_removed",
        "cover_set",
        "cover_cleared",
    ],
)
def test_catalog_recency_uses_every_card_edit_audit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    old = "2026-10-14T08:00:00-04:00"
    recent = "2026-10-14T10:15:00.125-04:00"
    monkeypatch.setattr(board_state, "now", lambda: old)
    path = tmp_path / "board.json"
    make_board(path, "Recent edits")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    board.create("other", "Other", "backlog", "terry")
    file = attachments.Attachment(
        "11111111-1111-4111-8111-111111111111",
        "picture.png",
        1,
        "image/png",
        "a" * 64,
        old,
        "terry",
    )
    board.add_attachment("card", file)
    board.set_cover("card", file.id, "terry")
    if event == "cover_set":
        board.set_cover("card", None, "terry")
    if event == "unlinked":
        board.link("card", "references", "other", "terry")
    if event == "unparented":
        board.set_parent("card", "other", "terry")
    if event == "unarchived":
        board.set_archived("card", archived=True, by="terry")
    monkeypatch.setattr(board_state, "now", lambda: recent)
    new_file = attachments.Attachment(
        "22222222-2222-4222-8222-222222222222",
        "second.png",
        1,
        "image/png",
        "b" * 64,
        recent,
        "terry",
    )
    actions: dict[str, Callable[[], str]] = {
        "created": partial(board.create, "new", "New card", "backlog", "terry"),
        "moved": partial(board.move, "card", "in_progress", "terry"),
        "assigned": partial(board.assign, "card", "terry", "terry"),
        "prioritized": partial(board.set_priority, "card", "P1", "terry"),
        "commented": partial(board.comment, "card", "Recent comment", "terry"),
        "linked": partial(board.link, "card", "references", "other", "terry"),
        "unlinked": partial(board.unlink, "card", "references", "other", "terry"),
        "parented": partial(board.set_parent, "card", "other", "terry"),
        "unparented": partial(board.set_parent, "card", None, "terry"),
        "subject": partial(board.set_subject, "card", "New subject", "terry"),
        "detail": partial(board.set_detail, "card", "New detail", "terry"),
        "archived": partial(board.set_archived, "card", archived=True, by="terry"),
        "unarchived": partial(board.set_archived, "card", archived=False, by="terry"),
        "attached": partial(board.add_attachment, "card", new_file),
        "attachment_removed": partial(board.remove_attachment, "card", file.id, "terry"),
        "cover_set": partial(board.set_cover, "card", file.id, "terry"),
        "cover_cleared": partial(board.set_cover, "card", None, "terry"),
    }
    actions[event]()
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    assert catalog_boards(instance)[0]["lastCardEditMs"] == int(
        dt.datetime.fromisoformat(recent).timestamp() * 1000
    )


def test_catalog_recency_includes_comments_on_archived_cards(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(board_state, "now", lambda: "2026-10-14T08:00:00-04:00")
    path = tmp_path / "board.json"
    make_board(path, "Archived edits")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    board.set_archived("card", archived=True, by="terry")
    recent = "2026-10-14T09:00:00-04:00"
    monkeypatch.setattr(board_state, "now", lambda: recent)
    board.comment("card", "Still relevant activity", "terry")
    board_state.save(board, path)
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert row["lastCardEditMs"] == int(dt.datetime.fromisoformat(recent).timestamp() * 1000)
    assert row["counts"] == {"blocked": 0, "needsHuman": 0, "review": 0}


def test_catalog_recency_normalizes_offsets_and_ignores_unreadable_stamps(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Legacy activity")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    item = board.find("card")
    item.history[0].at = "unreadable legacy stamp"
    item.comments = [
        board_state.Comment("2026-10-14T15:00:00+02:00", "terry", "Earlier instant"),
        board_state.Comment("2026-10-14T10:00:00-04:00", "terry", "Latest instant"),
        board_state.Comment("not a timestamp", "terry", "Legacy malformed stamp"),
    ]
    board_state.save(board, path)
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert "error" not in row
    assert row["lastCardEditMs"] == int(
        dt.datetime.fromisoformat("2026-10-14T14:00:00Z").timestamp() * 1000
    )


@pytest.mark.parametrize("stamp", [None, "no readable timestamp", "2026-10-14 09:30:00 EDT"])
def test_catalog_recency_has_no_file_or_project_name_fallback(
    tmp_path: pathlib.Path,
    stamp: str | None,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before rename")
    board = board_state.load(path)
    if stamp is not None:
        board.create("card", "Card", "backlog", "terry")
        board.find("card").history[0].at = stamp
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    before = catalog_boards(instance)[0]["lastCardEditMs"]
    parsed = board_state.parse_stamp(stamp)
    expected = None if parsed == board_state.parse_stamp(None) else int(parsed.timestamp() * 1000)
    assert before == expected
    board.project = "After rename"
    board_state.save(board, path)
    os.utime(path, (1900000000, 1900000000))
    assert catalog_boards(instance)[0]["lastCardEditMs"] == before


def test_catalog_recency_is_unknown_for_malformed_board(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Malformed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["items"] = "invalid cards"
    path.write_text(json.dumps(raw), encoding="utf-8")
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert row["lastCardEditMs"] is None
    assert "error" in row


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
