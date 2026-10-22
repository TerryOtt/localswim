"""Private generation/revision values and inert snapshot preparation."""

# ruff: noqa: SLF001 -- explicit private codec is intentionally exercised before activation

import copy
import json
from typing import cast

import pytest

from localswim import attachments, board_state
from localswim import write_protocol as protocol

GENERATION = "65e18b13-6c3b-483d-8c50-81e08fc12b56"
OTHER = "a5228746-7aa8-4b77-880c-f98296bc9781"


def receipt() -> board_state.JsonObject:
    return {
        "operationId": OTHER,
        "planDigest": "a" * 64,
        "sourceSnapshotDigest": "b" * 64,
        "priorRawDigest": "missing",
        "timestamp": "2026-10-22T12:00:00+00:00",
        "authority": "local-maintenance",
    }


@pytest.mark.parametrize("revision", [True, False, -1, 1.5, "1", None])
def test_token_rejects_invalid_revision(revision: object) -> None:
    with pytest.raises(board_state.BoardError, match="revision"):
        protocol.WriteToken(GENERATION, cast("int", revision))


@pytest.mark.parametrize("generation", [GENERATION.upper(), GENERATION.replace("-", ""), "", None])
def test_token_rejects_noncanonical_generation(generation: object) -> None:
    with pytest.raises(board_state.BoardError, match="generation"):
        protocol.WriteToken(cast("str", generation), 0)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        1,
        "",
        '"1"',
        f"generation-{GENERATION}-revision-1",
        f'W/"generation-{GENERATION}-revision-1"',
        f'"generation-{GENERATION}-revision-01"',
        f'"generation-{GENERATION}-revision--1"',
        f'"generation-{GENERATION.upper()}-revision-1"',
        f' "generation-{GENERATION}-revision-1"',
        f'"generation-{GENERATION}-revision-1", "other"',
    ],
)
def test_missing_and_malformed_are_not_stale(raw: object) -> None:
    with pytest.raises(protocol.InvalidWriteToken):
        protocol.WriteToken(GENERATION, 1).require_match(raw)


def test_complete_token_round_trip_and_stale_generation() -> None:
    token = protocol.WriteToken(GENERATION, 7)
    assert protocol.WriteToken.parse(token.format()) == token
    assert token.format() == f'"generation-{GENERATION}-revision-7"'
    token.require_match(token.format())
    for stale in [protocol.WriteToken(OTHER, 7), protocol.WriteToken(GENERATION, 8)]:
        with pytest.raises(protocol.StaleWriteToken, match="state_changed"):
            token.require_match(stale.format())


def test_migration_is_pure_allocates_once_and_roundtrips(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot", detail="Private detail")
    board.comment("alpha", "Private comment", "bot")
    board.set_detail("alpha", "Changed detail", "bot")
    board.set_subject("alpha", "Changed subject", "bot")
    board.assign("alpha", "terry", "bot")
    board.set_priority("alpha", "P2", "bot")
    board.create("beta", "Beta", "backlog", "bot")
    board.set_parent("beta", "alpha", "bot")
    board.set_parent("beta", None, "bot")
    board.link("alpha", "blocked_by", "beta", "bot")
    board.unlink("alpha", "blocked_by", "beta", "bot")
    board.add_attachment(
        "alpha",
        attachments.Attachment(
            OTHER,
            "evidence.txt",
            1,
            "application/octet-stream",
            "a" * 64,
            "2026-10-22T12:00:00Z",
            "bot",
        ),
    )
    board.remove_attachment("alpha", OTHER, "bot")
    board.set_archived("alpha", archived=True, by="bot")
    board.next_ticket = 900
    raw = board.to_json()
    before = copy.deepcopy(raw)
    calls = 0

    def allocate() -> str:
        nonlocal calls
        calls += 1
        return GENERATION

    prepared = protocol._prepare_migration_schema5(raw, "test", allocate_generation=allocate)
    assert calls == 1
    encoded = protocol._serialize_schema5(prepared)
    expected = dict(before, schema=5, generation=GENERATION, revision=board.revision + 1)
    assert encoded == expected
    assert raw == before
    for _ in range(3):
        prepared = protocol._parse_schema5(encoded, "roundtrip")
        assert protocol._serialize_schema5(prepared) == expected
    assert calls == 1


def test_initial_and_independent_candidates(board: board_state.Board) -> None:
    raw = board.to_json()
    first = protocol._prepare_initial_schema5(raw, "first", allocate_generation=lambda: GENERATION)
    second = protocol._prepare_initial_schema5(raw, "second", allocate_generation=lambda: OTHER)
    assert first.board.revision == board.revision
    assert first.generation != second.generation
    assert first.board is not second.board
    assert not hasattr(first, "to_json")


def test_receipt_roundtrip_and_detachment(board: board_state.Board) -> None:
    candidate = protocol._prepare_initial_schema5(
        board.to_json(), "test", allocate_generation=lambda: GENERATION
    )
    raw = protocol._serialize_schema5(candidate)
    raw["restoreReceipt"] = receipt()
    parsed = protocol._parse_schema5(raw, "receipt")
    encoded = protocol._serialize_schema5(parsed)
    assert encoded == raw
    raw["restoreReceipt"]["planDigest"] = "changed"
    assert protocol._serialize_schema5(parsed) == encoded
    assert parsed.restore_receipt is not None
    assert parsed.restore_receipt.prior_raw_digest == "missing"
    raw = encoded
    cast("dict[str, board_state.JsonValue]", raw["restoreReceipt"])["priorRawDigest"] = "c" * 64
    assert protocol._serialize_schema5(protocol._parse_schema5(raw, "digest")) == raw


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operationId", GENERATION.upper()),
        ("operationId", None),
        ("planDigest", "A" * 64),
        ("sourceSnapshotDigest", 1),
        ("priorRawDigest", None),
        ("priorRawDigest", "Missing"),
        ("timestamp", "2026-10-22"),
        ("timestamp", 1),
        ("authority", "http"),
        ("extra", "bad"),
    ],
)
def test_receipt_refuses_malformed_fields(field: str, value: board_state.JsonValue) -> None:
    raw = receipt()
    raw[field] = value
    with pytest.raises(board_state.BoardError):
        protocol._RestoreReceipt.from_json(raw)


@pytest.mark.parametrize("field", list(receipt()))
def test_receipt_requires_all_fields(field: str) -> None:
    raw = receipt()
    del raw[field]
    with pytest.raises(board_state.BoardError, match="exactly"):
        protocol._RestoreReceipt.from_json(raw)


@pytest.mark.parametrize("field", ["generation", "revision"])
def test_schema5_requires_complete_identity(board: board_state.Board, field: str) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION)
    del raw[field]
    with pytest.raises(board_state.BoardError):
        protocol._parse_schema5(raw, "missing")


def test_private_candidates_preserve_custom_roles_and_legacy_history(
    board: board_state.Board,
) -> None:
    board.create("legacy", "Legacy", "backlog", "bot")
    raw = board.to_json()
    # Rename both actors throughout policy and state; no reliance on ambient test users.
    renamed = json.dumps(raw).replace('"terry"', '"owner"').replace('"bot"', '"worker"')
    raw = cast("board_state.JsonObject", json.loads(renamed))
    for user in cast("list[board_state.JsonObject]", raw["users"]):
        if user["class"] == "worker":
            user["class"] = "bot"
    item = cast("list[board_state.JsonObject]", raw["items"])[0]
    item["history"] = []
    ambient = board_state.USER_CLASS
    ambient_policy = board_state._POLICY
    prepared = protocol._prepare_migration_schema5(
        raw, "roles", allocate_generation=lambda: GENERATION
    )
    assert prepared.board.cli_user == "worker"
    assert prepared.board.browser_user == "owner"
    assert prepared.board.default_owner == "worker"
    assert prepared.board.verify() == []
    assert board_state.USER_CLASS is ambient
    assert board_state._POLICY is ambient_policy
    assert protocol._serialize_schema5(prepared)["items"] == raw["items"]
    item["owner"] = "intruder"
    with pytest.raises(board_state.BoardError):
        protocol._prepare_migration_schema5(raw, "bad", allocate_generation=lambda: OTHER)
    assert board_state.USER_CLASS is ambient
    assert board_state._POLICY is ambient_policy


def test_audit_rejected_before_allocation(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    raw = board.to_json()
    cast("list[board_state.JsonObject]", raw["items"])[0]["state"] = "completed"

    def forbidden() -> str:
        pytest.fail("invalid admission must not allocate")

    with pytest.raises(board_state.BoardError, match="snapshot rejected"):
        protocol._prepare_migration_schema5(raw, "bad audit", allocate_generation=forbidden)


@pytest.mark.parametrize("revision", [True, False, -1, 1.5, "1", None])
def test_schema5_refuses_invalid_revision(
    board: board_state.Board, revision: board_state.JsonValue
) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION, revision=revision)
    with pytest.raises(board_state.BoardError, match="revision"):
        protocol._parse_schema5(raw, "invalid revision")


@pytest.mark.parametrize("value", [None, True, [], {}])
def test_present_receipt_must_be_a_valid_record(
    board: board_state.Board, value: board_state.JsonValue
) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION, restoreReceipt=value)
    with pytest.raises(board_state.BoardError, match="restoreReceipt"):
        protocol._parse_schema5(raw, "bad receipt")


def test_migration_adopts_priority_once_before_single_revision_bump(
    board: board_state.Board,
) -> None:
    board.create("urgent", "Urgent", "backlog", "bot", priority="P0")
    board.create("blocked", "Blocked", "backlog", "bot", priority="P5")
    board.links.append(board_state.Link("urgent", "blocks", "blocked"))
    original = board.to_json()
    prepared = protocol._prepare_migration_schema5(
        original, "priority adoption", allocate_generation=lambda: GENERATION
    )
    assert prepared.board.revision == board.revision + 1
    assert prepared.board.find("blocked").priority == "P0"
    assert len(prepared.board.find("blocked").history) == len(board.find("blocked").history) + 1
    event = prepared.board.find("blocked").history[-1]
    assert (event.by, event.priority_frm, event.priority_to) == ("bot", "P5", "P0")
    encoded = protocol._serialize_schema5(prepared)
    assert protocol._serialize_schema5(protocol._parse_schema5(encoded, "again")) == encoded
    assert board.to_json() == original
