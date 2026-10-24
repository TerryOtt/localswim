"""Archive persistence, reversible visibility, and intact audit and relationship data."""

import datetime as dt
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import board_state

if TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize("actor", ["bot", "terry"])
def test_archive_restore_preserves_card_and_relationships(
    board: board_state.Board,
    actor: str,
) -> None:
    board.create("parent", "Parent", "backlog", "bot")
    board.create("focus", "Private subject", "ready_for_work", "bot", detail="Private detail")
    board.create("child", "Child", "backlog", "bot")
    board.set_parent("focus", "parent", "bot")
    board.set_parent("child", "focus", "bot")
    board.link("child", "blocked_by", "focus", "bot")
    board.comment("focus", "Private comment", "terry")
    before = board.to_json()
    item = board.find("focus")

    assert "archived" in board.set_archived("#0002", archived=True, by=actor)
    assert item.archived
    assert item.state == "ready_for_work"
    assert board.find("2") is item
    assert all(item not in lane.items for lane in board.lanes())
    assert any(item in lane.items for lane in board.lanes(include_archived=True))
    assert board.verify() == []
    inspection = board_state.inspect_item(board, "2", include_comments=True)
    assert inspection.item.archived
    assert inspection.detail == "Private detail"
    assert inspection.comments
    assert inspection.comments[0].text == "Private comment"
    assert inspection.parent
    assert inspection.parent.item_id == "parent"
    assert inspection.children[0].item_id == "child"
    assert inspection.relationships[0].item.item_id == "child"
    assert board_state.inspect_item(board, "child").relationships[0].item.archived

    assert "already archived" in board.set_archived("focus", archived=True, by=actor)
    assert len(board.archive_history) == 1
    board.set_archived("0002", archived=False, by=actor)
    assert "already unarchived" in board.set_archived("focus", archived=False, by=actor)
    assert len(board.archive_history) == 2
    assert board.verify() == []
    after = board.to_json()
    archive_history = after.pop("archiveHistory")
    assert after == before
    assert isinstance(archive_history, list)
    assert all(cast("dict[str, Any]", change)["by"] == actor for change in archive_history)
    assert "Private" not in str(archive_history)
    board.create("new", "New", "backlog", "bot")
    assert board.find("new").ticket == 4


def test_archive_does_not_bypass_lane_permissions(board: board_state.Board) -> None:
    board.create("focus", "Focus", "ready_for_work", "bot")
    board.move("focus", "in_progress", "bot")
    board.move("focus", "ready_for_review", "bot")
    board.set_archived("focus", archived=True, by="bot")
    with pytest.raises(board_state.BoardError, match="not to completed"):
        board.move("focus", "completed", "bot")
    board.set_archived("focus", archived=False, by="bot")
    board.move("focus", "completed", "terry")
    board.set_archived("focus", archived=True, by="bot")
    board.set_archived("focus", archived=False, by="bot")
    assert board.find("focus").state == "completed"
    assert board.verify() == []


def test_archive_refusals_leave_data_unchanged(board: board_state.Board) -> None:
    board.create("focus", "Focus", "backlog", "bot")
    before = board.to_json()
    with pytest.raises(board_state.BoardError, match="unauthorized archive actor"):
        board.set_archived("focus", archived=True, by="unknown")
    with pytest.raises(board_state.BoardError, match="no item"):
        board.set_archived("missing", archived=True, by="bot")
    assert board.to_json() == before


def test_archive_round_trip_and_legacy_default(
    board: board_state.Board,
    tmp_path: pathlib.Path,
) -> None:
    board.create("focus", "Focus", "backlog", "bot")
    assert "archiveHistory" not in board.to_json()
    assert not board_state.Board.from_json(board.to_json(), "legacy").find("focus").archived
    board.set_archived("focus", archived=True, by="bot")
    path = tmp_path / "archive.json"
    board_state.save(board, path)
    restored = board_state.load(path)
    assert restored.to_json() == board.to_json()
    assert restored.verify() == []
    restored.set_archived("focus", archived=False, by="terry")
    board_state.save(restored, path)
    assert not board_state.load(path).find("focus").archived
    assert len(board_state.load(path).archive_history) == 2


@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}])
def test_archive_flag_requires_boolean(board: board_state.Board, value: object) -> None:
    board.create("focus", "Focus", "backlog", "bot")
    raw = cast("dict[str, Any]", board.to_json())
    raw["items"][0]["archived"] = value
    with pytest.raises(board_state.BoardError, match="archived must be a boolean"):
        board_state.Board.from_json(raw, "invalid flag")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("at", "yesterday", "unreadable timestamp"),
        ("item", "missing", "unknown card"),
        ("archived", "true", "must be a boolean"),
        ("extra", True, "unknown field"),
    ],
)
def test_malformed_archive_records_are_refused(
    board: board_state.Board,
    field: str,
    value: object,
    message: str,
) -> None:
    board.create("focus", "Focus", "backlog", "bot")
    board.set_archived("focus", archived=True, by="bot")
    raw = cast("dict[str, Any]", board.to_json())
    raw["archiveHistory"][0][field] = value
    with pytest.raises(board_state.BoardError, match=message):
        board_state.Board.from_json(raw, "invalid audit")


def test_archive_audit_refuses_forged_flag_and_broken_chain(board: board_state.Board) -> None:
    board.create("focus", "Focus", "backlog", "bot")
    board.find("focus").archived = True
    assert any("disagrees" in problem for problem in board.verify())
    board.find("focus").archived = False
    board.set_archived("focus", archived=True, by="bot")
    board.archive_history.append(board.archive_history[0])
    assert any("does not change" in problem for problem in board.verify())
    board.archive_history.pop()
    board.archive_history[0] = board_state.ArchiveChange(
        board_state.now(),
        "unknown",
        "focus",
        archived=True,
    )
    assert any("unauthorized actor" in problem for problem in board.verify())


def test_default_queries_hide_archives_before_applying_limits(board: board_state.Board) -> None:
    board.create("hidden", "Focus hidden", "backlog", "bot", priority="P1")
    board.create("visible", "Focus visible", "backlog", "bot", priority="P2")
    board.comment("visible", "Visible comment", "bot")
    board.comment("hidden", "Hidden comment", "bot")
    board.set_archived("hidden", archived=True, by="bot")
    assert [it.item.item_id for it in board_state.inspect_next_items(board, ("backlog",), 1)] == [
        "visible",
    ]
    assert [it.item.item_id for it in board_state.inspect_search_items(board, "Focus")] == [
        "visible"
    ]
    assert board_state.inspect_search_items(board, "#1") == ()
    assert board_state.inspect_search_items(board, "#1", include_archived=True)[0].item.archived
    assert board_state.inspect_next_items(
        board,
        ("backlog",),
        1,
        include_archived=True,
    )[0].item.archived
    assert board_state.newest_comments(board, 1)[0].item_id == "visible"
    assert board_state.newest_comments(board, 1, include_archived=True)[0].item_id == "hidden"


@pytest.mark.parametrize(
    ("raw_at", "public_at", "unarchived_at"),
    [
        ("2026-01-01T12:00:00Z", "2026-01-01T12:00:00Z", "2026-01-01T12:00:01Z"),
        ("2026-01-01T12:00:00+00:00", "2026-01-01T12:00:00Z", "2026-01-01T12:00:01Z"),
        (
            "2026-01-01T08:00:00-04:00",
            "2026-01-01T08:00:00-04:00",
            "2026-01-01T08:00:01-04:00",
        ),
    ],
    ids=["utc-z", "utc-numeric-offset", "non-utc-offset"],
)
def test_archive_activity_includes_hidden_cards_at_inclusive_bounds(
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    raw_at: str,
    public_at: str,
    unarchived_at: str,
) -> None:
    monkeypatch.setattr(board_state, "now", lambda: "2026-01-01T00:00:00Z")
    board.create("focus", "Private subject", "backlog", "bot")
    monkeypatch.setattr(board_state, "now", lambda: raw_at)
    board.set_archived("focus", archived=True, by="bot")
    assert board.archive_history[0].at == raw_at
    stamp = dt.datetime.fromisoformat(raw_at)
    assert stamp == dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
    events = board_state.activity_events(board, stamp, stamp)
    assert [event.kind for event in events] == ["archived"]
    assert events[0].at == raw_at
    assert events[0].instant == stamp
    assert events[0].to_json() == {
        "ticket": 1,
        "id": "focus",
        "kind": "archived",
        "at": public_at,
        "by": "bot",
    }
    later = stamp + dt.timedelta(seconds=1)
    monkeypatch.setattr(board_state, "now", later.isoformat)
    board.set_archived("focus", archived=False, by="terry")
    assert board.archive_history[0].at == raw_at
    assert board.archive_history[1].at == later.isoformat()
    later_events = board_state.activity_events(board, stamp, None)
    assert [event.kind for event in later_events] == [
        "archived",
        "unarchived",
    ]
    assert [event.instant for event in later_events] == [stamp, later]
    assert [event.to_json() for event in later_events] == [
        {"ticket": 1, "id": "focus", "kind": "archived", "at": public_at, "by": "bot"},
        {
            "ticket": 1,
            "id": "focus",
            "kind": "unarchived",
            "at": unarchived_at,
            "by": "terry",
        },
    ]
