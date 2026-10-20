"""Transactional BoardStore behavior."""

import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import cast
lazy from collections.abc import Generator

import pytest

from localswim import api_endpoint, attachments, board_state
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


def corrupt_admission_audit(board: board_state.Board, defect: str) -> None:
    """Use the archive/attachment audit defects that structural parsing tolerates."""
    if defect == "archive":
        board.find("alpha").archived = True
        return
    file = attachments.Attachment(
        str(uuid.uuid4()), "image.png", 1, "image/png", "0" * 64, board_state.now(), "bot"
    )
    board.add_attachment("alpha", file)
    if defect == "membership":
        board.find("alpha").attachment_history.clear()
    else:
        board.find("alpha").cover_attachment = file.id


@pytest.mark.parametrize("defect", ["archive", "membership", "cover"])
@pytest.mark.parametrize("entry", ["startup", "snapshot", "reload"])
@pytest.mark.parametrize("floor", [False, True])
def test_admission_rejects_audit_defect_without_repair_or_publication(
    tmp_path: pathlib.Path, defect: str, entry: str, *, floor: bool
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, create_alpha)
    accepted = store.snapshot()
    candidate = store.snapshot()
    accepted_policy = store.policy
    # Also prove that the policy of a rejected board never becomes accepted.
    policy_doc = candidate.policy.to_json()
    policy_doc["defaultPriority"] = "P4"
    candidate.policy = board_state.TransitionPolicy.from_json(policy_doc, "candidate")
    if floor:
        candidate.create("urgent", "Urgent", "backlog", "bot", priority="P5")
        candidate.find("alpha").priority = "P0"
        candidate.links.append(board_state.Link("alpha", "blocks", "urgent"))
    corrupt_admission_audit(candidate, defect)
    board_state.save(candidate, path)
    before = path.read_bytes()
    assert board_state.load(path).verify()  # Ordinary offline parsing stays available.

    if entry == "startup":
        with pytest.raises(board_state.BoardError, match="disk snapshot rejected"):
            api_endpoint.BoardStore(path)
    elif entry == "snapshot":
        with pytest.raises(board_state.BoardError, match="retaining last accepted"):
            store.snapshot()
    else:
        assert "disk snapshot rejected" in str(store.reload_policy_if_changed())
    assert path.read_bytes() == before
    assert store.policy is accepted_policy
    assert store._board.to_json() == accepted.to_json()  # noqa: SLF001 -- publication boundary

    # A retained snapshot is not a successful read; a later valid disk edit recovers.
    with pytest.raises(board_state.BoardError, match="retaining last accepted"):
        store.snapshot()
    board_state.save(accepted, path)
    assert store.snapshot().to_json() == accepted.to_json()
    assert store.admission_error is None


def test_admission_accepts_embedded_policy_and_legacy_history_together(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot")
    candidate.find("alpha").history.clear()
    # Non-lane history without a creation trail is an existing supported legacy case.
    candidate.set_priority("alpha", "P4", "bot")
    policy_doc = candidate.policy.to_json()
    policy_doc["defaultPriority"] = "P4"
    candidate.policy = board_state.TransitionPolicy.from_json(policy_doc, "candidate")
    board_state.save(candidate, path)
    assert store.reload_policy_if_changed() == "embedded policy reloaded: 7 lanes"
    assert store.snapshot().to_json() == candidate.to_json()
    assert store.policy.default_priority == "P4"
    assert api_endpoint.BoardStore(path).snapshot().verify() == []


@pytest.mark.parametrize("same_revision", [False, True])
def test_admission_rechecks_changed_disk_inside_reconciliation_transaction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, same_revision: bool
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot", priority="P0")
    candidate.create("urgent", "Urgent", "backlog", "bot", priority="P5")
    candidate.links.append(board_state.Link("alpha", "blocks", "urgent"))
    board_state.save(candidate, path)
    original_edit = board_state.edit
    injected = False

    @contextlib.contextmanager
    def racing_edit(
        edit_path: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> Generator[board_state.Board]:
        nonlocal injected
        if not injected:
            injected = True
            replacement = board_state.load(path)
            if same_revision:
                replacement.find("alpha").archived = True
            else:
                replacement.project = "Concurrent valid change"
                replacement.revision += 1
            board_state.save(replacement, path)
        with original_edit(edit_path, policy, install_context=install_context) as board:
            yield board

    monkeypatch.setattr(board_state, "edit", racing_edit)
    if same_revision:
        with pytest.raises(board_state.BoardError, match="disk snapshot rejected"):
            store.snapshot()
        assert board_state.load(path).revision == 0
        assert board_state.load(path).find("urgent").priority == "P5"
    else:
        result = store.snapshot()
        assert result.project == "Concurrent valid change"
        assert result.revision == 2
        assert result.find("urgent").priority == "P0"
        assert result.find("urgent").history[-1].by == "bot"
        assert result.verify() == []
        assert store.snapshot().revision == 2


def test_rejected_admission_does_not_install_candidate_actor_context(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot")
    candidate.users = (*candidate.users, board_state.User("outsider", "Outsider", "human", "blue"))
    candidate.find("alpha").archived = True
    board_state.save(candidate, path)
    before = board_state.ACTORS
    with pytest.raises(board_state.BoardError):
        store.snapshot()
    assert before == board_state.ACTORS


@pytest.mark.parametrize("entry", ["startup", "snapshot", "reload"])
def test_admission_promotion_save_failure_preserves_disk_and_accepted_state(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    accepted = store.snapshot()
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot", priority="P0")
    candidate.create("urgent", "Urgent", "backlog", "bot", priority="P5")
    candidate.links.append(board_state.Link("alpha", "blocks", "urgent"))
    board_state.save(candidate, path)
    before = path.read_bytes()

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected promotion save failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    if entry == "reload":
        assert "injected promotion save failure" in str(store.reload_policy_if_changed())
    elif entry == "startup":
        with pytest.raises(board_state.BoardError, match="injected promotion save failure"):
            api_endpoint.BoardStore(path)
    else:
        with pytest.raises(board_state.BoardError, match="injected promotion save failure"):
            store.snapshot()
    assert path.read_bytes() == before
    assert store._board.to_json() == accepted.to_json()  # noqa: SLF001 -- publication boundary


@pytest.mark.parametrize("entry", ["startup", "snapshot", "reload"])
def test_admission_promotes_once_and_accepts_valid_legacy_history(
    tmp_path: pathlib.Path, entry: str
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot", priority="P0")
    candidate.create("urgent", "Urgent", "backlog", "bot", priority="P5")
    candidate.links.append(board_state.Link("alpha", "blocks", "urgent"))
    candidate.find("alpha").history.clear()
    candidate.find("urgent").history.clear()
    board_state.save(candidate, path)
    if entry == "startup":
        store = api_endpoint.BoardStore(path)
    elif entry == "reload":
        assert store.reload_policy_if_changed() is None
    result = store.snapshot()
    assert result.revision == 1
    assert result.find("alpha").history == []
    assert len(result.find("urgent").history) == 1
    assert result.find("urgent").history[-1].kind == "priority"
    assert result.find("urgent").priority == "P0"
    assert result.verify() == []
    before = path.read_bytes()
    assert store.snapshot().revision == 1
    assert path.read_bytes() == before


def recast_candidate(raw: board_state.JsonValue) -> board_state.JsonValue:
    """Change actor identities while preserving actor classes in an isolated fixture."""
    names = {"terry": "newhuman", "bot": "newbot"}
    if isinstance(raw, str):
        return names.get(raw, raw)
    if isinstance(raw, list):
        return [recast_candidate(value) for value in raw]
    if isinstance(raw, dict):
        return {
            names.get(key, key): value if key == "class" else recast_candidate(value)
            for key, value in raw.items()
        }
    return raw


def test_accepted_cast_drives_http_credentials_owner_and_css_without_ambient_install(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    candidate = board_state.Board.from_json(
        recast_candidate(store.snapshot().to_json()), "recast", install_context=False
    )
    board_state.save(candidate, path)
    accepted = store.snapshot()
    # An unrelated store/offline context must not control this service's credentials.
    other = make_store(tmp_path / "other.json")
    board_state.load(other.path)
    ambient = board_state.ACTORS
    monkeypatch.setattr(api_endpoint, "STORE", store)
    handler = cast(
        "api_endpoint.Handler",
        SimpleNamespace(headers={"Authorization": f"Bearer {api_endpoint.CLI_TOKEN}"}),
    )
    actor = api_endpoint.Handler._actor(handler)  # noqa: SLF001 -- credential boundary
    assert actor == "newbot"
    assert other.credential_actor(browser=False) == "bot"
    assert accepted.browser_user == "newhuman"
    assert "--user-newhuman:" in api_endpoint.user_css(accepted)
    assert "--accent: var(--user-newhuman)" in api_endpoint.user_css(accepted)
    assert actor is not None
    api_endpoint.execute_http_command(
        store, 0, ["cards"], {"id": "alpha", "subject": "Alpha", "state": "backlog"}, actor
    )
    assert store.snapshot().find("alpha").owner == "newbot"
    api_endpoint.execute_http_command(
        store, 1, ["cards", "alpha", "assign"], {"owner": " Newhuman "}, actor
    )
    assert store.snapshot().find("alpha").owner == "newhuman"
    assert ambient == board_state.ACTORS


def test_supplied_actor_cast_refusal_lists_its_own_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = (
        board_state.User("newhuman", "New human", "human", "blue"),
        board_state.User("newbot", "New bot", "bot", "green"),
    )
    monkeypatch.setattr(board_state, "USERS", USERS)
    monkeypatch.setattr(board_state, "ACTORS", ("terry", "bot"))
    assert board_state.as_actor(" Newhuman ", users) == "newhuman"
    with pytest.raises(board_state.BoardError, match=r"want one of newhuman, newbot$"):
        board_state.as_actor("terry", users)
    with pytest.raises(board_state.BoardError, match=r"want one of terry, bot$"):
        board_state.as_actor("newhuman")


def test_startup_admission_refusal_never_starts_server_or_structural_http_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, create_alpha)
    candidate = store.snapshot()
    candidate.find("alpha").archived = True
    board_state.save(candidate, path)
    monkeypatch.setattr(api_endpoint, "STORE", store)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", path)

    def allow_synthetic_board(_path: pathlib.Path) -> str:
        return ""

    monkeypatch.setattr(api_endpoint, "source_checkout_board_problem", allow_synthetic_board)

    def unexpected_server(*_args: object) -> None:
        raise AssertionError("invalid startup must not start serving")

    monkeypatch.setattr(api_endpoint, "_run_board_server", unexpected_server)
    args = argparse.Namespace(board=path, port=0)
    lease = cast("api_endpoint.service_ownership.WriterLease", None)
    with pytest.raises(SystemExit, match="board admission failed"):
        api_endpoint._serve_owned_board(args, lease)  # noqa: SLF001 -- startup admission boundary
    assert api_endpoint.STORE is None
    assert "error" in json.loads(api_endpoint.payload())

    def empty_source_status(_server: object) -> board_state.JsonObject:
        return {}

    monkeypatch.setattr(api_endpoint, "source_status", empty_source_status)
    monkeypatch.setattr(api_endpoint, "push_status", dict)
    monkeypatch.setattr(board_state, "reload_rules_if_changed", lambda: None)
    statuses: list[dict[str, object]] = []
    handler = cast("api_endpoint.Handler", SimpleNamespace(server=None, _json=statuses.append))
    api_endpoint.Handler._status_get(handler)  # noqa: SLF001 -- truthful health boundary
    assert statuses[-1]["ok"] is False


def test_root_page_refuses_candidate_title_cast_and_context(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, create_alpha)
    candidate = board_state.Board.from_json(
        recast_candidate(store.snapshot().to_json()), "recast", install_context=False
    )
    candidate.project = "Rejected secret project"
    candidate.find("alpha").archived = True
    board_state.save(candidate, path)
    monkeypatch.setattr(api_endpoint, "STORE", store)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", path)
    before = board_state.ACTORS
    pages: list[bytes] = []

    def capture_page(body: bytes, _content_type: str) -> None:
        pages.append(body)

    handler = cast("api_endpoint.Handler", SimpleNamespace(path="/", _send=capture_page))
    api_endpoint.Handler.do_GET(handler)
    assert before == board_state.ACTORS
    assert b"Rejected secret project" not in pages[0]
    assert b"--user-newhuman:" not in pages[0]
    assert b"Work board" in pages[0]
    assert store.admission_error is not None


def test_bad_board_reports_external_policy_refusal_once_and_recovers(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    make_store(path)
    rules = tmp_path / "rules.json"
    rules.write_bytes(board_state.RULES_PATH.read_bytes())
    store = api_endpoint.BoardStore(path, board_state.TransitionPolicy.load(rules))
    accepted = store.snapshot()
    candidate = store.snapshot()
    candidate.create("alpha", "Alpha", "backlog", "bot")
    candidate.find("alpha").archived = True
    board_state.save(candidate, path)
    stamp = rules.stat().st_mtime_ns + 1_000_000_000
    rules.write_text('{"schema": -1}\n', encoding="utf-8")
    os.utime(rules, ns=(stamp, stamp))
    first = store.reload_policy_if_changed()
    assert "REFUSED" in str(first)
    assert "disk admission failed" in str(first)
    assert "REFUSED" not in str(store.reload_policy_if_changed())
    board_state.save(accepted, path)
    assert store.reload_policy_if_changed() is None
    assert store.admission_error is None
    assert store.snapshot().to_json() == accepted.to_json()


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
    accepted = store._board  # noqa: SLF001 -- observe cache before snapshot refresh
    accepted_policy = store.policy
    persisted = path.read_bytes()

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        store.execute(0, create_alpha)

    assert store._board is accepted, "failed save published cache"  # noqa: SLF001 -- before refresh
    assert store.policy is accepted_policy
    assert accepted.revision == 0
    assert accepted.items == []
    assert path.read_bytes() == persisted
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
    second_denied = tmp_path / "second-denied"
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
denied = pathlib.Path(sys.argv[3])
original_try_lock = api_endpoint.try_process_lock

def observed_try_lock(fd):
    acquired = original_try_lock(fd)
    if not acquired:
        denied.write_text("denied", encoding="utf-8")
    return acquired

api_endpoint.try_process_lock = observed_try_lock
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
            [sys.executable, "-c", second_code, str(repo), str(second_ready), str(second_denied)],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_file(second_denied)
            assert second_denied.read_text(encoding="utf-8") == "denied"
            assert first.poll() is None
            assert not release_first.exists()
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
    calls: list[tuple[pathlib.Path, float]] = []
    sleeps: list[float] = []
    original_bytes = board.read_bytes()
    original_mtime = board.stat().st_mtime_ns

    def fake_time() -> float:
        return clock

    def fake_sleep(_seconds: float) -> None:
        nonlocal clock
        clock += 0.5
        sleeps.append(clock)
        if clock > 5.0:
            raise StopLoopError

    def fake_push(path: pathlib.Path) -> tuple[bool, str]:
        calls.append((path, clock))
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

    assert calls == [(board, 0.5), (board, 2.5)], "retry deadline or recovery quiescence changed"
    assert [instant for instant in sleeps if 0.5 < instant < 2.5] == [1.0, 1.5, 2.0]
    assert sleeps[-1] == 5.5
    assert board.read_bytes() == original_bytes
    assert board.stat().st_mtime_ns == original_mtime


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
