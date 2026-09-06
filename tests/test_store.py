"""Transactional BoardStore behavior."""

import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from localswim import api_endpoint, board_state
from tests.support import USERS


def run_git(cwd: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one isolated local Git command for autopush integration tests."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def initialize_autopush_repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Create one working repository with a local bare upstream."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "state-store"
    run_git(tmp_path, "init", "--bare", "--quiet", str(remote))
    run_git(tmp_path, "init", "--initial-branch=main", "--quiet", str(repo))
    run_git(repo, "config", "user.name", "localswim test")
    run_git(repo, "config", "user.email", "localswim@example.invalid")
    (repo / "anchor.txt").write_text("anchor\n", encoding="utf-8")
    run_git(repo, "add", "anchor.txt")
    run_git(repo, "commit", "--quiet", "-m", "Anchor")
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "push", "--quiet", "--set-upstream", "origin", "main")
    return remote, repo


def wait_for_file(path: pathlib.Path) -> None:
    """Wait briefly for one child-process handoff file."""
    deadline = time.monotonic() + 5.0
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        time.sleep(0.01)


def make_store(path: pathlib.Path) -> api_endpoint.BoardStore:
    """Persist an empty board and return its store."""
    board = board_state.Board(
        project="Store",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board_state.save(board, path)
    return api_endpoint.BoardStore(path)


def create_alpha(board: board_state.Board) -> str:
    """Representative store command."""
    return board.create("alpha", "Alpha", "backlog", "bot")


def test_success_increments_revision_after_save(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    result, revision = store.execute(0, create_alpha)

    saved = board_state.load(path)
    assert "created" in result
    assert revision == 1
    assert saved.revision == 1
    assert saved.find("alpha").subject == "Alpha"


def test_stale_revision_changes_nothing(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, create_alpha)

    with pytest.raises(api_endpoint.RevisionConflict, match="revision is 1"):
        store.execute(0, lambda board: board.comment("alpha", "stale", "bot"))
    saved = board_state.load(path)
    assert saved.revision == 1
    assert saved.find("alpha").comments == []


def test_domain_refusal_changes_nothing(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)

    with pytest.raises(board_state.BoardError, match="may not create"):
        store.execute(0, lambda board: board.create("alpha", "Alpha", "completed", "bot"))
    saved = board_state.load(path)
    assert saved.revision == 0
    assert saved.items == []


def test_save_failure_does_not_publish_candidate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        store.execute(0, create_alpha)

    monkeypatch.undo()
    assert store.snapshot().revision == 0
    assert store.snapshot().items == []


def test_same_revision_concurrency_has_one_winner(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)

    def command(name: str) -> tuple[str, int]:
        return store.execute(0, lambda board: board.create(name, name.title(), "backlog", "bot"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(command, name) for name in ("alpha", "beta")]

    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], api_endpoint.RevisionConflict)
    saved = board_state.load(path)
    assert saved.revision == 1
    assert len(saved.items) == 1


def test_source_checkout_requires_ignored_board_directory() -> None:
    root = pathlib.Path(api_endpoint.__file__).resolve().parents[2]
    assert "refusing board data" in api_endpoint.source_checkout_board_problem(
        root / "sensitive-board.json"
    )
    assert "refusing board data" in api_endpoint.source_checkout_board_problem(
        root / ".venv" / "private.json"
    )
    assert api_endpoint.source_checkout_board_problem(root / "boards" / "private.json") == ""


def test_source_checkout_refuses_boards_directory_when_ignore_rule_is_ineffective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = pathlib.Path(api_endpoint.__file__).resolve().parents[2]

    def fake_git(args: list[str], _cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, str(root), "")
        if args[:2] == ["check-ignore", "--quiet"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(api_endpoint, "_git", fake_git)
    problem = api_endpoint.source_checkout_board_problem(root / "boards" / "private.json")
    assert "git does not ignore" in problem


def test_board_outside_source_checkout_remains_supported(tmp_path: pathlib.Path) -> None:
    assert api_endpoint.source_checkout_board_problem(tmp_path / "board.json") == ""


def test_ignored_board_disables_autopush(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/boards/\n", encoding="utf-8")
    board = tmp_path / "boards" / "private.json"
    board.parent.mkdir()
    assert api_endpoint.push_unavailable(board) == ("the board is ignored by git")


def test_autopush_adopts_an_untracked_board_without_staging_other_files(
    tmp_path: pathlib.Path,
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)

    board_path = repo / "localswim" / "board.json"
    board_path.parent.mkdir()
    board_state.save(
        board_state.Board(
            project="Untracked",
            users=USERS,
            browser_user="terry",
            cli_user="bot",
            default_owner="bot",
        ),
        board_path,
    )
    (repo / "unrelated.txt").write_text("must remain untracked\n", encoding="utf-8")

    assert api_endpoint.push_unavailable(board_path) == ""
    ok, detail = api_endpoint.push_board(board_path)

    assert ok is True
    assert detail == "committed and pushed"
    assert run_git(repo, "status", "--short").stdout.splitlines() == ["?? unrelated.txt"]
    first_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "rev-parse", "@{upstream}").stdout.strip() == first_head

    board = board_state.load(board_path)
    board.create("alpha", "Alpha", "backlog", "bot")
    board_state.save(board, board_path)
    ok, detail = api_endpoint.push_board(board_path)

    assert ok is True
    assert detail == "committed and pushed"
    second_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert second_head != first_head
    assert run_git(repo, "rev-parse", "@{upstream}").stdout.strip() == second_head
    assert run_git(repo, "status", "--short").stdout.splitlines() == ["?? unrelated.txt"]


def test_concurrent_board_pushes_serialize_for_one_repository(tmp_path: pathlib.Path) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    paths = [repo / name / "board.json" for name in ("alpha", "beta")]
    for path in paths:
        path.parent.mkdir()
        board_state.save(
            board_state.Board(
                project=path.parent.name.title(),
                users=USERS,
                browser_user="terry",
                cli_user="bot",
                default_owner="bot",
            ),
            path,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(api_endpoint.push_board, paths))

    assert results == [(True, "committed and pushed"), (True, "committed and pushed")]
    assert (
        run_git(repo, "rev-parse", "HEAD").stdout
        == run_git(repo, "rev-parse", "@{upstream}").stdout
    )
    assert run_git(repo, "status", "--short").stdout == ""
    tracked = run_git(repo, "ls-files", "*/board.json").stdout.splitlines()
    assert tracked == ["alpha/board.json", "beta/board.json"]


def test_repository_push_lock_serializes_separate_processes(tmp_path: pathlib.Path) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    first_ready = tmp_path / "first-ready"
    release_first = tmp_path / "release-first"
    second_ready = tmp_path / "second-ready"
    first_code = """
import pathlib
import sys
import time
from localswim import api_endpoint

repo = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
release = pathlib.Path(sys.argv[3])
with api_endpoint._repository_push_locked(repo):
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""
    second_code = """
import pathlib
import sys
from localswim import api_endpoint

repo = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
with api_endpoint._repository_push_locked(repo):
    ready.write_text("ready", encoding="utf-8")
"""
    first = subprocess.Popen(
        [sys.executable, "-c", first_code, str(repo), str(first_ready), str(release_first)],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_file(first_ready)
        second = subprocess.Popen(
            [sys.executable, "-c", second_code, str(repo), str(second_ready)],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.2)
            assert not second_ready.exists()
            release_first.write_text("release", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)
        finally:
            release_first.write_text("release", encoding="utf-8")
            if second.poll() is None:
                second.kill()
                second.communicate(timeout=5)
    finally:
        release_first.write_text("release", encoding="utf-8")
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)

    assert (first.returncode, first_stdout, first_stderr) == (0, "", "")
    assert (second.returncode, second_stdout, second_stderr) == (0, "", "")
    assert second_ready.read_text(encoding="utf-8") == "ready"


def test_clean_board_reconciles_an_already_committed_push_failure(tmp_path: pathlib.Path) -> None:
    remote, repo = initialize_autopush_repository(tmp_path)
    board_path = repo / "project" / "board.json"
    board_path.parent.mkdir()
    board_state.save(
        board_state.Board(
            project="Recovery",
            users=USERS,
            browser_user="terry",
            cli_user="bot",
            default_owner="bot",
        ),
        board_path,
    )
    run_git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    ok, detail = api_endpoint.push_board(board_path)

    assert ok is False
    assert detail.startswith("committed, but push failed:")
    assert run_git(repo, "status", "--short", "--", str(board_path)).stdout == ""
    run_git(repo, "remote", "set-url", "origin", str(remote))

    ok, detail = api_endpoint.push_board(board_path)

    assert ok is True
    assert detail == "repository synchronized"
    assert (
        run_git(repo, "rev-parse", "HEAD").stdout
        == run_git(repo, "rev-parse", "@{upstream}").stdout
    )


def test_autopush_loop_retries_failure_without_another_board_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopLoopError(Exception):
        """Terminate the deliberately infinite daemon loop after recovery."""

    board = tmp_path / "board.json"
    board.write_text("{}\n", encoding="utf-8")
    clock = 0.0
    outcomes = iter([(False, "push failed"), (True, "repository synchronized")])
    calls: list[pathlib.Path] = []

    def fake_time() -> float:
        return clock

    def fake_sleep(_seconds: float) -> None:
        nonlocal clock
        clock += 1.0
        if clock > 3.0:
            raise StopLoopError

    def fake_push(path: pathlib.Path) -> tuple[bool, str]:
        calls.append(path)
        return next(outcomes)

    def push_is_available(_path: pathlib.Path) -> str:
        return ""

    monkeypatch.setattr(api_endpoint, "push_unavailable", push_is_available)
    monkeypatch.setattr(api_endpoint, "_PUSH_QUIET_S", 0.0)
    monkeypatch.setattr(api_endpoint, "_PUSH_RETRY_S", 2.0)
    monkeypatch.setattr(api_endpoint.time, "time", fake_time)
    monkeypatch.setattr(api_endpoint.time, "sleep", fake_sleep)
    monkeypatch.setattr(api_endpoint, "_push_once", fake_push)

    with pytest.raises(StopLoopError):
        api_endpoint._push_loop(board)  # noqa: SLF001 -- exercise daemon retry scheduling

    assert calls == [board, board]


def test_autopush_is_disabled_unless_explicitly_enabled(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_thread(**_kwargs: object) -> None:
        raise AssertionError("disabled autopush must not create a thread")

    monkeypatch.setattr(api_endpoint.threading, "Thread", unexpected_thread)

    worker = api_endpoint.start_autopush(tmp_path / "board.json", enabled=False)

    assert worker is None
    assert api_endpoint.push_status()["state"] == "off"
    assert "--autopush" in str(api_endpoint.push_status()["detail"])


def test_shutdown_flush_pushes_final_snapshot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = tmp_path / "board.json"
    calls: list[pathlib.Path] = []

    def successful_push(path: pathlib.Path) -> tuple[bool, str]:
        calls.append(path)
        return True, "committed and pushed"

    monkeypatch.setattr(api_endpoint, "_autopush_enabled", True)
    monkeypatch.setattr(api_endpoint, "push_board", successful_push)

    result = api_endpoint.flush_autopush_for_shutdown(board)

    assert result == (True, "committed and pushed")
    assert calls == [board]
    assert api_endpoint.push_status()["state"] == "ok"


def test_enabled_autopush_starts_one_daemon_for_the_board(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = tmp_path / "board.json"
    captured: dict[str, object] = {}

    class FakeWorker:
        def start(self) -> None:
            captured["started"] = True

    def fake_thread(**kwargs: object) -> FakeWorker:
        captured.update(kwargs)
        worker = FakeWorker()
        captured["worker"] = worker
        return worker

    monkeypatch.setattr(api_endpoint.threading, "Thread", fake_thread)

    worker = api_endpoint.start_autopush(board, enabled=True)

    assert worker is captured["worker"]
    assert captured["args"] == (board,)
    assert captured["name"] == "autopush"
    assert captured["daemon"] is True
    assert captured["started"] is True


def test_autopush_flag_and_default_are_parsed() -> None:
    default = api_endpoint.parse_args(["board.json"])
    enabled = api_endpoint.parse_args(["--autopush", "board.json"])

    assert default.autopush is False
    assert enabled.autopush is True
