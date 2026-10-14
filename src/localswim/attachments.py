"""Bounded card attachments, immutable blobs, and replayable attachment audit."""

import base64
import binascii
import datetime
import hashlib
import os
import pathlib
import re
import tempfile
import uuid
from dataclasses import dataclass
from typing import cast


class AttachmentTooLargeError(ValueError):
    """A bounded upload refusal that the HTTP service reports as 413."""


SIZE_REFUSAL = "Attachment refused: the maximum is 10 MiB per file for Git storage."
MAX_NAME_LENGTH = 255
MAX_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * ((MAX_BYTES + 2) // 3) + 4096
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
ACTIONS = frozenset({"attached", "attachment_removed", "cover_set", "cover_cleared"})


def valid_id(value: object) -> str:
    """Require one canonical GUID, never a caller-provided filesystem path."""
    try:
        canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError as exc:
        raise ValueError("attachment ID must be a canonical GUID") from exc
    if canonical is None or canonical != value:
        raise ValueError("attachment ID must be a canonical GUID")
    return canonical


def extension(name: str) -> str:
    """Preserve a simple file extension while keeping generated paths unambiguous."""
    suffix = pathlib.PurePath(name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) else ".bin"


def valid_name(value: object) -> str:
    """Keep one printable filename for display and download headers."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_NAME_LENGTH
        or value in {".", ".."}
        or any(not char.isprintable() or char in "/\\" for char in value)
    ):
        raise ValueError("attachment name must be one printable filename (up to 255 characters)")
    return value


def _text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"attachment {key} must be a non-empty string")
    return value


def _record(raw: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(raw, dict) or set(cast("dict[str, object]", raw)) != keys:
        raise ValueError(f"attachment record must contain exactly {', '.join(sorted(keys))}")
    return cast("dict[str, object]", raw)


def _timestamp(value: str) -> str:
    if datetime.datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("attachment timestamp must include its time zone")
    return value


def media_type(blob: bytes) -> str:
    """Only known raster signatures get inline previews; all other files download."""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def decode_upload(raw: object) -> bytes:
    """Bound the encoded and decoded upload before any board or disk mutation."""
    if not isinstance(raw, str):
        raise TypeError("attachment data must be base64 text")
    if len(raw) > 4 * ((MAX_BYTES + 2) // 3):
        raise AttachmentTooLargeError(
            "Attachment refused: the maximum is 10 MiB per file for Git storage."
        )
    try:
        blob = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("attachment data is not valid base64") from exc
    if not blob:
        raise ValueError("empty attachments are not supported")
    if len(blob) > MAX_BYTES:
        raise AttachmentTooLargeError(
            "Attachment refused: the maximum is 10 MiB per file for Git storage."
        )
    return blob


@dataclass(frozen=True)
class Attachment:
    """One immutable content reference and its original upload attribution."""

    id: str
    name: str
    size: int
    media_type: str
    sha256: str
    at: str
    by: str

    def to_json(self) -> dict[str, object]:
        """Metadata travels in the board; file bytes remain beside it."""
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "at": self.at,
            "by": self.by,
        }

    @classmethod
    def from_json(cls, raw: object) -> Attachment:
        """Refuse malformed metadata before it can influence a path or response."""
        data = _record(raw, {"id", "name", "size", "mediaType", "sha256", "at", "by"})
        size = data["size"]
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise ValueError("attachment size must be between 1 byte and 10 MiB")
        mime = _text(data, "mediaType")
        if mime not in IMAGE_TYPES | {"application/octet-stream"}:
            raise ValueError("unsupported attachment mediaType")
        digest = _text(data, "sha256")
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("attachment sha256 must be a content digest")
        return cls(
            valid_id(data["id"]),
            valid_name(data["name"]),
            size,
            mime,
            digest,
            _timestamp(_text(data, "at")),
            _text(data, "by"),
        )


@dataclass(frozen=True)
class AttachmentEvent:
    """An actor-attributed upload, removal, or cover choice."""

    at: str
    by: str
    action: str
    attachment_id: str
    extension: str

    def to_json(self) -> dict[str, object]:
        """Keep audit records free of file contents and filename prose."""
        return {
            "at": self.at,
            "by": self.by,
            "action": self.action,
            "attachmentId": self.attachment_id,
            "extension": self.extension,
        }

    @classmethod
    def from_json(cls, raw: object) -> AttachmentEvent:
        """Read one complete, known audit action."""
        data = _record(raw, {"at", "by", "action", "attachmentId", "extension"})
        action = _text(data, "action")
        if action not in ACTIONS:
            raise ValueError("unknown attachment audit action")
        suffix = _text(data, "extension")
        if re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) is None:
            raise ValueError("attachment audit extension is not a simple suffix")
        return cls(
            _timestamp(_text(data, "at")),
            _text(data, "by"),
            action,
            valid_id(data["attachmentId"]),
            suffix,
        )


def parse_card(
    raw: dict[str, object],
) -> tuple[list[Attachment], list[AttachmentEvent], str | None]:
    """Older cards omit all three optional fields."""
    files, events = raw.get("attachments", []), raw.get("attachmentHistory", [])
    if not isinstance(files, list) or not isinstance(events, list):
        raise TypeError("attachments and attachmentHistory must be lists")
    attachments = [Attachment.from_json(file) for file in cast("list[object]", files)]
    history = [AttachmentEvent.from_json(event) for event in cast("list[object]", events)]
    if len({file.id for file in attachments}) != len(attachments):
        raise ValueError("duplicate attachment ID on card")
    cover = raw.get("coverAttachment")
    if cover is not None:
        valid_id(cover)
        if not any(file.id == cover and file.media_type in IMAGE_TYPES for file in attachments):
            raise ValueError("coverAttachment must name an attached raster image")
    return attachments, history, cast("str | None", cover)


def audit_problems(
    files: list[Attachment], history: list[AttachmentEvent], cover: str | None, actors: set[str]
) -> list[str]:
    """Replay attachment membership and cover choice without opening file contents."""
    active: set[str] = set()
    chosen: str | None = None
    problems: list[str] = []
    for event in history:
        aid = event.attachment_id
        if event.by not in actors:
            problems.append("attachment history names an unknown actor")
        if event.action == "attached":
            if aid in active:
                problems.append("attachment history repeats an active upload")
            active.add(aid)
        elif event.action == "attachment_removed":
            if aid not in active:
                problems.append("attachment history removes an unattached file")
            active.discard(aid)
        elif event.action == "cover_set":
            if aid not in active:
                problems.append("attachment history chooses an unattached cover")
            chosen = aid
        elif event.action == "cover_cleared":
            if chosen != aid:
                problems.append("attachment history clears a different cover")
            chosen = None
    if active != {file.id for file in files} or chosen != cover:
        problems.append("attachment history does not match card attachments or cover")
    if any(file.by not in actors for file in files):
        problems.append("attachment metadata names an unknown actor")
    return problems


def blob_path(
    board_path: pathlib.Path, ticket: int, attachment_id: str, suffix: str
) -> pathlib.Path:
    """Resolve only GUID-named files inside the board's per-ticket directory."""
    if ticket < 1 or re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) is None:
        raise ValueError("invalid attachment card number or extension")
    root = board_path.resolve().parent
    directory = (root / "attachments" / f"{ticket:04d}").resolve()
    path = (directory / (valid_id(attachment_id) + suffix)).resolve()
    if directory.parent != root / "attachments" or path.parent != directory:
        raise ValueError("attachment storage must stay inside the board directory")
    return path


def read_blob(board_path: pathlib.Path, ticket: int, file: Attachment) -> bytes:
    """Refuse oversized, missing, or altered bytes instead of serving another file."""
    with blob_path(board_path, ticket, file.id, extension(file.name)).open("rb") as stream:
        blob = stream.read(MAX_BYTES + 1)
    if len(blob) != file.size or hashlib.sha256(blob).hexdigest() != file.sha256:
        raise ValueError("attachment bytes do not match their saved size and digest")
    return blob


def write_blob(board_path: pathlib.Path, ticket: int, file: Attachment, blob: bytes) -> bool:
    """Flush an immutable file before the snapshot can reference it; return whether new."""
    path = blob_path(board_path, ticket, file.id, extension(file.name))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        read_blob(board_path, ticket, file)
        return False
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    temp_path = pathlib.Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return True
