"""Owner configuration and committed browser edits cross the real writer boundary."""

# ruff: noqa: SLF001 -- tests assert separation from the board transaction lock

import errno
import json
import pathlib
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest
from click.testing import CliRunner

from localswim import api_endpoint, board_state, cli, notifications, service_ownership
from tests.support import USERS
from tests.test_api import API, RunningApi, request_json
from tests.test_api import api as _api_fixture
from tests.test_store import make_store

api = _api_fixture
TARGET = "00000000-0000-4000-8000-000000000001"
OTHER_TARGET = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def private_settings(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep even disabled preference reads away from the real user's settings."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)


class CaptureNotifications:
    """Capture the immutable handoff without scheduling or delivering a message."""

    def __init__(self) -> None:
        self.events: list[notifications.NotificationEvent] = []

    def submit(self, event: notifications.NotificationEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


def capture(store: api_endpoint.BoardStore) -> CaptureNotifications:
    store.close_notifications()
    sink = CaptureNotifications()
    store._notifications = cast("notifications.NotificationDispatcher", sink)
    return sink


def test_bind_refusal_does_not_start_worker_or_mask_socket_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json")
    monkeypatch.setattr(api_endpoint, "STORE", store)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", store.path)
    started: list[bool] = []
    monkeypatch.setattr(store, "start_notifications", lambda: started.append(True))
    with socket.socket() as listener:
        listener.bind((api_endpoint.HOST, 0))
        listener.listen()
        with pytest.raises(OSError) as refusal:  # noqa: PT011 -- assert portable errno below
            api_endpoint.BoardHttpServer(listener.getsockname(), api_endpoint.Handler)
        assert refusal.value.errno == errno.EADDRINUSE or getattr(
            refusal.value, "winerror", None
        ) in {10013, 10048}
    assert started == []
    assert store._notifications is None


def test_only_successful_browser_mutations_submit_notifications(api: RunningApi) -> None:
    store = api_endpoint.STORE
    assert store is not None
    sink = capture(store)
    for token, revision, subject, expected in (
        (api_endpoint.CLI_TOKEN, 0, "Bot private prose", 200),
        (api_endpoint.BROWSER_TOKEN, 1, "Human private prose", 200),
        (api_endpoint.BROWSER_TOKEN, 0, "Stale private prose", 412),
        (api_endpoint.BROWSER_TOKEN, 2, "", 409),
    ):
        code, _ = request_json(
            api.base + API + "/cards",
            token=token,
            revision=revision,
            body={"subject": subject, "state": "backlog"},
        )
        assert code == expected
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.tickets == (2,)
    assert event.operations == (("Created",),)
    assert event.board_identity == service_ownership.board_identity(api.path)
    assert event.label == "API"
    payload = event.payload()
    assert set(payload) == {
        "boardIdentity",
        "label",
        "since",
        "until",
        "tickets",
        "truncated",
        "operations",
    }
    assert "private prose" not in json.dumps(payload)
    assert board_state.load(api.path).revision == 2


@pytest.mark.parametrize("credential", ["browser", "cli"])
def test_automation_browser_and_human_cli_cannot_notify(
    api: RunningApi,
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
) -> None:
    browser_user, cli_user = "bot", "terry"
    original_store = api_endpoint.STORE
    assert original_store is not None
    original_store.close_notifications()
    board = board_state.Board(
        project="Role regression",
        users=USERS,
        browser_user=browser_user,
        cli_user=cli_user,
        default_owner=cli_user,
    )
    board_state.save(board, api.path)
    store = api_endpoint.BoardStore(api.path)
    monkeypatch.setattr(api_endpoint, "STORE", store)
    sink = capture(store)
    token, expected_actor = (
        (api_endpoint.BROWSER_TOKEN, browser_user)
        if credential == "browser"
        else (api_endpoint.CLI_TOKEN, cli_user)
    )
    url = api.base + API + "/cards"
    body: dict[str, object] = {"subject": "Synthetic role edit", "state": "backlog"}
    code, _ = request_json(url, revision=0, body=body)
    assert code == 401
    code, response = request_json(url, token=token, revision=0, body=body)
    assert code == 200
    assert response["revision"] == 1
    saved = board_state.load(api.path)
    assert saved.find("1").history[0].by == expected_actor
    assert saved.browser_user == browser_user
    assert saved.cli_user == cli_user
    assert sink.events == []


def test_shared_browser_and_cli_identity_is_refused_before_notification_setup(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    board = board_state.Board(
        project="Invalid shared role",
        users=USERS,
        browser_user="terry",
        cli_user="terry",
        default_owner="terry",
    )
    board_state.save(board, path)
    before = path.read_bytes()

    def unexpected_worker(_store: api_endpoint.BoardStore) -> None:
        pytest.fail("invalid shared roles must never start a notification worker")

    monkeypatch.setattr(api_endpoint.BoardStore, "start_notifications", unexpected_worker)
    with pytest.raises(board_state.BoardError, match="MUST be two different actor ids"):
        api_endpoint.BoardStore(path)
    assert path.read_bytes() == before


def test_noop_and_failed_save_never_submit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json")
    sink = capture(store)
    store.execute(0, lambda _board: "nothing changed", notification_actor="terry")
    assert sink.events == []
    before = store.path.read_bytes()

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic unavailable disk")

    monkeypatch.setattr(board_state, "save", fail_save)
    with pytest.raises(OSError, match="synthetic"):
        store.execute(
            1,
            lambda board: board.create("alpha", "Private subject", "backlog", "terry"),
            notification_actor="terry",
        )
    assert sink.events == []
    assert store.path.read_bytes() == before


def test_large_committed_edit_bounds_ticket_metadata(tmp_path: pathlib.Path) -> None:
    store = make_store(tmp_path / "board.json")
    sink = capture(store)

    def create_many(board: board_state.Board) -> str:
        for index in range(notifications.MAX_TICKETS + 2):
            board.create(f"card-{index}", "Private subject", "backlog", "terry")
        return "Created"

    store.execute(0, create_many, notification_actor="terry")
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.tickets == tuple(range(1, notifications.MAX_TICKETS + 1))
    assert all(labels == ("Created",) for labels in event.operations)
    assert len(event.operations) == notifications.MAX_TICKETS
    assert event.truncated is True
    assert "Private subject" not in json.dumps(event.payload())


def test_submit_uses_cached_identity_and_failure_cannot_reverse_commit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path / "board.json")
    sink = capture(store)
    identity = service_ownership.board_identity(store.path)

    def identity_io(_path: pathlib.Path) -> str:
        raise AssertionError("notification handoff must not resolve paths")

    monkeypatch.setattr(service_ownership, "board_identity", identity_io)
    store.execute(
        0,
        lambda board: board.create("one", "One", "backlog", "terry"),
        notification_actor="terry",
    )
    assert len(sink.events) == 1
    assert sink.events[0].board_identity == identity

    def broken_submit(_event: notifications.NotificationEvent) -> None:
        raise RuntimeError("synthetic handoff failure")

    monkeypatch.setattr(sink, "submit", broken_submit)
    result, revision = store.execute(
        1,
        lambda board: board.create("two", "Two", "backlog", "terry"),
        notification_actor="terry",
    )
    assert result
    assert revision == 2
    assert board_state.load(store.path).find("two").ticket == 2


@pytest.mark.parametrize("token_kind", ["missing", "browser", "wrong"])
def test_notification_settings_require_cli_credential(api: RunningApi, token_kind: str) -> None:
    token = {"missing": None, "browser": api_endpoint.BROWSER_TOKEN, "wrong": "wrong"}[token_kind]
    before = api.path.read_bytes()
    commands: list[dict[str, object] | None] = [None, {"action": "target", "threadId": TARGET}]
    for body in commands:
        code, _ = request_json(api.base + API + "/notifications", token=token, body=body)
        assert code == 401
    assert api.path.read_bytes() == before


def test_target_configuration_preserves_enabled_and_board_identity(api: RunningApi) -> None:
    before = api.path.read_bytes()
    url = api.base + API + "/notifications"
    identity = service_ownership.board_identity(api.path)
    commands: list[tuple[dict[str, object] | None, bool, str | None]] = [
        (None, False, None),
        ({"action": "target", "threadId": TARGET}, False, TARGET),
        ({"action": "enable"}, True, TARGET),
        ({"action": "target", "threadId": OTHER_TARGET}, True, OTHER_TARGET),
        ({"action": "disable"}, False, OTHER_TARGET),
        ({"action": "target", "threadId": TARGET}, False, TARGET),
    ]
    for body, enabled, target in commands:
        code, result = request_json(url, token=api_endpoint.CLI_TOKEN, body=body)
        assert code == 200
        assert result == {
            "schema": 1,
            "boardIdentity": identity,
            "threadId": target,
            "enabled": enabled,
        }
    assert api.path.read_bytes() == before
    assert notifications.load_settings(identity).thread_id == TARGET
    assert notifications.load_settings(identity + "-other").thread_id is None


@pytest.mark.parametrize(
    "body",
    [
        {"action": "enable"},
        {"action": "target", "threadId": "not-a-thread"},
        {"action": "target", "threadId": "AAAAAAAA-0000-4000-8000-000000000001"},
        {"action": "target", "threadId": TARGET, "enabled": True},
        {"action": "disable", "boardIdentity": "foreign"},
    ],
)
def test_invalid_configuration_cannot_change_board_or_preferences(
    api: RunningApi, body: dict[str, object]
) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/notifications", token=api_endpoint.CLI_TOKEN, body=body
    )
    assert code == 409
    identity = service_ownership.board_identity(api.path)
    assert notifications.load_settings(identity) == notifications.NotificationSettings(identity)
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("block_phase", ["config", "delivery"])
def test_blocked_worker_does_not_block_board_writer(
    tmp_path: pathlib.Path, block_phase: str
) -> None:
    store = make_store(tmp_path / "board.json")
    identity = service_ownership.board_identity(store.path)
    entered = threading.Event()
    release = threading.Event()

    def block() -> None:
        entered.set()
        assert release.wait(5)

    def load() -> notifications.NotificationSettings:
        if block_phase == "config":
            block()
        return notifications.NotificationSettings(identity, TARGET, enabled=True)

    def deliver(_target: str, _payload: board_state.JsonObject, _stop: threading.Event) -> str:
        block()
        return "accepted"

    dispatcher = notifications.NotificationDispatcher(
        identity, deliver, quiet_seconds=0, settings_loader=load
    )
    store._notifications = dispatcher
    try:
        store.execute(
            0,
            lambda board: board.create("one", "One", "backlog", "terry"),
            notification_actor="terry",
        )
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                store.execute,
                1,
                lambda board: board.create("two", "Two", "backlog", "terry"),
                notification_actor="terry",
            )
            try:
                assert result.result(timeout=2)[1] == 2
            finally:
                release.set()
        assert board_state.load(store.path).find("two").ticket == 2
    finally:
        release.set()
        store.close_notifications()


def test_blocked_configuration_uses_separate_lock(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = api_endpoint.STORE
    assert store is not None
    entered = threading.Event()
    release = threading.Event()
    original = notifications.save_target

    def save(identity: str, target: str | None) -> notifications.NotificationSettings:
        entered.set()
        assert release.wait(5)
        return original(identity, target)

    monkeypatch.setattr(notifications, "save_target", save)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(store.notification_settings, "target", TARGET)
        try:
            assert entered.wait(2)
            code, _ = request_json(
                api.base + API + "/cards",
                token=api_endpoint.CLI_TOKEN,
                revision=0,
                body={"subject": "Concurrent edit", "state": "backlog"},
            )
            assert code == 200
        finally:
            release.set()
        assert result.result(timeout=2).thread_id == TARGET


def test_notification_cli_routes_through_live_configuration(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[board_state.JsonObject | None] = []

    def configured(
        path: pathlib.Path, body: board_state.JsonObject | None
    ) -> board_state.JsonObject:
        calls.append(body)
        assert path == api.path
        code, response = request_json(
            api.base + API + "/notifications",
            token=api_endpoint.CLI_TOKEN,
            body=dict(body) if body is not None else None,
        )
        assert code == 200
        return response

    monkeypatch.setattr(board_state, "configure_remote_notifications", configured)
    before = api.path.read_bytes()
    runner = CliRunner()
    for arguments in (
        ["target", TARGET],
        ["enable"],
        ["target", OTHER_TARGET],
        ["status", "--json"],
        ["disable"],
    ):
        result = runner.invoke(cli.cli, [str(api.path), "notifications", *arguments])
        assert result.exit_code == 0, result.output
        if arguments[0] == "status":
            assert json.loads(result.output)["enabled"] is True
            assert json.loads(result.output)["threadId"] == OTHER_TARGET
    assert calls == [
        {"action": "target", "threadId": TARGET},
        {"action": "enable"},
        {"action": "target", "threadId": OTHER_TARGET},
        None,
        {"action": "disable"},
    ]
    assert api.path.read_bytes() == before


def test_operations_cover_contents_status_lane_creation_and_related_tickets(
    tmp_path: pathlib.Path,
) -> None:
    store = make_store(tmp_path / "board.json")
    sink = capture(store)

    def create_and_edit(board: board_state.Board) -> str:
        board.create("one", "Private subject", "backlog", "terry")
        board.comment("one", "Private comment", "terry")
        return board.move("one", "ready_for_work", "terry")

    store.execute(0, create_and_edit, notification_actor="terry")
    assert sink.events[-1].operations == (("Created", "Comment added", "Moved lanes"),)
    store.execute(
        1,
        lambda board: board.set_archived("one", archived=True, by="terry"),
        notification_actor="terry",
    )
    assert sink.events[-1].operations == (("Archived",),)
    store.execute(
        2,
        lambda board: board.set_archived("one", archived=False, by="terry"),
        notification_actor="terry",
    )
    assert sink.events[-1].operations == (("Restored",),)
    store.execute(
        3,
        lambda board: board.create("two", "Other private", "backlog", "terry"),
        notification_actor="terry",
    )
    store.execute(
        4, lambda board: board.link("one", "relates_to", "two", "terry"), notification_actor="terry"
    )
    assert sink.events[-1].tickets == (1, 2)
    assert sink.events[-1].operations == (("Contents modified",), ("Contents modified",))
    assert "Private" not in json.dumps(sink.events[-1].payload())
    count = len(sink.events)
    store.execute(
        5, lambda board: board.comment("one", "Bot private", "bot"), notification_actor="bot"
    )
    assert len(sink.events) == count


def test_approval_hint_requires_successful_exact_command(tmp_path: pathlib.Path) -> None:
    from tests.test_lane_approval import ready, tokens

    store = make_store(tmp_path / "board.json")
    store.execute(0, lambda board: (ready(board), "seed")[1], notification_actor="bot")
    sink = capture(store)
    api_endpoint.execute_http_command(store, 1, ["cards", "first", "approve"], {}, "terry")
    assert sink.events[-1].operations == (("Reviewed & approved",),)
    count = len(sink.events)
    with pytest.raises(api_endpoint.RevisionConflict):
        api_endpoint.execute_http_command(store, 1, ["cards", "second", "approve"], {}, "terry")
    assert len(sink.events) == count
    # Copying the standard prose and moving manually must not impersonate the action.
    store.execute(
        2,
        lambda board: board.comment("second", "Reviewed & approved, moving to Completed.", "terry"),
        notification_actor="terry",
    )
    assert sink.events[-1].operations == (("Comment added",),)
    store.execute(
        3, lambda board: board.move("second", "completed", "terry"), notification_actor="terry"
    )
    assert sink.events[-1].operations == (("Moved lanes",),)
    # The now-empty eligible lane is a no-op and cannot fabricate an approval.
    result = store.approve_lane(4, "ready_for_review", "terry", {})
    assert result["approvedCount"] == 0
    assert len(sink.events) == count + 2
    assert tokens(board_state.load(store.path)) == {}


def test_bulk_approval_hint_covers_only_approved_cards(tmp_path: pathlib.Path) -> None:
    from tests.test_lane_approval import ready, tokens

    store = make_store(tmp_path / "board.json")
    store.execute(0, lambda board: (ready(board), "seed")[1], notification_actor="bot")
    sink = capture(store)
    cards = tokens(board_state.load(store.path))
    response = store.approve_lane(1, "ready_for_review", "terry", cards)
    assert response["approvedCount"] == 2
    assert sink.events[-1].tickets == (1, 2)
    assert sink.events[-1].operations == (("Reviewed & approved",),) * 2
    store.execute(
        2, lambda board: board.comment("first", "Later review", "terry"), notification_actor="terry"
    )
    assert sink.events[-1].operations == (("Comment added",),)


def test_ticket_reordering_reports_committed_ticket_numbers(tmp_path: pathlib.Path) -> None:
    store = make_store(tmp_path / "board.json")

    def seed(board: board_state.Board) -> str:
        board.create("leaf", "Private leaf", "backlog", "bot")
        board.create("root", "Private root", "backlog", "bot")
        return board.link("root", "blocks", "leaf", "bot")

    store.execute(0, seed, notification_actor="bot")
    sink = capture(store)

    def reorder(board: board_state.Board) -> str:
        board.reorder_tickets("backlog", ["leaf", "root"], "terry")
        return "reordered"

    store.execute(1, reorder, notification_actor="terry")
    assert len(sink.events) == 1
    assert sink.events[0].tickets == (1, 2)
    assert sink.events[0].operations == (("Ticket numbers reordered",),) * 2
    assert board_state.load(store.path).find("root").ticket == 1
