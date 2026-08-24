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
        [sys.executable, "-m", "localswim.board_state", str(path), *arguments],
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
    assert_cli(served_board, "--create", "a", "Alpha", "--state", "backlog")
    assert_cli(served_board, "--create", "b", "Beta", "--state", "backlog")
    assert_cli(served_board, "--comment", "a", "hello")
    assert_cli(served_board, "--assign", "a", "terry")
    assert_cli(served_board, "--set-priority", "a", "P1")
    assert_cli(served_board, "--set-detail", "a", "--detail", "description")
    assert_cli(served_board, "--set-subject", "a", "Renamed")
    assert_cli(served_board, "--link", "a", "relates_to", "b")
    assert_cli(served_board, "--set-parent", "b", "a")
    assert_cli(served_board, "--set-project", "Updated")
    assert_cli(served_board, "--unlink", "a", "relates_to", "b")
    assert_cli(served_board, "--clear-parent", "b")

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
    assert_cli(served_board, "--create", "alpha", "Alpha", "--state", "ready_for_work")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "localswim.board_state",
            str(served_board),
            "--move",
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

    result = run_cli(path, "--create", "alpha", "Alpha", "--state", "backlog")
    saved = board_state.load(path)
    assert result.returncode != 0
    assert "service is not running" in result.stderr
    assert "Traceback" not in result.stderr
    assert saved.revision == 0
    assert saved.items == []


def test_shutdown_command_stops_live_service(served_board: pathlib.Path) -> None:
    result = run_cli(served_board, "--shutdown")

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
    result = run_cli(path)
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
    board_state.save(board, path)


def test_activity_between_is_inclusive_chronological_and_sanitized(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    activity_board(path)

    result = run_cli(
        path,
        "--activity-between",
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

    result = run_cli(path, "--activity-since", "2026-08-23T10:00:00Z", "--json")

    assert result.returncode == 0, result.stderr
    events = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert [event["kind"] for event in events] == [
        "created",
        "moved",
        "assigned",
        "prioritized",
        "commented",
        "created",
    ]
    comment = next(event for event in events if event["kind"] == "commented")
    assert comment["commentChars"] == 19
    assert "text" not in comment
    assert "instant" not in comment
    assert "sequence" not in comment
    assert "private board words" not in result.stdout
    assert "Private subject" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--activity-since", "2026-08-23T10:00:00"), "UTC offset"),
        (
            (
                "--activity-between",
                "2026-08-23T10:01:00Z",
                "2026-08-23T10:00:00Z",
            ),
            "END must not be earlier",
        ),
        (
            (
                "--activity-since",
                "2026-08-23T10:00:00Z",
                "--comment",
                "alpha",
                "no",
            ),
            "cannot be combined",
        ),
    ],
)
def test_activity_query_rejects_invalid_or_conflicting_arguments(
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

    result = run_cli(path, "--migrate-lane", legacy_lane, "ready_for_work")

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
        result = run_cli(path, "--migrate-lane", legacy_lane, "ready_for_work")

    assert result.returncode != 0
    assert f"board port {listening_port} is listening" in result.stderr
    assert path.read_bytes() == before


def test_cli_initializes_stable_slugs_and_resolves_name_permissions(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)

    result = run_cli(path, "--init", str(description), str(permissions))

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

    result = run_cli(path, "--init", str(description_path), str(permissions))

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

    result = run_cli(path, "--init", str(description_path), str(permissions))

    assert result.returncode == 0, result.stderr
    board = board_state.load(path)
    assert board.policy.states[0] == "someday"
    assert board.policy.may_move("terry", "someday", "in_progress")


def test_cli_renames_label_without_changing_lane_identity(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)
    assert run_cli(path, "--init", str(description), str(permissions)).returncode == 0

    result = run_cli(path, "--rename-lane-label", "ready_for_work", "Selected Work")

    assert result.returncode == 0, result.stderr
    board = board_state.load(path)
    assert board.policy.lane_label["ready_for_work"] == "Selected Work"
    assert "selected_work" not in board.policy.states
    assert board.revision == 1


def test_cli_migrates_embedded_lane_id_atomically(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    description, permissions = initialization_inputs(tmp_path)
    assert run_cli(path, "--init", str(description), str(permissions)).returncode == 0
    board = board_state.load(path)
    board.create("alpha", "Alpha", "ready_for_work", "bot")
    board_state.save(board, path)

    result = run_cli(path, "--migrate-lane-id", "ready_for_work", "selected_work")

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

    result = run_cli(path, "--embed-policy", str(board_state.RULES_PATH))

    assert result.returncode == 0, result.stderr
    migrated = board_state.load(path)
    assert migrated.revision == 1
    assert migrated.policy.states == board_state.TransitionPolicy.load().states
