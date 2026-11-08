"""Exact board publication stays bounded for large audited attachment inventories."""

import hashlib
import os
import pathlib
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from localswim import api_endpoint, attachments, board_state
from tests.test_api import request_json
from tests.test_store import initialize_autopush_repository, make_store, run_git


def audited_board(path: pathlib.Path, count: int) -> list[pathlib.Path]:
    """Build disposable valid upload/removal history without repeated disk snapshots."""
    path.parent.mkdir()
    make_store(path)
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "bot")
    blob = b"audited attachment\n"
    paths: list[pathlib.Path] = []
    for number in range(count):
        aid = str(uuid.UUID(int=number + 1))
        file = attachments.Attachment(
            aid,
            f"upload {number}.txt",
            len(blob),
            "application/octet-stream",
            hashlib.sha256(blob).hexdigest(),
            board_state.now(),
            "bot",
        )
        saved = attachments.blob_path(path, 1, aid, ".txt")
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(blob)
        board.add_attachment("card", file)
        paths.append(saved)
    # A detached upload remains in the audited snapshot and on disk.
    if count > 1:
        board.remove_attachment("card", str(uuid.UUID(int=1)), "bot")
    assert board.verify() == []
    board_state.save(board, path)
    return paths


def head_paths(repo: pathlib.Path) -> set[str]:
    """Read literal Unicode tree paths independently of the console encoding."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return set(result.stdout.rstrip("\0").split("\0"))


def command_units(arguments: list[str]) -> int:
    """Count Windows process command-line units, including its terminating NUL."""
    return len(subprocess.list2cmdline(["git", *arguments]).encode("utf-16-le")) // 2 + 1


def force_single_path_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise later batches independently of disposable shadow-directory length."""

    def one_path(paths: list[str], snapshot: pathlib.Path | None = None) -> list[list[str]]:
        del snapshot
        return [[path] for path in paths]

    monkeypatch.setattr(api_endpoint, "_status_path_chunks", one_path)


def test_large_audited_snapshot_is_bounded_and_literal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "audit Ω 空間 [set] &^" / "board [01].json"
    blobs = audited_board(path, 512)
    staged = blobs[0].parent / "unrelated.txt"
    staged.write_text("keep staged\n", encoding="utf-8")
    glob_neighbor = path.with_name("board 0.json")
    glob_neighbor.write_text("keep staged too\n", encoding="utf-8")
    run_git(repo, "add", str(staged), str(glob_neighbor))
    (repo / "anchor.txt").write_text("keep unstaged\n", encoding="utf-8")
    (path.parent / "untracked.txt").write_text("keep untracked\n", encoding="utf-8")
    paths = [str(path), *sorted(str(blob) for blob in blobs)]
    old_arguments = ["status", "--porcelain=v1", "--untracked-files=all", "--", *paths]
    assert command_units(old_arguments) > 32_767
    if os.name == "nt":
        with pytest.raises(OSError, match="206") as oversized:
            subprocess.run(["git", *old_arguments], cwd=repo, check=False, timeout=10)
        assert oversized.value.winerror == 206

    original = api_endpoint._git  # noqa: SLF001 -- observe the existing subprocess boundary
    status_batches = 0
    pathspec_files: set[pathlib.Path] = set()
    snapshots: set[pathlib.Path] = set()

    def bounded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        nonlocal status_batches
        assert command_units(arguments) <= 16_000
        if arguments[0].startswith("--work-tree="):
            snapshots.add(pathlib.Path(arguments[0].split("=", 1)[1]))
        command = arguments[1:] if arguments[0].startswith("--work-tree=") else arguments
        if command[0] == "status":
            status_batches += 1
            assert all(value.startswith(":(literal)") for value in command[4:])
        if command[0] in {"add", "commit"}:
            option = next(value for value in command if value.startswith("--pathspec-from-file="))
            pathspec = pathlib.Path(option.split("=", 1)[1])
            pathspec_files.add(pathspec)
            assert "--pathspec-file-nul" in command
            assert pathspec.read_bytes() == b"".join(
                f":(literal){pathlib.Path(value).relative_to(repo).as_posix()}\0".encode()
                for value in paths
            )
            # Windows refuses this rename if the temporary writer still holds the file.
            moved = pathspec.with_name("closed-paths")
            pathspec.rename(moved)
            moved.rename(pathspec)
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", bounded_git)
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert status_batches > 1
    assert len(pathspec_files) == 1
    assert all(not file.parent.exists() for file in pathspec_files)
    assert snapshots and all(not snapshot.exists() for snapshot in snapshots)
    assert head_paths(repo) == {
        "anchor.txt",
        path.relative_to(repo).as_posix(),
        *(blob.relative_to(repo).as_posix() for blob in blobs),
    }
    assert run_git(repo, "diff", "--cached", "--numstat").stdout.count("\n") == 2
    assert run_git(repo, "show", "HEAD:anchor.txt").stdout == "anchor\n"
    assert (repo / "anchor.txt").read_text(encoding="utf-8") == "keep unstaged\n"
    assert api_endpoint.push_board(path) == (True, "repository synchronized")
    assert (
        run_git(repo, "rev-parse", "HEAD").stdout
        == run_git(repo, "rev-parse", "@{upstream}").stdout
    )


def test_later_dirty_status_batch_publishes_audited_blobs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    blobs = audited_board(path, 3)
    # Model recovery from an older board-only snapshot with untracked attachment bytes.
    run_git(repo, "add", str(path))
    run_git(repo, "commit", "-m", "Board metadata before attachment recovery")
    force_single_path_status(monkeypatch)
    original = api_endpoint._git  # noqa: SLF001 -- observe clean and dirty status batches
    dirty_batches: list[bool] = []

    def observed_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        result = original(arguments, cwd)
        command = arguments[1:] if arguments[0].startswith("--work-tree=") else arguments
        if command[0] == "status":
            dirty_batches.append(bool(result.stdout.strip()))
        return result

    monkeypatch.setattr(api_endpoint, "_git", observed_git)
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert dirty_batches[0] is False
    assert any(dirty_batches[1:])
    assert head_paths(repo) == {
        "anchor.txt",
        "board/board.json",
        *(blob.relative_to(repo).as_posix() for blob in blobs),
    }


@pytest.mark.parametrize("phase", ["status", "add", "commit", "push"])
def test_failed_git_phase_cleans_pathspec_and_retries_exact_snapshot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    blobs = audited_board(path, 3)
    unrelated = repo / "unrelated.txt"
    unrelated.write_text("keep staged\n", encoding="utf-8")
    run_git(repo, "add", str(unrelated))
    before = run_git(repo, "rev-parse", "HEAD").stdout
    original = api_endpoint._git  # noqa: SLF001 -- inject one subprocess failure
    failed = False
    status_batches = 0
    pathspec_files: set[pathlib.Path] = set()
    snapshots: set[pathlib.Path] = set()
    # Force several batches so a later failure cannot be hidden by earlier dirty output.
    force_single_path_status(monkeypatch)

    def failing_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        nonlocal failed, status_batches
        if arguments[0].startswith("--work-tree="):
            snapshots.add(pathlib.Path(arguments[0].split("=", 1)[1]))
        command = arguments[1:] if arguments[0].startswith("--work-tree=") else arguments
        if command[0] == "status":
            status_batches += 1
        for value in arguments:
            if value.startswith("--pathspec-from-file="):
                pathspec_files.add(pathlib.Path(value.split("=", 1)[1]))
        if command[0] == phase and not failed and (phase != "status" or status_batches == 2):
            failed = True
            return subprocess.CompletedProcess(arguments, 1, "", "injected failure")
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", failing_git)
    ok, detail = api_endpoint.push_board(path)
    assert not ok
    assert failed
    assert f"{phase} failed: injected failure" in detail
    assert all(not file.parent.exists() for file in pathspec_files)
    assert snapshots and all(not snapshot.exists() for snapshot in snapshots)
    failed_head = run_git(repo, "rev-parse", "HEAD").stdout
    assert (failed_head != before) == (phase == "push")
    if phase == "status":
        assert status_batches == 2
        assert not pathspec_files
        assert run_git(repo, "diff", "--cached", "--name-only").stdout == "unrelated.txt\n"

    assert api_endpoint.push_board(path)[0]
    assert all(not file.parent.exists() for file in pathspec_files)
    assert head_paths(repo) == {
        "anchor.txt",
        "board/board.json",
        *(blob.relative_to(repo).as_posix() for blob in blobs),
    }
    assert run_git(repo, "diff", "--cached", "--name-only").stdout == "unrelated.txt\n"
    if phase == "push":
        assert run_git(repo, "rev-parse", "HEAD").stdout == failed_head
    assert (
        run_git(repo, "rev-parse", "HEAD").stdout
        == run_git(repo, "rev-parse", "@{upstream}").stdout
    )


@pytest.mark.parametrize("missing", [False, True])
def test_invalid_active_bytes_prevent_staging_or_push(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, missing: bool
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    (blob,) = audited_board(path, 1)
    if missing:
        blob.unlink()
    else:
        blob.write_bytes(b"tampered")
    original = api_endpoint._git  # noqa: SLF001 -- observe the existing subprocess boundary
    commands: list[str] = []

    def checked_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        commands.append(arguments[0])
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", checked_git)
    ok, detail = api_endpoint.push_board(path)
    assert not ok
    assert detail.startswith("repository autopush failed:")
    assert commands == ["rev-parse", "rev-parse"]
    assert head_paths(repo) == {"anchor.txt"}


def test_status_chunks_account_for_quotes_and_utf16_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_endpoint, "_GIT_STATUS_ARGV_LIMIT", 400)
    paths = [f':(literal)folder with spaces/🙂{number}/quoted"file\\' for number in range(30)]
    chunks = list(api_endpoint._status_path_chunks(paths))  # noqa: SLF001 -- test argv bounds
    assert len(chunks) > 1
    assert [path for chunk in chunks for path in chunk] == paths
    assert all(
        command_units(["status", "--porcelain=v1", "--untracked-files=all", "--", *chunk]) <= 400
        for chunk in chunks
    )
    with pytest.raises(ValueError, match="command-line budget"):
        list(api_endpoint._status_path_chunks(["🙂" * 400]))  # noqa: SLF001 -- refuse oversized paths


@pytest.mark.parametrize("phase", ["verification", "status", "add", "commit", "push"])
def test_publication_keeps_command_admission_open_and_captures_one_revision(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    captured = path.read_bytes()
    entered = threading.Event()
    resume = threading.Event()
    original_git = api_endpoint._git  # noqa: SLF001 -- pause publication at real I/O boundaries
    original_read = attachments.read_blob
    paused = False

    def pause_once() -> None:
        nonlocal paused
        if not paused:
            paused = True
            entered.set()
            assert resume.wait(10)

    def paused_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        command = arguments[1:] if arguments[0].startswith("--work-tree=") else arguments
        if command[0] == phase:
            pause_once()
        return original_git(arguments, cwd)

    def paused_read(board_path: pathlib.Path, ticket: int, file: attachments.Attachment) -> bytes:
        if phase == "verification":
            pause_once()
        return original_read(board_path, ticket, file)

    monkeypatch.setattr(api_endpoint, "_git", paused_git)
    monkeypatch.setattr(attachments, "read_blob", paused_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(api_endpoint.push_board, path)
        try:
            assert entered.wait(10)
            # Exercise the same gate as HTTP commands without relying on a fast disk.
            assert api_endpoint._command_gate.acquire(timeout=0.25)  # noqa: SLF001
            try:
                newer = board_state.load(path)
                newer.create("newer", "Newer revision", "backlog", "bot")
                newer.remove_attachment("card", str(uuid.UUID(int=1)), "bot")
                board_state.save(newer, path)
            finally:
                api_endpoint._command_gate.release()  # noqa: SLF001
        finally:
            resume.set()
        assert pending.result(timeout=30) == (True, "committed and pushed")
    assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == captured
    assert path.read_bytes() != captured
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == path.read_bytes()


def test_git_commits_verified_attachment_bytes_without_reopening_live_blob(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    (blob,) = audited_board(path, 1)
    verified = blob.read_bytes()
    original = attachments.read_blob

    def tamper_after_read(
        board_path: pathlib.Path, ticket: int, file: attachments.Attachment
    ) -> bytes:
        contents = original(board_path, ticket, file)
        blob.write_bytes(b"external tampering after integrity verification")
        return contents

    monkeypatch.setattr(attachments, "read_blob", tamper_after_read)
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert (
        run_git(repo, "show", f"HEAD:{blob.relative_to(repo).as_posix()}").stdout.encode()
        == verified
    )
    assert blob.read_bytes() != verified


def test_captured_worktree_retains_unstaged_git_attributes_without_staging_them(
    tmp_path: pathlib.Path,
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    attributes = repo / ".gitattributes"
    attributes.write_text("*.json text eol=lf\n", encoding="utf-8")
    expected = path.read_bytes()
    path.write_bytes(expected.replace(b"\n", b"\r\n"))
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == expected
    assert ".gitattributes" not in head_paths(repo)
    assert attributes.read_text(encoding="utf-8") == "*.json text eol=lf\n"


def test_captured_worktree_retains_unstaged_ignore_rules(tmp_path: pathlib.Path) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    (repo / ".gitignore").write_text("board/board.json\n", encoding="utf-8")
    ok, detail = api_endpoint.push_board(path)
    assert not ok
    assert detail.startswith("git add failed:")
    assert head_paths(repo) == {"anchor.txt"}


@pytest.mark.parametrize("hook", ["pre-commit", "post-index-change", "reference-transaction"])
def test_partial_worktree_refuses_active_hooks_before_shadow_git(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, hook: str
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    script = repo / ".git" / "hooks" / hook
    script.write_text("#!/bin/sh\ngit add -A\n", encoding="utf-8")
    script.chmod(0o755)
    before = run_git(repo, "rev-parse", "HEAD").stdout
    index = (repo / ".git" / "index").read_bytes()
    original = api_endpoint._git  # noqa: SLF001 -- no shadow Git may run before hook refusal

    def guarded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        assert not arguments[0].startswith("--work-tree=")
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", guarded_git)
    assert api_endpoint.push_board(path) == (
        False,
        "board snapshot publishing requires no active Git hooks",
    )
    assert run_git(repo, "rev-parse", "HEAD").stdout == before
    assert (repo / ".git" / "index").read_bytes() == index
    assert head_paths(repo) == {"anchor.txt"}


@pytest.mark.parametrize("configured", ["relative-hooks", "empty-hooks"])
def test_partial_worktree_refuses_configured_hook_paths(
    tmp_path: pathlib.Path, configured: str
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    (repo / configured).mkdir()
    run_git(repo, "config", "core.hooksPath", configured)
    assert api_endpoint.push_board(path) == (
        False,
        "board snapshot publishing requires an unset core.hooksPath",
    )
    assert head_paths(repo) == {"anchor.txt"}


def test_partial_worktree_refuses_applied_external_filters_before_shadow_git(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    (repo / ".gitattributes").write_text("*.json filter=fixture\n", encoding="utf-8")
    run_git(repo, "config", "filter.fixture.clean", "git add -A")
    before = run_git(repo, "rev-parse", "HEAD").stdout
    index = (repo / ".git" / "index").read_bytes()
    original = api_endpoint._git  # noqa: SLF001 -- reject before any filter could execute

    def guarded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        assert not arguments[0].startswith("--work-tree=")
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", guarded_git)
    assert api_endpoint.push_board(path) == (
        False,
        "board snapshot publishing does not support Git filter attributes",
    )
    assert run_git(repo, "rev-parse", "HEAD").stdout == before
    assert (repo / ".git" / "index").read_bytes() == index


def test_unused_external_filter_configuration_allows_snapshot(tmp_path: pathlib.Path) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    run_git(repo, "config", "filter.fixture.clean", "git add -A")
    run_git(repo, "config", "core.fsmonitor", "false")
    assert api_endpoint.push_board(path) == (True, "committed and pushed")


@pytest.mark.parametrize("configured", ["true", "../custom-monitor"])
def test_partial_worktree_refuses_enabled_filesystem_monitor(
    tmp_path: pathlib.Path, configured: str
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    run_git(repo, "config", "core.fsmonitor", configured)
    assert api_endpoint.push_board(path) == (
        False,
        "board snapshot publishing requires disabled core.fsmonitor",
    )
    assert head_paths(repo) == {"anchor.txt"}


@pytest.mark.parametrize(("setting", "enabled"), [("fsmonitor", True), ("fsmonitor =", False)])
def test_filesystem_monitor_distinguishes_unvalued_and_explicit_empty_settings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, setting: str, enabled: bool
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as stream:
        stream.write(f"\n[core]\n\t{setting}\n")
    expected_boolean = "true\n" if enabled else "false\n"
    assert (
        run_git(repo, "config", "--type=bool", "--get", "core.fsmonitor").stdout == expected_boolean
    )
    original = api_endpoint._git  # noqa: SLF001 -- enabled monitors cannot reach shadow commands

    def guarded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        if enabled:
            assert not arguments[0].startswith("--work-tree=")
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", guarded_git)
    if enabled:
        assert api_endpoint.push_board(path) == (
            False,
            "board snapshot publishing requires disabled core.fsmonitor",
        )
        assert head_paths(repo) == {"anchor.txt"}
    else:
        assert api_endpoint.push_board(path) == (True, "committed and pushed")


def test_filter_guard_checks_captured_attributes_after_live_rules_change(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    attributes = repo / ".gitattributes"
    attributes.write_text("*.json filter=fixture\n", encoding="utf-8")
    run_git(repo, "config", "filter.fixture.clean", "git add -A")
    original_capture = api_endpoint._capture_board_snapshot  # noqa: SLF001 -- controlled rule race
    original_git = api_endpoint._git  # noqa: SLF001 -- permit only filter inspection in snapshot

    def capture(board_path: pathlib.Path, root: pathlib.Path, target: pathlib.Path) -> list[str]:
        paths = original_capture(board_path, root, target)
        attributes.write_text("*.json -filter\n", encoding="utf-8")
        return paths

    def guarded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        if arguments[0].startswith("--work-tree="):
            assert arguments[1] == "check-attr"
        return original_git(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_capture_board_snapshot", capture)
    monkeypatch.setattr(api_endpoint, "_git", guarded_git)
    assert api_endpoint.push_board(path) == (
        False,
        "board snapshot publishing does not support Git filter attributes",
    )
    assert head_paths(repo) == {"anchor.txt"}


def test_http_post_completes_while_attachment_publication_remains_slow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    audited_board(path, 1)
    captured = path.read_bytes()
    revision = board_state.load(path).revision
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", path)
    monkeypatch.setattr(api_endpoint, "STORE", api_endpoint.BoardStore(path))
    monkeypatch.setattr(api_endpoint, "_shutdown_requested", threading.Event())
    entered = threading.Event()
    resume = threading.Event()
    original = attachments.read_blob

    def slow_read(board_path: pathlib.Path, ticket: int, file: attachments.Attachment) -> bytes:
        entered.set()
        assert resume.wait(15)
        return original(board_path, ticket, file)

    monkeypatch.setattr(attachments, "read_blob", slow_read)
    server = api_endpoint.BoardHttpServer((api_endpoint.HOST, 0), api_endpoint.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api_endpoint.push_board, path)
            try:
                assert entered.wait(10)
                code, response = request_json(
                    f"http://127.0.0.1:{server.server_address[1]}{board_state.API_PREFIX}/cards",
                    token=api_endpoint.CLI_TOKEN,
                    revision=revision,
                    body={"id": "during-publication", "subject": "Accepted", "state": "backlog"},
                )
                assert code == 200
                assert response["revision"] == revision + 1
                # Stay paused beyond the gateway/CLI five-second transport budget.
                assert not resume.wait(5.1)
                assert not pending.done()
            finally:
                resume.set()
            assert pending.result(timeout=30) == (True, "committed and pushed")
        assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == captured
        assert board_state.load(path).find("during-publication").subject == "Accepted"
    finally:
        resume.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
