"""The real commit hook must isolate the gate from Git's repository context."""

import os
import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("gate_exit", [0, 37], ids=["successful-gate", "failed-gate"])
def test_commit_hook_clears_git_context_and_propagates_gate_exit(
    tmp_path: pathlib.Path, gate_exit: int
) -> None:
    git = shutil.which("git")
    assert git is not None
    shell = shutil.which("sh")
    if shell is None:
        shell_path = pathlib.Path(git).resolve().parent.parent / "bin" / "sh.exe"
        assert shell_path.is_file(), "Git's shell is required to exercise the actual hook"
        shell = str(shell_path)

    # Discard inherited context before even initializing this synthetic repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(tmp_path / "global.gitconfig")
    repository = tmp_path / "source"
    subprocess.run(
        [git, "init", "--quiet", str(repository)],
        env=environment,
        check=True,
        capture_output=True,
        timeout=10,
    )
    local_variables = subprocess.run(
        [git, "rev-parse", "--local-env-vars"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.splitlines()
    hook = repository / "pre-commit"
    source_hook = pathlib.Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"
    shutil.copyfile(source_hook, hook)
    source_config = (repository / ".git" / "config").read_bytes()
    source_head = (repository / ".git" / "HEAD").read_bytes()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ "$#" -eq 4 ]\n'
        '[ "$1" = "run" ]\n'
        '[ "$2" = "--frozen" ]\n'
        '[ "$3" = "python" ]\n'
        '[ "$4" = "check.py" ]\n'
        "for variable in $EXPECTED_LOCAL_VARIABLES; do\n"
        '    if printenv "$variable" >/dev/null; then\n'
        '        echo "repository context leaked: $variable" >&2\n'
        "        exit 23\n"
        "    fi\n"
        "done\n"
        'git rev-parse --absolute-git-dir > "$SOURCE_RESULT"\n'
        'git init --quiet "$FOREIGN_REPOSITORY"\n'
        'git -C "$FOREIGN_REPOSITORY" config hook.regression isolated\n'
        'git -C "$FOREIGN_REPOSITORY" rev-parse --absolute-git-dir > "$GATE_RESULT"\n'
        'exit "$FAKE_GATE_EXIT"\n',
        encoding="utf-8",
        newline="\n",
    )
    fake_uv.chmod(0o755)
    foreign = tmp_path / "foreign"
    result = tmp_path / "gate-result.txt"
    source_result = tmp_path / "source-result.txt"
    environment.update(
        PATH=str(fake_bin) + os.pathsep + environment["PATH"],
        GIT_DIR=str(repository / ".git"),
        GIT_COMMON_DIR=str(repository / ".git"),
        GIT_WORK_TREE=str(repository),
        GIT_INDEX_FILE=str(repository / ".git" / "hook-index"),
        GIT_PREFIX="",
        EXPECTED_LOCAL_VARIABLES=" ".join(local_variables),
        FOREIGN_REPOSITORY=foreign.as_posix(),
        GATE_RESULT=result.as_posix(),
        SOURCE_RESULT=source_result.as_posix(),
        FAKE_GATE_EXIT=str(gate_exit),
    )
    completed = subprocess.run(
        [shell, str(hook)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == gate_exit, completed.stderr
    assert (
        pathlib.Path(result.read_text(encoding="utf-8").strip()).resolve()
        == (foreign / ".git").resolve()
    )
    assert "isolated" in (foreign / ".git" / "config").read_text(encoding="utf-8")
    assert (
        pathlib.Path(source_result.read_text(encoding="utf-8").strip()).resolve()
        == (repository / ".git").resolve()
    )
    assert (repository / ".git" / "config").read_bytes() == source_config
    assert (repository / ".git" / "HEAD").read_bytes() == source_head
    assert not (repository / ".git" / "index").exists()
    assert not (repository / ".git" / "hook-index").exists()
