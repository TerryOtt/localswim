"""Exact board publication stays bounded for large audited attachment inventories."""

import hashlib
import os
import pathlib
import subprocess
import uuid

import pytest

from localswim import api_endpoint, attachments, board_state
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


def single_path_status_budget(paths: list[pathlib.Path]) -> int:
    """Force distinct status batches independently of the disposable directory length."""
    return max(
        command_units(
            ["status", "--porcelain=v1", "--untracked-files=all", "--", f":(literal){path}"]
        )
        for path in paths
    )


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

    def bounded_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        nonlocal status_batches
        assert command_units(arguments) <= 16_000
        if arguments[0] == "status":
            status_batches += 1
            assert all(value.startswith(":(literal)") for value in arguments[4:])
        if arguments[0] in {"add", "commit"}:
            option = next(value for value in arguments if value.startswith("--pathspec-from-file="))
            pathspec = pathlib.Path(option.split("=", 1)[1])
            pathspec_files.add(pathspec)
            assert "--pathspec-file-nul" in arguments
            assert pathspec.read_bytes() == b"".join(
                f":(literal){value}\0".encode() for value in paths
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
    monkeypatch.setattr(
        api_endpoint, "_GIT_STATUS_ARGV_LIMIT", single_path_status_budget([path, *blobs])
    )
    original = api_endpoint._git  # noqa: SLF001 -- observe clean and dirty status batches
    dirty_batches: list[bool] = []

    def observed_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        result = original(arguments, cwd)
        if arguments[0] == "status":
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
    # Force several batches so a later failure cannot be hidden by earlier dirty output.
    monkeypatch.setattr(
        api_endpoint, "_GIT_STATUS_ARGV_LIMIT", single_path_status_budget([path, *blobs])
    )

    def failing_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        nonlocal failed, status_batches
        if arguments[0] == "status":
            status_batches += 1
        for value in arguments:
            if value.startswith("--pathspec-from-file="):
                pathspec_files.add(pathlib.Path(value.split("=", 1)[1]))
        if arguments[0] == phase and not failed and (phase != "status" or status_batches == 2):
            failed = True
            return subprocess.CompletedProcess(arguments, 1, "", "injected failure")
        return original(arguments, cwd)

    monkeypatch.setattr(api_endpoint, "_git", failing_git)
    ok, detail = api_endpoint.push_board(path)
    assert not ok
    assert failed
    assert f"{phase} failed: injected failure" in detail
    assert all(not file.parent.exists() for file in pathspec_files)
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
    assert commands == ["rev-parse"]
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
