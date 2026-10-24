"""Coordinated public schema/protocol activation on synthetic stopped boards."""

import contextlib
import json
import os
import pathlib
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from click.testing import CliRunner

from localswim import board_state, cli, service_ownership, workspace
from localswim.write_protocol import RestoreReceipt, WriteToken

if TYPE_CHECKING:
    from collections.abc import Generator

API = board_state.API_PREFIX
ROOT = pathlib.Path(__file__).parents[1]
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def initialized(path: pathlib.Path) -> board_state.Board:
    board_state.initialize_board(
        path,
        ROOT / "examples/board-description.example.json",
        ROOT / "examples/permissions.example.json",
    )
    return board_state.load(path)


def legacy(path: pathlib.Path) -> board_state.JsonObject:
    raw = initialized(path).to_json()
    raw["schema"] = 4
    raw.pop("generation")
    path.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def receipt() -> board_state.JsonObject:
    return RestoreReceipt(OTHER, "a" * 64, "b" * 64, "missing", "2026-10-23T00:00:00Z").to_json()


def request(
    url: str,
    *,
    credential: str | None = None,
    token: str | None = None,
    body: board_state.JsonObject | None = None,
) -> tuple[int, board_state.JsonObject]:
    headers: dict[str, str] = {}
    if credential is not None:
        headers["Authorization"] = "Bearer " + credential
    if token is not None:
        headers["If-Match"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    command = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(command, timeout=5) as response:
            return response.status, cast("board_state.JsonObject", json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, cast("board_state.JsonObject", json.loads(exc.read()))


@contextlib.contextmanager
def serving(path: pathlib.Path) -> Generator[str]:
    environment = dict(os.environ)
    command = service_ownership.python_child_command(
        ["-m", "localswim.api_endpoint", str(path), "--port", "0"], environment
    )
    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 15
        while not service_ownership.service_is_healthy(path):
            assert child.poll() is None, "synthetic service exited before admission"
            assert time.monotonic() < deadline, "synthetic service did not start"
            time.sleep(0.02)
        descriptor = service_ownership.service_descriptor(path, require_cli_token=True)
        base = f"http://127.0.0.1:{descriptor['port']}"
        yield base
    finally:
        if child.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                descriptor = service_ownership.service_descriptor(path, require_cli_token=True)
                request(
                    f"http://127.0.0.1:{descriptor['port']}" + API + "/shutdown",
                    credential=str(descriptor["token"]),
                    body={},
                )
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        assert not service_ownership.writer_busy(path)


def test_initialization_and_reads_allocate_generation_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    allocations: list[bool] = []

    def allocate() -> uuid.UUID:
        allocations.append(True)
        return uuid.UUID(OTHER)

    monkeypatch.setattr(board_state.uuid, "uuid4", allocate)
    board = initialized(path)
    assert allocations == [True]
    assert board.generation == OTHER
    for _ in range(3):
        board_state.save(board_state.load(path), path)
    assert allocations == [True]
    assert board_state.load(path).revision == 0


def test_migration_admits_before_allocation_preserves_snapshot_and_outcome(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    original = legacy(path)
    allocations: list[bool] = []

    def allocate() -> uuid.UUID:
        allocations.append(True)
        return uuid.UUID(OTHER)

    monkeypatch.setattr(board_state.uuid, "uuid4", allocate)
    before = path.read_bytes()
    with pytest.raises(board_state.BoardError, match="migrate-generation"):
        board_state.load(path)
    assert path.read_bytes() == before
    result = CliRunner().invoke(cli.cli, [str(path), "board", "migrate-generation"])
    assert result.exit_code == 0, result.output
    assert allocations == [True]
    raw = board_state.load(path).to_json()
    assert raw.pop("generation") == OTHER
    assert raw["revision"] == cast("int", original["revision"]) + 1
    raw["schema"] = original["schema"]
    raw["revision"] = original["revision"]
    assert raw == original
    with pytest.raises(board_state.BoardError, match="schema-4"):
        board_state.migrate_generation(path)
    assert allocations == [True]

    invalid = tmp_path / "invalid.json"
    original["items"] = [{"id": "invalid"}]
    invalid.write_text(json.dumps(original), encoding="utf-8")
    outcome = board_state.SaveOutcome()
    invalid_before = invalid.read_bytes()
    with pytest.raises(board_state.BoardError):
        board_state.migrate_generation(invalid, outcome=outcome)
    assert outcome.state == "uncommitted"
    assert allocations == [True]
    assert invalid.read_bytes() == invalid_before


def test_migration_lease_refusal_and_replacement_outcome(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    legacy(path)
    before = path.read_bytes()
    with service_ownership.writer_claim(path), pytest.raises(OSError, match="owns"):
        board_state.migrate_generation(path)
    assert path.read_bytes() == before
    outcome = board_state.SaveOutcome()
    board_state.migrate_generation(path, outcome=outcome)
    assert outcome.state == "committed"


def test_maintenance_save_preserves_generation_and_receipt(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = initialized(path)
    board.restore_receipt = receipt()
    board_state.save(board, path)
    generation = board.generation
    board_state.add_lane(path, "Investigation")
    board_state.rename_lane_label(path, "investigation", "Investigating")
    board_state.migrate_lane_id(path, "investigation", "investigate")
    loaded = board_state.load(path)
    assert loaded.generation == generation
    assert loaded.restore_receipt == receipt()
    assert loaded.revision == 3
    board_state.save(loaded, path)
    assert board_state.load(path).to_json() == loaded.to_json()


def exercise_cli(path: pathlib.Path, base: str, generation: str) -> board_state.JsonObject:
    result = CliRunner().invoke(cli.cli, [str(path), "board", "set-project", "CLI fresh"])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        cli.cli, [str(path), "card", "create", "fresh", "Fresh card", "--state", "backlog"]
    )
    assert result.exit_code == 0, result.output
    _, snapshot = request(base + API + "/board")
    revision = str(snapshot["revision"])
    explicit = [str(path), "card", "set-priorities", "P3", "1", "--expected-revision", revision]
    result = CliRunner().invoke(cli.cli, [*explicit, "--expected-generation", OTHER])
    assert result.exit_code != 0
    assert "412" in result.output
    result = CliRunner().invoke(cli.cli, [*explicit, "--expected-generation", generation])
    assert result.exit_code == 0, result.output
    _, snapshot = request(base + API + "/board")
    return snapshot


def test_public_lifecycle_cli_direct_workspace_and_restart(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    legacy(path)
    board_state.migrate_generation(path)
    board = board_state.load(path)
    board.restore_receipt = receipt()
    board_state.save(board, path)
    generation = board.generation
    with serving(path) as base:
        snapshot = exercise_cli(path, base, generation)
        _, session = request(base + API + "/browser-session")
        assert session["generation"] == generation
        rev = cast("int", snapshot["revision"])
        wrong = WriteToken(OTHER, rev).format()
        body: board_state.JsonObject = {"project": "direct fresh"}
        for token, expected in ((f'"revision-{rev}"', 428), (wrong, 412)):
            code, response = request(
                base + API + "/board/project",
                credential=str(session["token"]),
                token=token,
                body=body,
            )
            assert code == expected
            assert response["code"] in {"precondition_required", "state_changed"}
        assert (
            request(
                base + API + "/board/project",
                credential=str(session["token"]),
                token=str(snapshot["writeToken"]),
                body=body,
            )[0]
            == 200
        )
        # A normal gateway reuses the real independently started writer.
        app = workspace.Workspace(tmp_path)
        server = workspace.WorkspaceServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            gateway = f"http://127.0.0.1:{server.server_port}"
            route = gateway + API + "/workspace/boards/board/board"
            _, current = request(route)
            assert current["generation"] == generation
            wrong = WriteToken(OTHER, cast("int", current["revision"])).format()
            assert (
                request(
                    route + "/project",
                    credential=app.token,
                    token=wrong,
                    body={"project": "stale workspace"},
                )[0]
                == 412
            )
            assert (
                request(
                    route + "/project",
                    credential=app.token,
                    token=str(current["writeToken"]),
                    body={"project": "workspace fresh"},
                )[0]
                == 200
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.close()
    with serving(path) as base:
        _, restarted = request(base + API + "/board")
        assert restarted["generation"] == generation
        _, fresh_session = request(base + API + "/browser-session")
        assert fresh_session["token"] != session["token"]
        stale = WriteToken(OTHER, cast("int", restarted["revision"])).format()
        assert (
            request(
                base + API + "/board/project",
                credential=str(fresh_session["token"]),
                token=stale,
                body={"project": "old draft fresh bearer"},
            )[0]
            == 412
        )
        assert board_state.load(path).restore_receipt == receipt()
    assert board_state.load(path).project == "workspace fresh"


def test_migration_retains_committed_outcome_after_replace_cleanup_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    legacy(path)
    original = board_state._replace_with_retry  # noqa: SLF001

    def replace_then_fail(
        source: pathlib.Path,
        target: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        original(source, target, outcome=outcome)
        raise OSError("synthetic post-replacement cleanup failure")

    monkeypatch.setattr(board_state, "_replace_with_retry", replace_then_fail)
    outcome = board_state.SaveOutcome()
    with pytest.raises(OSError, match="cleanup failure"):
        board_state.migrate_generation(path, outcome=outcome)
    assert outcome.state == "committed"
    assert board_state.load(path).revision == 1


def test_schema_four_workspace_and_live_health_refuse_with_migration_guidance(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    legacy(path)
    before = path.read_bytes()
    app = workspace.Workspace(tmp_path)
    try:
        rows = cast("list[board_state.JsonObject]", app.catalog()["boards"])
        assert len(rows) == 1
        assert "migrate-generation" in str(rows[0]["error"])
        with pytest.raises(board_state.BoardError, match="migrate-generation"):
            app.resolve_endpoint("board")
        assert not service_ownership.service_is_healthy(path)
        assert path.read_bytes() == before
    finally:
        app.close()
    board_state.migrate_generation(path)
    migrated = path.read_bytes()
    with serving(path) as base:
        path.write_bytes(before)
        code, status = request(base + API + "/status")
        assert code == 200
        assert status["ok"] is False
        assert "migrate-generation" in str(status["error"])
        assert path.read_bytes() == before
        path.write_bytes(migrated)
        assert request(base + API + "/status")[1]["ok"] is True


@pytest.mark.parametrize("schema", [[], {}, True, None, "5"])
def test_workspace_ignores_malformed_schema_envelopes(
    tmp_path: pathlib.Path, schema: board_state.JsonValue
) -> None:
    path = tmp_path / "board.json"
    raw = initialized(path).to_json()
    raw["schema"] = schema
    path.write_text(json.dumps(raw), encoding="utf-8")
    app = workspace.Workspace(tmp_path)
    try:
        assert app.catalog()["boards"] == []
    finally:
        app.close()
