"""Ticket permutations preserve immutable attachment storage and captured backups."""

import base64
import hashlib
import json
import pathlib
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, attachments, board_state
from tests.support import USERS
from tests.test_api import api, request_json
from tests.test_attachments import PNG
from tests.test_snapshot_publish import head_paths
from tests.test_store import initialize_autopush_repository, make_store, run_git

if TYPE_CHECKING:
    from tests.test_api import RunningApi

__all__ = ["api"]
API = board_state.API_PREFIX


def seed_cards(board: board_state.Board) -> str:
    """Give the selected pair reversed dependency order and rich untouched neighbors."""
    for ref in ("populated", "empty", "unselected", "archived"):
        board.create(ref, ref, "backlog", "bot", detail=f"Detail for {ref}")
        board.comment(ref, f"Comment for {ref}", "bot")
    board.set_archived("archived", archived=True, by="bot")
    board.link("empty", "blocks", "populated", "bot")
    return "seeded"


def upload_for(api: RunningApi, ref: str, data: bytes, name: str = "evidence.txt") -> pathlib.Path:
    """Exercise the actual HTTP upload path and return its expected birth path."""
    board = board_state.load(api.path)
    birth = board.find(ref).storage_ticket
    code, out = request_json(
        api.base + API + f"/cards/{ref}/attachment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=board.revision,
        body={"name": name, "data": base64.b64encode(data).decode("ascii")},
    )
    assert code == 200, out
    result = attachments.blob_path(
        api.path, birth, str(out["attachment"]["id"]), attachments.extension(name)
    )
    assert result.read_bytes() == data
    return result


def attachment_tree(path: pathlib.Path) -> dict[str, bytes]:
    return {
        file.relative_to(path.parent).as_posix(): file.read_bytes()
        for file in (path.parent / "attachments").rglob("*")
        if file.is_file()
    }


def assert_download(api: RunningApi, ref: str, path: pathlib.Path, data: bytes) -> None:
    with urllib.request.urlopen(
        api.base + api_endpoint.attachment_url(ref, path.stem) + "?download=1", timeout=5
    ) as response:
        assert response.read() == data
        assert response.headers["Content-Disposition"].startswith("attachment;")


def test_legacy_card_tokens_survive_another_cards_first_renumber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(board_state, "now", lambda: "2026-01-02T03:04:05Z")
    board = board_state.Board(
        project="Fixed token fixture",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    seed_cards(board)
    legacy_tokens: dict[str, str] = {}
    for ref in ("populated", "unselected", "archived"):
        # This is the pre-ticket-history token contract. The selected card has the
        # fixture's single relationship; neither untouched card has any links.
        legacy_state = {
            "item": board.find(ref).to_json(),
            "links": [link.to_json() for link in board.links] if ref == "populated" else [],
            "relationships": [event.to_json() for event in board.relationship_history]
            if ref == "populated"
            else [],
            "children": [],
            "parents": [],
            "subjects": [],
            "details": [],
            "archives": [event.to_json() for event in board.archive_history]
            if ref == "archived"
            else [],
        }
        legacy_tokens[ref] = hashlib.sha256(
            json.dumps(legacy_state, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert api_endpoint.card_version(board, board.find(ref)) == legacy_tokens[ref]
    board.reorder_tickets("backlog", ["populated", "empty"], "bot")
    assert api_endpoint.card_version(board, board.find("populated")) != legacy_tokens["populated"]
    for ref in ("unselected", "archived"):
        assert api_endpoint.card_version(board, board.find(ref)) == legacy_tokens[ref]


def test_swaps_preserve_paths_and_upload_to_empty_cards_birth_anchor(api: RunningApi) -> None:
    store = api_endpoint.STORE
    assert store is not None
    store.execute(0, seed_cards)
    retained = upload_for(api, "populated", PNG, "cover.png")
    detached = upload_for(api, "populated", b"removed before swap")
    board = board_state.load(api.path)
    store.execute(
        board.revision,
        lambda candidate: candidate.remove_attachment("populated", detached.stem, "bot"),
    )
    store.execute(
        board_state.load(api.path).revision,
        lambda candidate: candidate.set_cover("populated", retained.stem, "bot"),
    )
    board = board_state.load(api.path)
    board_before = board.to_json()
    selected_before = {ref: board.find(ref).to_json() for ref in ("populated", "empty")}
    untouched = {ref: board.find(ref).to_json() for ref in ("unselected", "archived")}
    original_files = attachment_tree(api.path)
    versions = {ref: api_endpoint.card_version(board, board.find(ref)) for ref in untouched}
    selected_versions = {
        ref: api_endpoint.card_version(board, board.find(ref)) for ref in ("populated", "empty")
    }
    result = store.ticket_order(
        "backlog", ["populated", "empty"], "bot", apply=True, expected_revision=board.revision
    )
    assert result["applied"] is True
    board = board_state.load(api.path)
    assert [
        (board.find(ref).ticket, board.find(ref).storage_ticket) for ref in ("populated", "empty")
    ] == [
        (2, 1),
        (1, 2),
    ]
    assert board.to_json()["schema"] == 6
    assert attachment_tree(api.path) == original_files
    assert board.to_json()["nextTicket"] == board_before["nextTicket"]
    for ref, previous in untouched.items():
        assert board.find(ref).to_json() == previous
        assert api_endpoint.card_version(board, board.find(ref)) == versions[ref]
    for ref, previous in selected_versions.items():
        assert api_endpoint.card_version(board, board.find(ref)) != previous
        persisted = board.find(ref).to_json()
        persisted.pop("originTicket")
        persisted["ticket"] = selected_before[ref]["ticket"]
        assert persisted == selected_before[ref]
    for field in ("links", "relationshipHistory", "parentHistory", "archiveHistory"):
        assert board.to_json().get(field) == board_before.get(field)
    assert_download(api, "populated", retained, PNG)
    for ref, file in (("populated", detached), ("empty", retained)):
        with pytest.raises(urllib.error.HTTPError) as refusal:
            urllib.request.urlopen(
                api.base + api_endpoint.attachment_url(ref, file.stem), timeout=5
            )
        with refusal.value:
            assert refusal.value.code == 404
    empty_upload = upload_for(api, "empty", b"first upload after swap")
    populated_upload = upload_for(api, "populated", b"second active upload")
    assert empty_upload.parent.name == "0002"
    assert populated_upload.parent.name == "0001"
    assert_download(api, "empty", empty_upload, b"first upload after swap")

    def reverse_dependency(candidate: board_state.Board) -> str:
        candidate.unlink("empty", "blocks", "populated", "bot")
        return candidate.link("populated", "blocks", "empty", "bot")

    store.execute(board_state.load(api.path).revision, reverse_dependency)
    before_reverse = board_state.load(api.path)
    before_versions = {
        ref: api_endpoint.card_version(before_reverse, before_reverse.find(ref))
        for ref in ("populated", "empty")
    }
    store.ticket_order(
        "backlog",
        ["populated", "empty"],
        "bot",
        apply=True,
        expected_revision=before_reverse.revision,
    )
    restored = board_state.load(api.path)
    for ref, birth in (("populated", 1), ("empty", 2)):
        item = restored.find(ref)
        assert item.ticket == item.origin_ticket == item.storage_ticket == birth
        assert api_endpoint.card_version(restored, item) != before_versions[ref]
        # Even after the displayed number returns, audit batches remain token inputs.
        assert api_endpoint.card_version(restored, item) != api_endpoint.card_version(
            restored, item, ticket_history={}
        )
    assert len(restored.ticket_history) == 2
    assert restored.verify() == []
    assert all(attachment_tree(api.path)[path] == blob for path, blob in original_files.items())
    assert len(attachment_tree(api.path)) == 4
    for ref, previous in untouched.items():
        assert restored.find(ref).to_json() == previous
    assert_download(api, "populated", retained, PNG)
    assert_download(api, "empty", empty_upload, b"first upload after swap")


def seed_publisher(path: pathlib.Path) -> dict[str, bytes]:
    path.parent.mkdir()
    make_store(path)
    board = board_state.load(path)
    seed_cards(board)
    files: dict[str, bytes] = {}
    for index in (1, 2):
        data = f"immutable audited evidence {index}\n".encode()
        aid = str(uuid.UUID(int=index))
        attachment = attachments.Attachment(
            aid,
            f"evidence-{index}.txt",
            len(data),
            "application/octet-stream",
            hashlib.sha256(data).hexdigest(),
            board_state.now(),
            "bot",
        )
        blob = attachments.blob_path(path, 1, aid, ".txt")
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        board.add_attachment("populated", attachment)
        files[blob.relative_to(path.parent).as_posix()] = data
    board.remove_attachment("populated", str(uuid.UUID(int=2)), "bot")
    assert board.verify() == []
    board_state.save(board, path)
    return files


@pytest.mark.parametrize("renumber_before_capture", [False, True])
def test_captured_backup_keeps_audited_birth_paths_during_concurrent_renumber(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, renumber_before_capture: bool
) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "board" / "board.json"
    expected_files = seed_publisher(path)
    if renumber_before_capture:
        board = board_state.load(path)
        board.reorder_tickets("backlog", ["populated", "empty"], "bot")
        board_state.save(board, path)
    captured = path.read_bytes()
    entered, resume = threading.Event(), threading.Event()
    original_read = attachments.read_blob
    paused = False

    def paused_read(board_path: pathlib.Path, ticket: int, file: attachments.Attachment) -> bytes:
        nonlocal paused
        if not paused:
            paused = True
            entered.set()
            assert resume.wait(10)
        assert ticket == 1
        return original_read(board_path, ticket, file)

    monkeypatch.setattr(attachments, "read_blob", paused_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(api_endpoint.push_board, path)
        try:
            assert entered.wait(10)
            assert api_endpoint._command_gate.acquire(timeout=0.25)  # noqa: SLF001
            try:
                newer = board_state.load(path)
                if renumber_before_capture:
                    newer.unlink("empty", "blocks", "populated", "bot")
                    newer.link("populated", "blocks", "empty", "bot")
                newer.reorder_tickets("backlog", ["populated", "empty"], "bot")
                board_state.save(newer, path)
            finally:
                api_endpoint._command_gate.release()  # noqa: SLF001
        finally:
            resume.set()
        assert pending.result(timeout=30) == (True, "committed and pushed")
    assert path.read_bytes() != captured
    assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == captured
    assert head_paths(repo) == {
        "anchor.txt",
        "board/board.json",
        *(f"board/{file}" for file in expected_files),
    }
    for file, data in expected_files.items():
        assert run_git(repo, "show", f"HEAD:board/{file}").stdout.encode() == data
    assert attachment_tree(path) == expected_files
    assert board_state.load(path).verify() == []
    assert api_endpoint.push_board(path) == (True, "committed and pushed")
    assert run_git(repo, "show", "HEAD:board/board.json").stdout.encode() == path.read_bytes()
