"""Synthetic 10x observed card-content and board-count qualification.

No private board content is copied. Timings are observations, never pass thresholds.
"""

# ruff: noqa: SLF001 -- deterministic admission work counters intentionally use the seam

import copy
import hashlib
import json
import pathlib
import threading
import time
import urllib.request
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from localswim import api_endpoint, attachments, board_state, workspace
from tests.support import USERS
from tests.test_attachments import PNG

CARD_COUNT = 4_120
LARGE_CARD_BYTES = 2_978_150


def _make_board(count: int, *, enlarged: bool) -> board_state.Board:
    board = board_state.Board(
        project="Synthetic scale matrix",
        users=USERS,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    # Synthetic permissions avoid replaying the growing board for every setup move.
    board.grant_permissions(["terry"], ["completed"], [], "terry")
    for number in range(count):
        ref = f"synthetic-{number}"
        state = "backlog" if number % 3 == 0 else "completed"
        board.create(ref, f"Synthetic card {number}", state, "terry")
        board.comment(ref, "Synthetic initial investigation. " * 8, "bot")
        board.comment(ref, "Synthetic verification evidence. " * 8, "bot")
        board.assign(ref, "terry", "bot")
        board.assign(ref, "bot", "terry")
        if number % 3 == 2:
            board.set_archived(ref, archived=True, by="terry")
    if enlarged:
        for number in range(192):
            board.comment("synthetic-0", "Synthetic bounded evidence. " * 640, "bot")
            board.assign("synthetic-0", "terry" if number % 2 == 0 else "bot", "terry")
        assert len(json.dumps(board.items[0].to_json()).encode()) >= LARGE_CARD_BYTES
    return board


@pytest.mark.parametrize(
    ("count", "enlarged"),
    [(1, True), (CARD_COUNT, False), (CARD_COUNT, True)],
    ids=["enlarged-card", "dense-board", "combined"],
)
def test_tenfold_scale_matrix(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    *,
    enlarged: bool,
) -> None:
    metrics: dict[str, Any] = {"cards": count, "enlarged": enlarged}
    started = time.perf_counter()
    board = _make_board(count, enlarged=enlarged)
    metrics["construction_seconds"] = time.perf_counter() - started
    assert not board.verify()
    path = tmp_path / "board.json"
    board_state.save(board, path)
    metrics["board_bytes"] = path.stat().st_size
    metrics["largest_card_bytes"] = max(len(json.dumps(i.to_json()).encode()) for i in board.items)
    metrics["comments"] = sum(len(i.comments) for i in board.items)
    metrics["history_events"] = sum(len(i.history) for i in board.items)
    metrics["archived"] = sum(i.archived for i in board.items)
    metrics["completed"] = sum(i.state == "completed" for i in board.items)
    assert path.stat().st_size < workspace.MAX_CANDIDATE_BYTES == 33_554_432
    started = time.perf_counter()
    with (
        patch.object(board_state, "load_bytes", wraps=board_state.load_bytes) as parse,
        patch.object(
            api_endpoint.BoardStore,
            "_verify_candidate",
            wraps=api_endpoint.BoardStore._verify_candidate,
        ) as verify,
    ):
        store = api_endpoint.BoardStore(path)
        assert parse.call_count == verify.call_count == 1
    metrics["cold_admission_seconds"] = time.perf_counter() - started
    original_read = pathlib.Path.read_bytes
    reads: list[pathlib.Path] = []

    def read(target: pathlib.Path) -> bytes:
        reads.append(target)
        return original_read(target)

    started = time.perf_counter()
    with (
        patch.object(pathlib.Path, "read_bytes", read),
        patch.object(board_state, "load_bytes", wraps=board_state.load_bytes) as parse,
        patch.object(
            api_endpoint.BoardStore,
            "_verify_candidate",
            wraps=api_endpoint.BoardStore._verify_candidate,
        ) as verify,
        patch.object(copy, "deepcopy", wraps=copy.deepcopy) as clone,
    ):
        for _ in range(3):
            status = store.disk_status()
            assert status[-1] == board.revision
        assert reads == [path] * 3
        assert parse.call_count == verify.call_count == clone.call_count == 0
    metrics["three_warm_status_seconds"] = time.perf_counter() - started
    metrics["warm_status_reads"] = len(reads)
    monkeypatch.setattr(api_endpoint, "STORE", store)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", path)
    started = time.perf_counter()
    wire = api_endpoint.payload()
    metrics["payload_seconds"] = time.perf_counter() - started
    metrics["payload_bytes"] = len(wire)
    assert len(wire) < workspace.MAX_UPSTREAM_ENTITY_BYTES == 100_663_296
    payload = json.loads(wire)
    items = [item for lane in payload["lanes"] for item in lane["items"]]
    assert len(items) == count
    assert sum(bool(i.get("archived")) for i in items) == metrics["archived"]
    first = next(i for i in items if i["id"] == "synthetic-0")
    assert [entry["text"] for entry in first["comments"]] == [
        entry.text for entry in board.items[0].comments
    ]
    assert len(next(i for i in items if i["id"] == "synthetic-0")["comments"]) == (
        194 if enlarged else 2
    )
    revision = board.revision
    started = time.perf_counter()
    _, committed = store.execute(
        revision, lambda b: b.comment("synthetic-0", "Synthetic CAS write.", "bot")
    )
    metrics["cas_write_seconds"] = time.perf_counter() - started
    assert committed == revision + 1
    with pytest.raises(api_endpoint.RevisionConflict):
        store.execute(revision, lambda b: b.comment("synthetic-0", "Must not be stored.", "bot"))
    persisted = board_state.load(path)
    assert persisted.revision == committed
    assert persisted.items[0].comments[-1].text == "Synthetic CAS write."
    (tmp_path / "scale-measurements.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    if count == CARD_COUNT and enlarged:
        _attachment_probe(path, store, metrics)
    (tmp_path / "scale-measurements.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )


def _attachment_probe(
    path: pathlib.Path,
    store: api_endpoint.BoardStore,
    metrics: dict[str, Any],
) -> None:
    blobs = [PNG, b"x" * attachments.MAX_BYTES]
    files = [
        attachments.Attachment(
            str(uuid.uuid4()),
            name,
            len(blob),
            attachments.media_type(blob),
            hashlib.sha256(blob).hexdigest(),
            board_state.now(),
            "bot",
        )
        for name, blob in zip(("cover.png", "bounded.bin"), blobs, strict=True)
    ]
    for file, blob in zip(files, blobs, strict=True):
        assert attachments.write_blob(path, 1, file, blob)

    def attach(board: board_state.Board) -> str:
        for file in files:
            board.add_attachment("synthetic-0", file)
        return board.set_cover("synthetic-0", files[0].id, "bot")

    store.execute(store.disk_status()[-1], attach)
    store.disk_status()  # Prime exact accepted bytes before counting GET work.
    server = api_endpoint.BoardHttpServer((api_endpoint.HOST, 0), api_endpoint.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.perf_counter()
        with (
            patch.object(copy, "deepcopy", wraps=copy.deepcopy) as clone,
            patch.object(board_state, "load_bytes", wraps=board_state.load_bytes) as parse,
            patch.object(
                api_endpoint.BoardStore,
                "_verify_candidate",
                wraps=api_endpoint.BoardStore._verify_candidate,
            ) as verify,
        ):
            for file, expected in zip(files, blobs, strict=True):
                url = f"http://127.0.0.1:{server.server_address[1]}{api_endpoint.API_PREFIX}/cards/synthetic-0/attachments/{file.id}"
                with urllib.request.urlopen(url, timeout=30) as response:
                    assert response.read() == expected
                    assert int(response.headers["Content-Length"]) == len(expected)
                    assert response.headers["X-Content-Type-Options"] == "nosniff"
            metrics["attachment_full_board_copies"] = clone.call_count
            assert clone.call_count == parse.call_count == verify.call_count == 0
        metrics["cover_and_ten_mib_download_seconds"] = time.perf_counter() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
