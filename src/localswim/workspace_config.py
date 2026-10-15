"""Durable gateway preferences, independent of every board snapshot."""

import contextlib
import json
import os
import pathlib
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from . import api_endpoint, board_state

if TYPE_CHECKING:
    from collections.abc import Collection, Generator

CONFIG_FILENAME = "localswim-gateway.json"
SCHEMA = 1
MAX_CONFIG_BYTES = 1024 * 1024
LOCK_WAIT_SECONDS = 15.0
FIRST_PRINTABLE_CHARACTER = 32


class GatewayConfigError(ValueError):
    """Preferences or commands violate the gateway configuration contract."""


class ConfigRevisionConflict(ValueError):  # noqa: N818 -- public revision-conflict contract
    """The caller's preferences revision is stale."""


@dataclass(frozen=True)
class GatewayPreferences:
    revision: int = 0
    autopush: bool = False
    pins_initialized: bool = False
    pinned_boards: tuple[str, ...] = ()

    def to_json(self) -> board_state.JsonObject:
        return {
            "schema": SCHEMA,
            "revision": self.revision,
            "autopush": self.autopush,
            "pinsInitialized": self.pins_initialized,
            "pinnedBoards": list(self.pinned_boards),
        }


def _key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < FIRST_PRINTABLE_CHARACTER for character in value)
        or any(character in value for character in '\\:*?"<>|')
        or any(not part or part.startswith(".") for part in value.split("/"))
    ):
        raise GatewayConfigError("invalid workspace board key")
    return value


def _keys(value: board_state.JsonValue) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GatewayConfigError("pinnedBoards must be an array")
    result = tuple(_key(item) for item in value)
    if len(result) != len(set(result)):
        raise GatewayConfigError("pinnedBoards must not contain duplicates")
    return result


def _unique_object(pairs: list[tuple[str, board_state.JsonValue]]) -> board_state.JsonObject:
    result: board_state.JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise GatewayConfigError("duplicate gateway configuration field")
        result[key] = value
    return result


def _preferences(raw: board_state.JsonValue) -> GatewayPreferences:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "revision",
        "autopush",
        "pinsInitialized",
        "pinnedBoards",
    }:
        raise GatewayConfigError("invalid gateway configuration fields")
    if type(raw["schema"]) is not int or raw["schema"] != SCHEMA:
        raise GatewayConfigError("unsupported gateway configuration schema")
    revision = raw["revision"]
    if type(revision) is not int or revision < 0:
        raise GatewayConfigError("gateway revision must be a nonnegative integer")
    autopush = raw["autopush"]
    initialized = raw["pinsInitialized"]
    if not isinstance(autopush, bool) or not isinstance(initialized, bool):
        raise GatewayConfigError("gateway flags must be booleans")
    pins = _keys(raw["pinnedBoards"])
    if pins and not initialized:
        raise GatewayConfigError("uninitialized gateway cannot contain pinned boards")
    return GatewayPreferences(revision, autopush, initialized, pins)


class GatewayConfigStore:
    """Serialize read/modify/replace transactions across threads and processes.

    Callers may hold ``locked()`` while publishing a snapshot. Nested calls on
    this instance reuse its operating-system lock rather than acquiring it again.
    """

    def __init__(self, root: pathlib.Path, *, autopush: bool = False) -> None:
        if root.is_symlink() or root.is_junction():
            raise GatewayConfigError("workspace root must not be a symbolic link")
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise GatewayConfigError("workspace root must be a directory")
        self.path = self.root / CONFIG_FILENAME
        self._lock_path = self.root / f".{CONFIG_FILENAME}.lock"
        self._mutex = threading.RLock()
        self._depth = 0
        with self.locked():
            if not self.path.exists():
                self._safe_file(self.path, missing_ok=True)
                self._write(GatewayPreferences(autopush=autopush))
            else:
                current = self._read()
                if autopush and not current.autopush:
                    self._write(replace(current, revision=current.revision + 1, autopush=True))

    @staticmethod
    def _safe_file(path: pathlib.Path, *, missing_ok: bool = False) -> None:
        if path.is_symlink() or path.is_junction():
            raise GatewayConfigError("gateway configuration paths must not be symbolic links")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(mode):
            raise GatewayConfigError("gateway configuration path must be a regular file")

    @contextlib.contextmanager
    def locked(self) -> Generator[GatewayConfigStore]:
        with self._mutex:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self._safe_file(self._lock_path, missing_ok=True)
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            acquired = False
            try:
                self._safe_file(self._lock_path)
                deadline = time.monotonic() + LOCK_WAIT_SECONDS
                while not api_endpoint.try_process_lock(descriptor):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for gateway configuration lock")
                    time.sleep(0.05)
                acquired = True
                self._depth = 1
                yield self
            finally:
                self._depth = 0
                if acquired:
                    api_endpoint.release_process_lock(descriptor)
                os.close(descriptor)

    def _read(self) -> GatewayPreferences:
        self._safe_file(self.path)
        with self.path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise GatewayConfigError("gateway configuration exceeds size limit")
        try:
            raw = cast("board_state.JsonValue", json.loads(data, object_pairs_hook=_unique_object))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise GatewayConfigError(f"invalid gateway configuration: {exc}") from exc
        return _preferences(raw)

    def _write(self, preferences: GatewayPreferences) -> None:
        self._safe_file(self.path, missing_ok=True)
        data = (
            json.dumps(preferences.to_json(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        if len(data.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise GatewayConfigError("gateway configuration exceeds size limit")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{CONFIG_FILENAME}.", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_file(self.path, missing_ok=True)
            pathlib.Path(temporary).replace(self.path)
        finally:
            pathlib.Path(temporary).unlink(missing_ok=True)

    def snapshot(self) -> GatewayPreferences:
        with self.locked():
            return self._read()

    def apply(
        self,
        expected_revision: int,
        command: board_state.JsonObject,
        known_keys: Collection[str],
    ) -> GatewayPreferences:
        with self.locked():
            current = self._read()
            if type(expected_revision) is not int or expected_revision < 0:
                raise GatewayConfigError("expected revision must be a nonnegative integer")
            if expected_revision != current.revision:
                raise ConfigRevisionConflict("gateway configuration revision conflict")
            updated = self._command(current, command, known_keys)
            if updated != current:
                updated = replace(updated, revision=current.revision + 1)
                self._write(updated)
            return updated

    @staticmethod
    def _command(
        current: GatewayPreferences,
        command: board_state.JsonObject,
        known_keys: Collection[str],
    ) -> GatewayPreferences:
        action = command.get("action")
        fields = {
            "pin": {"action", "key"},
            "unpin": {"action", "key"},
            "move": {"action", "key", "target", "after"},
            "initialize": {"action", "pinnedBoards"},
        }
        if not isinstance(action, str) or action not in fields or set(command) != fields[action]:
            raise GatewayConfigError("invalid gateway preferences command")
        pins = list(current.pinned_boards)
        if action == "initialize":
            imported = _keys(command["pinnedBoards"])
            if any(key not in known_keys for key in imported):
                raise GatewayConfigError("unknown workspace board")
            if current.pins_initialized or not imported:
                return current
            return replace(current, pins_initialized=True, pinned_boards=imported)
        key = _key(command["key"])
        if key not in known_keys and (action == "pin" or key not in pins):
            raise GatewayConfigError("unknown workspace board")
        if action == "pin" and key not in pins:
            pins.append(key)
        elif action == "unpin" and key in pins:
            pins.remove(key)
        elif action == "move":
            target = _key(command["target"])
            after = command["after"]
            if not isinstance(after, bool) or key not in pins or target not in pins:
                raise GatewayConfigError("move requires pinned boards and a boolean after flag")
            if key != target:
                pins.remove(key)
                pins.insert(pins.index(target) + int(after), key)
        return replace(current, pins_initialized=True, pinned_boards=tuple(pins))
