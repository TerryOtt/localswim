"""Service output must not depend on a pipe reader or change safe destinations."""

import contextlib
import io
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

import pytest

from localswim import service_output, workspace
from tests.test_workspace import make_board

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


@contextlib.contextmanager
def full_pipe() -> Generator[tuple[int, int, Callable[[], None]]]:
    """Hold a full pipe without consuming it until the tested child has exited."""
    reader, writer = os.pipe()
    started = threading.Event()

    def fill() -> None:
        started.set()
        with contextlib.suppress(BrokenPipeError):
            os.write(writer, b"p" * (4 * 1024 * 1024))

    thread = threading.Thread(target=fill)

    def start_fill() -> None:
        thread.start()
        assert started.wait(2)
        thread.join(0.2)
        assert thread.is_alive(), "fixture write must exceed pipe capacity"

    try:
        yield reader, writer, start_fill
    finally:
        os.close(reader)
        if thread.ident is not None:
            thread.join(5)
            assert not thread.is_alive()
        os.close(writer)


def test_full_inherited_pipes_allow_output_children_and_reexec(tmp_path: pathlib.Path) -> None:
    report = tmp_path / "report.json"
    script = """
import http.server, json, os, pathlib, subprocess, sys, time
from localswim import service_output
if len(sys.argv) == 3:
    # Exec must retain both CRT and native Windows standard handles.
    subprocess.run([sys.executable, '-c', "print('native-after-exec')"], check=True)
    print('after-exec', flush=True)
    pathlib.Path(sys.argv[1] + '.done').write_text('done')
else:
    pathlib.Path(sys.argv[1] + '.ready').write_text('ready')
    deadline = time.monotonic() + 10
    while not pathlib.Path(sys.argv[1] + '.go').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test synchronization timeout')
        time.sleep(.01)
    paths = service_output.configure()
    pathlib.Path(sys.argv[1]).write_text(json.dumps({k: str(v) for k,v in paths.items()}))
    print('x' * (1024 * 1024), flush=True)
    print('y' * (1024 * 1024), file=sys.stderr, flush=True)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            print('h' * (1024 * 1024), flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'progress')
        def log_message(self, *args):
            pass
    with http.server.HTTPServer(('127.0.0.1', 0), Handler) as server:
        pathlib.Path(sys.argv[1] + '.port').write_text(str(server.server_port))
        server.handle_request()
    subprocess.run([sys.executable, '-c', "print('explicit-child')"],
                   check=True, **service_output.child_output())
    subprocess.run([sys.executable, '-c', "print('native-child')"], check=True)
    assert service_output.configure() == {}
    os.execv(sys.executable, [sys.executable, __file__,
                             sys.argv[1], 'replacement'])
"""
    script_path = tmp_path / "child.py"
    script_path.write_text(script)
    with full_pipe() as (_, writer, start_fill):
        child = subprocess.Popen(
            [sys.executable, str(script_path), str(report)], stdout=writer, stderr=writer
        )
        try:
            deadline = time.monotonic() + 10
            while not pathlib.Path(str(report) + ".ready").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            start_fill()
            pathlib.Path(str(report) + ".go").write_text("go")
            deadline = time.monotonic() + 10
            port_path = pathlib.Path(str(report) + ".port")
            while not port_path.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with urllib.request.urlopen(
                "http://127.0.0.1:" + port_path.read_text(), timeout=5
            ) as response:
                assert response.read() == b"progress"
            assert child.wait(timeout=20) == 0
            # Windows execv replaces through a new process; wait for its marker.
            deadline = time.monotonic() + 10
            while not pathlib.Path(str(report) + ".done").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
        finally:
            # Only test-owned children may be cleaned up after a failed assertion.
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    assert pathlib.Path(str(report) + ".done").read_text() == "done"
    paths = json.loads(report.read_text())
    assert set(paths) == {"stdout", "stderr"}
    stdout = pathlib.Path(paths["stdout"])
    stderr = pathlib.Path(paths["stderr"])
    assert stdout.parent == stderr.parent
    assert stdout.stat().st_size > 1024 * 1024
    assert stderr.stat().st_size > 1024 * 1024
    text = stdout.read_text()
    assert "explicit-child" in text
    assert "native-child" in text
    assert "native-after-exec" in text
    assert "after-exec" in text
    if os.name == "nt":
        command = """
$paths = @($env:LOCAlSWIM_TEST_LOG, (Split-Path $env:LOCAlSWIM_TEST_LOG))
$result = foreach ($path in $paths) {
  (Get-Acl -LiteralPath $path).Access | ForEach-Object {
    $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  }
}
@{ sids = @($result); user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value } | ConvertTo-Json -Compress
"""
        acl = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            env={**os.environ, "LOCAlSWIM_TEST_LOG": str(stdout)},
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        permissions = json.loads(acl.stdout)
        assert set(permissions["sids"]) <= {permissions["user"], "S-1-5-18", "S-1-5-32-544"}
    else:
        assert stat.S_IMODE(stdout.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(stdout.stat().st_mode) == 0o600


@pytest.mark.parametrize("destination", ["file", "null"])
def test_safe_destinations_remain_unchanged(tmp_path: pathlib.Path, destination: str) -> None:
    target = tmp_path / "existing.log" if destination == "file" else pathlib.Path(os.devnull)
    report = tmp_path / "report.json"
    script = """
import json, os, pathlib, sys
from localswim import service_output
before = [os.fstat(s.fileno()) for s in (sys.stdout, sys.stderr)]
assert service_output.configure() == {}
after = [os.fstat(s.fileno()) for s in (sys.stdout, sys.stderr)]
assert [(s.st_dev,s.st_ino,s.st_mode) for s in before] == [(s.st_dev,s.st_ino,s.st_mode) for s in after]
print('preserved', flush=True)
pathlib.Path(sys.argv[1]).write_text('done')
"""
    with target.open("w") as output:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(report)],
            stdout=output,
            stderr=output,
            timeout=10,
            check=False,
        )
    assert completed.returncode == 0
    assert report.read_text() == "done"
    if destination == "file":
        assert target.read_text() == "preserved\n"


def test_embedded_memory_streams_remain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    assert service_output.configure() == {}
    assert service_output.child_output() == {"stdout": stdout, "stderr": stderr}


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal qualification")
def test_terminal_destination_is_safe() -> None:
    import pty

    master, terminal = pty.openpty()
    try:
        assert service_output._safe_destination(terminal)  # noqa: SLF001 -- boundary probe
    finally:
        os.close(master)
        os.close(terminal)


@pytest.mark.parametrize(
    ("arguments", "returncode", "channel", "expected"),
    [
        (["--help"], 0, "stdout", "Serve a localswim board."),
        (["--not-a-localswim-option"], 2, "stderr", "error:"),
    ],
)
def test_cli_parser_output_remains_captured(
    arguments: list[str], returncode: int, channel: str, expected: str
) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "localswim.api_endpoint", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == returncode
    assert expected in getattr(completed, channel)


@pytest.mark.skipif(os.name != "nt", reason="native Windows standard handle mapping")
def test_duplicate_stdout_descriptor_updates_native_stdout(tmp_path: pathlib.Path) -> None:
    report = tmp_path / "duplicate-output.json"
    script = """
import json, os, pathlib, subprocess, sys
from localswim import service_output
sys.stdout = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8')
assert sys.stdout.fileno() != 1
paths = service_output.configure()
pathlib.Path(sys.argv[1]).write_text(json.dumps({k: str(v) for k,v in paths.items()}))
subprocess.run([sys.executable, '-c', "print('n' * (1024 * 1024))"], check=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(report)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert not completed.stdout
    paths = json.loads(report.read_text())
    assert pathlib.Path(paths["stdout"]).stat().st_size > 1024 * 1024
    assert pathlib.Path(paths["stderr"]).stat().st_size == 0


@pytest.mark.parametrize("contents", ["{", "{}"])
def test_board_admission_error_remains_captured(tmp_path: pathlib.Path, contents: str) -> None:
    board = tmp_path / "invalid-board.json"
    board.write_text(contents)
    completed = subprocess.run(
        [sys.executable, "-m", "localswim.api_endpoint", str(board), "--port", "0"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode != 0
    assert "board admission failed" in completed.stderr
    assert "Serving " not in completed.stdout


@pytest.mark.parametrize("mode", ["board", "workspace"])
def test_listener_refusal_remains_captured(tmp_path: pathlib.Path, mode: str) -> None:
    board = tmp_path / "board.json"
    make_board(board, "output refusal fixture")
    target = board if mode == "board" else tmp_path
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "localswim.api_endpoint",
                str(target),
                "--port",
                str(listener.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert completed.returncode != 0
    assert completed.stderr.strip()
    assert not completed.stdout.strip()


def test_workspace_closes_listener_when_output_configuration_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers: list[workspace.WorkspaceServer] = []
    original = workspace.WorkspaceServer

    def capture_server(
        address: tuple[str, int], state: workspace.Workspace
    ) -> workspace.WorkspaceServer:
        server = original(address, state)
        servers.append(server)
        return server

    def fail_output() -> dict[str, pathlib.Path]:
        raise OSError("controlled output setup failure")

    monkeypatch.setattr(workspace, "WorkspaceServer", capture_server)
    monkeypatch.setattr(service_output, "configure", fail_output)
    with pytest.raises(OSError, match="controlled output setup failure"):
        workspace.serve(tmp_path, 0)
    assert len(servers) == 1
    assert servers[0].socket.fileno() == -1
