"""Gateway publication snapshots exclude every unrelated working-tree/index path."""

import json
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

from localswim import api_endpoint, gateway_publish
from localswim.gateway_publish import GatewayPublisher
from localswim.workspace_config import CONFIG_FILENAME, GatewayConfigStore


def git(root: pathlib.Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: pathlib.Path) -> pathlib.Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "readme.txt").write_text("initial", encoding="utf-8")
    git(root, "add", "readme.txt")
    git(root, "commit", "-m", "Initial")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    return root


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("publisher did not reach the expected state")
        time.sleep(0.01)


def test_publish_only_gateway_and_preserve_unrelated_index(repository: pathlib.Path) -> None:
    store = GatewayConfigStore(repository, autopush=True)
    unrelated = repository / "private.txt"
    unrelated.write_text("staged", encoding="utf-8")
    git(repository, "add", "private.txt")
    unrelated.write_text("unstaged", encoding="utf-8")
    before = git(repository, "show", ":private.txt")
    publisher = GatewayPublisher(store)
    assert publisher.status()["state"] == "off"
    assert publisher.flush()[0]
    assert publisher.status()["state"] == "ok"
    assert (
        git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        == CONFIG_FILENAME
    )
    assert git(repository, "show", ":private.txt") == before
    assert unrelated.read_text(encoding="utf-8") == "unstaged"
    remote = repository.parent / "remote.git"
    snapshot = json.loads(git(remote, "show", f"refs/heads/main:{CONFIG_FILENAME}"))
    assert snapshot == store.snapshot().to_json()
    assert git(repository, "diff", "--cached", "--name-only") == "private.txt"
    assert publisher.flush()[1] == "repository synchronized"


@pytest.mark.parametrize("reason", ["disabled", "no_remote", "ignored", "invalid"])
def test_unpublishable_settings_have_explicit_status(tmp_path: pathlib.Path, reason: str) -> None:
    git(tmp_path, "init")
    store = GatewayConfigStore(tmp_path, autopush=reason != "disabled")
    if reason == "ignored":
        git(tmp_path, "remote", "add", "origin", str(tmp_path / "unused"))
        (tmp_path / ".gitignore").write_text(CONFIG_FILENAME, encoding="utf-8")
    if reason == "invalid":
        store.path.write_text('{"schema":1}', encoding="utf-8")
    publisher = GatewayPublisher(store)
    ok, detail = publisher.flush()
    assert ok is (reason != "invalid")
    assert publisher.status()["state"] == ("failed" if reason == "invalid" else "off")
    assert detail
    assert git(tmp_path, "diff", "--cached", "--name-only") == ""


def test_board_repository_lock_serializes_gateway_publish(repository: pathlib.Path) -> None:
    publisher = GatewayPublisher(GatewayConfigStore(repository, autopush=True))
    entered = threading.Event()
    finished = threading.Event()

    def publish() -> None:
        entered.set()
        publisher.flush()
        finished.set()

    with api_endpoint.repository_push_locked(repository):
        worker = threading.Thread(target=publish)
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(0.15)
        assert git(repository, "log", "-1", "--format=%s") == "Initial"
    worker.join(10)
    assert finished.is_set()
    assert publisher.status()["state"] == "ok"


def test_network_push_releases_settings_lock(
    repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GatewayConfigStore(repository, autopush=True)
    publisher = GatewayPublisher(store)
    original = gateway_publish._git  # noqa: SLF001 -- intercept the network boundary
    pushing = threading.Event()
    release = threading.Event()

    def delayed_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        if arguments == ["push"]:
            pushing.set()
            assert release.wait(10)
        return original(arguments, cwd)

    monkeypatch.setattr(gateway_publish, "_git", delayed_git)
    worker = threading.Thread(target=publisher.flush)
    worker.start()
    try:
        assert pushing.wait(10)
        store.apply(0, {"action": "pin", "key": "one.json"}, {"one.json"})
        assert store.snapshot().revision == 1
    finally:
        release.set()
        worker.join(10)
    assert publisher.flush()[0]
    assert json.loads(git(repository, "show", f"HEAD:{CONFIG_FILENAME}"))["revision"] == 1


def test_startup_reconciles_and_retries_sanitized_push_failure(
    repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GatewayPublisher(GatewayConfigStore(repository, autopush=True))
    original = gateway_publish._git  # noqa: SLF001 -- intercept the network boundary
    attempts = 0
    allow_retry = threading.Event()

    def failing_git(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        if arguments == ["push"]:
            attempts += 1
            if not allow_retry.is_set():
                return subprocess.CompletedProcess(arguments, 1, "", "https://secret:token@host")
        return original(arguments, cwd)

    monkeypatch.setattr(gateway_publish, "_git", failing_git)
    monkeypatch.setattr(gateway_publish, "QUIET_SECONDS", 0.02)
    monkeypatch.setattr(gateway_publish, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(gateway_publish, "TICK_SECONDS", 0.01)
    publisher.start()
    try:
        wait_until(lambda: publisher.status()["state"] == "failed")
        assert "secret" not in str(publisher.status())
        assert "token" not in str(publisher.status())
        assert git(repository, "show", f"HEAD:{CONFIG_FILENAME}")
        allow_retry.set()
        wait_until(lambda: publisher.status()["state"] == "ok")
        assert attempts >= 2
    finally:
        allow_retry.set()
        assert publisher.stop()[0]
    remote = repository.parent / "remote.git"
    assert git(remote, "rev-parse", "refs/heads/main") == git(repository, "rev-parse", "HEAD")


def test_stop_flushes_latest_settings_before_quiet_period(repository: pathlib.Path) -> None:
    store = GatewayConfigStore(repository, autopush=True)
    publisher = GatewayPublisher(store)
    publisher.start()
    store.apply(0, {"action": "pin", "key": "latest.json"}, {"latest.json"})
    assert publisher.stop()[0]
    snapshot = json.loads(git(repository, "show", f"origin/main:{CONFIG_FILENAME}"))
    assert snapshot["pinnedBoards"] == ["latest.json"]


def test_changes_debounce_into_one_gateway_snapshot(
    repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GatewayConfigStore(repository, autopush=True)
    publisher = GatewayPublisher(store)
    monkeypatch.setattr(gateway_publish, "QUIET_SECONDS", 0.4)
    monkeypatch.setattr(gateway_publish, "TICK_SECONDS", 0.01)
    publisher.start()
    try:
        store.apply(0, {"action": "pin", "key": "one.json"}, {"one.json", "two.json"})
        time.sleep(0.1)
        store.apply(1, {"action": "pin", "key": "two.json"}, {"one.json", "two.json"})
        assert git(repository, "log", "-1", "--format=%s") == "Initial"
        wait_until(lambda: publisher.status()["state"] == "ok")
        assert git(repository, "rev-list", "--count", "HEAD") == "2"
        snapshot = json.loads(git(repository, "show", f"origin/main:{CONFIG_FILENAME}"))
        assert snapshot["pinnedBoards"] == ["one.json", "two.json"]
    finally:
        assert publisher.stop()[0]
