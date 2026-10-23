"""Workspace discovery, routing, and isolated live-board writer contracts."""

import base64
import contextlib
import datetime as dt
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, attachments, board_state, service_ownership, workspace
from tests.support import USERS
from tests.test_api import request_json
from tests.test_attachments import PNG
from tests.test_cli import run_cli

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Generator, Iterable, Iterator
    from typing import SupportsIndex

API = board_state.API_PREFIX


class _FiniteResponseStream(io.BytesIO):
    """Track explicit reads, with short reads even below the HTTP framing layer."""

    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.requests: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None
        assert size > 0
        self.requests.append(size)
        return super().read(min(size, 3))


class _FiniteResponseSocket:
    """A finite raw HTTP response without a real upstream listener."""

    def __init__(self, raw: bytes) -> None:
        self.stream = _FiniteResponseStream(raw)

    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return self.stream


def _finite_response(headers: bytes, body: bytes, status: int = 200) -> http.client.HTTPResponse:
    response = http.client.HTTPResponse(
        cast(
            "Any",
            _FiniteResponseSocket(
                f"HTTP/1.1 {status} Result\r\n".encode() + headers + b"\r\n" + body
            ),
        )
    )
    response.begin()
    return response


def test_upstream_entity_numeric_contract() -> None:
    assert workspace.MAX_UPSTREAM_ENTITY_BYTES == 100663296
    assert workspace.MAX_DECORATED_BOARD_BYTES == 100663296
    assert workspace.MAX_ATTACHMENT_ENTITY_BYTES == 10485760
    assert service_ownership.MAX_METADATA_BYTES == 65536


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("status", [200, 201, 400, 401, 412, 500])
@pytest.mark.parametrize(
    ("tail", "attachment"),
    [
        ("board", False),
        ("board/", False),
        ("status", False),
        ("cards/card/attachments/id", True),
        ("cards/card/attachments/id/", True),
        ("cards/a%2Fb/%61ttachments/id", True),
        ("%63ards/card/attachments/id", False),
        ("cards//attachments/id", False),
        ("cards/card/attachments/", False),
        ("cards/card/attachment/id", False),
        ("cards/card/attachments/id/extra", False),
        ("image.png", False),
    ],
)
def test_upstream_entity_route_classes(
    tail: str, status: int, *, write: bool, attachment: bool
) -> None:
    expected = (
        workspace.MAX_ATTACHMENT_ENTITY_BYTES
        if attachment and status == 200 and not write
        else workspace.MAX_UPSTREAM_ENTITY_BYTES
    )
    assert workspace._upstream_entity_limit(tail, status, write=write) == expected  # noqa: SLF001


@pytest.mark.parametrize("size", [15, 16, 17])
@pytest.mark.parametrize("framing", ["eof", "length", "chunked"])
def test_upstream_entity_inclusive_and_bounded(
    size: int, framing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"x" * size
    headers = b""
    body = payload
    if framing == "length":
        headers = f"Content-Length: {size}\r\n".encode()
    if framing == "chunked":
        headers = b"Transfer-Encoding: chunked\r\n"
        body = f"{size:x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
    response = _finite_response(headers, body)
    stream = cast("_FiniteResponseStream", response.fp)
    original = response.read
    requests: list[int] = []
    received = 0

    def short_read(amount: int) -> bytes:
        nonlocal received
        assert 0 < amount <= 17 - received
        requests.append(amount)
        chunk = original(min(amount, 3))
        received += len(chunk)
        return chunk

    monkeypatch.setattr(response, "read", short_read)
    if size > 16:
        with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
            workspace._read_upstream_entity(response, 16)  # noqa: SLF001
        if framing == "length":
            assert not requests
    else:
        assert workspace._read_upstream_entity(response, 16) == payload  # noqa: SLF001
    assert received <= 17
    assert all(0 < amount <= 17 for amount in stream.requests)
    response.close()


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        (b"Content-Length: 2\r\n", b"{}", b"{}"),
        (b"Content-Length: \t0002 \t\r\n", b"{}", b"{}"),
        (b"Content-Length: 3\r\n", b"{}", None),
        (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}", None),
        (b"Content-Length: 2\r\nContent-Length: 3\r\n", b"{}", None),
        (b"Content-Length: 2, 2\r\n", b"{}", None),
        (b"Content-Length: +2\r\n", b"{}", None),
        (b"Content-Length: -2\r\n", b"{}", None),
        (b"Content-Length: 2x\r\n", b"{}", None),
        (b"Content-Length: \r\n", b"{}", None),
        (b"Content-Length: " + b"9" * 1000 + b"\r\n", b"{}", None),
        (b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", b"{}", None),
        (b"Transfer-Encoding: gzip\r\n", b"{}", None),
        (b"Transfer-Encoding: gzip, chunked\r\n", b"{}", None),
        (b"Transfer-Encoding: chunked;foo=bar\r\n", b"{}", None),
        (b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n", b"{}", None),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n", b"{}"),
        (b"Transfer-Encoding: Chunked \t\r\n", b"2\r\n{}\r\n0\r\n\r\n", b"{}"),
        (b"Transfer-Encoding: chunked\r\n", b'2;x="a b"\r\n{}\r\n0\r\nX-Note: ok\r\n\r\n', b"{}"),
        (b"Transfer-Encoding: chunked\r\n", b"Z\r\n{}\r\n0\r\n\r\n", None),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\n{", None),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\n{}XX0\r\n\r\n", None),
        (b"Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n", None),
        (b"Transfer-Encoding: chunked\r\n", b"0\r\n", None),
        (b"Transfer-Encoding: chunked\r\n", b"0\r\ninvalid trailer\r\n\r\n", None),
        (b"Content-Encoding: identity\r\n", b"{}", b"{}"),
        (b"Content-Encoding: IDENTITY \t\r\n", b"{}", b"{}"),
        (b"Content-Encoding: gzip\r\n", b"{}", None),
        (b"Content-Encoding: identity, identity\r\n", b"{}", None),
        (b"Content-Encoding: identity\r\nContent-Encoding: identity\r\n", b"{}", None),
    ],
)
def test_upstream_entity_raw_framing(headers: bytes, body: bytes, expected: bytes | None) -> None:
    response = _finite_response(headers, body)
    if expected is None:
        with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
            workspace._read_upstream_entity(response, 16)  # noqa: SLF001
    else:
        assert workspace._read_upstream_entity(response, 16) == expected  # noqa: SLF001
    response.close()


@pytest.mark.parametrize("status", [200, 204, 304])
@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Length : 2\r\n",
        b"Content-Length: 2\r\nContent-Length : 999\r\n",
        b"Content-Length: 2\r\nMalformed header\r\nTransfer-Encoding: chunked\r\n",
    ],
)
def test_upstream_header_parser_defects_refused_before_read(
    headers: bytes, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _finite_response(headers, b"{}sentinel-private-body", status) as response:
        assert response.headers.defects

        def forbidden_read(_amount: int) -> bytes:
            pytest.fail("defective headers must be refused before any body read")

        monkeypatch.setattr(response, "read", forbidden_read)
        with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
            workspace._read_upstream_entity(response, 16)  # noqa: SLF001


@pytest.mark.parametrize(
    ("extension", "accepted"),
    [
        (b'"\x80\xff"', True),
        (b'"\\\x80\\\xff"', True),
        (b'"a\\"b"', True),
        (b'"\x00"', False),
        (b'"\\\x00"', False),
        (b'"\x7f"', False),
        (b'"\\\x7f"', False),
        (b'"\r\n"', False),
        (b'"unfinished', False),
        (b'"dangling\\"', False),
    ],
)
def test_upstream_chunk_extension_quoted_octets(extension: bytes, *, accepted: bool) -> None:
    body = b"2;x=" + extension + b"\r\n{}\r\n0\r\n\r\n"
    with _finite_response(b"Transfer-Encoding: chunked\r\n", body) as response:
        if accepted:
            assert workspace._read_upstream_entity(response, 16) == b"{}"  # noqa: SLF001
        else:
            with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
                workspace._read_upstream_entity(response, 16)  # noqa: SLF001


@pytest.mark.parametrize("status", [204, 304])
@pytest.mark.parametrize("length", [b"", b"Content-Length: 9999999999999999999999\r\n"])
def test_upstream_entity_no_body_semantics(
    status: int, length: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _finite_response(length, b"unframed surplus", status)

    def forbidden_read(_amount: int) -> bytes:
        pytest.fail("no-body response must not read an entity")

    monkeypatch.setattr(response, "read", forbidden_read)
    assert workspace._read_upstream_entity(response, 16) == b""  # noqa: SLF001
    response.close()


@pytest.mark.parametrize("status", [204, 304])
def test_upstream_entity_no_body_still_validates_headers(status: int) -> None:
    with (
        _finite_response(b"Content-Length: 0\r\nContent-Length: 0\r\n", b"", status) as response,
        pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED),
    ):
        workspace._read_upstream_entity(response, 16)  # noqa: SLF001


def test_upstream_entity_underdeclared_surplus_is_not_visible() -> None:
    response = _finite_response(b"Content-Length: 2\r\n", b"{}hidden surplus")
    assert workspace._read_upstream_entity(response, 16) == b"{}"  # noqa: SLF001
    response.close()
    # The framing layer hides surplus; the proxy must close, never pool, the connection.


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_upstream_entity_preserves_json_unicode_encoding(encoding: str) -> None:
    payload = '{"name":"é"}'.encode(encoding)
    with _finite_response(f"Content-Length: {len(payload)}\r\n".encode(), payload) as response:
        blob = workspace._read_upstream_entity(response, 256)  # noqa: SLF001
    handler = object.__new__(workspace.WorkspaceHandler)
    decorated = handler._decorate_board(
        blob, "board", 200, "key", workspace.Endpoint(1, "unused", "local CSS")
    )
    assert json.loads(decorated) == {"name": "é", "userCSS": "local CSS"}


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_decorated_board_final_escaped_output_limit(
    monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    key = urllib.parse.quote("long 'é/😀" * 20, safe="")
    css = "body::before {content: 'é😀\\\"';}" * 3
    url = API + "/cards/card/attachments/id"
    value = {"name": "é'😀", "files": [{"url": url}] * 8, "other": url}
    source = json.dumps(value, ensure_ascii=False).encode()
    expected = json.dumps(
        {
            **value,
            "files": [{"url": workspace.BOARD_PREFIX + key + url.removeprefix(API)}] * 8,
            "userCSS": css,
        }
    ).encode()
    assert len(source) < len(expected) - 1
    monkeypatch.setattr(workspace, "MAX_DECORATED_BOARD_BYTES", len(expected) + offset)
    handler = object.__new__(workspace.WorkspaceHandler)
    endpoint = workspace.Endpoint(1, "unused", css)
    if offset < 0:
        with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
            handler._decorate_board(source, "board", 200, key, endpoint)
    else:
        assert handler._decorate_board(source, "board", 200, key, endpoint) == expected


def test_decorated_encoder_stops_before_excess_append_or_later_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace, "MAX_DECORATED_BOARD_BYTES", 4)
    monkeypatch.setattr(workspace, "UPSTREAM_READ_BYTES", 2)
    appended: list[bytes] = []

    class Buffer(bytearray):
        def extend(self, value: Iterable[SupportsIndex]) -> None:
            appended.append(bytes(value))
            super().extend(value)

    def tokens(_self: json.JSONEncoder, _value: object) -> Iterator[str]:
        yield "abcdef"
        pytest.fail("encoder must stop consuming tokens at refusal")

    def forbidden_dumps(*_args: object, **_kwargs: object) -> str:
        pytest.fail("decoration must not serialize the complete output first")

    monkeypatch.setattr(workspace, "bytearray", Buffer, raising=False)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", tokens)
    monkeypatch.setattr(json, "dumps", forbidden_dumps)
    with pytest.raises(ValueError, match=workspace.UPSTREAM_RESPONSE_REJECTED):
        workspace._encode_decorated_board({})  # noqa: SLF001
    assert appended == [b"ab", b"cd"]


@pytest.mark.parametrize("value", [None, True, 1, "é😀", [1, {"url": API + "/cards/id"}]])
def test_decorated_board_preserves_nondictionary_json(value: board_state.JsonValue) -> None:
    handler = object.__new__(workspace.WorkspaceHandler)
    expected = workspace._rebase(value, workspace.BOARD_PREFIX + "key")  # noqa: SLF001
    assert (
        handler._decorate_board(
            json.dumps(value).encode(), "board", 200, "key", workspace.Endpoint(1, "", "CSS")
        )
        == json.dumps(expected).encode()
    )


def _proxy_payload(  # noqa: PLR0913 -- explicit upstream response controls
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    *,
    tail: str = "board",
    status: int = 200,
    write: bool = False,
    declared: int | None = None,
) -> list[tuple[bytes, str, int, dict[str, str] | None]]:
    """Exercise the handler's response boundary without a real upstream listener."""
    response = _finite_response(
        f"Content-Length: {len(payload) if declared is None else declared}\r\n"
        "Content-Type: application/json\r\n".encode(),
        payload,
        status,
    )

    def request(*_args: object, **_kwargs: object) -> None:
        pass

    connection = SimpleNamespace(
        request=request,
        getresponse=lambda: response,
        close=lambda: None,
    )

    def connect(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return connection

    def resolve(_key: str) -> tuple[None, workspace.Endpoint]:
        return None, workspace.Endpoint(1, "secret", "CSS")

    monkeypatch.setattr(http.client, "HTTPConnection", connect)
    handler = object.__new__(workspace.WorkspaceHandler)
    handler.server = cast(
        "Any",
        SimpleNamespace(
            workspace=SimpleNamespace(
                token="gateway-token",  # noqa: S106 -- isolated test credential
                resolve_endpoint=resolve,
            )
        ),
    )
    handler.headers = http.client.HTTPMessage()
    handler.headers["Authorization"] = "Bearer gateway-token"
    handler.headers["Content-Length"] = "2"
    handler.rfile = io.BytesIO(b"{}")
    handler.path = workspace.BOARD_PREFIX + "key/" + tail
    sent: list[tuple[bytes, str, int, dict[str, str] | None]] = []

    def send(
        blob: bytes, kind: str, code: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        sent.append((blob, kind, code, headers))

    monkeypatch.setattr(handler, "_send", send)
    handler._proxy(urllib.parse.urlsplit(handler.path).path, write=write)
    return sent


@pytest.mark.parametrize(
    ("tail", "status", "write", "decorated"),
    [
        ("board", 200, False, True),
        ("board?refresh=1", 200, False, True),
        ("board?download=1", 200, False, True),
        ("board", 200, True, False),
        ("board", 401, False, False),
        ("board", 412, False, False),
        ("board/", 200, False, False),
        ("boards", 200, False, False),
    ],
)
def test_decoration_get_exact_board_200_only(
    monkeypatch: pytest.MonkeyPatch, tail: str, status: int, *, write: bool, decorated: bool
) -> None:
    payload = b'{"name": "example"} '
    sent = _proxy_payload(monkeypatch, payload, tail=tail, status=status, write=write)
    assert len(sent) == 1
    assert sent[0][2] == status
    expected = b'{"name": "example", "userCSS": "CSS"}' if decorated else payload
    assert sent[0][0] == expected


@pytest.mark.parametrize(
    "failure",
    [
        "unicode",
        "json",
        "decode-depth",
        "rebase",
        "encode-depth",
        "encode-unicode",
        "encode-value",
        "size",
    ],
)
def test_decoration_failure_is_one_sanitized_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    payload = b'{"private-sentinel": "secret"}'
    if failure == "unicode":
        payload = b'"private-sentinel\xff"'
    if failure == "json":
        payload = b'{"private-sentinel":'
    if failure == "decode-depth":
        payload = b"[" * 2000 + b'"private-sentinel"' + b"]" * 2000
    if failure == "rebase":
        deep: board_state.JsonValue = "private-sentinel"
        for _ in range(2000):
            deep = [deep]

        def parsed(_blob: bytes) -> board_state.JsonValue:
            return deep

        monkeypatch.setattr(json, "loads", parsed)
    if failure.startswith("encode-"):

        class FailingEncoder(json.JSONEncoder):
            def iterencode(self, o: object, _one_shot: bool = False) -> Iterator[str]:  # noqa: ARG002, FBT001, FBT002 -- base protocol
                yield "partial private-sentinel"
                if failure == "encode-unicode":
                    raise UnicodeEncodeError("utf-8", "private-sentinel", 0, 1, "secret")
                if failure == "encode-value":
                    raise ValueError("private-sentinel secret")
                raise RecursionError("private-sentinel secret")

        monkeypatch.setattr(json, "JSONEncoder", FailingEncoder)
    if failure == "size":
        monkeypatch.setattr(workspace, "MAX_DECORATED_BOARD_BYTES", 2)
    sent = _proxy_payload(monkeypatch, payload)
    assert len(sent) == 1
    assert sent[0][:3] == (b'{"error": "board service response rejected"}', "application/json", 502)
    captured = capsys.readouterr()
    assert "private-sentinel" not in captured.out + captured.err + sent[0][0].decode()


def test_attachment_real_cap_plus_one_refused_before_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_read(_self: _FiniteResponseStream, _size: int | None = -1) -> bytes:
        pytest.fail("oversized declaration must be refused before reading")

    monkeypatch.setattr(_FiniteResponseStream, "read", forbidden_read)
    sent = _proxy_payload(
        monkeypatch,
        b"",
        tail="cards/card/attachments/id",
        declared=10485761,
    )
    assert len(sent) == 1
    assert sent[0][:3] == (b'{"error": "board service response rejected"}', "application/json", 502)


@pytest.mark.parametrize("route", ["browser-session", "status"])
@pytest.mark.parametrize(
    "payload",
    [b"x" * 65537, b"[" * 2000 + b"]" * 2000, b"{"],
    ids=["oversized", "deep", "malformed"],
)
def test_workspace_bad_session_and_status_metadata_is_unavailable(
    tmp_path: pathlib.Path, route: str, payload: bytes
) -> None:
    path = tmp_path / "board.json"
    if len(payload) > service_ownership.MAX_METADATA_BYTES:
        payload = json.dumps(
            {
                "boardIdentity": workspace.path_identity(path),
                "token": "browser-capability",
                "userCSS": "",
                "build": "legacy-build",
                "codeStale": False,
                "padding": "x" * 65537,
            }
        ).encode()

    class Handler(workspace.http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- HTTP handler protocol
            self.send_response(200 if self.path.endswith(route) else 404)
            self.end_headers()
            self.wfile.write(payload if self.path.endswith(route) else b"{}")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- base protocol
            pass

    server = workspace.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    descriptor = board_state.service_descriptor_path(path)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text(
        json.dumps(
            {
                "schema": 1,
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "board": str(path),
                "token": "cli-capability",
            }
        ),
        encoding="utf-8",
    )
    instance = workspace.Workspace(tmp_path)
    try:
        assert instance._existing(path) is None  # noqa: SLF001 -- metadata boundary oracle
    finally:
        instance.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def make_board(path: pathlib.Path, project: str, *, alternate: bool = False) -> None:
    """Create an isolated persisted board with distinguishable users and labels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    users = USERS
    policy = board_state.TransitionPolicy.load()
    if alternate:
        users = (
            board_state.User("terry", "Alternate human", board_state.HUMAN, "#992244"),
            board_state.User("bot", "Alternate bot", board_state.BOT, "#229944"),
        )
        document = policy.to_json()
        lanes = cast("list[board_state.JsonValue]", document["lanes"])
        cast("board_state.JsonObject", lanes[0])["label"] = "Alternate backlog"
        policy = board_state.TransitionPolicy.from_json(document, str(path))
    board = board_state.Board(
        project=project,
        users=users,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
        policy=policy,
    )
    board_state.save(board, path)


def board_route(base: str, key: str, suffix: str = "board") -> str:
    """Encode a relative board key as exactly one routing segment."""
    return base + API + "/workspace/boards/" + urllib.parse.quote(key, safe="") + "/" + suffix


@dataclass(frozen=True)
class RunningWorkspace:
    """One gateway over two independent scratch boards."""

    base: str
    root: pathlib.Path
    first: pathlib.Path
    second: pathlib.Path
    instance: workspace.Workspace


@pytest.fixture
def gateway(tmp_path: pathlib.Path) -> Iterator[RunningWorkspace]:
    """Bind an ephemeral gateway; all board children belong to this fixture."""
    root = tmp_path / "boards"
    first = root / "alpha" / "board.json"
    second = root / "beta" / "board.json"
    make_board(first, "Alpha")
    make_board(second, "Beta", alternate=True)
    instance = workspace.Workspace(root)
    server = workspace.WorkspaceServer((api_endpoint.HOST, 0), instance)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningWorkspace(
            f"http://127.0.0.1:{server.server_address[1]}", root, first, second, instance
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        instance.close()


def catalog_boards(instance: workspace.Workspace) -> list[dict[str, Any]]:
    """Narrow the JSON catalog at the test boundary."""
    return cast("list[dict[str, Any]]", instance.catalog()["boards"])


@pytest.mark.parametrize(
    ("tail", "write", "upstream_status", "size", "expected"),
    [
        ("board", False, 200, 31, 200),
        ("board?refresh=1", False, 200, 32, 200),
        ("board", False, 200, 33, 502),
        ("status", False, 200, 31, 200),
        ("status?refresh=1", False, 200, 32, 200),
        ("status", False, 200, 33, 502),
        ("status", False, 200, 65537, 200),
        ("board?malformed=1", False, 200, 2, 502),
        ("cards/card/attachments/id", False, 200, 15, 200),
        ("cards/card/%61ttachments/id/?download=1", False, 200, 16, 200),
        ("cards/card/attachments/id", False, 200, 17, 502),
        ("cards/card/attachments/id", False, 404, 32, 404),
        ("cards/card/attachments/id", True, 200, 32, 200),
        ("board", True, 200, 32, 200),
        ("board/", False, 200, 32, 200),
        ("image.png", False, 200, 32, 200),
        ("%63ards/card/attachments/id", False, 200, 32, 200),
        ("cards/card/attachment/id", False, 200, 32, 200),
    ],
)
def test_gateway_entity_route_boundaries(  # noqa: PLR0913 -- explicit route/status/size matrix
    gateway: RunningWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tail: str,
    upstream_status: int,
    size: int,
    expected: int,
    write: bool,
) -> None:
    monkeypatch.setattr(workspace, "MAX_UPSTREAM_ENTITY_BYTES", 65537 if size == 65537 else 32)
    monkeypatch.setattr(workspace, "MAX_ATTACHMENT_ENTITY_BYTES", 16)
    payload = b"{}" + b" " * (size - 2)
    if "malformed" in tail:
        payload = b"\xff\xff"

    class Handler(workspace.http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- HTTP protocol
            if self.command == "POST":
                self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(upstream_status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET  # noqa: N815 -- HTTP handler protocol

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = workspace.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    closed: list[int] = []
    original_close = http.client.HTTPConnection.close

    def close(connection: http.client.HTTPConnection) -> None:
        if connection.port == server.server_address[1]:
            closed.append(connection.port)
        original_close(connection)

    monkeypatch.setattr(http.client.HTTPConnection, "close", close)

    def existing(_path: pathlib.Path) -> workspace.Endpoint:
        return workspace.Endpoint(server.server_address[1], "upstream-secret", "")

    monkeypatch.setattr(gateway.instance, "_existing", existing)
    try:
        code, result = request_json(
            board_route(gateway.base, "alpha/board", tail),
            token=gateway.instance.token if write else None,
            revision=0 if write else None,
            body={} if write else None,
        )
        assert code == expected
        assert closed
        if expected == 502:
            assert result == {"error": workspace.UPSTREAM_RESPONSE_REJECTED}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "failure",
    ["oversized", "headers", "malformed-name", "hidden-framing", "disconnect", "exception"],
)
def test_gateway_committed_post_response_failure_is_uncertain(
    gateway: RunningWorkspace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    persisted = gateway.root / "command-receipt.txt"
    persisted.write_text("0", encoding="utf-8")
    observed: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(workspace, "MAX_UPSTREAM_ENTITY_BYTES", 32)

    class Handler(workspace.http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 -- HTTP protocol
            self.rfile.read(int(self.headers["Content-Length"]))
            observed.append((self.headers.get("Authorization"), self.headers.get("If-Match")))
            persisted.write_text(str(int(persisted.read_text()) + 1), encoding="utf-8")
            if failure in ("disconnect", "exception"):
                return
            self.send_response(200)
            if failure == "headers":
                self.send_header("Content-Length", "2")
                self.send_header("Content-Length", "2")
            elif failure == "malformed-name":
                self.send_header("Content-Length", "2")
                self.send_header("Content-Length ", "999")
            elif failure == "hidden-framing":
                self.send_header("Content-Length", "2")
                self.send_header("Malformed header\r\nTransfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", "33")
            self.end_headers()
            self.wfile.write(b"sentinel-private-body")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = workspace.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def existing(_path: pathlib.Path) -> workspace.Endpoint:
        return workspace.Endpoint(server.server_address[1], "upstream-secret", "")

    monkeypatch.setattr(gateway.instance, "_existing", existing)
    original_response = http.client.HTTPConnection.getresponse

    if failure == "exception":
        # Consume the committed request's disconnect, then expose a private transport exception.
        def private_exception(connection: http.client.HTTPConnection) -> http.client.HTTPResponse:
            try:
                return original_response(connection)
            except http.client.HTTPException as exc:
                raise OSError("sentinel-exception upstream-secret") from exc

        monkeypatch.setattr(http.client.HTTPConnection, "getresponse", private_exception)
    try:
        code, result = request_json(
            board_route(gateway.base, "alpha/board", "cards/card/attachments/id"),
            token=gateway.instance.token,
            revision=7,
            body={"subject": "one dispatch"},
        )
        assert code == 502
        assert result == {"error": workspace.POST_OUTCOME_UNKNOWN}
        assert persisted.read_text() == "1"
        assert observed == [("Bearer upstream-secret", '"revision-7"')]
        assert "sentinel" not in json.dumps(result)
        assert "upstream-secret" not in json.dumps(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_discovery_excludes_configuration_and_attachment_files(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "boards"
    good = root / "project" / "board.json"
    make_board(good, "Real board")
    (root / "config.json").write_text('{"port":8794}', encoding="utf-8")
    make_board(root / "attachments" / "0001" / "copied-board.json", "Attachment content")
    (root / "project" / "permissions.json").write_text('{"schema":4}', encoding="utf-8")
    instance = workspace.Workspace(root)
    try:
        rows = catalog_boards(instance)
        assert [row["key"] for row in rows] == ["project/board"]
        assert rows[0]["project"] == "Real board"
        assert rows[0]["counts"] == {"blocked": 0, "needsHuman": 0, "review": 0}
    finally:
        instance.close()


def test_discovery_isolates_nested_and_invalid_candidates(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Control")
    depth = sys.getrecursionlimit() * 2
    (tmp_path / "nested.json").write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
    (tmp_path / "invalid.json").write_bytes(b"\xff")
    (tmp_path / "syntax.json").write_text("{", encoding="utf-8")
    (tmp_path / "unrelated.json").write_text('{"schema":4}', encoding="utf-8")
    make_board(tmp_path / ".hidden" / "board.json", "Hidden")
    make_board(tmp_path / ".git" / "board.json", "Git")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "synthetic", "")

    def endpoint(_candidate: workspace._Candidate) -> workspace.Endpoint:
        return expected

    monkeypatch.setattr(instance, "_endpoint_under_gate", endpoint)
    assert [row["project"] for row in catalog_boards(instance)] == ["Control"]
    assert instance.board_path("board") == path.resolve()
    assert instance.resolve_endpoint("board") == (path.resolve(), expected)
    result = instance.update_preferences(0, {"action": "pin", "key": "board"})
    assert result.pinned_boards == ("board",)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_discovery_candidate_exact_byte_boundary(tmp_path: pathlib.Path, extra: int) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Boundary")
    original = path.read_bytes()
    ceiling = workspace.MAX_CANDIDATE_BYTES
    path.write_bytes(original + b" " * (ceiling + extra - len(original)))
    rows = catalog_boards(workspace.Workspace(tmp_path))
    assert len(rows) == (0 if extra > 0 else 1)
    if rows:
        assert rows[0]["project"] == "Boundary"


def test_discovery_growth_read_is_limited_to_ceiling_plus_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 200)
    monkeypatch.setattr(workspace, "MAX_CANDIDATE_BYTES", 100)
    original_stat = os.fstat
    reads: list[int] = []
    original_fdopen = os.fdopen

    def small_stat(fd: int) -> os.stat_result:
        values = list(original_stat(fd))
        values[6] = 1
        return os.stat_result(values)

    @contextlib.contextmanager
    def recording_open(fd: int, mode: str) -> Generator[Any]:
        with original_fdopen(fd, mode) as stream:

            class Reader:
                def fileno(self) -> int:
                    return stream.fileno()

                def read(self, size: int) -> bytes:
                    reads.append(size)
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(os, "fstat", small_stat)
    monkeypatch.setattr(os, "fdopen", recording_open)
    assert workspace._read_candidate(path) is None  # noqa: SLF001 -- reader boundary probe
    assert reads == [101]


def test_proxy_resolution_scans_and_reads_each_candidate_once(
    gateway: RunningWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[None] = []
    reads: list[pathlib.Path] = []
    original_paths = gateway.instance._paths  # noqa: SLF001 -- deterministic scan counter
    original_reader = workspace._read_candidate  # noqa: SLF001 -- candidate read counter

    def scan() -> dict[str, workspace._Candidate]:
        scans.append(None)
        return original_paths()

    def read(path: pathlib.Path) -> workspace._Candidate | None:
        reads.append(path)
        return original_reader(path)

    monkeypatch.setattr(gateway.instance, "_paths", scan)
    monkeypatch.setattr(workspace, "_read_candidate", read)
    status, _ = request_json(board_route(gateway.base, "alpha/board", "browser-session"))
    assert status == 200
    assert len(scans) == 1
    assert sorted(reads) == sorted([gateway.first, gateway.second])


@pytest.mark.parametrize("change", ["remove", "replace", "directory"])
def test_request_resolution_refuses_changed_candidates(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic filesystem race

    def scan_then_change() -> dict[str, workspace._Candidate]:
        candidates = original_paths()
        path.unlink()
        if change == "replace":
            make_board(path, "Replacement with a different size")
        elif change == "directory":
            path.mkdir()
        return candidates

    monkeypatch.setattr(instance, "_paths", scan_then_change)
    with pytest.raises((ValueError, OSError)):
        instance.resolve_endpoint("board")
    assert not instance._children  # noqa: SLF001 -- refusal must precede child launch


@pytest.mark.parametrize("change", ["none", "remove", "replace", "symlink"])
def test_launch_gate_rechecks_candidate_before_reuse_or_launch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = tmp_path / "root"
    path = root / "board.json"
    make_board(path, "Inside")
    outside = tmp_path / "outside.json"
    make_board(outside, "Outside")
    instance = workspace.Workspace(root)
    scans = 0
    reuse_calls = 0
    original_paths = instance._paths  # noqa: SLF001 -- no second scan after gate wait
    expected = workspace.Endpoint(12345, "synthetic", "")

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        return original_paths()

    @contextlib.contextmanager
    def delayed_gate(target: pathlib.Path) -> Generator[object]:
        if change != "none":
            target.unlink()
            if change == "replace":
                target.write_text("{}", encoding="utf-8")
            elif change == "symlink":
                try:
                    target.symlink_to(outside)
                except OSError:
                    pytest.skip("creating symlinks requires unavailable OS privilege")
        yield object()

    def reuse(_path: pathlib.Path) -> workspace.Endpoint:
        nonlocal reuse_calls
        reuse_calls += 1
        return expected

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("changed candidate reached process launch")

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(instance, "_launch_locked", delayed_gate)
    monkeypatch.setattr(instance, "_existing", reuse)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    if change == "none":
        assert instance.resolve_endpoint("board") == (path.resolve(), expected)
        assert reuse_calls == 1
    else:
        with pytest.raises((OSError, ValueError)):
            instance.resolve_endpoint("board")
        assert reuse_calls == 0
    assert scans == (2 if change == "replace" else 1)


@pytest.mark.parametrize("boundary", ["busy", "upgrade", "ready", "before_launch"])
def test_endpoint_wait_paths_retain_candidate_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    attempts = 0

    def replace() -> None:
        path.unlink()
        path.write_text("{}", encoding="utf-8")

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        nonlocal attempts
        attempts += 1
        if boundary == "upgrade":
            replace()
            raise workspace.ServiceUpgradePendingError("synthetic upgrade")
        if boundary == "ready":
            replace()
            return workspace.Endpoint(12345, "synthetic", "")
        return None

    def busy(_path: pathlib.Path) -> bool:
        replace()
        return boundary == "busy"

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("changed candidate reached process launch")

    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0)
    with pytest.raises((OSError, ValueError)):
        instance.resolve_endpoint("board")
    assert attempts == 1


@pytest.mark.parametrize("boundary", ["scan", "gate", "busy", "upgrade", "ready"])
def test_atomic_save_recovers_through_fresh_discovery(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
) -> None:
    path = tmp_path / "private-board-key.json"
    make_board(path, "Private project prose")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "private-capability", "private-css")
    original_paths = instance._paths  # noqa: SLF001 -- deterministic scan boundary
    scans = 0
    changed = False

    def replace() -> None:
        nonlocal changed
        if not changed:
            changed = True
            make_board(path, "After atomic replacement")

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        found = original_paths()
        if boundary == "scan":
            replace()
        return found

    @contextlib.contextmanager
    def gate(_path: pathlib.Path) -> Generator[object]:
        if boundary == "gate":
            replace()
        yield object()

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        if boundary in {"busy", "upgrade"} and not changed:
            if boundary == "upgrade":
                replace()
                raise workspace.ServiceUpgradePendingError("synthetic upgrade")
            return None
        if boundary == "ready":
            replace()
        return expected

    def busy(_path: pathlib.Path) -> bool:
        replace()
        return True

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("existing writer recovery reached process launch")

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(instance, "_launch_locked", gate)
    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    monkeypatch.setattr(workspace, "START_POLL_SECONDS", 0)
    assert instance.resolve_endpoint("private-board-key") == (path.resolve(), expected)
    assert scans == 2
    assert capsys.readouterr().err == (
        "Workspace discovery retry (attempt 1/2).\nWorkspace discovery recovered (attempt 1/2).\n"
    )


@pytest.mark.parametrize("boundary", ["before_launch", "after_launch", "child_ready"])
def test_atomic_save_around_launch_retains_child_and_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    expected = workspace.Endpoint(12345, "synthetic", "")
    launches = 0
    gates = 0

    class Child:
        @staticmethod
        def poll() -> None:
            return None

    child = Child()

    def launch(*_args: object, **_kwargs: object) -> Child:
        nonlocal launches
        launches += 1
        if boundary == "after_launch":
            make_board(path, "After child launch")
        return child

    @contextlib.contextmanager
    def gate(target: pathlib.Path) -> Generator[service_ownership.LaunchGate]:
        nonlocal gates
        gates += 1
        yield service_ownership.LaunchGate(target)

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        if launches and boundary == "child_ready":
            make_board(path, "At child readiness")
        return expected if launches else None

    def busy(_path: pathlib.Path) -> bool:
        if boundary == "before_launch":
            make_board(path, "Before child launch")
        return False

    def environment(_gate: service_ownership.LaunchGate) -> dict[str, str]:
        return {}

    monkeypatch.setattr(instance, "_launch_locked", gate)
    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(service_ownership.LaunchGate, "child_environment", environment)
    monkeypatch.setattr(subprocess, "Popen", launch)
    assert instance.resolve_endpoint("board") == (path.resolve(), expected)
    assert launches == gates == 1
    assert instance._children[path] is child  # noqa: SLF001 -- original launch retained


@pytest.mark.parametrize("replacement", ["malformed", "foreign", "invalid_board", "oversized"])
def test_discovery_refresh_rejects_invalid_replacement(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic scan boundary
    changed = False

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal changed
        found = original_paths()
        if not changed:
            changed = True
            blobs = {
                "malformed": b"{",
                "foreign": b'{"schema":1,"project":"X","items":[],"policy":{}}',
                "invalid_board": b'{"schema":4,"project":"X","items":[],"policy":{}}',
                "oversized": b" " * (workspace.MAX_CANDIDATE_BYTES + 1),
            }
            temporary = path.with_name(".replacement")
            temporary.write_bytes(blobs[replacement])
            temporary.replace(path)
        return found

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid replacement reached process launch")

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    with pytest.raises(ValueError, match=r"unknown workspace board|policy"):
        instance.resolve_endpoint("board")


def test_discovery_refresh_churn_is_bounded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic churn boundary
    scans = 0

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        found = original_paths()
        make_board(path, f"Replacement {scans}")
        return found

    monkeypatch.setattr(instance, "_paths", scan)
    with pytest.raises(ValueError, match="changed during discovery"):
        instance.resolve_endpoint("board")
    assert scans == 1 + workspace.MAX_DISCOVERY_REFRESHES
    assert "Workspace discovery exhausted (attempt 2/2)." in capsys.readouterr().err
    assert not instance._children  # noqa: SLF001 -- churn cannot reach launch


@pytest.mark.parametrize("churn", [False, True])
def test_discovery_refresh_rechecks_after_full_validation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, churn: bool
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic validation boundary
    original_load = board_state.load
    scans = 0
    validations = 0
    expected = workspace.Endpoint(12345, "synthetic", "")

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        found = original_paths()
        if scans == 1:
            make_board(path, "After initial scan")
        return found

    def load(target: pathlib.Path, *, install_context: bool = True) -> board_state.Board:
        nonlocal validations
        board = original_load(target, install_context=install_context)
        if not install_context:
            validations += 1
            if churn or validations == 1:
                make_board(path, f"Replaced after validation {validations}")
        return board

    def reuse(_candidate: workspace._Candidate) -> workspace.Endpoint:
        return expected

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(board_state, "load", load)
    monkeypatch.setattr(instance, "_endpoint_under_gate", reuse)
    if churn:
        with pytest.raises(ValueError, match="changed during discovery"):
            instance.resolve_endpoint("board")
    else:
        assert instance.resolve_endpoint("board") == (path.resolve(), expected)
    assert scans == 3
    assert validations == 2


def test_discovery_refresh_rejects_escape_after_fresh_scan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "root" / "board.json"
    outside = tmp_path / "outside.json"
    make_board(path, "Before")
    make_board(outside, "Outside")
    instance = workspace.Workspace(path.parent)
    original_paths = instance._paths  # noqa: SLF001 -- deterministic fresh-scan escape
    scans = 0

    def scan() -> dict[str, workspace._Candidate]:
        nonlocal scans
        scans += 1
        found = original_paths()
        if scans == 1:
            make_board(path, "Ordinary replacement")
        else:
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("creating symlinks requires unavailable OS privilege")
        return found

    def refuse_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("escaped replacement reached process launch")

    monkeypatch.setattr(instance, "_paths", scan)
    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    with pytest.raises(ValueError, match=r"outside the workspace|path changed"):
        instance.resolve_endpoint("board")
    assert scans == 2


@pytest.mark.parametrize("disconnect", [False, True])
def test_discovery_recovery_never_replays_proxy_post(
    gateway: RunningWorkspace, monkeypatch: pytest.MonkeyPatch, *, disconnect: bool
) -> None:
    posts = 0

    class Handler(workspace.http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 -- HTTP handler protocol
            nonlocal posts
            posts += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            if not disconnect:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = workspace.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    changed = False

    def existing(_path: pathlib.Path) -> workspace.Endpoint:
        nonlocal changed
        if not changed:
            changed = True
            make_board(gateway.first, "Atomic save before dispatch")
        return workspace.Endpoint(server.server_address[1], "upstream-capability", "")

    monkeypatch.setattr(gateway.instance, "_existing", existing)
    try:
        code, _ = request_json(
            board_route(gateway.base, "alpha/board", "cards"),
            token=gateway.instance.token,
            revision=0,
            body={"id": "one", "subject": "One mutation", "state": "backlog"},
        )
        assert code == (502 if disconnect else 200)
        assert posts == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_fresh_requests_observe_removal_and_replacement(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before")
    instance = workspace.Workspace(tmp_path)
    assert catalog_boards(instance)[0]["project"] == "Before"
    path.unlink()
    assert catalog_boards(instance) == []
    with pytest.raises(ValueError, match="unknown"):
        instance.board_path("board")
    make_board(path, "After")
    assert catalog_boards(instance)[0]["project"] == "After"


def test_catalog_contains_recursion_in_recognized_board_validation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Recognized")

    def too_deep(_path: pathlib.Path) -> board_state.Board:
        raise RecursionError("synthetic validation recursion")

    monkeypatch.setattr(board_state, "load", too_deep)
    instance = workspace.Workspace(tmp_path)
    row = catalog_boards(instance)[0]
    assert row["key"] == "board"
    assert "error" in row
    with pytest.raises(ValueError, match="nesting"):
        instance.resolve_endpoint("board")


def test_nonregular_candidates_are_not_opened(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fifo.json"
    path.touch()
    original_stat = type(path).stat

    def nonregular(target: pathlib.Path, *, follow_symlinks: bool = True) -> os.stat_result:
        values = list(original_stat(target, follow_symlinks=follow_symlinks))
        if target == path:
            values[0] = stat.S_IFIFO
        return os.stat_result(values)

    monkeypatch.setattr(type(path), "stat", nonregular)

    def fail_open(*_args: object) -> int:
        pytest.fail("nonregular file opened")

    monkeypatch.setattr(os, "open", fail_open)
    assert workspace._read_candidate(path) is None  # noqa: SLF001 -- reader boundary probe


def test_direct_endpoint_rejects_undiscovered_and_outside_paths(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    make_board(outside, "Outside")
    unrelated = root / "unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")
    instance = workspace.Workspace(root)
    for path in (outside, unrelated):
        with pytest.raises(ValueError, match="outside"):
            instance.endpoint(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction behavior")
def test_discovery_excludes_directory_junction_escape(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "board.json"
    make_board(outside, "Outside")
    link = root / "junction"
    command = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside.parent)],
        capture_output=True,
        check=False,
    )
    if command.returncode:
        pytest.skip("creating directory junctions is unavailable")
    assert link.is_junction()
    assert catalog_boards(workspace.Workspace(root)) == []


@pytest.mark.parametrize("directory", [False, True])
def test_discovery_omits_privileged_symlink_escapes(
    tmp_path: pathlib.Path, *, directory: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "board.json"
    make_board(outside, "Outside")
    link = root / ("linked" if directory else "linked.json")
    try:
        link.symlink_to(outside.parent if directory else outside, target_is_directory=directory)
    except OSError:
        pytest.skip("creating symlinks requires unavailable OS privilege")
    assert catalog_boards(workspace.Workspace(root)) == []


def test_stable_keys_survive_label_changes_and_new_boards(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "boards"
    path = root / "shared" / "board.json"
    make_board(path, "Before rename")
    instance = workspace.Workspace(root)
    try:
        before = catalog_boards(instance)[0]["key"]
        board = board_state.load(path)
        board.project = "After rename"
        board_state.save(board, path)
        make_board(root / "shared" / "another.json", "Another board")
        rows = catalog_boards(instance)
        renamed = next(row for row in rows if row["project"] == "After rename")
        assert renamed["key"] == before == "shared/board"
        assert instance.board_path(before) == path.resolve()
    finally:
        instance.close()


@pytest.mark.parametrize("key", ["missing", "../outside", "/absolute", "alpha/../beta/board"])
def test_unknown_or_escaping_keys_never_fall_back(gateway: RunningWorkspace, key: str) -> None:
    with pytest.raises(ValueError, match=r"board|key|path|unknown"):
        gateway.instance.board_path(key)
    status, _ = request_json(board_route(gateway.base, key))
    assert status == 404
    assert not board_state.service_descriptor_path(gateway.first).exists()
    assert not board_state.service_descriptor_path(gateway.second).exists()


def test_attention_counts_include_only_nonarchived_cards(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Counts")
    board = board_state.load(path)
    for ref, lane in (
        ("blocked-one", "blocked"),
        ("blocked-two", "blocked"),
        ("hidden", "blocked"),
        ("human", "needs_terry_action"),
        ("review", "ready_for_review"),
    ):
        board.create(ref, ref, "backlog", "terry")
        board.move(ref, "in_progress", "terry")
        board.move(ref, lane, "terry")
    board.set_archived("hidden", archived=True, by="terry")
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    try:
        assert catalog_boards(instance)[0]["counts"] == {
            "blocked": 2,
            "needsHuman": 1,
            "review": 1,
        }
    finally:
        instance.close()


@pytest.mark.parametrize(
    "event",
    [
        "created",
        "moved",
        "assigned",
        "prioritized",
        "commented",
        "linked",
        "unlinked",
        "parented",
        "unparented",
        "subject",
        "detail",
        "archived",
        "unarchived",
        "attached",
        "attachment_removed",
        "cover_set",
        "cover_cleared",
    ],
)
def test_catalog_recency_uses_every_card_edit_audit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    old = "2026-10-14T08:00:00-04:00"
    recent = "2026-10-14T10:15:00.125-04:00"
    monkeypatch.setattr(board_state, "now", lambda: old)
    path = tmp_path / "board.json"
    make_board(path, "Recent edits")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    board.create("other", "Other", "backlog", "terry")
    file = attachments.Attachment(
        "11111111-1111-4111-8111-111111111111",
        "picture.png",
        1,
        "image/png",
        "a" * 64,
        old,
        "terry",
    )
    board.add_attachment("card", file)
    board.set_cover("card", file.id, "terry")
    if event == "cover_set":
        board.set_cover("card", None, "terry")
    if event == "unlinked":
        board.link("card", "references", "other", "terry")
    if event == "unparented":
        board.set_parent("card", "other", "terry")
    if event == "unarchived":
        board.set_archived("card", archived=True, by="terry")
    monkeypatch.setattr(board_state, "now", lambda: recent)
    new_file = attachments.Attachment(
        "22222222-2222-4222-8222-222222222222",
        "second.png",
        1,
        "image/png",
        "b" * 64,
        recent,
        "terry",
    )
    actions: dict[str, Callable[[], str]] = {
        "created": partial(board.create, "new", "New card", "backlog", "terry"),
        "moved": partial(board.move, "card", "in_progress", "terry"),
        "assigned": partial(board.assign, "card", "terry", "terry"),
        "prioritized": partial(board.set_priority, "card", "P1", "terry"),
        "commented": partial(board.comment, "card", "Recent comment", "terry"),
        "linked": partial(board.link, "card", "references", "other", "terry"),
        "unlinked": partial(board.unlink, "card", "references", "other", "terry"),
        "parented": partial(board.set_parent, "card", "other", "terry"),
        "unparented": partial(board.set_parent, "card", None, "terry"),
        "subject": partial(board.set_subject, "card", "New subject", "terry"),
        "detail": partial(board.set_detail, "card", "New detail", "terry"),
        "archived": partial(board.set_archived, "card", archived=True, by="terry"),
        "unarchived": partial(board.set_archived, "card", archived=False, by="terry"),
        "attached": partial(board.add_attachment, "card", new_file),
        "attachment_removed": partial(board.remove_attachment, "card", file.id, "terry"),
        "cover_set": partial(board.set_cover, "card", file.id, "terry"),
        "cover_cleared": partial(board.set_cover, "card", None, "terry"),
    }
    actions[event]()
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    assert catalog_boards(instance)[0]["lastCardEditMs"] == int(
        dt.datetime.fromisoformat(recent).timestamp() * 1000
    )


def test_catalog_recency_includes_comments_on_archived_cards(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(board_state, "now", lambda: "2026-10-14T08:00:00-04:00")
    path = tmp_path / "board.json"
    make_board(path, "Archived edits")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    board.set_archived("card", archived=True, by="terry")
    recent = "2026-10-14T09:00:00-04:00"
    monkeypatch.setattr(board_state, "now", lambda: recent)
    board.comment("card", "Still relevant activity", "terry")
    board_state.save(board, path)
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert row["lastCardEditMs"] == int(dt.datetime.fromisoformat(recent).timestamp() * 1000)
    assert row["counts"] == {"blocked": 0, "needsHuman": 0, "review": 0}


def test_catalog_recency_normalizes_offsets_and_ignores_unreadable_stamps(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Legacy activity")
    board = board_state.load(path)
    board.create("card", "Card", "backlog", "terry")
    item = board.find("card")
    item.history[0].at = "unreadable legacy stamp"
    item.comments = [
        board_state.Comment("2026-10-14T15:00:00+02:00", "terry", "Earlier instant"),
        board_state.Comment("2026-10-14T10:00:00-04:00", "terry", "Latest instant"),
        board_state.Comment("not a timestamp", "terry", "Legacy malformed stamp"),
    ]
    board_state.save(board, path)
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert "error" not in row
    assert row["lastCardEditMs"] == int(
        dt.datetime.fromisoformat("2026-10-14T14:00:00Z").timestamp() * 1000
    )


@pytest.mark.parametrize("stamp", [None, "no readable timestamp", "2026-10-14 09:30:00 EDT"])
def test_catalog_recency_has_no_file_or_project_name_fallback(
    tmp_path: pathlib.Path,
    stamp: str | None,
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before rename")
    board = board_state.load(path)
    if stamp is not None:
        board.create("card", "Card", "backlog", "terry")
        board.find("card").history[0].at = stamp
    board_state.save(board, path)
    instance = workspace.Workspace(tmp_path)
    before = catalog_boards(instance)[0]["lastCardEditMs"]
    parsed = board_state.parse_stamp(stamp)
    expected = None if parsed == board_state.parse_stamp(None) else int(parsed.timestamp() * 1000)
    assert before == expected
    board.project = "After rename"
    board_state.save(board, path)
    os.utime(path, (1900000000, 1900000000))
    assert catalog_boards(instance)[0]["lastCardEditMs"] == before


def test_catalog_recency_is_unknown_for_malformed_board(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Malformed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["items"] = "invalid cards"
    path.write_text(json.dumps(raw), encoding="utf-8")
    row = catalog_boards(workspace.Workspace(tmp_path))[0]
    assert row["lastCardEditMs"] is None
    assert "error" in row


def test_cold_deep_link_serves_app_without_loading_wrong_board(gateway: RunningWorkspace) -> None:
    for key in ("alpha/board", "beta/board"):
        with urllib.request.urlopen(
            gateway.base + "/board/" + urllib.parse.quote(key, safe=""), timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert "workspace" in response.read().decode("utf-8").lower()
    assert board_state.load(gateway.first).revision == 0
    assert board_state.load(gateway.second).revision == 0


def test_proxy_mutations_keep_human_attribution_and_revision_isolation(
    gateway: RunningWorkspace,
) -> None:
    session_code, session = request_json(gateway.base + API + "/browser-session")
    assert session_code == 200
    token = str(session["token"])
    code, first = request_json(board_route(gateway.base, "alpha/board"))
    assert code == 200
    code, second = request_json(board_route(gateway.base, "beta/board"))
    assert code == 200
    assert first["project"] == "Alpha"
    assert second["project"] == "Beta"
    code, out = request_json(
        board_route(gateway.base, "beta/board", "cards"),
        token=token,
        revision=0,
        body={"id": "human-card", "subject": "Human work", "state": "backlog"},
    )
    assert code == 200, out
    assert out["revision"] == 1
    assert board_state.load(gateway.second).find("human-card").history[0].by == "terry"
    assert board_state.load(gateway.first).revision == 0
    code, _ = request_json(
        board_route(gateway.base, "beta/board", "cards/human-card/comment"),
        token=token,
        revision=0,
        body={"text": "Stale write"},
    )
    assert code == 412
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "cards"),
        revision=0,
        body={"id": "uncredentialed", "subject": "Refused", "state": "backlog"},
    )
    assert code == 401
    assert board_state.load(gateway.second).find("human-card").comments == []
    assert board_state.load(gateway.first).items == []


def test_board_session_styles_and_policy_do_not_leak_across_projects(
    gateway: RunningWorkspace,
) -> None:
    for key, expected_color, expected_label in (
        ("alpha/board", "#2266aa", "Backlog"),
        ("beta/board", "#992244", "Alternate backlog"),
        ("alpha/board", "#2266aa", "Backlog"),
    ):
        code, session = request_json(board_route(gateway.base, key, "browser-session"))
        assert code == 200
        assert expected_color in session["userCSS"]
        code, payload = request_json(board_route(gateway.base, key))
        assert code == 200
        assert payload["lanes"][0]["label"] == expected_label


def test_attachment_proxy_rebases_urls_and_preserves_download_headers(
    gateway: RunningWorkspace,
) -> None:
    _, session = request_json(gateway.base + API + "/browser-session")
    token = str(session["token"])
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "cards"),
        token=token,
        revision=0,
        body={"id": "picture", "subject": API + "/cards/picture", "state": "backlog"},
    )
    assert code == 200
    code, out = request_json(
        board_route(gateway.base, "alpha/board", "cards/picture/attachment"),
        token=token,
        revision=1,
        body={"name": "evidence.png", "data": base64.b64encode(PNG).decode("ascii")},
    )
    assert code == 200, out
    _, payload = request_json(board_route(gateway.base, "alpha/board"))
    assert payload["lanes"][0]["items"][0]["subject"] == API + "/cards/picture"
    attachment = payload["lanes"][0]["items"][0]["attachments"][0]
    expected = board_route(
        "", "alpha/board", "cards/picture/attachments/" + str(out["attachment"]["id"])
    )
    assert attachment["url"] == expected
    with urllib.request.urlopen(gateway.base + attachment["url"], timeout=5) as response:
        assert response.read() == PNG
        assert response.headers["Content-Disposition"].startswith("inline;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    with urllib.request.urlopen(
        gateway.base + attachment["url"] + "?download=1", timeout=5
    ) as response:
        assert response.read() == PNG
        assert response.headers["Content-Disposition"].startswith("attachment;")
    assert board_state.load(gateway.second).revision == 0


def test_gateway_real_exact_ten_mib_attachment(gateway: RunningWorkspace) -> None:
    unit = b"synthetic attachment bytes\n"
    payload = (unit * (10485760 // len(unit) + 1))[:10485760]
    assert len(payload) == attachments.MAX_BYTES == 10485760
    token = gateway.instance.token
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "cards"),
        token=token,
        revision=0,
        body={"id": "large-file", "subject": "Large synthetic attachment", "state": "backlog"},
    )
    assert code == 200
    code, result = request_json(
        board_route(gateway.base, "alpha/board", "cards/large-file/attachment"),
        token=token,
        revision=1,
        body={"name": "synthetic.bin", "data": base64.b64encode(payload).decode("ascii")},
    )
    assert code == 200, result
    tail = "cards/large-file/attachments/" + str(result["attachment"]["id"])
    endpoint = gateway.instance._existing(gateway.first)  # noqa: SLF001 -- isolated writer
    assert endpoint is not None
    with (
        urllib.request.urlopen(
            f"http://127.0.0.1:{endpoint.port}{API}/{tail}", timeout=5
        ) as upstream,
        urllib.request.urlopen(
            board_route(gateway.base, "alpha/board", tail), timeout=5
        ) as response,
    ):
        assert upstream.read() == payload
        assert response.read() == payload
        assert response.headers["Content-Length"] == "10485760"
        for header in (
            "Content-Type",
            "Content-Disposition",
            "X-Content-Type-Options",
            "Content-Security-Policy",
        ):
            assert response.headers[header] == upstream.headers[header]
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response.headers["Content-Security-Policy"]


@contextlib.contextmanager
def existing_writer(path: pathlib.Path) -> Generator[dict[str, Any]]:
    """Run one real board process without affecting any interactive services."""
    child = subprocess.Popen(
        [sys.executable, "-m", "localswim.api_endpoint", str(path), "--port", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    descriptor_path = board_state.service_descriptor_path(path)
    try:
        deadline = time.monotonic() + 10
        while not descriptor_path.exists():
            assert child.poll() is None, "isolated board process exited during startup"
            assert time.monotonic() < deadline, "board startup timed out"
            time.sleep(0.02)
        yield cast("dict[str, Any]", json.loads(descriptor_path.read_text(encoding="utf-8")))
    finally:
        with contextlib.suppress(board_state.BoardError, OSError):
            board_state.shutdown_service(path)
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_gateway_reuses_existing_writer_and_cli_keeps_bot_attribution(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200
        assert payload["project"] == "Alpha"
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current == descriptor
        result = run_cli(
            gateway.first, "card", "create", "bot-card", "Bot work", "--state", "backlog"
        )
        assert result.returncode == 0, result.stderr
        assert board_state.load(gateway.first).find("bot-card").history[0].by == "bot"
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200
        assert payload["revision"] == 1


def test_wrong_service_descriptor_cannot_cross_route_board_data(gateway: RunningWorkspace) -> None:
    with existing_writer(gateway.second) as second:
        fake = dict(second)
        fake["board"] = str(gateway.first.resolve())
        board_state.service_descriptor_path(gateway.first).write_text(
            json.dumps(fake), encoding="utf-8"
        )
        code, payload = request_json(board_route(gateway.base, "alpha/board"))
        assert code == 200, payload
        assert payload["project"] == "Alpha"
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current["pid"] != second["pid"]
        assert current["port"] != second["port"]
        assert board_state.load(gateway.first).revision == 0
        assert board_state.load(gateway.second).revision == 0


def test_invalid_board_is_visible_as_error_without_spawning_a_writer(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "damaged.json"
    make_board(path, "Damaged board")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["items"] = "invalid cards"
    path.write_text(json.dumps(raw), encoding="utf-8")
    instance = workspace.Workspace(tmp_path)
    try:
        rows = catalog_boards(instance)
        assert len(rows) == 1
        assert rows[0]["key"] == "damaged"
        assert rows[0]["error"]
        assert not board_state.service_descriptor_path(path).exists()
    finally:
        instance.close()


def test_browser_sessions_identify_board_and_never_expose_cli_capability(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        base = f"http://127.0.0.1:{descriptor['port']}"
        code, upstream = request_json(base + API + "/browser-session")
        assert code == 200
        assert upstream["boardIdentity"] == workspace.path_identity(gateway.first)
        assert upstream["token"] != descriptor["token"]
        code, scoped = request_json(board_route(gateway.base, "alpha/board", "browser-session"))
        assert code == 200
        assert scoped["boardIdentity"] == upstream["boardIdentity"]
        assert scoped["userCSS"] == upstream["userCSS"]
        assert scoped["token"] == gateway.instance.token
        assert scoped["token"] != upstream["token"]


def test_close_stops_owned_children_but_preserves_existing_writers(
    gateway: RunningWorkspace,
) -> None:
    with existing_writer(gateway.first) as descriptor:
        assert request_json(board_route(gateway.base, "alpha/board"))[0] == 200
        assert request_json(board_route(gateway.base, "beta/board"))[0] == 200
        owned_descriptor = board_state.service_descriptor_path(gateway.second)
        assert owned_descriptor.exists()
        gateway.instance.close()
        assert not owned_descriptor.exists()
        original_descriptor = board_state.service_descriptor_path(gateway.first)
        assert json.loads(original_descriptor.read_text(encoding="utf-8")) == descriptor
        base = f"http://127.0.0.1:{descriptor['port']}"
        assert request_json(base + API + "/board")[0] == 200


@pytest.mark.parametrize("interrupted", [False, True])
def test_normal_gateway_exit_leaves_owned_children_live(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, interrupted: bool
) -> None:
    events: list[str] = []

    class Server:
        server_address = ("127.0.0.1", 12345)

        def __init__(self, *_args: object) -> None:
            pass

        @staticmethod
        def serve_forever() -> None:
            events.append("serve")
            if interrupted:
                raise KeyboardInterrupt

        @staticmethod
        def server_close() -> None:
            events.append("server-close")

    def start(_instance: workspace.Workspace) -> None:
        events.append("publisher-start")

    def stop(_instance: workspace.Workspace) -> None:
        events.append("publisher-stop")

    def close(_instance: workspace.Workspace) -> None:
        pytest.fail("normal gateway exit must leave owned children live")

    def reexec() -> None:
        events.append("reexec")

    monkeypatch.setattr(workspace, "WorkspaceServer", Server)
    monkeypatch.setattr(workspace.Workspace, "start_publishing", start)
    monkeypatch.setattr(workspace.Workspace, "stop_publishing", stop)
    monkeypatch.setattr(workspace.Workspace, "close", close)
    monkeypatch.setattr(api_endpoint, "reexec_if_requested", reexec)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            workspace.serve(tmp_path)
    else:
        workspace.serve(tmp_path)
    assert events == ["publisher-start", "serve", "server-close", "publisher-stop"] + (
        [] if interrupted else ["reexec"]
    )


@pytest.mark.parametrize("replacement", ["malformed", "nonregular", "ordinary"])
def test_residual_child_wait_rechecks_discovery_before_new_launch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    path = tmp_path / "board.json"
    make_board(path, "Before cleanup")
    instance = workspace.Workspace(tmp_path)
    token = instance.token
    expected = workspace.Endpoint(12345, "synthetic", "")
    events: list[str] = []
    spawned = False

    class PreviousChild:
        @staticmethod
        def terminate() -> None:
            events.append("terminate")

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == workspace.REQUEST_TIMEOUT
            events.append("wait")
            if replacement == "ordinary":
                make_board(path, "After atomic save during cleanup")
            elif replacement == "malformed":
                temporary = path.with_name(".replacement")
                temporary.write_bytes(b"{")
                temporary.replace(path)
            else:
                path.unlink()
                path.mkdir()
            return 0

    class NewChild:
        @staticmethod
        def poll() -> None:
            return None

    previous = cast("subprocess.Popen[bytes]", PreviousChild())
    child = NewChild()
    instance._children[path] = previous  # noqa: SLF001 -- captured residual ownership
    instance._launch_ids[path] = "previous"  # noqa: SLF001 -- paired residual ownership

    @contextlib.contextmanager
    def gate(target: pathlib.Path) -> Generator[service_ownership.LaunchGate]:
        events.append("gate")
        yield service_ownership.LaunchGate(target)

    def existing(_path: pathlib.Path) -> workspace.Endpoint | None:
        return expected if spawned else None

    def busy(_path: pathlib.Path) -> bool:
        return False

    def environment(_gate: service_ownership.LaunchGate) -> dict[str, str]:
        events.append("capability")
        return {}

    def launch(*_args: object, **_kwargs: object) -> NewChild:
        nonlocal spawned
        assert replacement == "ordinary", "unsafe replacement reached process launch"
        spawned = True
        events.append("launch")
        return child

    monkeypatch.setattr(instance, "_launch_locked", gate)
    monkeypatch.setattr(instance, "_existing", existing)
    monkeypatch.setattr(service_ownership, "writer_busy", busy)
    monkeypatch.setattr(service_ownership.LaunchGate, "child_environment", environment)
    monkeypatch.setattr(subprocess, "Popen", launch)
    if replacement == "ordinary":
        assert instance.resolve_endpoint("board") == (path.resolve(), expected)
        assert events == ["gate", "terminate", "wait", "capability", "launch"]
        assert instance._children[path] is child  # noqa: SLF001 -- single new child retained
        assert instance._launch_ids[path] != "previous"  # noqa: SLF001 -- new launch pair
    else:
        with pytest.raises(ValueError, match=r"unknown workspace board|not a regular file"):
            instance.resolve_endpoint("board")
        assert events == ["gate", "terminate", "wait"]
        assert not instance._children  # noqa: SLF001 -- prior child confirmed reaped
        assert not instance._launch_ids  # noqa: SLF001 -- prior pair removed only after reap
    assert instance.token == token
