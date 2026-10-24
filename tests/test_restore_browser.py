"""Run supplied PAGE/workspace code over actual restore envelopes and restarted HTTP clients."""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from localswim import api_endpoint
from tests.test_restore_integration import browser_restore_fixture


@pytest.mark.skipif(os.name != "nt", reason="Windows native restore contract")
def test_actual_restore_supplied_browser_intent_matrix(tmp_path: pathlib.Path) -> None:
    """Deterministic DOM scheduling with real restore and fresh mutation HTTP responses."""
    node = shutil.which("node")
    assert node, "Node.js is required for the executable supplied-client restore matrix"
    page = tmp_path / "browser.html"
    page.write_text(api_endpoint.PAGE.replace("%POLL%", "400"), encoding="utf-8")
    with browser_restore_fixture(tmp_path / "restore") as fixture:
        responses = tmp_path / "restore-responses.json"
        responses.write_text(json.dumps(fixture), encoding="utf-8")
        result = subprocess.run(
            [
                node,
                str(pathlib.Path(__file__).with_name("browser_intent.cjs")),
                str(page),
                str(pathlib.Path(api_endpoint.__file__).with_name("workspace.js")),
                str(responses),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads(result.stdout)
        assert summary["passed"] is True
        restore = summary["restore"]
        assert restore["provenance"]["restoreOutcome"] == "committed"
        assert restore["provenance"]["sameRevision"] is True
        assert restore["nativeBrowser"] is False
        assert restore["freshMutationResponses"] == "actual restarted HTTP service/gateway"
        assert len(restore["cases"]) == 14
        (tmp_path / "browser-matrix-summary.json").write_text(result.stdout, encoding="utf-8")
