"""Frozen restore digests, read-only proofs, and refusal at every seal boundary."""

import base64
import hashlib
import json
from datetime import datetime, timedelta
from typing import cast
lazy import pathlib

import pytest

from localswim import attachments, board_state, restore_candidate
from localswim import restore_plan as plan
from localswim.restore_paths import PathBoundaryError
from tests.test_restore_candidate import AID, BLOB, attach, floor_board, snapshot


def parsed(blob: bytes) -> board_state.JsonObject:
    return cast("board_state.JsonObject", json.loads(blob))


def object_field(raw: board_state.JsonObject, key: str) -> board_state.JsonObject:
    return cast("board_state.JsonObject", raw[key])


def seal(tmp_path: pathlib.Path, board: board_state.Board) -> tuple[plan.SealedPlan, pathlib.Path]:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    return plan.create_plan(source, target, acknowledge_incomplete_recovery=True), target


def test_domains_are_acyclic_and_exact_final_bytes_are_preserved(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.project = "cafe\u0301 / café\nno normalization"
    sealed, target = seal(tmp_path, board)
    envelope = parsed(sealed.document)
    core = sealed.core
    final = parsed(sealed.final_snapshot)
    receipt = object_field(final, "restoreReceipt")
    body = {key: value for key, value in final.items() if key != "restoreReceipt"}
    assert core["preparedBodyDigest"] == hashlib.sha256(plan.canonical_json(body)).hexdigest()
    assert sealed.plan_digest == hashlib.sha256(plan.canonical_json(core)).hexdigest()
    assert receipt["planDigest"] == sealed.plan_digest
    assert envelope["finalSnapshotDigest"] == hashlib.sha256(sealed.final_snapshot).hexdigest()
    assert base64.b64decode(cast("str", envelope["finalSnapshotBase64"])) == sealed.final_snapshot
    assert sealed.final_snapshot.endswith(b"\n")
    assert plan.canonical_json(core).endswith(b"}")
    assert not {"planDigest", "finalSnapshotDigest", "finalSnapshotBase64"} & core.keys()
    assert "restoreReceipt" not in body
    assert final["project"] == board.project
    before = target.read_bytes()
    verified = plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)
    assert verified.final_snapshot == sealed.final_snapshot
    assert verified.document is sealed.document
    assert target.read_bytes() == before
    core["generation"] = "changed detached view"
    assert sealed.core["generation"] != core["generation"]


def test_frozen_verification_never_allocates_reserializes_or_adopts(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    floor_board(board)
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "target" / "board.json"
    sealed = plan.create_plan(source, target, port=9040, acknowledge_raw_recovery=True)
    assert sealed.core["promotedCards"] == ["leaf"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("verification regenerated frozen content")

    monkeypatch.setattr(board_state, "now", forbidden)
    monkeypatch.setattr(plan.uuid, "uuid4", forbidden)
    monkeypatch.setattr(board_state.Board, "to_json", forbidden)
    monkeypatch.setattr(board_state.Board, "enforce_dependency_priorities", forbidden)
    for _ in range(2):
        verified = plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)
        assert verified.final_snapshot == sealed.final_snapshot
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert not target.exists()


def test_independent_plans_have_distinct_generations_and_keep_counters(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    board.revision = 21
    source = snapshot(tmp_path / "source", board, schema=4)
    board.revision = 90
    board.next_ticket = 99
    target = snapshot(tmp_path / "target", board)
    one, two = plan.create_plan(source, target), plan.create_plan(source, target)
    assert one.core["generation"] != two.core["generation"]
    assert one.core["operationId"] != two.core["operationId"]
    assert one.core["generation"] != board.generation
    assert one.core["revision"] == 91
    assert one.core["nextTicket"] == 99
    assert object_field(one.core, "source")["revision"] == 21
    assert object_field(one.core, "source")["generation"] is None
    assert parsed(target.read_bytes())["generation"] == board.generation


@pytest.mark.parametrize("domain", ["manifest", "receipt", "body", "final", "envelope", "expected"])
def test_mutations_cannot_substitute_a_valid_frozen_board(
    tmp_path: pathlib.Path, board: board_state.Board, domain: str
) -> None:
    attach(board, tmp_path / "source")
    sealed, target = seal(tmp_path, board)
    envelope = parsed(sealed.document)
    core = object_field(envelope, "core")
    final = parsed(sealed.final_snapshot)
    expected = sealed.plan_digest
    if domain == "manifest":
        cast("list[board_state.JsonObject]", core["manifest"])[0]["sha256"] = "a" * 64
    elif domain == "receipt":
        object_field(final, "restoreReceipt")["operationId"] = board.generation
    elif domain == "body":
        final["project"] = "Valid substituted body"
    elif domain == "final":
        envelope["finalSnapshotDigest"] = "f" * 64
    elif domain == "envelope":
        envelope["credential"] = "forbidden extra field"
    else:
        expected = "a" * 64
    if domain in {"body", "receipt"}:
        payload = plan.canonical_json(final) + b"\n"
        envelope["finalSnapshotBase64"] = base64.b64encode(payload).decode("ascii")
        envelope["finalSnapshotDigest"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(plan.PlanError):
        plan.verify_plan(plan.canonical_json(envelope), expect_plan=expected, target=target)


@pytest.mark.parametrize(
    "alternate", ["leading", "interior", "key_order", "extra_lf", "missing_lf", "unicode"]
)
def test_reviewed_digest_refuses_alternate_final_byte_encodings(
    tmp_path: pathlib.Path, board: board_state.Board, alternate: str
) -> None:
    board.project = "café"
    sealed, target = seal(tmp_path, board)
    envelope = parsed(sealed.document)
    final = sealed.final_snapshot
    if alternate == "leading":
        changed = b" " + final
    elif alternate == "interior":
        changed = final.replace(b":", b": ", 1)
    elif alternate == "key_order":
        changed = (
            json.dumps(
                dict(reversed(list(parsed(final).items()))),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    elif alternate == "extra_lf":
        changed = final + b"\n"
    elif alternate == "missing_lf":
        changed = final[:-1]
    else:
        changed = final.replace("café".encode(), b"caf\\u00e9")
    assert changed != final
    assert parsed(changed) == parsed(final)
    envelope["finalSnapshotBase64"] = base64.b64encode(changed).decode("ascii")
    envelope["finalSnapshotDigest"] = hashlib.sha256(changed).hexdigest()
    with pytest.raises(plan.PlanError, match=r"canonical encoding|final LF"):
        plan.verify_plan(
            plan.canonical_json(envelope), expect_plan=sealed.plan_digest, target=target
        )


@pytest.mark.parametrize("drift", ["source", "target", "source_blob", "collision", "current_blob"])
def test_every_fresh_byte_domain_refuses_drift(
    tmp_path: pathlib.Path, board: board_state.Board, drift: str
) -> None:
    source_blob = attach(board, tmp_path / "source")
    source = snapshot(tmp_path / "source", board)
    target_blob = tmp_path / "target" / "attachments" / "0001" / f"{AID}.bin"
    target_blob.parent.mkdir(parents=True)
    target_blob.write_bytes(BLOB)
    target = snapshot(tmp_path / "target", board)
    sealed = plan.create_plan(source, target)
    path = {
        "source": source,
        "target": target,
        "source_blob": source_blob,
        "collision": target_blob,
        "current_blob": target_blob,
    }[drift]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises((ValueError, PathBoundaryError)):
        plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)


def test_same_bytes_replaced_source_identity_and_wrong_target_refuse(
    tmp_path: pathlib.Path, board: board_state.Board
) -> None:
    sealed, target = seal(tmp_path, board)
    with pytest.raises(plan.PlanError):
        plan.verify_plan(
            sealed.document,
            expect_plan=sealed.plan_digest,
            target=tmp_path / "other" / "board.json",
        )
    source = tmp_path / "source" / "board.json"
    replacement = source.with_suffix(".new")
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
    with pytest.raises(ValueError, match=r"identity|drift"):
        plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)


def test_expiry_uses_only_current_time_and_refuses_at_boundary(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed, target = seal(tmp_path, board)
    expires = datetime.fromisoformat(cast("str", sealed.core["expiresAt"]))
    timestamp = datetime.fromisoformat(cast("str", sealed.core["timestamp"]))
    assert expires - timestamp == timedelta(minutes=30)

    class Expired(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # noqa: ARG003
            return expires

    monkeypatch.setattr(plan, "datetime", Expired)
    with pytest.raises(plan.PlanError, match="expired"):
        plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)


@pytest.mark.parametrize(
    "blob",
    [
        b'{"core":NaN}',
        b'{"core":Infinity}',
        b'{"core":1.0}',
        b'{"core":{},"core":{}}',
        b"[]",
        b"\xff",
        b"{",
    ],
)
def test_malformed_or_non_integer_json_refuses(blob: bytes, tmp_path: pathlib.Path) -> None:
    with pytest.raises(plan.PlanError):
        plan.verify_plan(blob, expect_plan="a" * 64, target=tmp_path / "target.json")


@pytest.mark.parametrize("kind", ["missing", "corrupt", "incomplete"])
def test_recovery_acknowledgments_and_explicit_missing_port(
    tmp_path: pathlib.Path, board: board_state.Board, kind: str
) -> None:
    source = snapshot(tmp_path / "source", board)
    target = tmp_path / "target" / "board.json"
    if kind == "corrupt":
        target.parent.mkdir()
        target.write_bytes(b"corrupt")
    elif kind == "incomplete":
        current_blob = attach(board, target.parent)
        snapshot(target.parent, board)
        current_blob.unlink()
    with pytest.raises(plan.PlanError, match="acknowledgment"):
        plan.create_plan(source, target, port=9010)
    if kind != "incomplete":
        with pytest.raises(plan.PlanError, match="explicit intended port"):
            plan.create_plan(source, target, acknowledge_raw_recovery=True)
    sealed = plan.create_plan(
        source,
        target,
        port=9010,
        acknowledge_raw_recovery=True,
        acknowledge_incomplete_recovery=True,
    )
    assert (
        plan.verify_plan(
            sealed.document, expect_plan=sealed.plan_digest, target=target
        ).final_snapshot
        == sealed.final_snapshot
    )


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_invalid_port_refuses(tmp_path: pathlib.Path, board: board_state.Board, port: int) -> None:
    source = snapshot(tmp_path / "source", board)
    target = snapshot(tmp_path / "target", board)
    with pytest.raises(plan.PlanError, match="port"):
        plan.create_plan(source, target, port=port)


@pytest.mark.parametrize("limit", ["document", "snapshot", "references", "total", "file"])
def test_plan_and_verifier_caps(
    tmp_path: pathlib.Path, board: board_state.Board, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    attach(board, tmp_path / "source")
    sealed, target = seal(tmp_path, board)
    if limit == "document":
        monkeypatch.setattr(plan, "MAX_PLAN_BYTES", len(sealed.document) - 1)
    elif limit == "snapshot":
        monkeypatch.setattr(restore_candidate, "MAX_SNAPSHOT_BYTES", len(sealed.final_snapshot) - 1)
    elif limit == "references":
        monkeypatch.setattr(restore_candidate, "MAX_REFERENCES", 0)
    elif limit == "total":
        monkeypatch.setattr(restore_candidate, "MAX_TOTAL_BYTES", len(BLOB) - 1)
    else:
        monkeypatch.setattr(attachments, "MAX_BYTES", len(BLOB) - 1)
    with pytest.raises((plan.PlanError, PathBoundaryError)):
        plan.verify_plan(sealed.document, expect_plan=sealed.plan_digest, target=target)


def test_canonical_json_has_no_floats_or_text_normalization() -> None:
    assert plan.canonical_json({"z": "cafe\u0301", "a": "café"}) == (
        '{"a":"café","z":"cafe\u0301"}'.encode()
    )
    with pytest.raises(plan.PlanError, match="floating"):
        plan.canonical_json({"nested": [0.5]})
