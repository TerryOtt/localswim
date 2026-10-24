"""Offline restore command outcomes through synthetic native filesystem fixtures."""

import json
import os
import socket
import subprocess
lazy import pathlib

import pytest
from click.testing import CliRunner, Result

from localswim import board_state, cli, restore, restore_plan, service_ownership
from tests.test_restore_candidate import snapshot

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


def setup(
    root: pathlib.Path, board: board_state.Board
) -> tuple[pathlib.Path, pathlib.Path, restore_plan.SealedPlan]:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        board.port = reserved.getsockname()[1]
    source = snapshot(root / "source", board)
    board.project = "Current"
    target = snapshot(root / "target", board)
    sealed = restore_plan.create_plan(source, target)
    artifact = root / "review.json"
    artifact.write_bytes(sealed.document)
    return target, artifact, sealed


def invoke(
    target: pathlib.Path,
    artifact: pathlib.Path,
    sealed: restore_plan.SealedPlan,
    command: str,
    *flags: str,
) -> Result:
    arguments = [
        str(target),
        "board",
        "restore",
        command,
        str(artifact),
        "--expect-plan",
        sealed.plan_digest,
    ]
    if command == "apply":
        arguments.extend(
            [
                "--staging",
                str(artifact.parent / "stage"),
                "--checkpoint",
                str(artifact.parent / "checkpoint"),
            ]
        )
    return CliRunner().invoke(cli.cli, [*arguments, *flags])


def test_apply_repeat_and_inspect_report_exact_committed_receipt(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, artifact, sealed = setup(tmp_path, board)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline restore attempted service mutation or lifecycle action")

    monkeypatch.setattr(board_state, "apply_remote_mutation", forbidden)
    monkeypatch.setattr(board_state, "shutdown_service", forbidden)
    first = invoke(target, artifact, sealed, "apply", "--json")
    assert first.exit_code == 0, first.output
    outcome = json.loads(first.output)
    assert outcome["state"] == "committed"
    assert outcome["operationId"] == sealed.core["operationId"]
    assert outcome["planDigest"] == sealed.plan_digest
    assert target.read_bytes() == sealed.final_snapshot
    # Reusing the same paths proves repeat recognition precedes preparation.
    monkeypatch.setattr(restore, "_commit", forbidden)
    repeat = invoke(target, artifact, sealed, "apply", "--json")
    inspected = invoke(target, artifact, sealed, "inspect", "--json")
    assert repeat.exit_code == inspected.exit_code == 0
    assert json.loads(repeat.output) == json.loads(inspected.output) == outcome
    assert target.read_bytes() == sealed.final_snapshot


def test_inspect_uncommitted_is_read_only_and_nonzero(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    before = target.read_bytes()
    result = invoke(target, artifact, sealed, "inspect", "--json")
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["state"] == "uncommitted"
    assert target.read_bytes() == before
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("as_json", [False, True])
def test_unknown_outcome_is_explicit_without_replay(
    tmp_path: pathlib.Path, board: board_state.Board, *, as_json: bool
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    target.write_bytes(b"different current bytes")
    result = invoke(target, artifact, sealed, "inspect", *(["--json"] if as_json else []))
    assert result.exit_code == 2, result.output
    if as_json:
        assert json.loads(result.output)["state"] == "unknown"
    else:
        assert "Restore outcome: unknown" in result.output
        assert "Inspect this sealed plan before any retry" in result.output
    assert target.read_bytes() == b"different current bytes"
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


def test_stale_plan_refuses_without_staging_or_replacement(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    target.write_bytes(b"newer owner state")
    result = invoke(target, artifact, sealed, "apply", "--json")
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert target.read_bytes() == b"newer owner state"
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("command", ["apply", "inspect"])
def test_writer_claim_refuses_offline_commands_without_fallback(
    tmp_path: pathlib.Path, board: board_state.Board, command: str
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    before = target.read_bytes()
    with service_ownership.writer_claim(target):
        result = invoke(target, artifact, sealed, command, "--json")
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert target.read_bytes() == before
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "oversized", "malformed", "digest", "expected"]
)
def test_plan_artifact_and_digest_refusals_preserve_target(
    tmp_path: pathlib.Path,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    before = target.read_bytes()
    if kind == "missing":
        artifact.unlink()
    elif kind == "directory":
        artifact.unlink()
        artifact.mkdir()
    elif kind == "oversized":
        monkeypatch.setattr(restore_plan, "MAX_PLAN_BYTES", 1)
    elif kind == "malformed":
        artifact.write_bytes(b"{}")
    elif kind == "digest":
        changed = json.loads(sealed.document)
        changed["planDigest"] = "0" * 64
        artifact.write_text(json.dumps(changed), encoding="utf-8")
    flags = ["--expect-plan", "0" * 64] if kind == "expected" else []
    result = invoke(target, artifact, sealed, "apply", "--json", *flags)
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert target.read_bytes() == before
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("role", ["target", "plan"])
def test_alias_spelling_is_not_resolved_away(
    tmp_path: pathlib.Path, board: board_state.Board, role: str
) -> None:
    target, artifact, sealed = setup(tmp_path, board)
    alias = tmp_path / "alias"
    actual = target.parent if role == "target" else tmp_path
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(actual)],
        capture_output=True,
        check=False,
    )
    assert linked.returncode == 0
    try:
        selected_target = alias / target.name if role == "target" else target
        selected_artifact = alias / artifact.name if role == "plan" else artifact
        result = invoke(selected_target, selected_artifact, sealed, "inspect")
        assert result.exit_code != 0
        assert "Error:" in result.output
        assert "Reparse" in result.output or "canonical target differs" in result.output
    finally:
        alias.rmdir()


def test_apply_requires_explicit_review_and_storage_paths(tmp_path: pathlib.Path) -> None:
    arguments = [str(tmp_path / "board.json"), "board", "restore", "apply", "plan.json"]
    runner = CliRunner()
    for flags in ([], ["--expect-plan", "a" * 64], ["--staging", "stage"]):
        result = runner.invoke(cli.cli, [*arguments, *flags])
        assert result.exit_code == 2
        assert "Missing option" in result.output
