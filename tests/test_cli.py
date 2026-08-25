"""CLI-to-service boundary tests."""

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state
from tests.support import USERS

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def served_board(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    """Publish the production rendezvous file for an isolated service."""
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="CLI", users=USERS, browser_user="terry", cli_user="bot", default_owner="bot"
    )
    board_state.save(board, path)
    api_endpoint.BOARD_PATH = path
    api_endpoint.STORE = api_endpoint.BoardStore(path)
    api_endpoint._shutdown_requested.clear()  # noqa: SLF001 -- lifecycle fixture reset
    server = api_endpoint.http.server.ThreadingHTTPServer(
        (api_endpoint.HOST, 0), api_endpoint.Handler
    )
    api_endpoint.publish_service(path, server.server_address[1])

    def serve() -> None:
        try:
            server.serve_forever()
        finally:
            server.server_close()
            api_endpoint.remove_service(path)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        if thread.is_alive():
            server.shutdown()
        thread.join(timeout=2)
        api_endpoint.remove_service(path)
        api_endpoint._shutdown_requested.clear()  # noqa: SLF001 -- lifecycle fixture reset
        api_endpoint.STORE = None


def run_cli(path: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real command-line entry point."""
    return subprocess.run(
        [sys.executable, "-m", "localswim.cli", str(path), *arguments],
        cwd=pathlib.Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def assert_cli(path: pathlib.Path, *arguments: str) -> None:
    """Require one CLI command to succeed."""
    result = run_cli(path, *arguments)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        (),
        ("activity",),
        ("activity", "between"),
        ("activity", "since"),
        ("board",),
        ("board", "embed-policy"),
        ("board", "init"),
        ("board", "set-project"),
        ("board", "show"),
        ("board", "shutdown"),
        ("board", "verify"),
        ("card",),
        ("card", "assign"),
        ("card", "clear-parent"),
        ("card", "comment"),
        ("card", "create"),
        ("card", "link"),
        ("card", "move"),
        ("card", "next"),
        ("card", "search"),
        ("card", "set-detail"),
        ("card", "set-parent"),
        ("card", "set-priority"),
        ("card", "set-subject"),
        ("card", "show"),
        ("card", "unlink"),
        ("comments",),
        ("comments", "newest"),
        ("lane",),
        ("lane", "migrate"),
        ("lane", "migrate-id"),
        ("lane", "rename-label"),
    ],
)
def test_every_command_level_has_help(
    tmp_path: pathlib.Path,
    command: tuple[str, ...],
) -> None:
    """Keep root, group, and leaf help available without loading the board."""
    result = run_cli(tmp_path / "absent-board.json", *command, "--help")

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "--help" in result.stdout
    assert result.stderr == ""


def write_schema_two_lane_board(path: pathlib.Path, port: int) -> str:
    """Write a valid pre-rename board without teaching production code its old lane ID."""
    current_lane = "ready_for_work"
    legacy_lane = "ready_for_" + "claude"
    board = board_state.Board(
        project="Migration",
        port=port,
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("alpha", "Alpha", current_lane, "bot")
    board.move("alpha", "in_progress", "bot")
    board.move("alpha", current_lane, "bot")
    raw = cast("dict[str, Any]", board.to_json())
    raw["schema"] = board_state.PREVIOUS_BOARD_SCHEMA
    del raw["policy"]
    items = cast("list[dict[str, Any]]", raw["items"])
    for item in items:
        if item.get("state") == current_lane:
            item["state"] = legacy_lane
        history = cast("list[dict[str, Any]]", item.get("history", []))
        for change in history:
            for key in ("from", "to"):
                if change.get(key) == current_lane:
                    change[key] = legacy_lane
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return legacy_lane


def initialization_inputs(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Copy the checked initialization inputs with an isolated unused port."""
    examples = pathlib.Path(__file__).resolve().parents[1] / "examples"
    description = cast(
        "dict[str, Any]",
        json.loads((examples / "board-description.example.json").read_text(encoding="utf-8")),
    )
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        description["port"] = candidate.getsockname()[1]
    description_path = tmp_path / "description.json"
    permissions_path = tmp_path / "permissions.json"
    description_path.write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")
    permissions_path.write_text(
        (examples / "permissions.example.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return description_path, permissions_path


def test_every_cli_mutation_uses_service(served_board: pathlib.Path) -> None:
    assert_cli(served_board, "card", "create", "a", "Alpha", "--state", "backlog")
    assert_cli(served_board, "card", "create", "b", "Beta", "--state", "backlog")
    assert_cli(served_board, "card", "comment", "a", "hello")
    assert_cli(served_board, "card", "assign", "a", "terry")
    assert_cli(served_board, "card", "set-priority", "a", "P1")
    assert_cli(served_board, "card", "set-detail", "a", "--detail", "description")
    assert_cli(served_board, "card", "set-subject", "a", "Renamed")
    assert_cli(served_board, "card", "link", "a", "relates_to", "b")
    assert_cli(served_board, "card", "set-parent", "b", "a")
    assert_cli(served_board, "board", "set-project", "Updated")
    assert_cli(served_board, "card", "unlink", "a", "relates_to", "b")
    assert_cli(served_board, "card", "clear-parent", "b")

    board = board_state.load(served_board)
    item = board.find("a")
    assert board.revision == 12
    assert board.project == "Updated"
    assert item.subject == "Renamed"
    assert item.detail == "description"
    assert item.owner == "terry"
    assert item.priority == "P1"
    assert item.comments[0].by == "bot"
    assert board.links == []
    assert board.find("b").parent is None


def test_cli_forces_utf8_when_inherited_output_encoding_cannot_print_move_arrow(
    served_board: pathlib.Path,
) -> None:
    assert_cli(served_board, "card", "create", "alpha", "Alpha", "--state", "ready_for_work")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "localswim.cli",
            str(served_board),
            "card",
            "move",
            "alpha",
            "in_progress",
        ],
        cwd=pathlib.Path(__file__).resolve().parents[1],
        capture_output=True,
        env=environment,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert "ready_for_work → in_progress" in result.stdout.decode("utf-8")
    assert result.stderr == b""


def test_offline_mutation_has_no_direct_write(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="Offline",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board_state.save(board, path)
    board_state.service_descriptor_path(path).unlink(missing_ok=True)

    result = run_cli(path, "card", "create", "alpha", "Alpha", "--state", "backlog")
    saved = board_state.load(path)
    assert result.returncode != 0
    assert "service is not running" in result.stderr
    assert "Traceback" not in result.stderr
    assert saved.revision == 0
    assert saved.items == []


def test_shutdown_command_stops_live_service(served_board: pathlib.Path) -> None:
    result = run_cli(served_board, "board", "shutdown")

    assert result.returncode == 0, result.stderr
    assert "shutdown scheduled" in result.stdout
    assert not board_state.service_descriptor_path(served_board).exists()


def test_read_only_report_works_offline(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="Offline report",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board_state.save(board, path)
    result = run_cli(path, "board", "show")
    assert result.returncode == 0
    assert "Offline report" in result.stdout


def activity_board(path: pathlib.Path) -> None:
    """Write every persisted activity shape at deterministic timestamps."""
    board = board_state.Board(
        project="Activity",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("alpha", "Private subject", "ready_for_work", "bot")
    alpha = board.find("alpha")
    alpha.history[-1].at = "2026-08-23T10:00:00Z"
    board.move("alpha", "in_progress", "bot")
    alpha.history[-1].at = "2026-08-23T10:01:00Z"
    board.assign("alpha", "terry", "bot")
    alpha.history[-1].at = "2026-08-23T10:02:00Z"
    board.set_priority("alpha", "P1", "bot")
    alpha.history[-1].at = "2026-08-23T10:03:00Z"
    board.comment("alpha", "private board words", "bot")
    alpha.comments[-1].at = "2026-08-23T10:04:00Z"
    board.create("beta", "Later private subject", "backlog", "bot")
    board.find("beta").history[-1].at = "2026-08-23T10:06:00Z"
    board.link("alpha", "blocked_by", "beta", "bot")
    board.relationship_history[-1].at = "2026-08-23T10:07:00Z"
    board.unlink("alpha", "blocked_by", "beta", "bot")
    board.relationship_history[-1].at = "2026-08-23T10:08:00Z"
    board_state.save(board, path)


def newest_comments_board(path: pathlib.Path) -> None:
    """Write comments on two cards at deterministic timestamps."""
    board = board_state.Board(
        project="Newest comments",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("alpha", "Alpha subject", "ready_for_work", "bot")
    board.create("beta", "Beta subject", "backlog", "bot")
    board.comment("alpha", "Older comment", "bot")
    board.find("alpha").comments[-1].at = "2026-08-23T10:00:00Z"
    board.comment("beta", "Second-newest comment\nfollow-up line", "terry")
    board.find("beta").comments[-1].at = "2026-08-23T11:00:00Z"
    board.comment("alpha", "Newest comment", "terry")
    board.find("alpha").comments[-1].at = "2026-08-23T12:00:00Z"
    board_state.save(board, path)


def inspection_board(path: pathlib.Path) -> None:
    """Write one card with every focused-inspection relationship shape."""
    board = board_state.Board(
        project="Inspection",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("parent", "Parent subject", "backlog", "bot")
    board.create(
        "focus",
        "Focused subject",
        "ready_for_work",
        "bot",
        priority="P1",
        detail="Private focused detail",
    )
    board.create("child", "Child subject", "backlog", "bot")
    board.create("blocker", "Blocker subject", "ready_for_work", "bot", priority="P2")
    board.set_parent("focus", "parent", "bot")
    board.set_parent("child", "focus", "bot")
    board.link("focus", "blocked_by", "blocker", "bot")
    board.comment("focus", "Private focused comment", "terry")
    board_state.save(board, path)


def test_show_json_resolves_ticket_and_relationships_without_private_text(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(path, "card", "show", "#0002", "--json")

    assert result.returncode == 0, result.stderr
    report = cast("dict[str, Any]", json.loads(result.stdout))
    assert report["id"] == "focus"
    assert report["ticket"] == 2
    assert report["subject"] == "Focused subject"
    assert report["state"] == "ready_for_work"
    assert report["priority"] == "P1"
    assert report["owner"] == "bot"
    assert report["commentCount"] == 1
    assert cast("dict[str, Any]", report["parent"])["id"] == "parent"
    assert [child["id"] for child in cast("list[dict[str, Any]]", report["children"])] == ["child"]
    relationships = cast("list[dict[str, Any]]", report["relationships"])
    assert relationships == [
        {
            "kind": "blocked_by",
            "item": {
                "id": "blocker",
                "ticket": 4,
                "subject": "Blocker subject",
                "state": "ready_for_work",
                "priority": "P2",
                "owner": "bot",
            },
        }
    ]
    assert "detail" not in report
    assert "comments" not in report
    assert "Private focused detail" not in result.stdout
    assert "Private focused comment" not in result.stdout


def test_show_human_output_is_safe_until_private_text_is_explicitly_requested(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    safe = run_cli(path, "card", "show", "2")
    with_comments = run_cli(path, "card", "show", "focus", "--include-comments")
    with_comments_json = run_cli(path, "card", "show", "focus", "--include-comments", "--json")

    assert safe.returncode == 0, safe.stderr
    assert "#0002 focus" in safe.stdout
    assert "blocked_by: #0004 blocker" in safe.stdout
    assert "comments: 1" in safe.stdout
    assert "Private focused detail" not in safe.stdout
    assert "Private focused comment" not in safe.stdout
    assert with_comments.returncode == 0, with_comments.stderr
    assert "Private focused detail" in with_comments.stdout
    assert "Private focused comment" in with_comments.stdout
    assert with_comments_json.returncode == 0, with_comments_json.stderr
    comments_report = cast("dict[str, Any]", json.loads(with_comments_json.stdout))
    assert comments_report["detail"] == "Private focused detail"
    comments = cast("list[dict[str, Any]]", comments_report["comments"])
    assert comments[0]["text"] == "Private focused comment"


def test_show_refuses_audit_drift(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)
    board = board_state.load(path)
    board.find("focus").state = "completed"
    board_state.save(board, path)

    result = run_cli(path, "card", "show", "focus")

    assert result.returncode != 0
    assert "focused inspection refused 1 audit-trail problem" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("card", "show"), "Missing argument 'REFERENCE'"),
        (("card", "show", "focus", "--include-prose"), "No such option"),
        (("card", "show", "999"), "no card with ticket #0999"),
    ],
)
def test_show_rejects_invalid_arguments(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(path, *arguments)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_next_json_uses_shared_total_order_and_returns_focused_details(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(
        path,
        "card",
        "next",
        "3",
        "--lane",
        "ready_for_work",
        "--lane",
        "backlog",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = cast("dict[str, Any]", json.loads(result.stdout))
    assert report["lanes"] == ["ready_for_work", "backlog"]
    assert report["limit"] == 3
    assert report["order"] == [
        "policyPriority",
        "ticket",
    ]
    items = cast("list[dict[str, Any]]", report["items"])
    assert [item["id"] for item in items] == ["focus", "blocker", "parent"]
    assert items[0]["commentCount"] == 1
    assert items[0]["childCount"] == 1
    assert "children" not in items[0]
    assert cast("list[dict[str, Any]]", items[0]["relationships"])[0]["kind"] == "blocked_by"
    assert "detail" not in items[0]
    assert "comments" not in items[0]
    assert "Private focused detail" not in result.stdout
    assert "Private focused comment" not in result.stdout


def test_next_human_output_exposes_details_and_comments_when_requested(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    safe = run_cli(
        path,
        "card",
        "next",
        "1",
        "--lane",
        "backlog",
        "--lane",
        "ready_for_work",
    )
    with_comments = run_cli(
        path,
        "card",
        "next",
        "1",
        "--lane",
        "backlog",
        "--lane",
        "ready_for_work",
        "--include-comments",
    )

    assert safe.returncode == 0, safe.stderr
    assert "policy priority, then ticket" in safe.stdout
    assert "#0002 focus" in safe.stdout
    assert "children: 1" in safe.stdout
    assert "blocked_by: #0004 blocker" in safe.stdout
    assert "Private focused detail" not in safe.stdout
    assert "Private focused comment" not in safe.stdout
    assert with_comments.returncode == 0, with_comments.stderr
    assert "Private focused detail" in with_comments.stdout
    assert "Private focused comment" in with_comments.stdout


def test_next_refuses_audit_drift(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)
    board = board_state.load(path)
    board.find("focus").state = "completed"
    board_state.save(board, path)

    result = run_cli(path, "card", "next", "2", "--lane", "ready_for_work")

    assert result.returncode != 0
    assert "prioritized inspection refused 1 audit-trail problem" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("card", "next", "0", "--lane", "backlog"), "must be a positive integer"),
        (("card", "next", "2"), "Missing option '--lane'"),
        (
            ("card", "next", "2", "--lane", "not_a_lane"),
            "contains unknown lane(s): not_a_lane",
        ),
    ],
)
def test_next_rejects_invalid_arguments(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(path, *arguments)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_search_json_matches_ids_tickets_and_subjects_in_shared_total_order(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(
        path,
        "card",
        "search",
        "SUBJECT",
        "--lane",
        "ready_for_work",
        "--lane",
        "backlog",
        "--json",
    )
    ticket_result = run_cli(path, "card", "search", "#0002", "--json")
    id_result = run_cli(path, "card", "search", "LOCK", "--json")

    assert result.returncode == 0, result.stderr
    report = cast("dict[str, Any]", json.loads(result.stdout))
    assert report["query"] == "SUBJECT"
    assert report["lanes"] == ["ready_for_work", "backlog"]
    assert report["searchedFields"] == ["id", "ticket", "subject"]
    assert report["order"] == ["policyPriority", "ticket"]
    items = cast("list[dict[str, Any]]", report["items"])
    assert [item["id"] for item in items] == ["focus", "blocker", "parent", "child"]
    assert all("detail" not in item for item in items)
    assert all("comments" not in item for item in items)

    assert ticket_result.returncode == 0, ticket_result.stderr
    ticket_report = cast("dict[str, Any]", json.loads(ticket_result.stdout))
    ticket_items = cast("list[dict[str, Any]]", ticket_report["items"])
    assert [item["id"] for item in ticket_items] == ["focus"]

    assert id_result.returncode == 0, id_result.stderr
    id_report = cast("dict[str, Any]", json.loads(id_result.stdout))
    id_items = cast("list[dict[str, Any]]", id_report["items"])
    assert [item["id"] for item in id_items] == ["blocker"]


def test_search_details_and_comments_require_explicit_request(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    safe = run_cli(path, "card", "search", "private focused")
    with_comments = run_cli(
        path,
        "card",
        "search",
        "PRIVATE FOCUSED",
        "--include-comments",
        "--json",
    )

    assert safe.returncode == 0, safe.stderr
    assert "No cards matched" in safe.stdout
    assert "#0002 focus" not in safe.stdout
    assert with_comments.returncode == 0, with_comments.stderr
    report = cast("dict[str, Any]", json.loads(with_comments.stdout))
    assert report["searchedFields"] == ["id", "ticket", "subject", "detail", "comments"]
    items = cast("list[dict[str, Any]]", report["items"])
    assert [item["id"] for item in items] == ["focus"]
    assert items[0]["detail"] == "Private focused detail"
    comments = cast("list[dict[str, Any]]", items[0]["comments"])
    assert comments[0]["text"] == "Private focused comment"


def test_search_refuses_audit_drift(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)
    board = board_state.load(path)
    board.find("focus").state = "completed"
    board_state.save(board, path)

    result = run_cli(path, "card", "search", "focus")

    assert result.returncode != 0
    assert "ticket search refused 1 audit-trail problem" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("card", "search", " "), "card search QUERY requires non-whitespace text"),
        (
            ("card", "search", "focus", "--lane", "not_a_lane"),
            "contains unknown lane(s): not_a_lane",
        ),
    ],
)
def test_search_rejects_invalid_arguments(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "board.json"
    inspection_board(path)

    result = run_cli(path, *arguments)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_newest_comments_is_bounded_newest_first_and_includes_text(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    newest_comments_board(path)

    result = run_cli(path, "comments", "newest", "2")
    json_result = run_cli(path, "comments", "newest", "2", "--json")

    assert result.returncode == 0, result.stderr
    assert "Newest 2 comment(s):" in result.stdout
    assert result.stdout.index("Newest comment") < result.stdout.index("Second-newest comment")
    assert "  follow-up line" in result.stdout
    assert "Older comment" not in result.stdout

    assert json_result.returncode == 0, json_result.stderr
    report = cast("dict[str, Any]", json.loads(json_result.stdout))
    assert report["limit"] == 2
    assert report["order"] == ["commentTimeDesc", "ticketDesc", "commentSequenceDesc"]
    comments = cast("list[dict[str, Any]]", report["comments"])
    assert comments == [
        {
            "ticket": 1,
            "id": "alpha",
            "at": "2026-08-23T12:00:00Z",
            "by": "terry",
            "text": "Newest comment",
        },
        {
            "ticket": 2,
            "id": "beta",
            "at": "2026-08-23T11:00:00Z",
            "by": "terry",
            "text": "Second-newest comment\nfollow-up line",
        },
    ]


def test_newest_comments_refuses_audit_drift(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    newest_comments_board(path)
    board = board_state.load(path)
    board.find("alpha").state = "completed"
    board_state.save(board, path)

    result = run_cli(path, "comments", "newest", "1")

    assert result.returncode != 0
    assert "newest-comments report refused 1 audit-trail problem" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("comments", "newest", "0"), "must be a positive integer"),
    ],
)
def test_newest_comments_rejects_invalid_arguments(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "board.json"
    newest_comments_board(path)

    result = run_cli(path, *arguments)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_activity_between_is_inclusive_chronological_and_sanitized(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    activity_board(path)

    result = run_cli(
        path,
        "activity",
        "between",
        "2026-08-23T10:01:00Z",
        "2026-08-23T10:04:00Z",
    )

    assert result.returncode == 0, result.stderr
    assert [
        result.stdout.index(kind) for kind in ("moved", "assigned", "prioritized", "commented")
    ] == sorted(
        result.stdout.index(kind) for kind in ("moved", "assigned", "prioritized", "commented")
    )
    assert "2026-08-23T10:01:00Z" in result.stdout
    assert "2026-08-23T10:04:00Z" in result.stdout
    assert "2026-08-23T10:00:00Z" not in result.stdout
    assert "2026-08-23T10:06:00Z" not in result.stdout
    assert "private board words" not in result.stdout
    assert "Private subject" not in result.stdout
    assert "19 character(s)" in result.stdout


def test_activity_since_json_is_composable_and_omits_comment_text(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    activity_board(path)

    result = run_cli(path, "activity", "since", "2026-08-23T10:00:00Z", "--json")

    assert result.returncode == 0, result.stderr
    events = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert [event["kind"] for event in events] == [
        "created",
        "moved",
        "assigned",
        "prioritized",
        "commented",
        "created",
        "linked",
        "unlinked",
    ]
    comment = next(event for event in events if event["kind"] == "commented")
    assert comment["commentChars"] == 19
    assert "text" not in comment
    assert "instant" not in comment
    assert "sequence" not in comment
    assert "private board words" not in result.stdout
    assert "Private subject" not in result.stdout
    relationship_events = [event for event in events if event["kind"] in {"linked", "unlinked"}]
    assert relationship_events == [
        {
            "ticket": 1,
            "id": "alpha",
            "kind": "linked",
            "at": "2026-08-23T10:07:00Z",
            "by": "bot",
            "relationshipKind": "blocked_by",
            "otherTicket": 2,
            "otherId": "beta",
        },
        {
            "ticket": 1,
            "id": "alpha",
            "kind": "unlinked",
            "at": "2026-08-23T10:08:00Z",
            "by": "bot",
            "relationshipKind": "blocked_by",
            "otherTicket": 2,
            "otherId": "beta",
        },
    ]


def test_activity_relationship_human_output_names_opposite_endpoint(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    activity_board(path)

    result = run_cli(path, "activity", "since", "2026-08-23T10:07:00Z")

    assert result.returncode == 0, result.stderr
    assert (
        "2026-08-23T10:07:00Z  #0001 alpha  linked by bot  blocked_by #0002 beta" in result.stdout
    )
    assert (
        "2026-08-23T10:08:00Z  #0001 alpha  unlinked by bot  blocked_by #0002 beta" in result.stdout
    )
    assert "Private subject" not in result.stdout


def test_activity_microsecond_bounds_exclude_an_event_before_the_cursor(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="Activity precision",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("alpha", "Private subject", "ready_for_work", "bot")
    alpha = board.find("alpha")
    alpha.history[-1].at = "2026-08-24T12:00:00.100000-04:00"
    board.comment("alpha", "private board words", "bot")
    alpha.comments[-1].at = "2026-08-24T12:00:00.300000-04:00"
    board_state.save(board, path)

    result = run_cli(
        path,
        "activity",
        "between",
        "2026-08-24T12:00:00.200000-04:00",
        "2026-08-24T12:00:00.400000-04:00",
    )

    assert result.returncode == 0, result.stderr
    assert "commented by bot" in result.stdout
    assert "created by bot" not in result.stdout
    assert "private board words" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("activity", "since", "2026-08-23T10:00:00"), "UTC offset"),
        (
            (
                "activity",
                "between",
                "2026-08-23T10:01:00Z",
                "2026-08-23T10:00:00Z",
            ),
            "END must not be earlier",
        ),
    ],
)
def test_activity_query_rejects_invalid_arguments(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    path = tmp_path / "board.json"
    activity_board(path)

    result = run_cli(path, *arguments)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_migrates_lane_state_and_history_offline(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        unused_port = candidate.getsockname()[1]
    legacy_lane = write_schema_two_lane_board(path, unused_port)

    result = run_cli(path, "lane", "migrate", legacy_lane, "ready_for_work")

    assert result.returncode == 0, result.stderr
    assert "migrated schema 2 -> 4" in result.stdout
    assert "replaced 4 lane value(s)" in result.stdout
    board = board_state.load(path)
    assert board.revision == 1
    assert board.find("alpha").state == "ready_for_work"
    assert board.find("alpha").replayed_state() == "ready_for_work"
    assert board.verify() == []


def test_lane_migration_refuses_a_listening_board_port(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listening_port = listener.getsockname()[1]
        listener.listen()
        legacy_lane = write_schema_two_lane_board(path, listening_port)
        before = path.read_bytes()
        result = run_cli(path, "lane", "migrate", legacy_lane, "ready_for_work")

    assert result.returncode != 0
    assert f"board port {listening_port} is listening" in result.stderr
    assert path.read_bytes() == before


def test_cli_initializes_stable_slugs_and_resolves_name_permissions(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)

    result = run_cli(path, "board", "init", str(description), str(permissions))

    assert result.returncode == 0, result.stderr
    board = board_state.load(path)
    assert board.policy.states == (
        "backlog",
        "ready_for_work",
        "in_progress",
        "ready_for_review",
        "completed",
    )
    assert board.policy.may_move("terry", "backlog", "in_progress")
    assert board.policy.may_move("terry", "ready_for_work", "in_progress")
    assert board.policy.may_move("bot", "ready_for_work", "in_progress")
    assert board.revision == 0
    assert board.items == []


def test_cli_initializer_rejects_slug_collisions(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description_path, permissions = initialization_inputs(tmp_path)
    description = cast("dict[str, Any]", json.loads(description_path.read_text(encoding="utf-8")))
    description["lanes"] = [{"name": "Ready For Work"}, {"name": "ready-for-work"}]
    description_path.write_text(json.dumps(description), encoding="utf-8")

    result = run_cli(path, "board", "init", str(description_path), str(permissions))

    assert result.returncode != 0
    assert "slug collision" in result.stderr
    assert not path.exists()


def test_cli_initializer_accepts_an_explicit_import_id(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description_path, permissions = initialization_inputs(tmp_path)
    description = cast("dict[str, Any]", json.loads(description_path.read_text(encoding="utf-8")))
    lanes = cast("list[dict[str, Any]]", description["lanes"])
    lanes[0]["id"] = "someday"
    description_path.write_text(json.dumps(description), encoding="utf-8")

    result = run_cli(path, "board", "init", str(description_path), str(permissions))

    assert result.returncode == 0, result.stderr
    board = board_state.load(path)
    assert board.policy.states[0] == "someday"
    assert board.policy.may_move("terry", "someday", "in_progress")


def test_cli_renames_label_without_changing_lane_identity(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)
    assert run_cli(path, "board", "init", str(description), str(permissions)).returncode == 0

    result = run_cli(path, "lane", "rename-label", "ready_for_work", "Selected Work")

    assert result.returncode == 0, result.stderr
    board = board_state.load(path)
    assert board.policy.lane_label["ready_for_work"] == "Selected Work"
    assert "selected_work" not in board.policy.states
    assert board.revision == 1


def test_cli_migrates_embedded_lane_id_atomically(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)
    assert run_cli(path, "board", "init", str(description), str(permissions)).returncode == 0
    board = board_state.load(path)
    board.create("alpha", "Alpha", "ready_for_work", "bot")
    board_state.save(board, path)

    result = run_cli(path, "lane", "migrate-id", "ready_for_work", "selected_work")

    assert result.returncode == 0, result.stderr
    migrated = board_state.load(path)
    assert "ready_for_work" not in json.dumps(migrated.to_json())
    assert migrated.find("alpha").state == "selected_work"
    assert migrated.find("alpha").replayed_state() == "selected_work"
    assert migrated.policy.may_move("terry", "selected_work", "in_progress")
    assert migrated.verify() == []
    assert migrated.revision == 1


def test_cli_embeds_policy_into_a_schema_three_board(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    board = board_state.Board(
        project="Migration",
        port=port,
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    raw = cast("dict[str, Any]", board.to_json())
    raw["schema"] = board_state.EMBEDDED_POLICY_PREVIOUS_SCHEMA
    del raw["policy"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    result = run_cli(path, "board", "embed-policy", str(board_state.RULES_PATH))

    assert result.returncode == 0, result.stderr
    migrated = board_state.load(path)
    assert migrated.revision == 1
    assert migrated.policy.states == board_state.TransitionPolicy.load().states
