"""Malformed manifests must never silently omit or start a different set of owners."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from localswim import monitor_pool

if TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize(
    "encoded",
    [
        '{"schema":true,"boards":[[]]}',
        '{"schema":1,"schema":1,"boards":[[]]}',
        '{"schema":1,"boards":[[]],"ignoredBoards":[]}',
        '{"schema":1,"boards":[["--help"]]}',
        '{"schema":1,"boards":[["-h"]]}',
        '{"schema":1,"boards":[["--hel"]]}',
        '{"schema":1,"boards":[["--he"]]}',
        '{"schema":1,"boards":[["--h"]]}',
        '{"schema":1,"boards":[["-hh"]]}',
    ],
)
def test_invalid_manifest_has_no_runtime_side_effect(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, encoded: str
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(encoded, encoding="utf-8")
    run = Mock(side_effect=AssertionError("invalid manifest must not start owners"))
    stop = Mock(side_effect=AssertionError("invalid manifest must not retarget owners"))
    configure = Mock(side_effect=AssertionError("invalid config must retain CLI error output"))
    monkeypatch.setattr(monitor_pool, "run_pool", run)
    monkeypatch.setattr(monitor_pool, "request_pool_stop", stop)
    monkeypatch.setattr(monitor_pool.legacy.service_output, "configure", configure)
    assert monitor_pool.main(["--manifest", str(path)]) == 1
    run.assert_not_called()
    stop.assert_not_called()
    configure.assert_not_called()
