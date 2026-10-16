"""Loopback workspace gateway over independent, sole-writer board services."""

import contextlib
import hashlib
import html
import http.client
import http.server
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from . import (
    api_endpoint,
    attachments,
    board_state,
    gateway_publish,
    service_ownership,
    workspace_config,
)

if TYPE_CHECKING:
    from collections.abc import Generator

DEFAULT_PORT = 8810
PREFIX = board_state.API_PREFIX + "/workspace"
BOARD_PREFIX = PREFIX + "/boards/"
PREFERENCES_ROUTE = PREFIX + "/preferences"
MAX_PREFERENCES_BYTES = 64 * 1024
START_WAIT_SECONDS = 15.0
START_POLL_SECONDS = 0.1
REQUEST_TIMEOUT = 5.0
PORT_LIMIT = 65536
ASSET_DIR = pathlib.Path(__file__).resolve().parent


def path_identity(path: pathlib.Path) -> str:
    """Identify the canonical board independently of its service port."""
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()


def _card_edit_stamps(board: board_state.Board) -> Generator[str]:
    """Read audited card events, including edits to cards currently archived."""
    for item in board.items:
        for history in (item.history, item.comments, item.attachment_history):
            for event in history:
                yield event.at
    for history in (
        board.relationship_history,
        board.parent_history,
        board.subject_history,
        board.detail_history,
        board.archive_history,
    ):
        for event in history:
            yield event.at


def last_card_edit_ms(board: board_state.Board) -> int | None:
    """Return the latest readable card-event instant, never the snapshot file time.

    Legacy unreadable stamps use the shared parser's sentinel and do not hide other
    readable events. Empty or entirely unreadable audit history has no known recency.
    """
    fallback = board_state.parse_stamp(None)
    latest = max(
        (board_state.parse_stamp(stamp) for stamp in _card_edit_stamps(board)), default=fallback
    )
    return None if latest == fallback else int(latest.timestamp() * 1000)


class ServiceUpgradePendingError(OSError):
    """An existing service must finish its browser-session protocol upgrade."""


@dataclass(frozen=True)
class Endpoint:
    """Validated upstream browser capability; never a CLI capability."""

    port: int
    token: str
    user_css: str


class Workspace:
    """Discover boards and acquire their independently owned service endpoints."""

    def __init__(self, root: pathlib.Path, *, autopush: bool = False) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            msg = "workspace root must be a directory"
            raise ValueError(msg)
        self.identity = path_identity(self.root)
        self.token = secrets.token_urlsafe(32)
        self.settings = workspace_config.GatewayConfigStore(self.root, autopush=autopush)
        self.autopush = self.settings.snapshot().autopush
        self.publisher = gateway_publish.GatewayPublisher(self.settings)
        self._children: dict[pathlib.Path, subprocess.Popen[bytes]] = {}
        self._launch_ids: dict[pathlib.Path, str] = {}
        self._lock = threading.RLock()

    def _paths(self) -> dict[str, pathlib.Path]:
        found: dict[str, pathlib.Path] = {}
        for directory, names, filenames in os.walk(self.root, followlinks=False):
            parent = pathlib.Path(directory)
            names[:] = sorted(
                name
                for name in names
                if not name.startswith(".")
                and name != "attachments"
                and not (parent / name).is_symlink()
                and not (parent / name).is_junction()
                and (parent / name).resolve().is_relative_to(self.root)
            )
            for name in sorted(filenames):
                path = parent / name
                if (
                    name.startswith(".")
                    or name == workspace_config.CONFIG_FILENAME
                    or path.suffix.lower() != ".json"
                ):
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    continue
                try:
                    raw = cast(
                        "board_state.JsonValue", json.loads(path.read_text(encoding="utf-8"))
                    )
                except OSError, ValueError:
                    continue
                if (
                    isinstance(raw, dict)
                    and raw.get("schema") == board_state.SCHEMA
                    and all(field in raw for field in ("project", "items", "policy"))
                ):
                    key = path.relative_to(self.root).with_suffix("").as_posix()
                    found[key] = path.resolve()
        return found

    def board_path(self, key: str) -> pathlib.Path:
        """Resolve only a discovered board, including after filesystem changes."""
        path = self._paths().get(key)
        if path is None:
            msg = "unknown workspace board"
            raise ValueError(msg)
        return path

    def catalog(self) -> board_state.JsonObject:
        """Read attention counts without starting services or changing snapshots."""
        boards: list[board_state.JsonValue] = []
        for key, path in self._paths().items():
            counts: board_state.JsonObject = {"blocked": 0, "needsHuman": 0, "review": 0}
            entry: board_state.JsonObject = {
                "key": key,
                "project": path.stem,
                "counts": counts,
                "lastCardEditMs": None,
            }
            try:
                board = board_state.load(path)
                entry["project"] = board.project or path.stem
                entry["lastCardEditMs"] = last_card_edit_ms(board)
                counts.update(
                    {
                        "blocked": sum(
                            not item.archived and item.state == "blocked" for item in board.items
                        ),
                        "needsHuman": sum(
                            not item.archived
                            and item.state
                            in ("needs_terry_action", "needs_terry", "needs_human_action")
                            for item in board.items
                        ),
                        "review": sum(
                            not item.archived and item.state == "ready_for_review"
                            for item in board.items
                        ),
                    }
                )
            except (OSError, ValueError) as exc:
                entry["error"] = str(exc)
            boards.append(entry)
        return {
            "root": str(self.root),
            "identity": self.identity,
            "boards": boards,
            "preferences": self.settings.snapshot().to_json(),
            "gatewayPush": cast("board_state.JsonObject", self.publisher.status()),
        }

    def update_preferences(
        self, revision: int, command: board_state.JsonObject
    ) -> workspace_config.GatewayPreferences:
        """Apply one revision-checked command to this root's discovered stable keys."""
        return self.settings.apply(revision, command, self._paths().keys())

    def _existing(self, path: pathlib.Path) -> Endpoint | None:
        try:
            raw = cast(
                "board_state.JsonValue",
                json.loads(board_state.service_descriptor_path(path).read_text(encoding="utf-8")),
            )
            if not isinstance(raw, dict) or raw.get("schema") != 1:
                return None
            port = raw.get("port")
            board = raw.get("board")
            if (
                raw.get("host") != api_endpoint.HOST
                or type(port) is not int
                or not 0 < port < PORT_LIMIT
                or not isinstance(board, str)
                or path_identity(pathlib.Path(board)) != path_identity(path)
            ):
                return None
            return self._browser_endpoint(path, port)
        except ServiceUpgradePendingError:
            raise
        except OSError, ValueError, http.client.HTTPException:
            return None

    def _browser_endpoint(self, path: pathlib.Path, port: int) -> Endpoint | None:
        connection = http.client.HTTPConnection(api_endpoint.HOST, port, timeout=1)
        try:
            connection.request("GET", board_state.API_PREFIX + "/browser-session")
            response = connection.getresponse()
            blob = response.read()
            if response.status != HTTPStatus.OK:
                self._request_existing_upgrade(port)
                return None
            session = cast("board_state.JsonValue", json.loads(blob))
            if not isinstance(session, dict):
                return None
            token, css = session.get("token"), session.get("userCSS")
            if (
                session.get("boardIdentity") != path_identity(path)
                or not isinstance(token, str)
                or not token
                or not isinstance(css, str)
            ):
                return None
            return Endpoint(port, token, css)
        finally:
            connection.close()

    @staticmethod
    def _request_existing_upgrade(port: int) -> None:
        connection = http.client.HTTPConnection(api_endpoint.HOST, port, timeout=1)
        try:
            connection.request("GET", board_state.API_PREFIX + "/status")
            response = connection.getresponse()
            status = cast("board_state.JsonValue", json.loads(response.read()))
            if (
                response.status == HTTPStatus.OK
                and isinstance(status, dict)
                and isinstance(status.get("build"), str)
                and "codeStale" in status
            ):
                raise ServiceUpgradePendingError("existing board service is upgrading")
        finally:
            connection.close()

    def _await_existing(
        self, path: pathlib.Path, *, wait_for_upgrade: bool = True
    ) -> Endpoint | None:
        deadline = time.monotonic() + START_WAIT_SECONDS
        while True:
            upgrading = False
            try:
                if endpoint := self._existing(path):
                    return endpoint
            except ServiceUpgradePendingError:
                if not wait_for_upgrade:
                    raise
                upgrading = True
            if not upgrading and not service_ownership.writer_busy(path):
                return None
            if time.monotonic() >= deadline:
                msg = "existing board writer did not publish a healthy endpoint before timeout"
                raise TimeoutError(msg)
            time.sleep(START_POLL_SECONDS)

    @contextlib.contextmanager
    def _launch_locked(self, path: pathlib.Path) -> Generator[service_ownership.LaunchGate]:
        with service_ownership.launch_locked(path, timeout=START_WAIT_SECONDS) as gate:
            yield gate

    def endpoint(self, path: pathlib.Path) -> Endpoint:
        """Reuse a matching live writer, otherwise launch one on an ephemeral port."""
        path = path.resolve()
        if path not in self._paths().values():
            msg = "board is outside the workspace"
            raise ValueError(msg)
        board_state.load(path)
        with self._lock:
            while True:
                try:
                    return self._endpoint_under_gate(path)
                except ServiceUpgradePendingError:
                    # The old writer takes this same gate before its restart.
                    # Release it before waiting for the replacement to become ready.
                    if endpoint := self._await_existing(path):
                        return endpoint

    def _endpoint_under_gate(self, path: pathlib.Path) -> Endpoint:
        with self._launch_locked(path) as gate:
            if endpoint := self._await_existing(path, wait_for_upgrade=False):
                return endpoint
            arguments = ["-m", "localswim.api_endpoint", str(path), "--port", "0"]
            if self.settings.snapshot().autopush:
                arguments.append("--autopush")
            launch_id = secrets.token_urlsafe(32)
            environment = {
                **os.environ,
                **gate.child_environment(),
                "LOCALSWIM_WORKSPACE_LAUNCH": launch_id,
            }
            command = service_ownership.python_child_command(arguments, environment)
            child = subprocess.Popen(  # noqa: S603 -- fixed Python module and discovered path
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=environment,
            )
            self._children[path] = child
            self._launch_ids[path] = launch_id
            deadline = time.monotonic() + START_WAIT_SECONDS
            while time.monotonic() < deadline:
                try:
                    if endpoint := self._existing(path):
                        return endpoint
                except ServiceUpgradePendingError:
                    pass
                if child.poll() is not None and not service_ownership.writer_busy(path):
                    msg = "board service could not start"
                    raise OSError(msg)
                time.sleep(START_POLL_SECONDS)
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=REQUEST_TIMEOUT)
            msg = "board service did not publish a healthy endpoint"
            raise TimeoutError(msg)

    def start_publishing(self) -> None:
        """Start gateway configuration publishing only for the live gateway lifecycle."""
        self.publisher.start()

    def stop_publishing(self) -> None:
        """Flush pending gateway configuration and stop its publisher worker."""
        ok, _ = self.publisher.stop()
        if not ok:
            print(
                "Gateway preferences backup failed during shutdown; local settings retained.",
                file=sys.stderr,
                flush=True,
            )

    def close(self) -> None:
        """Explicitly stop only children launched here; normal gateway exit leaves writers live."""
        for path, child in self._children.items():
            try:
                raw = cast(
                    "board_state.JsonObject",
                    json.loads(
                        board_state.service_descriptor_path(path).read_text(encoding="utf-8")
                    ),
                )
                port, token = raw.get("port"), raw.get("token")
                if (
                    isinstance(port, int)
                    and isinstance(token, str)
                    and raw.get("workspaceLaunch") == self._launch_ids[path]
                    and self._existing(path)
                ):
                    connection = http.client.HTTPConnection(
                        api_endpoint.HOST, port, timeout=REQUEST_TIMEOUT
                    )
                    try:
                        connection.request(
                            "POST",
                            board_state.API_PREFIX + "/shutdown",
                            headers={"Authorization": "Bearer " + token},
                        )
                        connection.getresponse().read()
                    finally:
                        connection.close()
                self._wait_child_stopped(path, child)
            except OSError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=REQUEST_TIMEOUT)
        self._children.clear()
        self._launch_ids.clear()

    def _wait_child_stopped(self, path: pathlib.Path, child: subprocess.Popen[bytes]) -> None:
        child.wait(timeout=REQUEST_TIMEOUT)
        deadline = time.monotonic() + REQUEST_TIMEOUT
        descriptor = board_state.service_descriptor_path(path)
        while time.monotonic() < deadline:
            try:
                raw = cast(
                    "board_state.JsonObject", json.loads(descriptor.read_text(encoding="utf-8"))
                )
            except OSError, ValueError:
                return
            if raw.get("workspaceLaunch") != self._launch_ids[path]:
                return
            time.sleep(START_POLL_SECONDS)
        msg = "owned board service did not release its descriptor"
        raise TimeoutError(msg)


class WorkspaceServer(http.server.ThreadingHTTPServer):
    """Drain in-flight requests before source re-execution."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, address: tuple[str, int], workspace: Workspace) -> None:
        self.workspace = workspace
        super().__init__(address, WorkspaceHandler)


def _rebase(value: board_state.JsonValue, prefix: str) -> board_state.JsonValue:
    if isinstance(value, dict):
        return {
            key: prefix + item.removeprefix(board_state.API_PREFIX)
            if key == "url"
            and isinstance(item, str)
            and item.startswith(board_state.API_PREFIX + "/cards/")
            else _rebase(item, prefix)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rebase(item, prefix) for item in value]
    return value


def _unique_json_object(pairs: list[tuple[str, board_state.JsonValue]]) -> board_state.JsonObject:
    value: board_state.JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


class WorkspaceHandler(http.server.BaseHTTPRequestHandler):
    """Serve the shared page and proxy scoped requests with the browser actor."""

    @property
    def workspace(self) -> Workspace:
        return cast("WorkspaceServer", self.server).workspace

    def _send(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        self._send(json.dumps(value).encode("utf-8"), "application/json", status)

    def _page(self, route: str) -> None:
        title = "localswim workspace"
        if route.startswith("/board/"):
            key = urllib.parse.unquote(route.removeprefix("/board/"))
            try:
                board = board_state.load(self.workspace.board_path(key))
                title = board.project or title
            except OSError, ValueError:
                pass
        page = (
            api_endpoint.PAGE.replace("%POLL%", str(api_endpoint.POLL_MS))
            .replace("%BUILD%", html.escape(api_endpoint.BUILD))
            .replace("%TOKEN%", self.workspace.token)
            .replace("%API_PREFIX%", board_state.API_PREFIX)
            .replace("%BRANDURI%", api_endpoint.brand_data_uri())
            .replace("%USERCSS%", api_endpoint.user_css())
            .replace("%TITLE%", html.escape(title))
            .replace(
                "%WORKSPACE%",
                "<script>window.localswimWorkspace=true;</script>"
                '<script defer src="/workspace.js"></script>',
            )
            .replace("</head>", '<link rel="stylesheet" href="/workspace.css"></head>')
        )
        self._send(page.encode("utf-8"), "text/html; charset=utf-8")

    def _decorate_board(
        self, blob: bytes, tail: str, status: int, key: str, endpoint: Endpoint
    ) -> bytes:
        if tail != "board" or status != HTTPStatus.OK:
            return blob
        value = cast("board_state.JsonValue", json.loads(blob))
        if isinstance(value, dict):
            value["userCSS"] = endpoint.user_css
        return json.dumps(_rebase(value, BOARD_PREFIX + key)).encode("utf-8")

    def _proxy(self, route: str, *, write: bool = False) -> None:
        encoded_key, separator, tail = route.removeprefix(BOARD_PREFIX).partition("/")
        if not separator or not tail or tail == "shutdown":
            self._json({"error": "unknown workspace route"}, 404)
            return
        if write and self.headers.get("Authorization") != "Bearer " + self.workspace.token:
            self._json({"error": "missing or invalid workspace credential"}, 401)
            return
        body: bytes | None = None
        if write:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return
            if size < 0 or size > attachments.MAX_REQUEST_BYTES:
                self._json({"error": attachments.SIZE_REFUSAL}, 413)
                return
            body = self.rfile.read(size)
        try:
            path = self.workspace.board_path(urllib.parse.unquote(encoded_key))
            endpoint = self.workspace.endpoint(path)
            if tail == "browser-session":
                self._json(
                    {
                        "token": self.workspace.token,
                        "userCSS": endpoint.user_css,
                        "boardIdentity": path_identity(path),
                    }
                )
                return
            connection = http.client.HTTPConnection(
                api_endpoint.HOST, endpoint.port, timeout=REQUEST_TIMEOUT
            )
            query = urllib.parse.urlsplit(self.path).query
            target = board_state.API_PREFIX + "/" + tail + ("?" + query if query else "")
            headers = {"Authorization": "Bearer " + endpoint.token}
            if write:
                headers["Content-Type"] = "application/json"
                if revision := self.headers.get("If-Match"):
                    headers["If-Match"] = revision
            try:
                connection.request("POST" if write else "GET", target, body=body, headers=headers)
                response = connection.getresponse()
                blob = response.read()
                content_type = response.getheader("Content-Type", "application/octet-stream")
                blob = self._decorate_board(blob, tail, response.status, encoded_key, endpoint)
                passthrough = {
                    key: value
                    for key in (
                        "Content-Disposition",
                        "Content-Security-Policy",
                        "X-Content-Type-Options",
                    )
                    if (value := response.getheader(key)) is not None
                }
                self._send(blob, content_type, response.status, passthrough)
            finally:
                connection.close()
        except ValueError as exc:
            self._json({"error": str(exc)}, 404)
        except (OSError, http.client.HTTPException) as exc:
            self._json({"error": "board service unavailable: " + str(exc)}, 503)

    def _preferences_write(self) -> None:
        if self.headers.get("Authorization") != "Bearer " + self.workspace.token:
            self._json({"error": "missing or invalid workspace credential"}, 401)
            return
        raw_revision = self.headers.get("If-Match")
        if raw_revision is None:
            self._json({"error": "If-Match must name the expected preferences revision"}, 428)
            return
        revision = raw_revision.strip().strip('"').removeprefix("revision-")
        if not revision.isascii() or not revision.isdigit():
            self._json({"error": "invalid preferences revision"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_PREFERENCES_BYTES:
                self._json({"error": "preferences request is too large"}, 413)
                return
            command = cast(
                "board_state.JsonValue",
                json.loads(self.rfile.read(length), object_pairs_hook=_unique_json_object),
            )
            if not isinstance(command, dict):
                self._json({"error": "preferences command must be an object"}, 400)
                return
            snapshot = self.workspace.update_preferences(int(revision), command)
        except workspace_config.ConfigRevisionConflict as exc:
            self._json({"error": str(exc)}, 412)
        except ValueError, TypeError, RecursionError:
            self._json({"error": "invalid preferences command"}, 400)
        except OSError:
            self._json({"error": "preferences storage unavailable"}, 503)
        else:
            self._json(snapshot.to_json())

    def do_GET(self) -> None:
        """Read the catalog, page assets, or a scoped upstream resource."""
        route = urllib.parse.urlsplit(self.path).path
        if route == board_state.API_PREFIX + "/browser-session":
            self._json(
                {"token": self.workspace.token, "workspaceIdentity": self.workspace.identity}
            )
        elif route == PREFERENCES_ROUTE:
            self._json(self.workspace.settings.snapshot().to_json())
        elif route == PREFIX:
            self._json({**self.workspace.catalog(), **api_endpoint.source_status(self.server)})
        elif route.startswith(BOARD_PREFIX):
            self._proxy(route)
        elif route in ("/", "/index.html") or route.startswith("/board/"):
            self._page(route)
        elif route in ("/workspace.js", "/workspace.css"):
            mime = "text/javascript" if route.endswith(".js") else "text/css"
            self._send((ASSET_DIR / route.removeprefix("/")).read_bytes(), mime + "; charset=utf-8")
        elif route == "/favicon.svg":
            self._send(api_endpoint.FAVICON.encode("utf-8"), "image/svg+xml")
        elif route in api_endpoint.FONTS:
            self._send(
                (api_endpoint.FONT_DIR / api_endpoint.FONTS[route]).read_bytes(), "font/woff2"
            )
        else:
            self._json({"error": "unknown workspace route"}, 404)

    def do_POST(self) -> None:
        """Forward only scoped browser commands, preserving upstream revision checks."""
        route = urllib.parse.urlsplit(self.path).path
        if route == PREFERENCES_ROUTE:
            self._preferences_write()
        elif route.startswith(BOARD_PREFIX):
            self._proxy(route, write=True)
        else:
            self._json({"error": "unknown workspace route"}, 404)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep frequent workspace polling quiet."""


def serve(root: pathlib.Path, port: int = DEFAULT_PORT, *, autopush: bool = False) -> None:
    """Run one gateway; independently owned board services remain available on exit."""
    workspace = Workspace(root, autopush=autopush)
    server = WorkspaceServer((api_endpoint.HOST, port), workspace)
    print(f"Workspace {workspace.root}", flush=True)
    print(f"  view: http://{api_endpoint.HOST}:{server.server_address[1]}/", flush=True)
    workspace.start_publishing()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        workspace.stop_publishing()
    api_endpoint.reexec_if_requested()
