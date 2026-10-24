"""Offline sealed-plan CLI behavior through real Windows filesystem proofs."""

import json
import os
import subprocess
lazy import pathlib

import pytest
from click.testing import CliRunner

from localswim import board_state, cli, restore_plan
from localswim.restore_paths import CheckedPath, PathBoundaryError
from tests.test_restore_candidate import attach, snapshot

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


def test_plan_writes_only_sealed_output_and_reports_review_summary(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(tmp_path / "source", board)
    board.project = "Current"
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def no_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Plan attempted active-board write or service access")

    monkeypatch.setattr(board_state, "save", no_write)
    monkeypatch.setattr(board_state, "apply_remote_mutation", no_write)
    monkeypatch.setattr(board_state, "shutdown_service", no_write)
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(output.read_bytes())
    assert envelope["planDigest"] in result.output
    assert "--expect-plan" in result.output
    for field in ("target", "expiresAt", "policyChanged", "actorsChanged", "recovery"):
        assert field in result.output
    assert 'policy"' not in result.output
    assert (
        restore_plan.verify_plan(
            output.read_bytes(), expect_plan=envelope["planDigest"], target=target
        ).document
        == output.read_bytes()
    )
    assert all(path.read_bytes() == data for path, data in before.items())
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == {*before, output}


@pytest.mark.parametrize("current", ["missing", "corrupt"])
def test_raw_recovery_needs_explicit_ack_and_intended_port(
    tmp_path: pathlib.Path, board: board_state.Board, current: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "current" / "board.json"
    target.parent.mkdir()
    if current == "corrupt":
        target.write_bytes(b"unparseable current snapshot")
    output = tmp_path / "review.json"
    arguments = [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    runner = CliRunner()
    for flags in ([], ["--acknowledge-raw-recovery"], ["--port", "8091"]):
        assert runner.invoke(cli.cli, [*arguments, *flags]).exit_code != 0
        assert not output.exists()
    result = runner.invoke(cli.cli, [*arguments, "--acknowledge-raw-recovery", "--port", "8091"])
    assert result.exit_code == 0, result.output
    assert "validated recovery point" in result.output
    assert '"port": 8091' in result.output
    assert json.loads(output.read_bytes())["core"]["current"]["status"] == current


def test_incomplete_current_ack_and_retained_provenance_are_truthful(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    retained = attach(board, tmp_path / "source", removed=True)
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    assert retained.exists()
    output = tmp_path / "review.json"
    arguments = [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    runner = CliRunner()
    assert runner.invoke(cli.cli, arguments).exit_code != 0
    assert not output.exists()
    result = runner.invoke(cli.cli, [*arguments, "--acknowledge-incomplete-recovery"])
    assert result.exit_code == 0, result.output
    assert "recovery is incomplete" in result.output
    assert "original upload digest unavailable" in result.output


@pytest.mark.parametrize(
    "role",
    [
        "source",
        "target",
        "source attachments",
        "target attachments",
        "existing",
        "missing parent",
        "directory",
    ],
)
def test_output_alias_overlap_and_existing_bytes_are_never_overwritten(
    tmp_path: pathlib.Path, board: board_state.Board, role: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"do not overwrite")
    output = {
        "source": source,
        "target": target,
        "source attachments": source.parent / "attachments" / "plan.json",
        "target attachments": target.parent / "attachments" / "plan.json",
        "existing": existing,
        "missing parent": tmp_path / "absent" / "plan.json",
        "directory": tmp_path,
    }[role]
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code != 0
    assert all(path.read_bytes() == data for path, data in before.items())
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == set(before)


def test_output_created_during_planning_refuses_without_clobber(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    original = restore_plan.create_plan

    def competing_output(
        source: pathlib.Path,
        target: pathlib.Path,
        *,
        port: int | None = None,
        acknowledge_raw_recovery: bool = False,
        acknowledge_incomplete_recovery: bool = False,
    ) -> restore_plan.SealedPlan:
        plan = original(
            source,
            target,
            port=port,
            acknowledge_raw_recovery=acknowledge_raw_recovery,
            acknowledge_incomplete_recovery=acknowledge_incomplete_recovery,
        )
        output.write_bytes(b"competing owner")
        return plan

    monkeypatch.setattr(restore_plan, "create_plan", competing_output)
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code != 0
    assert output.read_bytes() == b"competing owner"


def test_output_cap_refuses_before_creation(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    monkeypatch.setattr(restore_plan, "MAX_PLAN_BYTES", 1)
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code != 0
    assert not output.exists()


def test_large_envelope_readback_uses_plan_cap_and_refuses_overflow(
    tmp_path: pathlib.Path,
) -> None:
    output = tmp_path / "large-plan.json"
    document = b"x" * (65 * 1024 * 1024)
    cli._write_restore_plan(  # noqa: SLF001 -- bounded artifact creation seam
        output, document, restore_plan.MAX_PLAN_BYTES
    )
    with CheckedPath.capture(output) as proof:
        assert proof.read_bytes(restore_plan.MAX_PLAN_BYTES) == document
        with pytest.raises(PathBoundaryError, match="exceeds"):
            proof.read_bytes(64 * 1024 * 1024)
        with pytest.raises(PathBoundaryError, match="Invalid bounded"):
            proof.read_bytes(restore_plan.MAX_PLAN_BYTES + 1)
    oversized = tmp_path / "oversized-plan.json"
    with pytest.raises(PathBoundaryError, match="bounded output cap"):
        cli._write_restore_plan(  # noqa: SLF001 -- refusal must precede file creation
            oversized, b"x" * (restore_plan.MAX_PLAN_BYTES + 1), restore_plan.MAX_PLAN_BYTES
        )
    assert not oversized.exists()


def test_attachment_drift_between_freezing_and_output_refuses(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    board.items.clear()
    board.next_ticket = 2
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    original = restore_plan.create_plan

    def changed_source(
        source: pathlib.Path,
        target: pathlib.Path,
        *,
        port: int | None = None,
        acknowledge_raw_recovery: bool = False,
        acknowledge_incomplete_recovery: bool = False,
    ) -> restore_plan.SealedPlan:
        plan = original(
            source,
            target,
            port=port,
            acknowledge_raw_recovery=acknowledge_raw_recovery,
            acknowledge_incomplete_recovery=acknowledge_incomplete_recovery,
        )
        attachment.write_bytes(b"changed after freeze")
        return plan

    monkeypatch.setattr(restore_plan, "create_plan", changed_source)
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code != 0
    assert "drift" in result.output
    assert not output.exists()


@pytest.mark.parametrize("aliased_role", ["source", "target", "output"])
def test_junction_spelling_refuses_including_raw_board_argument(
    tmp_path: pathlib.Path, board: board_state.Board, aliased_role: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    alias = tmp_path / "alias"
    actual = {"source": source.parent, "target": target.parent, "output": tmp_path}[aliased_role]
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(actual)],
        capture_output=True,
        check=False,
    )
    assert linked.returncode == 0
    try:
        paths = {"source": source, "target": target, "output": output}
        paths[aliased_role] = alias / paths[aliased_role].name
        result = CliRunner().invoke(
            cli.cli,
            [
                str(paths["target"]),
                "board",
                "restore",
                "plan",
                str(paths["source"]),
                "--output",
                str(paths["output"]),
            ],
        )
        assert result.exit_code != 0
        assert "Reparse" in result.output
        assert not output.exists()
    finally:
        alias.rmdir()


def test_hardlinked_output_refuses_and_preserves_both_names(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    original = tmp_path / "original"
    original.write_bytes(b"existing bytes")
    output = tmp_path / "review.json"
    os.link(original, output)
    result = CliRunner().invoke(
        cli.cli, [str(target), "board", "restore", "plan", str(source), "--output", str(output)]
    )
    assert result.exit_code != 0
    assert original.read_bytes() == output.read_bytes() == b"existing bytes"


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_port_never_creates_output(
    tmp_path: pathlib.Path, board: board_state.Board, port: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "current", board)
    output = tmp_path / "review.json"
    result = CliRunner().invoke(
        cli.cli,
        [
            str(target),
            "board",
            "restore",
            "plan",
            str(source),
            "--output",
            str(output),
            "--port",
            port,
        ],
    )
    assert result.exit_code != 0
    assert not output.exists()
