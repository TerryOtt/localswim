"""Legacy invocations fail immediately without starting or touching runtime state."""

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from localswim import monitor, monitor_pool


@pytest.mark.parametrize("entry", [monitor.main, monitor_pool.main])
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--help"],
        ["--reload"],
        ["--allow-polling"],
        ["--manifest", "missing.json", "--stop"],
        ["--board-path", "missing.json"],
    ],
)
def test_stale_entrypoints_refuse_without_side_effects(
    capsys: pytest.CaptureFixture[str],
    entry: object,
    arguments: list[str],
) -> None:
    forbidden = Mock(side_effect=AssertionError("retirement must not touch runtime or start work"))
    with pytest.MonkeyPatch.context() as guarded:
        guarded.setattr(Path, "open", forbidden)
        guarded.setattr(subprocess, "Popen", forbidden)
        guarded.setattr(importlib, "import_module", forbidden)
        assert callable(entry)
        assert entry(arguments) == 1
    assert "Legacy watcher retired" in capsys.readouterr().err
    forbidden.assert_not_called()


@pytest.mark.parametrize("module", ["localswim.monitor", "localswim.monitor_pool"])
@pytest.mark.parametrize("arguments", [[], ["--help"], ["--manifest", "missing.json", "--stop"]])
def test_modules_and_stale_console_wrapper_fail_loudly(module: str, arguments: list[str]) -> None:
    # Installed historical exe wrappers simply import main and exit with its result.
    stale_wrapper = f"from {module} import main; raise SystemExit(main())"
    for command in (
        [sys.executable, "-m", module, *arguments],
        [sys.executable, "-c", stale_wrapper, *arguments],
    ):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode == 1
        assert "Legacy watcher retired" in result.stderr
        assert "localswim-cli BOARD notifications" in result.stderr
        assert not result.stdout


def test_legacy_commands_and_extra_are_absent_from_package() -> None:
    root = Path(__file__).resolve().parents[1]
    value = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert set(value["scripts"]) == {"localswim", "localswim-cli"}
    assert "monitor" not in value.get("optional-dependencies", {})
    installed = importlib.metadata.distribution("localswim")
    assert {entry.name for entry in installed.entry_points if entry.group == "console_scripts"} == {
        "localswim",
        "localswim-cli",
    }
    assert all("watchfiles" not in requirement for requirement in installed.requires or [])


def test_obsolete_startup_implementations_are_absent() -> None:
    for name in ("monitor", "claim_monitor", "request_target", "watch_board", "prepare_runtime"):
        assert not hasattr(monitor, name)
    for name in ("NativeSubscription", "Coordinator", "run_pool", "request_pool_stop"):
        assert not hasattr(monitor_pool, name)
