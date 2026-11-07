"""Writer feed coverage, privacy, bounds and content-free repeated reads."""

import datetime as dt
import http.client
import json
import os
import pathlib
import urllib.parse
from typing import cast

import pytest

from localswim import activity_feed, api_endpoint, board_state
from tests.test_api import RunningApi
from tests.test_api import api as api


def make_store(path: pathlib.Path, board: board_state.Board) -> api_endpoint.BoardStore:
    board_state.save(board, path)
    return api_endpoint.BoardStore(path)


def query(store: api_endpoint.BoardStore, cursor: str | None = None) -> board_state.JsonObject:
    return store.monitor_activity(activity_feed.MIN_TIME, None, cursor)


def coverage(result: board_state.JsonObject) -> board_state.JsonObject:
    return cast("board_state.JsonObject", result["coverage"])


def test_known_commits_use_cached_sanitized_precise_events(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json", board)
    cursor = str(query(store)["cursor"])
    monkeypatch.setattr(board_state, "now", lambda: "2026-11-07T12:34:56.123456Z")
    store.execute(0, lambda current: current.create("alpha", "Private subject", "backlog", "bot"))
    store.execute(1, lambda current: current.comment("alpha", "Private comment", "terry"))

    def refuse_read(_path: pathlib.Path) -> bytes:
        raise AssertionError("feed queried board content")

    def refuse_load(*_args: object, **_kwargs: object) -> board_state.Board:
        pytest.fail("feed parsed board")

    monkeypatch.setattr(pathlib.Path, "read_bytes", refuse_read)
    monkeypatch.setattr(board_state, "load", refuse_load)
    result = query(store, cursor)
    assert coverage(result) == {"complete": True, "code": "complete"}
    events = cast("list[board_state.JsonObject]", result["events"])
    assert [event["by"] for event in events] == ["bot", "terry"]
    assert events[0]["at"] == "2026-11-07T12:34:56.123456Z"
    assert "Private" not in json.dumps(result)
    assert result["fileIdentity"] == list(activity_feed.fingerprint(store.path))
    assert query(store, cursor) == result


def test_external_same_size_preserved_mtime_never_gets_bot_coverage(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    store = make_store(tmp_path / "board.json", board)
    cursor = str(query(store)["cursor"])
    store.execute(0, lambda current: current.create("alpha", "Alpha", "backlog", "bot"))
    before = store.path.stat()
    data = store.path.read_bytes().replace(b'"Alpha"', b'"Other"')
    store.path.write_bytes(data)
    os.utime(store.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.path.stat().st_size == before.st_size
    assert coverage(query(store, cursor))["complete"] is False
    store.execute(1, lambda current: current.comment("alpha", "bot after external", "bot"))
    assert coverage(query(store, cursor))["complete"] is False
    fresh_cursor = str(query(store)["cursor"])
    store.execute(2, lambda current: current.comment("alpha", "known bot", "bot"))
    assert coverage(query(store, fresh_cursor))["complete"] is True


def test_metadata_failure_cannot_refuse_committed_mutation(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json", board)
    cursor = str(query(store)["cursor"])

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("sensitive error text")

    monkeypatch.setattr(store._monitor_feed, "publish", fail_publish)  # noqa: SLF001 -- failure seam
    _, revision = store.execute(
        0, lambda current: current.create("alpha", "Alpha", "backlog", "bot")
    )
    assert revision == 1
    assert board_state.load(store.path).find("alpha").subject == "Alpha"
    result = query(store, cursor)
    assert coverage(result)["complete"] is False
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["before_load", "during_command"])
def test_external_write_during_transaction_selection_or_command_remains_gap(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    store = make_store(tmp_path / "board.json", board)
    store.execute(0, lambda current: current.create("alpha", "Alpha", "backlog", "bot"))
    cursor = str(query(store)["cursor"])
    original_load = board_state.load
    injected = False

    def external_write() -> None:
        external = original_load(store.path, install_context=False)
        external.comment("alpha", "Private human activity", "terry")
        # Deliberately uncooperative external edit retaining the live revision.
        board_state.save(external, store.path)

    def injecting_load(
        path: pathlib.Path,
        policy: board_state.TransitionPolicy | None = None,
        *,
        install_context: bool = True,
    ) -> board_state.Board:
        nonlocal injected
        if not injected:
            injected = True
            external_write()
        return original_load(path, policy, install_context=install_context)

    def command(current: board_state.Board) -> str:
        if phase == "during_command":
            external_write()
        return current.comment("alpha", "Private bot activity", "bot")

    if phase == "before_load":
        monkeypatch.setattr(board_state, "load", injecting_load)
    _, revision = store.execute(1, command)
    assert revision == 2
    result = query(store, cursor)
    assert coverage(result)["complete"] is False
    assert "Private" not in json.dumps(result)


def test_startup_restart_cursor_empty_and_timestamp_gaps(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    store = make_store(tmp_path / "board.json", board)
    assert coverage(query(store))["complete"] is False
    cursor = str(query(store)["cursor"])
    assert coverage(query(store, cursor))["code"] == "empty"
    store.execute(0, lambda current: current.create("alpha", "Alpha", "backlog", "bot"))
    restarted = api_endpoint.BoardStore(store.path)
    assert coverage(query(restarted, cursor))["complete"] is False
    assert coverage(query(store, "invalid"))["complete"] is False
    start = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    assert coverage(store.monitor_activity(start, None, cursor))["complete"] is False


def test_bounded_retention_and_oversized_commit_fail_open(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json", board)
    feed = store._monitor_feed  # noqa: SLF001 -- retention seam
    cursor = str(query(store)["cursor"])
    monkeypatch.setattr(activity_feed, "MAX_RECORDS", 2)
    for _ in range(3):
        feed.append([{"at": "2026-11-07T12:00:00Z", "by": "bot"}], complete=True)
    assert len(feed.records) == 2
    assert coverage(query(store, cursor))["complete"] is False
    cursor = str(query(store)["cursor"])
    monkeypatch.setattr(activity_feed, "MAX_BYTES", 64)
    feed.append([{"at": "2026-11-07T12:00:00Z", "by": "b" * 100}], complete=True)
    assert feed.bytes <= 64
    assert coverage(query(store, cursor))["complete"] is False


def test_binding_checks_exact_content_and_racing_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fixture"
    path.write_bytes(b"safe")
    assert activity_feed.bind_written(path, b"other") is None
    assert activity_feed.bind_written(path, b"safe") is not None

    def changed_identity(_path: pathlib.Path) -> tuple[int, ...]:
        return (0,)

    monkeypatch.setattr(activity_feed, "fingerprint", changed_identity)
    assert activity_feed.bind_written(path, b"safe") is None


def test_retention_accounts_for_actual_ascii_escaped_http_bytes(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json", board)
    feed = store._monitor_feed  # noqa: SLF001 -- retention seam
    cursor = str(query(store)["cursor"])
    monkeypatch.setattr(activity_feed, "MAX_BYTES", 256)
    event: board_state.JsonObject = {"at": "2026-11-07T12:00:00Z", "by": "人" * 40}
    assert len(json.dumps([event], ensure_ascii=False).encode("utf-8")) < 256
    assert len(json.dumps([event]).encode("utf-8")) > 256
    feed.append([event], complete=True)
    result = query(store, cursor)
    assert result["events"] == []
    assert coverage(result)["complete"] is False
    assert feed.bytes == 2


@pytest.mark.parametrize(
    ("query_string", "credential", "expected_status"),
    [
        ("since=2026-11-07T00%3A00%3A00Z", "cli", 200),
        ("since=2026-11-07T00%3A00%3A00Z", "browser", 401),
        ("since=2026-11-07T00%3A00%3A00Z", "missing", 401),
        ("since=private-sentinel", "cli", 400),
        ("since=2026-11-07T00%3A00%3A00Z&since=duplicate", "cli", 400),
        ("since=2026-11-07T00%3A00%3A00Z&unknown=private-sentinel", "cli", 400),
        ("since=" + "x" * 2050, "cli", 400),
    ],
)
def test_feed_http_authentication_and_bounded_query(
    api: RunningApi, query_string: str, credential: str, expected_status: int
) -> None:
    parsed = urllib.parse.urlsplit(api.base)
    assert parsed.hostname is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    token = api_endpoint.CLI_TOKEN if credential == "cli" else api_endpoint.BROWSER_TOKEN
    headers = {"Authorization": f"Bearer {token}"} if credential != "missing" else {}
    try:
        connection.request(
            "GET", f"{api_endpoint.API_PREFIX}/monitor-activity?{query_string}", headers=headers
        )
        response = connection.getresponse()
        body = response.read(activity_feed.MAX_RESPONSE_BYTES + 1)
        assert response.status == expected_status
        assert b"private-sentinel" not in body
        assert api_endpoint.CLI_TOKEN.encode() not in body
        assert api_endpoint.BROWSER_TOKEN.encode() not in body
        result = json.loads(body)
        if expected_status == 200:
            assert set(result) == {
                "schema",
                "boardIdentity",
                "cursor",
                "coverage",
                "events",
                "range",
                "fileIdentity",
            }
            assert result["range"]["since"] == "2026-11-07T00:00:00+00:00"
    finally:
        connection.close()
