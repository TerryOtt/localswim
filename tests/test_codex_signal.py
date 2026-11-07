"""Queue adapter qualification using synthetic local children, never real delivery."""

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from localswim import codex_signal as signal
from localswim.service_ownership import python_child_command

_THREAD = "00000000-0000-4000-8000-000000000001"
_CHILD = r"""
import json,os,pathlib,sys,time
mode,capture,*arguments=sys.argv[1:]
pathlib.Path(capture).write_text(json.dumps({"pid":os.getpid(),"arguments":arguments}),encoding="utf-8")
if mode=="stall": time.sleep(30)
if mode=="noisy":
    sys.stdout.write("SECRET"*100000)
    sys.stderr.write("SECRET"*100000)
sys.exit(1 if mode=="reject" else 0)
"""

type Harness = Callable[[str], tuple[Path, list[subprocess.Popen[bytes]]]]


def qualified_executable(_name: str) -> str:
    return "qualified-codex.exe"


def payload(**overrides: object) -> dict[str, object]:
    return {
        "boardIdentity": "a" * 64,
        "label": "Synthetic board",
        "since": "2026-11-07T10:30:00+00:00",
        "until": "2026-11-07T10:31:00Z",
        "tickets": [12, 3, 12],
        "truncated": False,
        **overrides,
    }


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    original = subprocess.Popen

    def configure(mode: str) -> tuple[Path, list[subprocess.Popen[bytes]]]:
        capture = tmp_path / f"{mode}.json"
        children: list[subprocess.Popen[bytes]] = []

        def spawn(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            assert args[:4] == ["qualified-codex.exe", "queue", "--thread", _THREAD]
            assert args[4] == "--message" and len(args) == 6
            assert set(kwargs) == {"stdin", "stdout", "stderr", "creationflags"}
            assert all(
                kwargs[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr")
            )
            environment = os.environ.copy()
            command = python_child_command(
                ["-u", "-c", _CHILD, mode, str(capture), *args[1:]], environment
            )
            child = original(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=cast("int", kwargs["creationflags"]),
            )
            children.append(child)
            return child

        monkeypatch.setattr(signal.shutil, "which", qualified_executable)
        monkeypatch.setattr(signal.subprocess, "Popen", spawn)
        return capture, children

    return configure


@pytest.mark.parametrize(
    "mode,expected", [("accept", "accepted"), ("reject", "rejected"), ("noisy", "accepted")]
)
def test_native_queue_is_single_attempt_and_output_is_discarded(
    mode: str, expected: str, harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    capture, children = harness(mode)
    assert signal.deliver(_THREAD, payload()) == expected
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["pid"] == children[0].pid
    arguments = captured["arguments"]
    assert arguments[:4] == ["queue", "--thread", _THREAD, "--message"]
    assert arguments[4] == (
        "localswim board-change notice (metadata only).\n"
        'Board label: "Synthetic board"\n'
        f"Board identity: {'a' * 64}\n"
        "Committed activity: 2026-11-07T10:30:00+00:00 through 2026-11-07T10:31:00+00:00\n"
        "Tickets: #0003, #0012\n"
        "Inspect activity before acting; follow the board's authorization gates."
    )
    assert len(children) == 1 and children[0].poll() is not None
    assert capsys.readouterr() == ("", "")


def test_message_quotes_label_offsets_and_reports_truncation(harness: Harness) -> None:
    capture, _children = harness("accept")
    assert (
        signal.deliver(
            _THREAD,
            payload(
                label='Synthetic "quoted"',
                tickets=[],
                truncated=True,
                since="2026-11-07T05:30:00-05:00",
            ),
        )
        == "accepted"
    )
    message = json.loads(capture.read_text(encoding="utf-8"))["arguments"][-1]
    assert 'Board label: "Synthetic \\"quoted\\""' in message
    assert "2026-11-07T10:30:00+00:00" in message
    assert "Tickets: unspecified (additional tickets omitted)" in message


def test_timeout_reaps_exact_child_without_retry(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture, children = harness("stall")
    monkeypatch.setattr(signal, "_TIMEOUT", 0.3)
    started = time.monotonic()
    assert signal.deliver(_THREAD, payload()) == "timeout"
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None


def test_stop_reaps_exact_child_without_retry(harness: Harness) -> None:
    _capture, children = harness("stall")
    stopped = threading.Event()
    timer = threading.Timer(0.2, stopped.set)
    timer.start()
    try:
        assert signal.deliver(_THREAD, payload(), stopped) == "stopped"
    finally:
        timer.join()
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret": "not allowed"},
        {"boardIdentity": "file://private"},
        {"label": ""},
        {"label": "x" * 161},
        {"label": "line\nbreak"},
        {"label": "control\x7f"},
        {"since": "2026-11-07T10:32:00Z"},
        {"since": "2026-11-07T10:30:00"},
        {"until": "bad"},
        {"until": "x" * 1000},
        {"tickets": [True]},
        {"tickets": [0]},
        {"tickets": [-1]},
        {"tickets": ["12"]},
        {"tickets": [1] * 129},
        {"tickets": None},
        {"truncated": 1},
    ],
)
def test_invalid_metadata_never_resolves_executable(
    overrides: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_name: str) -> str:
        pytest.fail("invalid metadata reached executable resolution")

    monkeypatch.setattr(signal.shutil, "which", forbidden)
    assert signal.deliver(_THREAD, payload(**overrides)) == "invalid"


def test_missing_field_message_bound_and_thread_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signal.shutil, "which", qualified_executable)
    value = payload()
    value.pop("label")
    assert signal.deliver(_THREAD, value) == "invalid"
    assert signal.deliver("thread\nsecret", payload()) == "invalid"
    monkeypatch.setattr(signal, "_MAX_MESSAGE_BYTES", 100)
    assert signal.deliver(_THREAD, payload()) == "invalid"


def test_stopped_before_start_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_name: str) -> str:
        pytest.fail("stopped delivery reached executable resolution")

    monkeypatch.setattr(signal.shutil, "which", forbidden)
    stopped = threading.Event()
    stopped.set()
    assert signal.deliver(_THREAD, payload(), stopped) == "stopped"


def test_unavailable_and_launch_failure_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_name: str) -> None:
        return None

    monkeypatch.setattr(signal.shutil, "which", missing)
    assert signal.deliver(_THREAD, payload()) == "unavailable"
    monkeypatch.setattr(signal.shutil, "which", qualified_executable)

    def failed(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("SECRET")

    monkeypatch.setattr(signal.subprocess, "Popen", failed)
    assert signal.deliver(_THREAD, payload()) == "unavailable"
    assert capsys.readouterr() == ("", "")


def test_cleanup_escalates_only_exact_owned_child() -> None:
    owned = Mock()
    owned.poll.return_value = None
    owned.wait.side_effect = [subprocess.TimeoutExpired("synthetic-queue", 0.5), 0]
    signal._cleanup(cast("subprocess.Popen[bytes]", owned))  # noqa: SLF001 - cleanup seam
    owned.terminate.assert_called_once_with()
    owned.kill.assert_called_once_with()
    assert owned.wait.call_count == 2


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".com", ""])
def test_windows_refuses_command_wrappers(suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signal.sys, "platform", "win32")

    def wrapper(name: str) -> str:
        assert name == "codex.exe"
        return "C:/synthetic/codex" + suffix

    def forbidden(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        pytest.fail("command wrapper reached process creation")

    monkeypatch.setattr(signal.shutil, "which", wrapper)
    monkeypatch.setattr(signal.subprocess, "Popen", forbidden)
    assert signal.deliver(_THREAD, payload()) == "unavailable"


def test_windows_prefers_native_executable_over_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signal.sys, "platform", "win32")

    def resolve(name: str) -> str:
        return "C:/synthetic/codex.EXE" if name == "codex.exe" else "C:/synthetic/codex.cmd"

    owned = Mock()
    owned.wait.return_value = 0
    owned.poll.return_value = 0
    spawn = Mock(return_value=owned)
    monkeypatch.setattr(signal.shutil, "which", resolve)
    monkeypatch.setattr(signal.subprocess, "Popen", spawn)
    assert signal.deliver(_THREAD, payload()) == "accepted"
    assert spawn.call_args.args[0][0] == "C:/synthetic/codex.EXE"
