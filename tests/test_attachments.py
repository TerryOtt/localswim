"""Attachment upload, bounded storage, covers, removal, CLI, and Git durability."""

import base64
import contextlib
import datetime as dt
import hashlib
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, cast
lazy from collections.abc import Generator

import pytest

from localswim import api_endpoint, attachments, board_state
from tests.test_api import api, request_json
from tests.test_cli import assert_cli, run_cli, served_board
from tests.test_store import initialize_autopush_repository, make_store, run_git

__all__ = ["api", "served_board"]  # Pytest collects the imported service fixtures.

if TYPE_CHECKING:
    import pathlib

    from tests.test_api import RunningApi

API = board_state.API_PREFIX
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
)


def create_card(api: RunningApi, ref: str = "card", revision: int = 0) -> None:
    """Create one card through the real service."""
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=revision,
        body={"id": ref, "subject": ref, "state": "backlog"},
    )
    assert code == 200


def upload(api: RunningApi, revision: int, name: str = "picture.png", blob: bytes = PNG) -> str:
    """Upload through the browser credential and return its assigned GUID."""
    code, out = request_json(
        api.base + API + "/cards/card/attachment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=revision,
        body={"name": name, "data": base64.b64encode(blob).decode("ascii")},
    )
    assert code == 200, out
    assert out["revision"] == revision + 1
    return str(out["attachment"]["id"])


def test_upload_preview_cover_and_removal_have_atomic_attributed_history(api: RunningApi) -> None:
    create_card(api)
    aid = upload(api, 1, "picture.PNG")
    assert str(uuid.UUID(aid)) == aid
    saved = board_state.load(api.path)
    item = saved.find("card")
    file = item.attachments[0]
    path = api.path.parent / "attachments" / "0001" / f"{aid}.png"
    assert path.read_bytes() == PNG
    assert file.name == "picture.PNG"
    assert file.by == "terry"
    assert file.media_type == "image/png"
    url = api.base + api_endpoint.attachment_url("card", aid)
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.read() == PNG
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Content-Disposition"].startswith("inline;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "private, max-age=86400, immutable"
    with urllib.request.urlopen(url + "?download=1", timeout=5) as response:
        assert response.headers["Content-Disposition"].startswith("attachment;")
    code, out = request_json(
        api.base + API + "/cards/card/cover",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"attachmentId": aid},
    )
    assert code == 200
    assert out["revision"] == 3
    code, payload = request_json(api.base + API + "/board")
    assert code == 200
    card = payload["lanes"][0]["items"][0]
    assert card["coverAttachment"] == aid
    assert card["attachments"][0]["url"] == api_endpoint.attachment_url("card", aid)
    assert card["attachmentHistory"][0]["when"]
    code, out = request_json(
        api.base + API + "/cards/card/attachment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=3,
        body={"remove": True, "attachmentId": aid},
    )
    assert code == 200
    assert out["revision"] == 4
    saved = board_state.load(api.path)
    item = saved.find("card")
    assert item.attachments == []
    assert item.cover_attachment is None
    assert [event.action for event in item.attachment_history] == [
        "attached",
        "cover_set",
        "cover_cleared",
        "attachment_removed",
    ]
    assert all(event.by == "terry" for event in item.attachment_history)
    assert path.read_bytes() == PNG
    assert saved.verify() == []
    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(url, timeout=5)
    with refusal.value:
        assert refusal.value.code == 404
    events = board_state.activity_events(saved, dt.datetime(2000, 1, 1, tzinfo=dt.UTC), None)
    attachment_events = [event.to_json() for event in events if event.attachment_id]
    assert [event["kind"] for event in attachment_events] == [
        "attached",
        "cover_set",
        "cover_cleared",
        "attachment_removed",
    ]
    assert "picture" not in json.dumps(attachment_events)


def test_generic_files_download_and_cannot_be_covers(api: RunningApi) -> None:
    create_card(api)
    aid = upload(api, 1, "drawing.svg", b"<svg onload='alert(1)'></svg>")
    url = api.base + api_endpoint.attachment_url("card", aid)
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert "sandbox" in response.headers["Content-Security-Policy"]
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards/card/cover",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"attachmentId": aid},
    )
    assert code == 409
    assert api.path.read_bytes() == before


def test_uploads_get_distinct_guids_and_stale_writes_leave_no_files(api: RunningApi) -> None:
    create_card(api)
    first = upload(api, 1)
    second = upload(api, 2)
    assert first != second
    paths = list((api.path.parent / "attachments" / "0001").iterdir())
    assert len(paths) == 2
    code, _ = request_json(
        api.base + API + "/cards/card/attachment",
        token=api_endpoint.CLI_TOKEN,
        revision=1,
        body={"name": "stale.png", "data": base64.b64encode(PNG).decode("ascii")},
    )
    assert code == 412
    assert list((api.path.parent / "attachments" / "0001").iterdir()) == paths
    assert board_state.load(api.path).revision == 3


@pytest.mark.parametrize(
    "name", ["../secret.png", "folder/picture.png", "bad\\name.png", "bad\nname.png", ""]
)
def test_invalid_names_do_not_touch_board_or_storage(api: RunningApi, name: str) -> None:
    create_card(api)
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/cards/card/attachment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={"name": name, "data": base64.b64encode(PNG).decode("ascii")},
    )
    assert code == 409
    assert api.path.read_bytes() == before
    assert not (api.path.parent / "attachments").exists()


def test_size_boundary_and_oversized_http_refusal(api: RunningApi) -> None:
    blob = b"x" * attachments.MAX_BYTES
    assert (
        len(attachments.decode_upload(base64.b64encode(blob).decode("ascii")))
        == attachments.MAX_BYTES
    )
    with pytest.raises(attachments.AttachmentTooLargeError, match="10 MiB"):
        attachments.decode_upload(base64.b64encode(blob + b"x").decode("ascii"))
    create_card(api)
    before = api.path.read_bytes()
    request = urllib.request.Request(
        api.base + API + "/cards/card/attachment",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_endpoint.BROWSER_TOKEN}",
            "If-Match": '"revision-1"',
            "Content-Length": str(attachments.MAX_REQUEST_BYTES + 1),
        },
    )
    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(request, timeout=5)
    with refusal.value:
        assert refusal.value.code == 413
        assert b"10 MiB" in refusal.value.read()
    assert api.path.read_bytes() == before
    assert not (api.path.parent / "attachments").exists()


def test_upload_requires_credentials_revision_and_valid_base64(api: RunningApi) -> None:
    create_card(api)
    body: dict[str, object] = {"name": "image.png", "data": "not base64"}
    for token, revision, expected in [
        (None, 1, 401),
        (api_endpoint.CLI_TOKEN, None, 428),
        (api_endpoint.CLI_TOKEN, 1, 409),
    ]:
        code, _ = request_json(
            api.base + API + "/cards/card/attachment", token=token, revision=revision, body=body
        )
        assert code == expected
    assert board_state.load(api.path).revision == 1
    assert not (api.path.parent / "attachments").exists()


def test_snapshot_failure_rolls_back_uploaded_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    before = path.read_bytes()

    fsync = board_state.os.fsync
    calls = 0

    def fail_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected snapshot failure")
        fsync(fd)

    monkeypatch.setattr(board_state.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected"):
        store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert path.read_bytes() == before
    assert list((tmp_path / "attachments" / "0001").iterdir()) == []
    assert store.snapshot().find("card").attachments == []


@pytest.mark.parametrize("damage", ["size", "digest", "missing"])
def test_missing_or_changed_bytes_are_not_served(api: RunningApi, damage: str) -> None:
    create_card(api)
    aid = upload(api, 1)
    path = api.path.parent / "attachments" / "0001" / f"{aid}.png"
    url = api.base + api_endpoint.attachment_url("card", aid)
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.read() == PNG
    if damage == "missing":
        path.unlink()
    elif damage == "digest":
        changed = bytes([PNG[0] ^ 1]) + PNG[1:]
        assert len(changed) == len(PNG)
        path.write_bytes(changed)
    else:
        path.write_bytes(b"tampered")
    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(url, timeout=5)
    with refusal.value:
        assert refusal.value.code == 404


def test_attachment_git_snapshot_excludes_unrelated_files(tmp_path: pathlib.Path) -> None:
    _remote, repo = initialize_autopush_repository(tmp_path)
    path = repo / "localswim" / "board.json"
    path.parent.mkdir()
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    _, _, file = store.upload(
        1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot"
    )
    unrelated = path.parent / "attachments" / "0001" / "unrelated.txt"
    unrelated.write_text("do not commit", encoding="utf-8")
    run_git(repo, "add", str(unrelated))
    ok, detail = api_endpoint.push_board(path)
    assert ok, detail
    tracked = run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    assert tracked == [
        "anchor.txt",
        f"localswim/attachments/0001/{file.id}.png",
        "localswim/board.json",
    ]
    assert run_git(repo, "diff", "--cached", "--name-only").stdout.strip().endswith("unrelated.txt")
    store.execute(2, lambda board: board.remove_attachment("card", file.id, "bot"))
    assert api_endpoint.push_board(path)[0]
    assert (
        run_git(repo, "rev-parse", "HEAD").stdout
        == run_git(repo, "rev-parse", "@{upstream}").stdout
    )
    assert attachments.read_blob(path, 1, file) == PNG


def test_cli_add_list_cover_remove_and_size_refusal(
    served_board: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    assert_cli(served_board, "card", "create", "card", "Card", "--state", "backlog")
    image = tmp_path / "picture.png"
    image.write_bytes(PNG)
    assert_cli(served_board, "card", "attachment", "add", "1", str(image))
    result = run_cli(served_board, "card", "attachment", "list", "1", "--json")
    assert result.returncode == 0, result.stderr
    file = json.loads(result.stdout)["attachments"][0]
    assert file["by"] == "bot"
    assert_cli(served_board, "card", "attachment", "cover", "1", file["id"])
    assert board_state.load(served_board).find("card").cover_attachment == file["id"]
    attached = board_state.load(served_board).find("card").attachments
    assert_cli(served_board, "card", "attachment", "cover", "1", "--clear")
    cleared = board_state.load(served_board).find("card")
    assert cleared.cover_attachment is None
    assert cleared.attachments == attached
    assert_cli(served_board, "card", "attachment", "cover", "1", file["id"])
    assert_cli(served_board, "card", "attachment", "remove", "1", file["id"])
    assert board_state.load(served_board).find("card").attachments == []
    before = served_board.read_bytes()
    image.write_bytes(b"x" * (attachments.MAX_BYTES + 1))
    result = run_cli(served_board, "card", "attachment", "add", "1", str(image))
    assert result.returncode != 0
    assert "10 MiB" in result.stderr
    assert served_board.read_bytes() == before


def test_legacy_cards_omit_attachment_fields_and_audit_mismatch_is_reported(
    board: board_state.Board,
) -> None:
    board.create("card", "Card", "backlog", "bot")
    raw = board.to_json()
    assert "attachments" not in board.find("card").to_json()
    assert board_state.Board.from_json(raw, "old card").to_json() == raw
    file = attachments.Attachment(
        str(uuid.uuid4()), "image.png", len(PNG), "image/png", "0" * 64, board_state.now(), "bot"
    )
    board.add_attachment("card", file)
    board.find("card").attachment_history.clear()
    assert any("attachment history does not match" in problem for problem in board.verify())


def reattached_image(board: board_state.Board) -> attachments.Attachment:
    """Build a repeated-ID lifecycle with different current upload attribution."""
    board.create("card", "Card", "backlog", "bot")
    first = attachments.Attachment(
        str(uuid.uuid4()),
        "image.PNG",
        len(PNG),
        "image/png",
        hashlib.sha256(PNG).hexdigest(),
        "2026-10-20T12:00:00Z",
        "bot",
    )
    board.add_attachment("card", first)
    board.set_cover("card", first.id, "terry")
    board.remove_attachment("card", first.id, "terry")
    current = replace(first, at="2026-10-21T13:00:00Z", by="terry", name="renamed.png")
    board.add_attachment("card", current)
    return current


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("by", "bot", "by"),
        ("at", "2026-10-20T12:00:00Z", "at"),
        ("name", "image.jpg", "name suffix"),
    ],
)
def test_attachment_metadata_matches_current_upload_event(
    board: board_state.Board, field: str, value: str, diagnostic: str
) -> None:
    current = reattached_image(board)
    assert board.verify() == []
    board.find("card").attachments[0] = replace(current, **{field: value})
    assert board.verify() == [
        f"card: attachments[0].{diagnostic} does not match attachmentHistory[4] attached event"
    ]


@pytest.mark.parametrize("index", range(5))
def test_every_repeated_id_event_keeps_one_blob_suffix(
    board: board_state.Board, index: int
) -> None:
    reattached_image(board)
    item = board.find("card")
    assert [event.action for event in item.attachment_history] == [
        "attached",
        "cover_set",
        "cover_cleared",
        "attachment_removed",
        "attached",
    ]
    item.attachment_history[index] = replace(item.attachment_history[index], extension=".jpg")
    problems = board.verify()
    assert any(
        "attachmentHistory[" in problem and ".extension conflicts" in problem
        for problem in problems
    )
    assert all(problem.startswith("card:") for problem in problems)


def test_distinct_attachment_ids_keep_independent_suffixes(board: board_state.Board) -> None:
    current = reattached_image(board)
    other = replace(current, id=str(uuid.uuid4()), name="other.jpg")
    board.add_attachment("card", other)
    board.set_cover("card", other.id, "bot")
    assert board.verify() == []
    board.remove_attachment("card", other.id, "terry")
    assert board.verify() == []
    assert board.find("card").attachments == [current]


def test_reattached_upload_roundtrip_and_publisher_inventory(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    file = reattached_image(board)
    board.set_cover("card", file.id, "bot")
    path = tmp_path / "board.json"
    assert attachments.write_blob(path, 1, file, PNG)
    orphan = path.parent / "attachments" / "0001" / "unrelated.txt"
    orphan.write_bytes(b"unrelated synthetic bytes")
    board_state.save(board, path)
    restored = board_state.load(path)
    assert restored.verify() == []
    assert restored.to_json() == board.to_json()
    assert api_endpoint._snapshot_paths(path) == [  # noqa: SLF001 -- audited publisher inventory
        str(path),
        str(attachments.blob_path(path, 1, file.id, ".png")),
    ]
    restored.remove_attachment("card", file.id, "bot")
    board_state.save(restored, path)
    assert board_state.load(path).verify() == []
    assert api_endpoint._snapshot_paths(path) == [  # noqa: SLF001 -- retained audited blob
        str(path),
        str(attachments.blob_path(path, 1, file.id, ".png")),
    ]
    assert orphan.read_bytes() == b"unrelated synthetic bytes"


@pytest.mark.parametrize("bad_id", ["../secret", "", "0" * 64])
def test_paths_reject_non_guid_ids(tmp_path: pathlib.Path, bad_id: str) -> None:
    with pytest.raises(ValueError, match="GUID"):
        attachments.blob_path(tmp_path / "board.json", 29, bad_id, ".png")


def test_storage_uses_four_digit_card_numbers(tmp_path: pathlib.Path) -> None:
    aid = str(uuid.uuid4())
    assert (
        attachments.blob_path(tmp_path / "board.json", 29, aid, ".png")
        == tmp_path / "attachments" / "0029" / f"{aid}.png"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../escape"),
        ("name", "folder/file.png"),
        ("size", True),
        ("mediaType", "text/html"),
        ("sha256", "bad digest"),
        ("at", "2026-10-14T13:00:00"),
        ("extra", "unknown"),
    ],
)
def test_malformed_attachment_metadata_is_refused(
    board: board_state.Board, field: str, value: object
) -> None:
    file = attachments.Attachment(
        str(uuid.uuid4()), "picture.png", len(PNG), "image/png", "0" * 64, board_state.now(), "bot"
    )
    raw = file.to_json()
    raw[field] = value
    board.create("card", "Card", "backlog", "bot")
    card = board.find("card").to_json()
    card["attachments"] = [cast("board_state.JsonValue", raw)]
    with pytest.raises(board_state.BoardError, match="attachment"):
        board_state.Item.from_json(card, "card")


@pytest.mark.parametrize(
    "boundary", ["cleanup", "publication", "interruption", "primitive_response", "helper_failure"]
)
def test_upload_retains_bytes_when_snapshot_committed_or_unknown(  # noqa: PLR0915 -- fault matrix
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    accepted = store._board  # noqa: SLF001 -- inspect publication boundary
    locked = board_state.locked
    replace = board_state.pathlib.Path.replace
    replace_with_retry = board_state._replace_with_retry  # noqa: SLF001 -- fault seam
    failure = KeyboardInterrupt("replacement bookkeeping interrupted")

    @contextlib.contextmanager
    def fail_cleanup(target: pathlib.Path) -> Generator[None]:
        with locked(target):
            yield
        raise OSError("transaction cleanup failed")

    def fail_publication(_candidate: board_state.Board) -> None:
        raise MemoryError("memory publication failed")

    def interrupt_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        result = replace(source, target)
        if target == path:
            if boundary == "primitive_response":
                raise OSError("primitive response failed")
            raise failure
        return result

    def fail_after_helper(
        source: pathlib.Path,
        target: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        replace_with_retry(source, target, outcome=outcome)
        raise OSError("replacement helper response failed")

    with monkeypatch.context() as fault:
        if boundary == "cleanup":
            fault.setattr(board_state, "locked", fail_cleanup)
            expected = OSError
        elif boundary == "publication":
            fault.setattr(store, "_publish_candidate", fail_publication)
            expected = MemoryError
        elif boundary == "helper_failure":
            fault.setattr(board_state, "_replace_with_retry", fail_after_helper)
            expected = OSError
        else:
            fault.setattr(board_state.pathlib.Path, "replace", interrupt_replace)
            expected = OSError if boundary == "primitive_response" else KeyboardInterrupt
        with pytest.raises(expected) as caught:
            store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
        if boundary == "interruption":
            assert caught.value is failure
    assert store._board is accepted  # noqa: SLF001 -- failure did not acknowledge publication
    committed = board_state.load(path)
    assert committed.revision == 2
    assert committed.verify() == []
    [file] = committed.find("card").attachments
    blob = attachments.blob_path(path, 1, file.id, attachments.extension(file.name)).read_bytes()
    assert blob == PNG
    assert hashlib.sha256(blob).hexdigest() == file.sha256
    fresh = store.snapshot()
    assert fresh.revision == 2
    assert store.policy.to_json() == fresh.policy.to_json()
    assert len(fresh.find("card").attachments) == 1


def test_upload_failed_replace_preserves_snapshot_and_retains_unknown_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    store.upload(1, "card", "prior.png", base64.b64encode(PNG).decode("ascii"), "bot")
    before = path.read_bytes()
    prior = list((tmp_path / "attachments" / "0001").iterdir())

    replace = board_state.pathlib.Path.replace

    def fail_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        if target == path:
            raise OSError("snapshot replacement refused")
        return replace(source, target)

    monkeypatch.setattr(board_state.pathlib.Path, "replace", fail_replace)
    with pytest.raises(OSError, match="snapshot replacement refused"):
        store.upload(2, "card", "next.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert path.read_bytes() == before
    retained = list((tmp_path / "attachments" / "0001").iterdir())
    assert len(retained) == 2
    assert prior[0] in retained
    assert all(file.read_bytes() == PNG for file in retained)
    assert len(store.snapshot().find("card").attachments) == 1


def test_upload_compensation_failure_preserves_original_exception(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    unlink = board_state.pathlib.Path.unlink
    primary = OSError("snapshot replacement refused")

    fsync = board_state.os.fsync
    calls = 0

    def fail_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary
        fsync(fd)

    def fail_compensation(target: pathlib.Path, *, missing_ok: bool = False) -> None:
        if target.suffix == ".png":
            raise PermissionError("blob cleanup refused")
        unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(board_state.os, "fsync", fail_fsync)
    monkeypatch.setattr(board_state.pathlib.Path, "unlink", fail_compensation)
    with pytest.raises(OSError, match="snapshot replacement refused") as caught:
        store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert caught.value is primary
    assert any("blob cleanup refused" in note for note in caught.value.__notes__)
    assert board_state.load(path).revision == 1


def test_upload_retains_orphan_on_pre_replace_uncertainty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    store = make_store(path)
    store.execute(0, lambda board: board.create("card", "Card", "backlog", "bot"))
    before = path.read_bytes()

    def interrupt_helper(
        _source: pathlib.Path,
        _target: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        assert outcome is not None
        assert outcome.state == "unknown"
        raise KeyboardInterrupt("replacement call interrupted")

    monkeypatch.setattr(board_state, "_replace_with_retry", interrupt_helper)
    with pytest.raises(KeyboardInterrupt, match="replacement call interrupted"):
        store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert path.read_bytes() == before
    [orphan] = (tmp_path / "attachments" / "0001").iterdir()
    assert orphan.read_bytes() == PNG
    assert store.snapshot().revision == 1


@pytest.mark.parametrize("remove", [None, False, 0, 1, "true", "false", [], {}])
def test_attachment_selector_requires_exact_true(api: RunningApi, remove: object) -> None:
    create_card(api)
    code, response = request_json(
        api.base + API + "/cards/card/attachment",
        token=api_endpoint.BROWSER_TOKEN,
        revision=1,
        body={
            "remove": remove,
            "attachmentId": None,
            "name": "file.txt",
            "data": base64.b64encode(b"synthetic file").decode("ascii"),
        },
    )
    assert code == 200
    assert response["revision"] == 2
    saved = board_state.load(api.path)
    assert saved.find("card").attachments[0].id == response["attachment"]["id"]
    assert [event.action for event in saved.find("card").attachment_history] == ["attached"]


@pytest.mark.parametrize("body", [{}, {"attachmentId": None}])
def test_cover_omitted_or_null_clears_existing_cover(
    api: RunningApi, body: dict[str, object]
) -> None:
    create_card(api)
    aid = upload(api, 1)
    code, _ = request_json(
        api.base + API + "/cards/card/cover",
        token=api_endpoint.BROWSER_TOKEN,
        revision=2,
        body={"attachmentId": aid},
    )
    assert code == 200
    code, response = request_json(
        api.base + API + "/cards/card/cover",
        token=api_endpoint.BROWSER_TOKEN,
        revision=3,
        body=body,
    )
    assert code == 200
    assert response["revision"] == 4
    item = board_state.load(api.path).find("card")
    assert item.cover_attachment is None
    assert item.attachments[0].id == aid
    assert item.attachment_history[-1].action == "cover_cleared"
