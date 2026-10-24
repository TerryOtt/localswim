"""Repository gate behavior."""

import subprocess
import sys
lazy import pathlib

import pytest

import check

STAGES = (
    ("run_line_endings", "line-endings"),
    ("run_ruff", "ruff"),
    ("run_format", "ruff-format"),
    ("run_pyright", "pyright"),
    ("run_tests", "pytest"),
    ("run_workflows", "actionlint/shellcheck"),
)


def test_non_lf_tracked_finds_only_text_with_crlf_or_mixed_endings() -> None:
    report = (
        "i/lf    w/lf    attr/text eol=lf\tclean.py\n"
        "i/lf    w/crlf  attr/text eol=lf\twindows.py\n"
        "i/lf    w/mixed attr/text eol=lf\tmixed.json\n"
        "i/-text w/-text attr/text eol=lf\tfont.woff2\n"
    )

    assert check.non_lf_tracked(report) == ["windows.py", "mixed.json"]


def _stub_stages(monkeypatch: pytest.MonkeyPatch, failures: tuple[str, ...]) -> list[str]:
    visited: list[str] = []

    def stage(name: str) -> bool:
        visited.append(name)
        return name not in failures

    for name, _label in STAGES:
        monkeypatch.setattr(check, name, lambda name=name: stage(name))
    monkeypatch.setattr(sys, "argv", ["check.py"])
    return visited


@pytest.mark.parametrize("table_env", [None, "missing-table.py"])
def test_gate_needs_no_external_repository_or_environment(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    table_env: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(check, "ROOT", tmp_path)
    if table_env is None:
        monkeypatch.delenv("CLAUDE_WORD_TABLE", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_WORD_TABLE", str(tmp_path / table_env))
    visited = _stub_stages(monkeypatch, ())

    assert check.main() == 0
    assert visited == [name for name, _label in STAGES]
    assert "Gate clean." in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "failures",
    [*((name,) for name, _label in STAGES), tuple(name for name, _label in STAGES)],
    ids=[*(label for _name, label in STAGES), "all-fail"],
)
def test_gate_reports_failures_and_runs_every_stage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failures: tuple[str, ...],
) -> None:
    visited = _stub_stages(monkeypatch, failures)

    assert check.main() == 1
    assert visited == [name for name, _label in STAGES]
    expected = ", ".join(label for name, label in STAGES if name in failures)
    assert f"GATE FAILED: {expected}" in capsys.readouterr().out


@pytest.mark.parametrize("option", ["--word-table", "--unknown"])
def test_gate_rejects_unsupported_options_before_running_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    visited = _stub_stages(monkeypatch, ())
    monkeypatch.setattr(sys, "argv", ["check.py", option])

    with pytest.raises(SystemExit) as error:
        check.main()

    assert error.value.code == 2
    assert visited == []
    assert f"unrecognized arguments: {option}" in capsys.readouterr().err


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def test_line_endings_checks_tracked_working_tree_text(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "core.quotePath", "false")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    tracked = tmp_path / "café prose.md"
    tracked.write_bytes(b"clean\ninput\n")
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"\x00\r\n")
    _git(tmp_path, "add", "--", ".gitattributes", tracked.name, binary.name)
    monkeypatch.setattr(check, "ROOT", tmp_path)

    assert check.run_line_endings()
    tracked.write_bytes(b"changed\r\ninput\r\n")
    (tmp_path / "untracked.txt").write_bytes(b"scratch\r\n")

    assert not check.run_line_endings()
    output = capsys.readouterr().out
    assert f"FAIL  {tracked.name}: tracked text must use LF" in output
    assert "binary.dat: tracked text must use LF" not in output
    assert "untracked.txt" not in output


def test_line_endings_refuses_failed_git_inventory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check, "ROOT", tmp_path)

    assert not check.run_line_endings()
    assert "FAIL -- git ls-files --eol did not run" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["actionlint", "shellcheck", "both"])
def test_missing_workflow_tools_fail_the_gate_without_skipping_other_stages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    run_workflows = check.run_workflows
    visited = _stub_stages(monkeypatch, ())
    monkeypatch.setattr(check, "run_workflows", run_workflows)

    def which(name: str) -> str | None:
        return None if missing in {name, "both"} else name

    monkeypatch.setattr(check.shutil, "which", which)

    assert check.main() == 1
    assert visited == [name for name, _label in STAGES[:-1]]
    output = capsys.readouterr().out
    assert "FAIL -- missing required tool(s):" in output
    assert "GATE FAILED: actionlint/shellcheck" in output


@pytest.mark.parametrize("runner", ["run_ruff", "run_format", "run_pyright", "run_tests"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_python_checks_propagate_tool_exit_status(
    monkeypatch: pytest.MonkeyPatch, runner: str, returncode: int
) -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode)

    monkeypatch.setattr(check.subprocess, "run", run)

    assert getattr(check, runner)() is (returncode == 0)
