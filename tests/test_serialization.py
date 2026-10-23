"""JSON validation and atomic snapshot tests."""

import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state
from localswim.write_protocol import _prepare_migration_schema5 as prepare_schema5
from localswim.write_protocol import _serialize_schema5 as serialize_schema5
from tests.support import USERS

if TYPE_CHECKING:
    import pathlib


def saved_board(path: pathlib.Path) -> board_state.Board:
    """Write one representative board and return it."""
    board = board_state.Board(
        project="Round trip",
        revision=7,
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board.create("alpha", "Alpha", "backlog", "bot", detail="Detail")
    board.comment("alpha", "Comment", "bot")
    board_state.save(board, path)
    return board


def editable_json(board: board_state.Board) -> dict[str, Any]:
    """Expose serialization as a deliberately mutable shape for corruption tests."""
    return cast("dict[str, Any]", board.to_json())


def test_save_load_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    original = saved_board(path)
    assert board_state.load(path).to_json() == original.to_json()


@pytest.mark.parametrize("value", [["bot"], {"id": "bot"}, True, 1, "unknown"])
def test_selected_default_owner_refuses_with_field_context_without_rewriting(
    tmp_path: pathlib.Path, value: object
) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    raw["defaultOwner"] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    previous = path.read_bytes()

    with pytest.raises(board_state.BoardError, match="defaultOwner names unknown user"):
        board_state.load(path)

    assert path.read_bytes() == previous


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_falsy_default_owner_preserves_cli_fallback(tmp_path: pathlib.Path, value: object) -> None:
    raw = editable_json(saved_board(tmp_path / "board.json"))
    raw["defaultOwner"] = value
    assert board_state.Board.from_json(raw, "falsy default owner").default_owner == "bot"
    del raw["defaultOwner"]
    assert board_state.Board.from_json(raw, "omitted default owner").default_owner == "bot"


def test_default_owner_accepts_user_without_actor_role(tmp_path: pathlib.Path) -> None:
    raw = editable_json(saved_board(tmp_path / "board.json"))
    raw["users"].append({"id": "Owner", "label": "Owner", "class": "human", "color": "blue"})
    raw["defaultOwner"] = "Owner"
    board = board_state.Board.from_json(raw, "owner only user")
    assert (board.browser_user, board.cli_user, board.default_owner) == ("terry", "bot", "Owner")


@pytest.mark.parametrize("field", ["id", "label", "class", "color"])
@pytest.mark.parametrize("value", [1, 1.5, True, False, None, [], {}, ""])
def test_malformed_user_fields_are_refused_without_rewriting(
    tmp_path: pathlib.Path, field: str, value: object
) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    raw["users"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    previous = path.read_bytes()

    with pytest.raises(
        board_state.BoardError, match=rf"users\[0\]\.{field} must be a nonempty string"
    ):
        board_state.load(path)

    assert path.read_bytes() == previous


@pytest.mark.parametrize("field", ["id", "label", "class", "color"])
def test_missing_user_fields_are_refused(tmp_path: pathlib.Path, field: str) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    del raw["users"][0][field]

    with pytest.raises(
        board_state.BoardError, match=rf"users\[0\]\.{field} must be a nonempty string"
    ):
        board_state.Board.from_json(raw, str(path))


@pytest.mark.parametrize("duplicate", ["terry", 1])
def test_duplicate_and_mixed_type_user_ids_are_refused(
    tmp_path: pathlib.Path, duplicate: object
) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    if duplicate == 1:
        raw["users"].append({"id": "1", "label": "String", "class": "bot", "color": "blue"})
    raw["users"].append({"id": duplicate, "label": "Additional", "class": "bot", "color": "green"})
    index = len(raw["users"]) - 1
    message = "must be a nonempty string" if duplicate == 1 else "repeats id 'terry'"
    path.write_text(json.dumps(raw), encoding="utf-8")
    previous = path.read_bytes()

    with pytest.raises(board_state.BoardError, match=rf"users\[{index}\]\.id {message}"):
        board_state.load(path)

    assert path.read_bytes() == previous


@pytest.mark.parametrize("user_class", ["human ", "BOT", "unknown"])
def test_unknown_user_class_is_refused(tmp_path: pathlib.Path, user_class: str) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    raw["users"][0]["class"] = user_class

    with pytest.raises(board_state.BoardError, match=r"users\[0\]\.class .*want one of"):
        board_state.Board.from_json(raw, str(path))


def test_user_strings_preserve_exact_contents_and_distinct_ids(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    raw["users"] += [
        {"id": user_id, "label": f" Label {user_id} ", "class": "bot", "color": " blue "}
        for user_id in ("Terry", " terry ", "1", " ")
    ]
    raw["defaultOwner"] = "1"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = board_state.load(path)

    assert loaded.to_json()["users"] == raw["users"]
    assert (loaded.browser_user, loaded.cli_user, loaded.default_owner) == ("terry", "bot", "1")


def test_relationship_history_round_trips_after_link_is_removed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.create("beta", "Beta", "backlog", "bot")
    board.link("alpha", "blocked_by", "beta", "bot")
    board.relationship_history[-1].at = "2026-08-24T10:00:00Z"
    board.unlink("alpha", "blocked_by", "beta", "bot")
    board.relationship_history[-1].at = "2026-08-24T10:01:00Z"
    board_state.save(board, path)

    loaded = board_state.load(path)

    assert loaded.links == []
    assert [change.action for change in loaded.relationship_history] == ["linked", "unlinked"]
    assert loaded.to_json() == board.to_json()


def test_parent_history_round_trips_after_parent_is_cleared(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.create("beta", "Beta", "backlog", "bot")
    board.set_parent("beta", "alpha", "bot")
    board.parent_history[-1].at = "2026-08-24T10:00:00Z"
    board.set_parent("beta", None, "bot")
    board.parent_history[-1].at = "2026-08-24T10:01:00Z"
    board_state.save(board, path)

    loaded = board_state.load(path)

    assert loaded.find("beta").parent is None
    assert [(change.frm, change.to) for change in loaded.parent_history] == [
        (None, "alpha"),
        ("alpha", None),
    ]
    assert loaded.to_json() == board.to_json()


def test_subject_history_round_trips_without_subject_values(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.set_subject("alpha", "Renamed alpha", "bot")
    board.subject_history[-1].at = "2026-08-24T10:00:00Z"
    board_state.save(board, path)

    loaded = board_state.load(path)

    assert loaded.subject_history == [
        board_state.SubjectChange(
            at="2026-08-24T10:00:00Z",
            by="bot",
            item_id="alpha",
        )
    ]
    history_json = cast("list[dict[str, Any]]", loaded.to_json()["subjectHistory"])
    assert history_json == [{"at": "2026-08-24T10:00:00Z", "by": "bot", "item": "alpha"}]


def test_detail_history_round_trips_without_description_values(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.set_detail("alpha", "Private replacement detail", "bot")
    board.detail_history[-1].at = "2026-08-24T10:00:00.123456Z"
    board_state.save(board, path)

    loaded = board_state.load(path)

    assert loaded.detail_history == [
        board_state.DetailChange(
            at="2026-08-24T10:00:00.123456Z",
            by="bot",
            item_id="alpha",
        )
    ]
    event = cast("list[dict[str, Any]]", loaded.to_json()["detailHistory"])
    assert event == [{"at": "2026-08-24T10:00:00.123456Z", "by": "bot", "item": "alpha"}]
    assert "Private replacement detail" not in str(event)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("item", "missing", "unknown card"),
        ("at", "", "has no at"),
    ],
)
def test_invalid_detail_history_is_refused(
    tmp_path: pathlib.Path, field: str, value: str, message: str
) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.set_detail("alpha", "Changed", "bot")
    raw = editable_json(board)
    raw["detailHistory"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(board_state.BoardError, match=message):
        board_state.load(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("item", "missing", "unknown card"),
        ("at", "", "has no at"),
    ],
)
def test_invalid_subject_history_is_refused(
    tmp_path: pathlib.Path, field: str, value: str, message: str
) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.set_subject("alpha", "Renamed alpha", "bot")
    raw = editable_json(board)
    raw["subjectHistory"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(board_state.BoardError, match=message):
        board_state.load(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("action", "changed", "unknown relationship action"),
        ("kind", "near", "unknown relationship kind"),
        ("to", "missing", "unknown card"),
    ],
)
def test_invalid_relationship_history_is_refused(
    tmp_path: pathlib.Path, field: str, value: str, message: str
) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.create("beta", "Beta", "backlog", "bot")
    board.link("alpha", "blocks", "beta", "bot")
    raw = editable_json(board)
    raw["relationshipHistory"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(board_state.BoardError, match=message):
        board_state.load(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("from", "missing", "unknown parent"),
        ("from", "alpha", "does not change the parent"),
        ("child", "missing", "unknown child"),
    ],
)
def test_invalid_parent_history_is_refused(
    tmp_path: pathlib.Path, field: str, value: str, message: str
) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    board.create("beta", "Beta", "backlog", "bot")
    board.set_parent("beta", "alpha", "bot")
    raw = editable_json(board)
    raw["parentHistory"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(board_state.BoardError, match=message):
        board_state.load(path)


def test_invalid_board_json_reports_parser_location(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    path.write_text('{\n  "schema": 4,\n  "items": nope\n}\n', encoding="utf-8")

    with pytest.raises(
        board_state.BoardError,
        match=r"board\.json: invalid JSON at line 3, column 12: Expecting value",
    ):
        board_state.load(path)


def test_duplicate_board_json_key_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    path.write_text('{"schema": 4, "schema": 4, "items": []}\n', encoding="utf-8")

    with pytest.raises(
        board_state.BoardError,
        match="duplicate JSON object key 'schema'",
    ):
        board_state.load(path)


def test_legacy_board_without_revision_loads_at_zero(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    del raw["revision"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert board_state.load(path).revision == 0


@pytest.mark.parametrize("revision", [True, False, -1, "1", 1.5, None])
def test_invalid_revision_is_refused(tmp_path: pathlib.Path, revision: object) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    raw["revision"] = revision
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="revision"):
        board_state.load(path)


def test_duplicate_id_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    raw["items"].append(dict(raw["items"][0]))
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="duplicate id"):
        board_state.load(path)


def test_duplicate_ticket_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    duplicate = dict(raw["items"][0])
    duplicate["id"] = "beta"
    raw["items"].append(duplicate)
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="duplicate ticket"):
        board_state.load(path)


def test_rewound_ticket_counter_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    raw["nextTicket"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="counter went backwards"):
        board_state.load(path)


def test_unknown_parent_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    board = saved_board(path)
    raw = editable_json(board)
    raw["items"][0]["parent"] = "missing"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="unknown parent"):
        board_state.load(path)


def test_failed_replace_leaves_previous_snapshot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    original = saved_board(path).to_json()
    changed = board_state.load(path)
    changed.project = "Changed"

    def fail_replace(_tmp: pathlib.Path, _target: pathlib.Path) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr(board_state, "_replace_with_retry", fail_replace)
    with pytest.raises(OSError, match="injected"):
        board_state.save(changed, path)
    assert board_state.load(path).to_json() == original
    assert not list(tmp_path.glob("*.tmp*"))


def test_edit_locks_load_mutate_save_as_one_operation(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    with board_state.edit(path) as board:
        board.project = "Committed"
    assert board_state.load(path).project == "Committed"


def test_save_retries_transient_permission_errors_and_commits_snapshot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    changed = saved_board(path)
    previous = path.read_bytes()
    changed.project = "Retried snapshot"
    expected = (json.dumps(changed.to_json(), indent=2, ensure_ascii=False) + "\n").encode()
    attempts: list[pathlib.Path] = []
    sleeps: list[float] = []
    replace = board_state.pathlib.Path.replace

    def transient_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        attempts.append(source)
        assert target == path
        assert path.read_bytes() == previous
        assert source.read_bytes() == expected
        if len(attempts) < 3:
            raise PermissionError("injected transient refusal")
        return replace(source, target)

    monkeypatch.setattr(board_state, "RENAME_TRIES", 3)
    monkeypatch.setattr(board_state, "RENAME_PAUSE", 0.125)
    monkeypatch.setattr(board_state.time, "sleep", sleeps.append)
    monkeypatch.setattr(board_state.pathlib.Path, "replace", transient_replace)

    board_state.save(changed, path)

    assert len(attempts) == 3
    assert len(set(attempts)) == 1
    assert sleeps == [0.125, 0.125]
    assert path.read_bytes() == expected
    assert board_state.load(path).to_json() == changed.to_json()
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("failure", "expected_attempts", "expected_sleeps"),
    [
        (PermissionError("injected persistent refusal"), 3, [0.125, 0.125]),
        (OSError("injected non-permission refusal"), 1, []),
    ],
    ids=["permission-exhaustion", "other-oserror-immediate"],
)
def test_save_real_replace_refusal_preserves_bytes_and_removes_temporary_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_attempts: int,
    expected_sleeps: list[float],
) -> None:
    path = tmp_path / "board.json"
    changed = saved_board(path)
    previous = path.read_bytes()
    changed.project = "Refused snapshot"
    attempts: list[pathlib.Path] = []
    sleeps: list[float] = []

    def refuse_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        attempts.append(source)
        assert target == path
        assert source.is_file()
        assert path.read_bytes() == previous
        raise failure

    monkeypatch.setattr(board_state, "RENAME_TRIES", 3)
    monkeypatch.setattr(board_state, "RENAME_PAUSE", 0.125)
    monkeypatch.setattr(board_state.time, "sleep", sleeps.append)
    monkeypatch.setattr(board_state.pathlib.Path, "replace", refuse_replace)

    with pytest.raises(type(failure), match="injected") as caught:
        board_state.save(changed, path)

    assert caught.value is failure
    assert len(attempts) == expected_attempts
    assert len(set(attempts)) == 1
    assert sleeps == expected_sleeps
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_save_fsync_failure_preserves_bytes_without_attempting_replace(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    changed = saved_board(path)
    previous = path.read_bytes()
    changed.project = "Unflushed snapshot"
    synced: list[int] = []
    replacements: list[pathlib.Path] = []

    def fail_fsync(descriptor: int) -> None:
        synced.append(descriptor)
        raise OSError("injected fsync failure")

    def track_replace(source: pathlib.Path, _target: pathlib.Path) -> pathlib.Path:
        replacements.append(source)
        return source

    monkeypatch.setattr(board_state.os, "fsync", fail_fsync)
    monkeypatch.setattr(board_state.pathlib.Path, "replace", track_replace)

    with pytest.raises(OSError, match="injected fsync failure"):
        board_state.save(changed, path)

    assert len(synced) == 1
    assert replacements == []
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_check_edges_reports_no_inconsistent_permissions() -> None:
    assert board_state.check_edges() == []


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-19T10:27:01.123456-04:00",
        "2026-08-19T10:27:01-04:00",
        "2026-08-18 12:40 ET",
        "2026-08-18T16:02:45",
        "2026-08-18",
    ],
)
def test_parse_stamp_accepts_every_persisted_shape(raw: str) -> None:
    assert board_state.parse_stamp(raw) > board_state.parse_stamp(None)


def test_parse_stamp_falls_back_for_absent_or_unreadable_values() -> None:
    assert board_state.parse_stamp(None) == board_state.parse_stamp("utter nonsense")


@pytest.mark.parametrize("boundary", ["fsync", "replace", "committed", "unknown"])
def test_save_reports_replacement_outcome_separately_from_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    changed = saved_board(path)
    before = path.read_bytes()
    changed.project = "New project"
    outcome = board_state.SaveOutcome()
    replace = board_state.pathlib.Path.replace

    def fail_fsync(_fd: int) -> None:
        raise OSError("precommit fsync failure")

    def replace_fault(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        if boundary == "replace":
            raise OSError("replace refused")
        replace(source, target)
        raise KeyboardInterrupt("unknown replacement")

    with monkeypatch.context() as fault:
        if boundary == "fsync":
            fault.setattr(board_state.os, "fsync", fail_fsync)
        elif boundary != "committed":
            fault.setattr(board_state.pathlib.Path, "replace", replace_fault)
        if boundary == "committed":
            assert board_state.save(changed, path, outcome=outcome) is None
        else:
            with pytest.raises(KeyboardInterrupt if boundary == "unknown" else OSError):
                board_state.save(changed, path, outcome=outcome)
    expected = "uncommitted" if boundary == "fsync" else "unknown"
    if boundary == "committed":
        expected = "committed"
    assert outcome.state == expected
    assert (path.read_bytes() == before) == (boundary in {"fsync", "replace"})


@pytest.mark.parametrize("prior_outcome", ["committed", "unknown"])
@pytest.mark.parametrize("failure", ["command", "load"])
def test_reused_edit_outcome_resets_before_early_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    prior_outcome: str,
) -> None:
    path = tmp_path / "board.json"
    saved_board(path)
    outcome = board_state.SaveOutcome()
    with board_state.edit(path, outcome=outcome) as board:
        board.project = "Committed edit"
    assert outcome.state == "committed"
    if prior_outcome == "unknown":
        outcome.state = "unknown"
    before = path.read_bytes()

    def fail_load(
        _path: pathlib.Path,
        _policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> board_state.Board:
        del install_context
        raise OSError("injected load refusal")

    with monkeypatch.context() as fault:
        if failure == "load":
            fault.setattr(board_state, "load", fail_load)
        with pytest.raises(OSError, match="injected"), board_state.edit(path, outcome=outcome):
            raise OSError("injected command refusal")
    assert outcome.state == "uncommitted"
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["from", "ownerFrom", "ownerTo", "priorityFrom", "priorityTo"])
@pytest.mark.parametrize("value", [True, False, 1, 1.5, [], {}])
def test_history_optional_types_refuse_without_rewriting(
    tmp_path: pathlib.Path, field: str, value: object
) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    event = dict(raw["items"][0]["history"][0])
    event[field] = value
    raw["items"][0]["history"].append(event)
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(
        board_state.BoardError, match=rf"items\[0\]\.history\[1\].*{field} must be a string or null"
    ):
        board_state.load(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "fields",
    [
        {"ownerTo": "bot", "priorityTo": "P1", "to": ""},
        {"ownerTo": "bot", "to": "backlog"},
        {"priorityTo": "P1", "to": "", "from": "backlog"},
        {"ownerFrom": "bot"},
        {"priorityFrom": "P3"},
        {"ownerTo": "bot", "to": "", "priorityFrom": "P3"},
        {"priorityTo": "P1", "to": "", "ownerFrom": "bot"},
    ],
)
def test_history_mixed_shapes_are_refused(fields: board_state.JsonObject) -> None:
    event: board_state.JsonObject = {"at": "2026-10-22T12:00:00Z", "by": "bot", "to": "backlog"}
    event.update(fields)
    with pytest.raises(board_state.BoardError, match="mixes lane, owner or priority fields"):
        board_state.Change.from_json(event, "history[1]")


@pytest.mark.parametrize("kind", ["lane", "owner", "priority"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_history_shapes_round_trip(kind: str, *, explicit_null: bool) -> None:
    event: board_state.JsonObject = {"at": "2026-10-22T12:00:00Z", "by": "bot"}
    if explicit_null:
        event.update(dict.fromkeys(("from", "ownerFrom", "ownerTo", "priorityFrom", "priorityTo")))
    if kind == "lane":
        event["to"] = "backlog"
    elif kind == "owner":
        event.update({"ownerTo": "terry", "ownerFrom": "bot"})
    else:
        event.update({"priorityTo": "P1", "priorityFrom": "P3"})
    parsed = board_state.Change.from_json(event, "history[0]")
    assert parsed.kind == kind
    assert (
        board_state.Change.from_json(cast("board_state.JsonObject", parsed.to_json()), "round trip")
        == parsed
    )


def test_nested_comment_index_and_empty_strings(tmp_path: pathlib.Path) -> None:
    raw = editable_json(saved_board(tmp_path / "board.json"))
    raw["items"][0]["comments"].append({"at": "", "by": "", "text": ""})
    loaded = board_state.Board.from_json(raw, "test")
    assert loaded.items[0].comments[1] == board_state.Comment("", "", "")
    raw["items"][0]["comments"][1]["text"] = False
    with pytest.raises(board_state.BoardError, match=r"items\[0\]\.comments\[1\].*no text"):
        board_state.Board.from_json(raw, "test")


@pytest.mark.parametrize("field", ["ticket", "nextTicket"])
@pytest.mark.parametrize("value", [True, False])
def test_boolean_ticket_fields_refuse_without_rewriting(
    tmp_path: pathlib.Path, field: str, *, value: bool
) -> None:
    path = tmp_path / "board.json"
    raw = editable_json(saved_board(path))
    target = raw["items"][0] if field == "ticket" else raw
    target[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(board_state.BoardError, match=field):
        board_state.load(path)
    assert path.read_bytes() == before


def test_legacy_numeric_defaults_zero_and_allocator_high_water(tmp_path: pathlib.Path) -> None:
    raw = editable_json(saved_board(tmp_path / "board.json"))
    del raw["revision"]
    del raw["nextTicket"]
    del raw["items"][0]["ticket"]
    raw["items"][0]["history"] = []
    loaded = board_state.Board.from_json(raw, "legacy")
    assert (loaded.revision, loaded.next_ticket, loaded.items[0].ticket) == (0, 1, 0)
    raw["items"][0]["ticket"] = 1000
    loaded = board_state.Board.from_json(raw, "high water")
    loaded.create("beta", "Beta", "backlog", "bot")
    assert (loaded.find("beta").ticket, loaded.next_ticket) == (1001, 1002)
    assert board_state.Board.from_json(loaded.to_json(), "round trip").to_json() == loaded.to_json()


@pytest.mark.parametrize("fault", ["mixed", "unknown_actor", "optional_type"])
def test_invalid_history_admission_preserves_memory_and_disk(
    tmp_path: pathlib.Path, fault: str
) -> None:
    path = tmp_path / "board.json"
    accepted = saved_board(path)
    store = api_endpoint.BoardStore(path)
    policy = store.policy
    raw = editable_json(accepted)
    event: dict[str, object] = {"at": "2026-10-22T12:00:00Z", "by": "bot", "ownerTo": "terry"}
    if fault == "mixed":
        event["priorityTo"] = "P1"
    elif fault == "unknown_actor":
        event["by"] = "unknown"
    else:
        event["ownerFrom"] = False
    raw["items"][0]["history"].append(event)
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(board_state.BoardError, match=r"history\[1\]"):
        store.snapshot()
    assert store._board.to_json() == accepted.to_json()  # noqa: SLF001 -- admission memory invariant
    assert store.policy is policy
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["history", "comments"])
def test_nested_non_object_reports_index(tmp_path: pathlib.Path, field: str) -> None:
    raw = editable_json(saved_board(tmp_path / "board.json"))
    raw["items"][0][field].append(False)
    with pytest.raises(board_state.BoardError, match=rf"items\[0\]\.{field}\[1\] is not an object"):
        board_state.Board.from_json(raw, "test")


def test_private_schema5_stays_out_of_public_load_save(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    original = saved_board(path)
    original_bytes = path.read_bytes()
    snapshot = prepare_schema5(
        original.to_json(),
        "private",
        allocate_generation=lambda: "65e18b13-6c3b-483d-8c50-81e08fc12b56",
    )
    with pytest.raises(AttributeError, match="to_json"):
        board_state.save(cast("board_state.Board", snapshot), path)
    assert path.read_bytes() == original_bytes
    path.write_text(json.dumps(serialize_schema5(snapshot)), encoding="utf-8")
    with pytest.raises(board_state.BoardError, match="this build reads 4"):
        board_state.load(path)
    assert board_state.SCHEMA == 4
    assert original.to_json()["schema"] == 4
