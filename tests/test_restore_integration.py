"""Real offline restore across supplied clients, synthetic processes and recovery bytes."""

# These adapters drive production entrypoints; they do not implement board storage.
# ruff: noqa: SLF001

import contextlib
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, cast
lazy from collections.abc import Generator

import pytest

from localswim import (
    api_endpoint,
    attachments,
    board_state,
    restore_plan,
    service_ownership,
    workspace,
)
from tests.support import GENERATION, USERS
from tests.test_attachments import PNG
from tests.test_restore_candidate import BLOB, attach, snapshot
from tests.test_service_ownership import child_event

API = board_state.API_PREFIX
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native restore contract")


def unused_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def request(
    url: str,
    *,
    credential: str | None = None,
    write_token: str | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One request only: transport failure and refusals are never replayed."""
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = "Bearer " + credential
    if write_token is not None:
        headers["If-Match"] = write_token
    outgoing = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(outgoing, timeout=30)
    except urllib.error.HTTPError as failure:
        response = failure
    with response:
        return cast("int", response.status), json.loads(response.read())


def command(target: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "localswim.cli", str(target), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def apply_plan(root: pathlib.Path, target: pathlib.Path, plan_file: pathlib.Path) -> dict[str, Any]:
    digest = json.loads(plan_file.read_bytes())["planDigest"]
    result = command(
        target,
        "board",
        "restore",
        "apply",
        str(plan_file),
        "--expect-plan",
        digest,
        "--staging",
        str(root / "stage"),
        "--checkpoint",
        str(root / "checkpoint"),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["state"] == "committed"
    inspected = command(
        target, "board", "restore", "inspect", str(plan_file), "--expect-plan", digest, "--json"
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout) == outcome
    return outcome


def gateway_worker() -> None:
    """A parent-owned stdin lifecycle avoids signals or foreign process discovery."""
    # Preserve the actual loaded build identity, but exclude repeated Git metadata
    # refresh from this bounded restore fixture (Windows subprocess pipe delays).
    api_endpoint._next_build_refresh_at = time.monotonic() + 3600
    instance = workspace.Workspace(pathlib.Path(sys.argv[1]))
    server = workspace.WorkspaceServer(("127.0.0.1", int(sys.argv[2])), instance)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    print("ready", flush=True)
    try:
        sys.stdin.readline()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)
        instance.close()


class RestoreClients:
    """Owned direct writer and gateway over one private synthetic board."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.target = root / "boards" / "alpha" / "board.json"
        self.source = root / "backup" / "board.json"
        self.direct_port = unused_port()
        self.gateway_port = unused_port()
        self.direct_base = f"http://127.0.0.1:{self.direct_port}"
        self.gateway_base = f"http://127.0.0.1:{self.gateway_port}"
        self.gateway_prefix = API + "/workspace/boards/alpha%2Fboard"
        self.writer: subprocess.Popen[str] | None = None
        self.gateway: subprocess.Popen[str] | None = None
        self.sealed: restore_plan.SealedPlan | None = None
        board = board_state.Board(
            generation=GENERATION,
            project="Synthetic restore acceptance",
            users=USERS,
            browser_user="terry",
            cli_user="bot",
            default_owner="bot",
        )
        board.port = self.direct_port
        for card_id, subject in (("a", "draft test"), ("b", "other draft test")):
            board.create(card_id, subject, "ready_for_work", "bot")
            board.move(card_id, "in_progress", "bot")
            board.move(card_id, "ready_for_review", "bot")
        snapshot(self.source.parent, board)
        self.prior_snapshot = self.source.read_bytes()
        board.revision += 1
        snapshot(self.target.parent, board)

    def start(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = dict(os.environ)
        self.writer = subprocess.Popen(
            service_ownership.python_child_command(
                ["-m", "localswim.api_endpoint", str(self.target)], environment
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=flags,
            env=environment,
        )
        deadline = time.monotonic() + 20
        while not service_ownership.service_is_healthy(self.target):
            if self.writer.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("synthetic writer startup failed")
            time.sleep(0.025)
        self.gateway = subprocess.Popen(
            service_ownership.python_child_command(
                [
                    "-c",
                    "from tests.test_restore_integration import gateway_worker; gateway_worker()",
                    str(self.target.parent.parent),
                    str(self.gateway_port),
                ],
                environment,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=flags,
            env=environment,
        )
        child_event(self.gateway, "ready")
        assert request(self.gateway_base + self.gateway_prefix + "/board")[0] == 200

    def stop(self) -> None:
        if self.gateway is not None and self.gateway.poll() is None:
            assert self.gateway.stdin is not None
            self.gateway.stdin.write("stop\n")
            self.gateway.stdin.flush()
            assert self.gateway.wait(timeout=20) == 0
        if self.gateway is not None:
            for pipe in (self.gateway.stdin, self.gateway.stdout):
                if pipe is not None:
                    pipe.close()
            self.gateway = None
        if self.writer is not None and self.writer.poll() is None:
            descriptor = board_state._service_descriptor(self.target)
            assert (
                request(
                    self.direct_base + API + "/shutdown",
                    credential=str(descriptor["token"]),
                    body={},
                )[0]
                == 200
            )
            assert self.writer.wait(timeout=20) == 0
        self.writer = None
        assert not service_ownership.writer_busy(self.target)

    def restore(self) -> dict[str, Any]:
        self.stop()
        # Explicit synthetic older-disk replacement: the old live capture was
        # revision N; the modeled observed disk and supplied backup are N-1.
        # A real restore therefore returns to N under a freshly allocated generation.
        self.target.write_bytes(self.prior_snapshot)
        plan_file = self.root / "plan.json"
        result = command(
            self.target, "board", "restore", "plan", str(self.source), "--output", str(plan_file)
        )
        assert result.returncode == 0, result.stderr
        self.sealed = restore_plan.verify_plan(
            plan_file.read_bytes(),
            expect_plan=json.loads(plan_file.read_bytes())["planDigest"],
            target=self.target,
        )
        result = command(
            self.target,
            "board",
            "restore",
            "apply",
            str(plan_file),
            "--expect-plan",
            self.sealed.plan_digest,
            "--staging",
            str(self.root / "stage"),
            "--checkpoint",
            str(self.root / "checkpoint"),
            "--json",
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert outcome["state"] == "committed"
        assert self.target.read_bytes() == self.sealed.final_snapshot
        self.start()
        return outcome


@contextlib.contextmanager
def browser_restore_fixture(root: pathlib.Path) -> Generator[dict[str, Any]]:
    """Expose actual responses and active endpoints for the separate executable DOM matrix."""
    clients = RestoreClients(root)
    try:
        clients.start()
        before = {
            "board": request(clients.direct_base + API + "/board")[1],
            "status": request(clients.direct_base + API + "/status")[1],
        }
        old_direct = request(clients.direct_base + API + "/browser-session")[1]
        old_gateway = request(clients.gateway_base + API + "/browser-session")[1]
        outcome = clients.restore()
        after = {
            "board": request(clients.direct_base + API + "/board")[1],
            "status": request(clients.direct_base + API + "/status")[1],
        }
        assert before["board"]["revision"] == after["board"]["revision"]
        assert before["board"]["generation"] != after["board"]["generation"]
        yield {
            "before": before,
            "after": after,
            "directSession": request(clients.direct_base + API + "/browser-session")[1],
            "gatewaySession": request(clients.gateway_base + API + "/browser-session")[1],
            "gatewayBoardSession": request(
                clients.gateway_base + clients.gateway_prefix + "/browser-session"
            )[1],
            "gatewayCatalog": request(clients.gateway_base + API + "/workspace")[1],
            "oldDirectSession": old_direct,
            "oldGatewaySession": old_gateway,
            "directBase": clients.direct_base,
            "gatewayBase": clients.gateway_base,
            "directPrefix": API,
            "gatewayPrefix": clients.gateway_prefix,
            "cardId": "a",
            "otherCardId": "b",
            "evidence": {
                "restoreOutcome": outcome["state"],
                "sameRevision": True,
                "lostSnapshotModel": "captured N; modeled older disk/backup N-1; restored N",
                "finalDigest": outcome["finalDigest"],
            },
        }
    finally:
        clients.stop()


def test_real_restore_rejects_same_revision_old_capture_across_clients(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser_restore_fixture(tmp_path) as fixture:
        before = fixture["before"]["board"]
        after = fixture["after"]["board"]
        target = tmp_path / "boards" / "alpha" / "board.json"
        persisted = target.read_bytes()
        for base, prefix, session in (
            (fixture["directBase"], API, fixture["directSession"]),
            (fixture["gatewayBase"], fixture["gatewayPrefix"], fixture["gatewaySession"]),
        ):
            code, refusal = request(
                base + prefix + "/cards/a/subject",
                credential=session["token"],
                write_token=before["writeToken"],
                body={"subject": "stale must never land"},
            )
            assert code == 412
            assert refusal["code"] == "state_changed"
            assert target.read_bytes() == persisted
        # The real CLI command retains the one earlier snapshot while discovering
        # the new process credential. Count the actual HTTP dispatch, with no retry.
        calls: list[str] = []
        original = board_state._service_json

        def captured_snapshot(_base: str, _credential: str) -> board_state.JsonObject:
            return before

        def observed(
            url: str,
            token: str,
            *,
            body: dict[str, object] | None = None,
            write_token: str | None = None,
        ) -> board_state.JsonObject:
            calls.append(url)
            return original(url, token, body=body, write_token=write_token)

        from click.testing import CliRunner  # noqa: PLC0415 -- focused command driver

        from localswim import cli  # noqa: PLC0415 -- focused command driver

        with monkeypatch.context() as patches:
            patches.setattr(board_state, "_service_snapshot", captured_snapshot)
            patches.setattr(board_state, "_service_json", observed)
            result = CliRunner().invoke(
                cli.cli, [str(target), "card", "set-subject", "a", "stale CLI intent"]
            )
        assert result.exit_code != 0
        assert "HTTP 412" in result.output
        assert len(calls) == 1
        assert target.read_bytes() == persisted
        for base, prefix, session in (
            (fixture["directBase"], API, fixture["directSession"]),
            (fixture["gatewayBase"], fixture["gatewayPrefix"], fixture["gatewaySession"]),
        ):
            current = request(base + prefix + "/board")[1]
            assert (
                request(
                    base + prefix + "/cards/a/comment",
                    credential=session["token"],
                    write_token=current["writeToken"],
                    body={"text": "fresh reviewed generation"},
                )[0]
                == 200
            )
        result = command(target, "card", "set-subject", "a", "fresh CLI reviewed generation")
        assert result.returncode == 0, result.stderr
        loaded = board_state.load(target, install_context=False)
        assert loaded.generation == after["generation"]
        assert loaded.find("a").subject == "fresh CLI reviewed generation"
        assert len(loaded.find("a").comments) == 2


def test_real_restore_recovers_retained_bytes_and_newer_allocator_checkpoint(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.port = unused_port()
    source_blob = attach(board, tmp_path / "backup", removed=True)
    cover_id = "22222222-2222-4222-8222-222222222222"
    board.create("cover", "Archived active cover", "backlog", "bot")
    board.add_attachment(
        "cover",
        attachments.Attachment(
            cover_id,
            "cover.png",
            len(PNG),
            "image/png",
            hashlib.sha256(PNG).hexdigest(),
            board_state.now(),
            "bot",
        ),
    )
    board.set_cover("cover", cover_id, "bot")
    board.set_archived("cover", archived=True, by="bot")
    cover_blob = tmp_path / "backup" / "attachments" / "0002" / f"{cover_id}.png"
    cover_blob.parent.mkdir(parents=True)
    cover_blob.write_bytes(PNG)
    source = snapshot(tmp_path / "backup", board)
    board.create("newer", "Newer recovery state", "backlog", "bot")
    board.next_ticket = 700
    target = snapshot(tmp_path / "current", board)
    # Copy the retained immutable file using the exact source reference, never a glob.
    target_blob = target.parent / source_blob.relative_to(source.parent)
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(BLOB)
    target_cover = target.parent / cover_blob.relative_to(source.parent)
    target_cover.parent.mkdir(parents=True)
    target_cover.write_bytes(PNG)
    before = target.read_bytes()
    plan_file = tmp_path / "plan.json"
    result = command(target, "board", "restore", "plan", str(source), "--output", str(plan_file))
    assert result.returncode == 0, result.stderr
    apply_plan(tmp_path, target, plan_file)
    loaded = board_state.load(target, install_context=False)
    assert loaded.find("card").archived
    assert loaded.find("card").attachments == []
    assert loaded.next_ticket == 700
    assert len(loaded.items) == 2
    assert loaded.find("cover").archived
    assert loaded.find("cover").cover_attachment == cover_id
    assert loaded.find("cover").attachments[0].sha256 == hashlib.sha256(PNG).hexdigest()
    assert (tmp_path / "checkpoint" / "current.json").read_bytes() == before
    recovered = board_state.load(tmp_path / "checkpoint" / "current.json", install_context=False)
    assert recovered.find("newer").subject == "Newer recovery state"
    assert recovered.next_ticket == 700
    assert target_blob.read_bytes() == BLOB
    assert target_cover.read_bytes() == PNG


@pytest.mark.parametrize("kind", ["missing", "corrupt", "incomplete"])
def test_real_cli_requires_truthful_recovery_acknowledgment(
    tmp_path: pathlib.Path, board: board_state.Board, kind: str
) -> None:
    board.port = unused_port()
    source = snapshot(tmp_path / "backup", board)
    target = tmp_path / "current" / "board.json"
    target.parent.mkdir()
    prior = None
    if kind == "corrupt":
        prior = b"{synthetic corrupt snapshot"
        target.write_bytes(prior)
    elif kind == "incomplete":
        missing = attach(board, target.parent)
        snapshot(target.parent, board)
        missing.unlink()
        prior = target.read_bytes()
    plan_file = tmp_path / "plan.json"
    args = ["board", "restore", "plan", str(source), "--output", str(plan_file)]
    if kind != "incomplete":
        args.extend(["--port", str(board.port)])
    refused = command(target, *args)
    assert refused.returncode != 0
    assert "acknowledgment" in refused.stderr
    assert not plan_file.exists()
    assert not (tmp_path / "stage").exists()
    flag = (
        "--acknowledge-incomplete-recovery"
        if kind == "incomplete"
        else "--acknowledge-raw-recovery"
    )
    admitted = command(target, *args, flag)
    assert admitted.returncode == 0, admitted.stderr
    apply_plan(tmp_path, target, plan_file)
    loaded = board_state.load(target, install_context=False)
    assert loaded.verify() == []
    assert loaded.items == []
    checkpoint = tmp_path / "checkpoint"
    if prior is None:
        assert (checkpoint / "current.missing").read_bytes() == b"missing\n"
    else:
        assert (checkpoint / "current.json").read_bytes() == prior
    assert (checkpoint / "complete.json").is_file()


def test_real_cli_immutable_conflict_never_creates_plan_or_changes_target(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.port = unused_port()
    source_blob = attach(board, tmp_path / "backup")
    source = snapshot(tmp_path / "backup", board)
    target = snapshot(tmp_path / "current", board)
    conflicting = target.parent / source_blob.relative_to(source.parent)
    conflicting.parent.mkdir(parents=True)
    conflicting.write_bytes(b"different immutable bytes")
    before = target.read_bytes()
    plan_file = tmp_path / "plan.json"
    result = command(target, "board", "restore", "plan", str(source), "--output", str(plan_file))
    assert result.returncode != 0
    assert "immutable target" in result.stderr
    assert not plan_file.exists()
    assert target.read_bytes() == before
    assert conflicting.read_bytes() == b"different immutable bytes"


def test_independent_real_cli_restores_allocate_distinct_persisted_generations(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.port = unused_port()
    source = snapshot(tmp_path / "backup", board)
    target = snapshot(tmp_path / "current", board)
    generations: list[str] = []
    for index in range(2):
        root = tmp_path / f"operation-{index}"
        root.mkdir()
        plan_file = root / "plan.json"
        result = command(
            target, "board", "restore", "plan", str(source), "--output", str(plan_file)
        )
        assert result.returncode == 0, result.stderr
        outcome = apply_plan(root, target, plan_file)
        loaded = board_state.load(target, install_context=False)
        assert loaded.generation == outcome["generation"]
        generations.append(loaded.generation)
    assert len(set(generations)) == 2
