"""Read-only restore admission, exact references and truthful recovery inventory."""

import hashlib
import json
from dataclasses import replace
from typing import cast
lazy import pathlib

import pytest

from localswim import attachments, board_state
from localswim import restore_candidate as restore
from localswim.restore_paths import PathBoundaryError

AID = "11111111-2222-4333-8444-555555555555"
BLOB = b"backup bytes"


def snapshot(root: pathlib.Path, board: board_state.Board, *, schema: int = 5) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "board.json"
    raw = board.to_json()
    raw["schema"] = schema
    if schema == 4:
        raw.pop("generation")
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def attach(board: board_state.Board, root: pathlib.Path, *, removed: bool = False) -> pathlib.Path:
    board.create("card", "Card", "backlog", "bot")
    file = attachments.Attachment(
        AID,
        "backup.bin",
        len(BLOB),
        "application/octet-stream",
        hashlib.sha256(BLOB).hexdigest(),
        board_state.now(),
        "bot",
    )
    board.add_attachment("card", file)
    path = root / "attachments" / "0001" / f"{AID}.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(BLOB)
    if removed:
        board.remove_attachment("card", AID, "bot")
        board.set_archived("card", archived=True, by="bot")
    return path


@pytest.mark.parametrize("schema", [4, 5])
def test_older_embedded_policy_is_isolated_and_never_allocates_identity(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    source = snapshot(tmp_path / "source", board, schema=schema)
    target = snapshot(tmp_path / "target", board)
    before = target.read_bytes()
    ambient = (board_state.USERS, board_state.RULES)

    def no_uuid() -> None:
        pytest.fail("preflight allocated a UUID")

    monkeypatch.setattr(board_state.uuid, "uuid4", no_uuid)
    result = restore.preflight(
        source, target, checkpoint=tmp_path / "recovery", staging=tmp_path / "stage"
    )
    assert result.source.schema == schema
    assert result.board.to_json()["policy"] == board.to_json()["policy"]
    assert result.target.recovery == "complete"
    assert ambient == (board_state.USERS, board_state.RULES)
    assert target.read_bytes() == before
    assert not (tmp_path / "recovery").exists()
    assert not (tmp_path / "stage").exists()


def test_removed_archived_bytes_are_required_and_extras_are_ignored(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    retained = attach(board, tmp_path / "source", removed=True)
    source = snapshot(tmp_path / "source", board)
    extra = retained.parent / "unreferenced.bin"
    extra.write_bytes(b"unreferenced")
    result = restore.preflight(source, tmp_path / "target" / "board.json")
    assert len(result.source.blobs) == 1
    observed = result.source.blobs[0]
    assert observed.sha256 == hashlib.sha256(BLOB).hexdigest()
    assert not observed.active
    assert not observed.original_digest_available
    assert restore.RETAINED_LIMITATION in result.limitations
    assert result.board.find("card").archived
    retained.unlink()
    target_blob = tmp_path / "target" / observed.relative_path
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(BLOB)
    with pytest.raises(restore.CandidateError, match="missing referenced"):
        restore.preflight(source, tmp_path / "target" / "board.json")


@pytest.mark.parametrize("defect", ["digest", "size", "type"])
def test_active_bytes_authenticate_all_metadata(
    tmp_path: pathlib.Path, board: board_state.Board, defect: str
) -> None:
    path = attach(board, tmp_path / "source")
    if defect == "digest":
        path.write_bytes(b"changed bytes")
    else:
        file = board.find("card").attachments[0]
        board.find("card").attachments[0] = replace(
            file,
            **{
                "size" if defect == "size" else "media_type": len(BLOB) + 1
                if defect == "size"
                else "image/png"
            },
        )
    source = snapshot(tmp_path / "source", board)
    with pytest.raises(restore.CandidateError, match="mismatch active"):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_immutable_target_conflict_including_unreferenced_target_path(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "target" / "board.json"
    target_blob = target.parent / "attachments" / "0001" / f"{AID}.bin"
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(b"newer conflicting immutable bytes")
    with pytest.raises(restore.CandidateError, match="immutable target"):
        restore.preflight(source, target)
    assert target_blob.read_bytes() == b"newer conflicting immutable bytes"
    assert not target.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":4,"schema":5}',
        b"{",
        b'{"x":NaN}',
        b"[" * 2000 + b"]" * 2000,
        b'{"x":' * 130 + b"0" + b"}" * 130,
    ],
)
def test_malformed_duplicate_deep_json_refuses_before_adoption(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    source = tmp_path / "source" / "board.json"
    source.parent.mkdir()
    source.write_bytes(payload)

    def forbidden(*_args: object) -> None:
        pytest.fail("adoption must not run")

    monkeypatch.setattr(board_state.Board, "enforce_dependency_priorities", forbidden)
    with pytest.raises(restore.CandidateError):
        restore.preflight(source, tmp_path / "target" / "board.json")


def floor_board(board: board_state.Board) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P1")
    board.create("leaf", "Leaf", "backlog", "bot", priority="P5")
    board.link("root", "blocks", "leaf", "bot")
    board.find("leaf").priority = "P5"
    board.find("leaf").history = [
        entry for entry in board.find("leaf").history if entry.kind != "priority"
    ]


def test_mixed_invalid_audit_floor_refuses_before_adoption(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor_board(board)
    board.find("leaf").history[0].by = "unknown"
    source = snapshot(tmp_path / "source", board)

    def forbidden(*_args: object) -> None:
        pytest.fail("invalid audit reached adoption")

    monkeypatch.setattr(board_state.Board, "enforce_dependency_priorities", forbidden)
    with pytest.raises(restore.CandidateError, match="audit rejected"):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_valid_floor_only_adopts_using_candidate_actor_and_preserves_source(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    floor_board(board)
    source = snapshot(tmp_path / "source", board)
    before = source.read_bytes()
    result = restore.preflight(source, tmp_path / "target" / "board.json")
    assert result.promoted_cards == ("leaf",)
    assert result.board.find("leaf").priority == "P1"
    assert result.board.find("leaf").history[-1].by == result.board.cli_user
    assert not result.board.verify()
    assert source.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "corrupt", "incomplete", "complete"])
def test_recovery_inventory_distinguishes_raw_and_available_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, kind: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "target" / "board.json"
    if kind == "corrupt":
        target.parent.mkdir()
        target.write_bytes(b"corrupt raw bytes")
    elif kind in {"incomplete", "complete"}:
        attach(board, target.parent)
        snapshot(target.parent, board)
        if kind == "incomplete":
            (target.parent / "attachments" / "0001" / f"{AID}.bin").unlink()
    result = restore.preflight(source, target)
    assert result.target.recovery == ("raw-only" if kind in {"missing", "corrupt"} else kind)
    if target.exists():
        assert result.target.raw_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result.known_next_ticket == (2 if kind in {"complete", "incomplete"} else 1)


@pytest.mark.parametrize("limit", ["snapshot", "references", "total", "file"])
def test_each_restore_cap_is_enforced(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    if limit == "snapshot":
        monkeypatch.setattr(restore, "MAX_SNAPSHOT_BYTES", source.stat().st_size - 1)
    elif limit == "references":
        monkeypatch.setattr(restore, "MAX_REFERENCES", 0)
    elif limit == "total":
        monkeypatch.setattr(restore, "MAX_TOTAL_BYTES", len(BLOB) - 1)
    else:
        monkeypatch.setattr(attachments, "MAX_BYTES", len(BLOB) - 1)
    with pytest.raises((restore.CandidateError, board_state.BoardError, PathBoundaryError)):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_conflicting_retained_extension_refuses_without_reading_bytes(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    attach(board, tmp_path / "source", removed=True)
    event = board.find("card").attachment_history[-1]
    board.find("card").attachment_history[-1] = replace(event, extension=".png")
    with pytest.raises(restore.CandidateError, match="audit rejected"):
        restore.preflight(snapshot(tmp_path / "source", board), tmp_path / "target" / "board.json")


def test_external_policy_and_old_schemas_are_not_guessed(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    source = snapshot(tmp_path / "source", board)
    raw = cast("dict[str, object]", json.loads(source.read_text()))
    raw.pop("policy")
    source.write_text(json.dumps(raw))
    with pytest.raises(restore.CandidateError, match="embedded-policy"):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_legacy_empty_history_is_preserved(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.create("legacy", "Legacy", "backlog", "bot")
    board.find("legacy").history.clear()
    result = restore.preflight(
        snapshot(tmp_path / "source", board), tmp_path / "target" / "board.json"
    )
    assert not result.board.find("legacy").history


def test_checkpoint_inside_attachment_namespace_refuses(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    source = snapshot(tmp_path / "source", board)
    with pytest.raises(PathBoundaryError):
        restore.preflight(
            source,
            tmp_path / "target" / "board.json",
            checkpoint=tmp_path / "target" / "attachments" / "checkpoint",
        )


def test_policy_and_actor_comparison_uses_candidate_context_only(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    target = snapshot(tmp_path / "target", board)
    ambient = (board_state.USERS, board_state.RULES)
    board.users = tuple(replace(user, label="Restored " + user.label) for user in board.users)
    source = snapshot(tmp_path / "source", board)
    raw = cast("board_state.JsonObject", json.loads(source.read_text()))
    policy = cast("board_state.JsonObject", raw["policy"])
    policy["description"] = "Restored embedded policy"
    source.write_text(json.dumps(raw))
    result = restore.preflight(source, target)
    assert result.actors_changed
    assert result.policy_changed
    assert result.board.users[0].label.startswith("Restored ")
    assert ambient == (board_state.USERS, board_state.RULES)


def test_cover_events_deduplicate_and_boundary_caps_accept_equal(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = attach(board, tmp_path / "source")
    image = b"\x89PNG\r\n\x1a\nsynthetic raster bytes"
    file = board.find("card").attachments[0]
    board.find("card").attachments[0] = replace(
        file, size=len(image), media_type="image/png", sha256=hashlib.sha256(image).hexdigest()
    )
    path.write_bytes(image)
    board.set_cover("card", AID, "bot")
    board.set_cover("card", None, "bot")
    board.set_cover("card", AID, "bot")
    source = snapshot(tmp_path / "source", board)
    monkeypatch.setattr(restore, "MAX_REFERENCES", 1)
    monkeypatch.setattr(restore, "MAX_TOTAL_BYTES", len(image))
    monkeypatch.setattr(restore, "MAX_SNAPSHOT_BYTES", source.stat().st_size)
    result = restore.preflight(source, tmp_path / "target" / "board.json")
    assert len(result.source.blobs) == 1
    assert result.board.find("card").cover_attachment == AID
    assert result.source.blobs[0].active


def test_unreadable_current_snapshot_refuses_instead_of_claiming_raw_preservation(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    original = restore.CheckedPath.read_bytes

    def unreadable(self: restore.CheckedPath, max_bytes: int) -> bytes:
        if self.path == target:
            raise PermissionError("synthetic unreadable target")
        return original(self, max_bytes)

    monkeypatch.setattr(restore.CheckedPath, "read_bytes", unreadable)
    with pytest.raises(PermissionError):
        restore.preflight(source, target)


def test_real_retained_file_cap_is_enforced_without_metadata_digest(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    retained = attach(board, tmp_path / "source", removed=True)
    with retained.open("wb") as stream:
        stream.truncate(attachments.MAX_BYTES + 1)
    source = snapshot(tmp_path / "source", board)
    with pytest.raises(PathBoundaryError):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_combined_inventory_reference_cap_and_repeated_collision_reads(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    target_blob = target.parent / "attachments" / "0001" / f"{AID}.bin"
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(BLOB)
    monkeypatch.setattr(restore, "MAX_REFERENCES", 2)
    monkeypatch.setattr(restore, "MAX_TOTAL_BYTES", 2 * len(BLOB))
    assert restore.preflight(source, target).target.recovery == "complete"
    monkeypatch.setattr(restore, "MAX_REFERENCES", 1)
    with pytest.raises(restore.CandidateError, match="distinct referenced"):
        restore.preflight(source, target)


def test_same_id_across_cards_is_not_remapped(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    attach(board, tmp_path / "source")
    board.create("other", "Other", "backlog", "bot")
    board.add_attachment("other", board.find("card").attachments[0])
    source = snapshot(tmp_path / "source", board)
    with pytest.raises(restore.CandidateError, match="conflicting card"):
        restore.preflight(source, tmp_path / "target" / "board.json")


def test_corrupt_current_blob_is_preserved_and_recovery_incomplete(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    source = snapshot(tmp_path / "source", board)
    target_blob = attach(board, tmp_path / "target")
    target = snapshot(tmp_path / "target", board)
    target_blob.write_bytes(b"readable corrupt current attachment")
    result = restore.preflight(source, target)
    assert result.target.status == "valid"
    assert result.target.recovery == "incomplete"
    assert result.target.blobs[0].status == "corrupt"
    assert target_blob.read_bytes() == b"readable corrupt current attachment"


def test_invalid_current_logical_references_preserve_raw_without_following_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(tmp_path / "source", board)
    target_blob = attach(board, tmp_path / "target", removed=True)
    event = board.find("card").attachment_history[-1]
    board.find("card").attachment_history[-1] = replace(event, extension=".png")
    target = snapshot(tmp_path / "target", board)
    original = restore.CheckedPath.read_bytes

    def no_reference_reads(self: restore.CheckedPath, max_bytes: int) -> bytes:
        if "attachments" in self.path.parts:
            pytest.fail("invalid current references granted authority to read bytes")
        return original(self, max_bytes)

    monkeypatch.setattr(restore.CheckedPath, "read_bytes", no_reference_reads)
    result = restore.preflight(source, target)
    assert result.target.status == "corrupt"
    assert result.target.recovery == "raw-only"
    assert not result.target.blobs
    assert result.target.raw_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert target_blob.read_bytes() == BLOB


@pytest.mark.parametrize(
    "role", ["source_attachments", "target_attachments", "checkpoint", "staging"]
)
def test_directory_roles_reject_files_even_without_references(
    tmp_path: pathlib.Path, board: board_state.Board, role: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    checkpoint, staging = tmp_path / "checkpoint", tmp_path / "staging"
    role_path = {
        "source_attachments": source.parent / "attachments",
        "target_attachments": target.parent / "attachments",
        "checkpoint": checkpoint,
        "staging": staging,
    }[role]
    role_path.write_bytes(b"existing file is not a directory role")
    before = {path: path.read_bytes() for path in (source, target, role_path)}
    with pytest.raises(restore.CandidateError, match="require directories"):
        restore.preflight(source, target, checkpoint=checkpoint, staging=staging)
    assert {path: path.read_bytes() for path in before} == before
    assert checkpoint.exists() == (role == "checkpoint")
    assert staging.exists() == (role == "staging")


@pytest.mark.parametrize("schema", [4, 5])
def test_only_legacy_schema4_defaults_omitted_revision(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    source = snapshot(tmp_path / "source", board, schema=schema)
    raw = cast("board_state.JsonObject", json.loads(source.read_text()))
    raw.pop("revision")
    source.write_text(json.dumps(raw))
    before = source.read_bytes()

    def no_uuid() -> None:
        pytest.fail("legacy admission allocated a UUID")

    monkeypatch.setattr(board_state.uuid, "uuid4", no_uuid)
    target = tmp_path / "target" / "board.json"
    if schema == board_state.GENERATION_PREVIOUS_SCHEMA:
        result = restore.preflight(source, target)
        assert result.board.revision == 0
        assert result.source.schema == schema
    else:
        with pytest.raises(board_state.BoardError, match="revision"):
            restore.preflight(source, target)
    assert source.read_bytes() == before
    assert not target.exists()


@pytest.mark.parametrize("schema", [4, 5])
@pytest.mark.parametrize("revision", [None, True, -1])
def test_explicit_invalid_revision_is_never_defaulted(
    tmp_path: pathlib.Path, board: board_state.Board, schema: int, revision: object
) -> None:
    source = snapshot(tmp_path / "source", board, schema=schema)
    raw = cast("dict[str, object]", json.loads(source.read_text()))
    raw["revision"] = revision
    source.write_text(json.dumps(raw))
    before = source.read_bytes()
    with pytest.raises(board_state.BoardError, match="revision"):
        restore.preflight(source, tmp_path / "target" / "board.json")
    assert source.read_bytes() == before
