"""Attachment upload, bounded storage, covers, removal, CLI, and Git durability."""

import base64
import datetime
import json
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING, cast

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
    events = board_state.activity_events(
        saved, datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC), None
    )
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

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected snapshot failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        store.upload(1, "card", "picture.png", base64.b64encode(PNG).decode("ascii"), "bot")
    assert path.read_bytes() == before
    assert list((tmp_path / "attachments" / "0001").iterdir()) == []
    assert store.snapshot().find("card").attachments == []


def test_missing_or_changed_bytes_are_not_served(api: RunningApi) -> None:
    create_card(api)
    aid = upload(api, 1)
    path = api.path.parent / "attachments" / "0001" / f"{aid}.png"
    path.write_bytes(b"tampered")
    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(api.base + api_endpoint.attachment_url("card", aid), timeout=5)
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
    assert_cli(served_board, "card", "attachment", "cover", "1", "--clear")
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
