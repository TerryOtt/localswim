"""Private generation/revision values and inert snapshot preparation."""

# ruff: noqa: SLF001 -- explicit private codec is intentionally exercised before activation

import copy
import json
from typing import cast
lazy from collections.abc import Callable

import pytest

from localswim import attachments, board_state
from localswim import write_protocol as protocol

GENERATION = "65e18b13-6c3b-483d-8c50-81e08fc12b56"
OTHER = "a5228746-7aa8-4b77-880c-f98296bc9781"


def prepare_migration(
    raw: board_state.JsonObject, where: str, *, allocate_generation: Callable[[], str]
) -> board_state.Board:
    """Exercise the actual pure admission boundary before test-controlled allocation."""
    legacy = copy.deepcopy(raw)
    legacy["schema"] = 4
    legacy.pop("generation", None)
    candidate = board_state._generation_candidate(legacy, where)
    candidate.generation = allocate_generation()
    candidate.revision += 1
    return candidate


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

    prepared = prepare_migration(raw, "test", allocate_generation=allocate)
    assert calls == 1
    encoded = prepared.to_json()
    expected = dict(before, schema=5, generation=GENERATION, revision=board.revision + 1)
    assert encoded == expected
    assert raw == before
    for _ in range(3):
        prepared = board_state.Board.from_json(encoded, "roundtrip")
        assert prepared.to_json() == expected
    assert calls == 1


def test_initial_and_independent_candidates(board: board_state.Board) -> None:
    raw = board.to_json()
    first = board_state.Board.from_json(dict(raw, generation=GENERATION), "first")
    second = board_state.Board.from_json(dict(raw, generation=OTHER), "second")
    assert first.revision == board.revision
    assert first.generation != second.generation
    assert first is not second
    assert first.to_json()["schema"] == 5


def test_receipt_roundtrip_and_detachment(board: board_state.Board) -> None:
    candidate = board_state.Board.from_json(dict(board.to_json(), generation=GENERATION), "test")
    raw = candidate.to_json()
    raw["restoreReceipt"] = receipt()
    parsed = board_state.Board.from_json(raw, "receipt")
    encoded = parsed.to_json()
    assert encoded == raw
    raw["restoreReceipt"]["planDigest"] = "changed"
    assert parsed.to_json() == encoded
    assert parsed.restore_receipt is not None
    assert parsed.restore_receipt["priorRawDigest"] == "missing"
    raw = encoded
    cast("dict[str, board_state.JsonValue]", raw["restoreReceipt"])["priorRawDigest"] = "c" * 64
    assert board_state.Board.from_json(raw, "digest").to_json() == raw


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
        protocol.RestoreReceipt.from_json(raw)


@pytest.mark.parametrize("field", list(receipt()))
def test_receipt_requires_all_fields(field: str) -> None:
    raw = receipt()
    del raw[field]
    with pytest.raises(board_state.BoardError, match="exactly"):
        protocol.RestoreReceipt.from_json(raw)


@pytest.mark.parametrize("field", ["generation", "revision"])
def test_schema5_requires_complete_identity(board: board_state.Board, field: str) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION)
    del raw[field]
    with pytest.raises(board_state.BoardError):
        board_state.Board.from_json(raw, "missing")


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
    prepared = prepare_migration(raw, "roles", allocate_generation=lambda: GENERATION)
    assert prepared.cli_user == "worker"
    assert prepared.browser_user == "owner"
    assert prepared.default_owner == "worker"
    assert prepared.verify() == []
    assert board_state.USER_CLASS is ambient
    assert board_state._POLICY is ambient_policy
    assert prepared.to_json()["items"] == raw["items"]
    item["owner"] = "intruder"
    with pytest.raises(board_state.BoardError):
        prepare_migration(raw, "bad", allocate_generation=lambda: OTHER)
    assert board_state.USER_CLASS is ambient
    assert board_state._POLICY is ambient_policy


def test_audit_rejected_before_allocation(board: board_state.Board) -> None:
    board.create("alpha", "Alpha", "backlog", "bot")
    raw = board.to_json()
    cast("list[board_state.JsonObject]", raw["items"])[0]["state"] = "completed"

    def forbidden() -> str:
        pytest.fail("invalid admission must not allocate")

    with pytest.raises(board_state.BoardError, match="snapshot rejected"):
        prepare_migration(raw, "bad audit", allocate_generation=forbidden)


@pytest.mark.parametrize("revision", [True, False, -1, 1.5, "1", None])
def test_schema5_refuses_invalid_revision(
    board: board_state.Board, revision: board_state.JsonValue
) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION, revision=revision)
    with pytest.raises(board_state.BoardError, match="revision"):
        board_state.Board.from_json(raw, "invalid revision")


@pytest.mark.parametrize("value", [None, True, [], {}])
def test_present_receipt_must_be_a_valid_record(
    board: board_state.Board, value: board_state.JsonValue
) -> None:
    raw = dict(board.to_json(), schema=5, generation=GENERATION, restoreReceipt=value)
    with pytest.raises(board_state.BoardError, match="restoreReceipt"):
        board_state.Board.from_json(raw, "bad receipt")


def test_migration_adopts_priority_once_before_single_revision_bump(
    board: board_state.Board,
) -> None:
    board.create("urgent", "Urgent", "backlog", "bot", priority="P0")
    board.create("blocked", "Blocked", "backlog", "bot", priority="P5")
    board.links.append(board_state.Link("urgent", "blocks", "blocked"))
    original = board.to_json()
    prepared = prepare_migration(
        original, "priority adoption", allocate_generation=lambda: GENERATION
    )
    assert prepared.revision == board.revision + 1
    assert prepared.find("blocked").priority == "P0"
    assert len(prepared.find("blocked").history) == len(board.find("blocked").history) + 1
    event = prepared.find("blocked").history[-1]
    assert (event.by, event.priority_frm, event.priority_to) == ("bot", "P5", "P0")
    encoded = prepared.to_json()
    assert board_state.Board.from_json(encoded, "again").to_json() == encoded
    assert board.to_json() == original
