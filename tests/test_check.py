"""Repository gate behavior."""

import sys
from types import ModuleType
lazy import pathlib

import pytest

import check


def test_non_lf_tracked_finds_only_text_with_crlf_or_mixed_endings() -> None:
    report = (
        "i/lf    w/lf    attr/text eol=lf\tclean.py\n"
        "i/lf    w/crlf  attr/text eol=lf\twindows.py\n"
        "i/lf    w/mixed attr/text eol=lf\tmixed.json\n"
        "i/-text w/-text attr/text eol=lf\tfont.woff2\n"
    )

    assert check.non_lf_tracked(report) == ["windows.py", "mixed.json"]


def _stub_other_stages(monkeypatch: pytest.MonkeyPatch, source: pathlib.Path) -> None:
    def tracked(_suffixes: tuple[str, ...]) -> list[pathlib.Path]:
        return [source]

    for name in (
        "run_line_endings",
        "run_ruff",
        "run_format",
        "run_pyright",
        "run_tests",
        "run_workflows",
    ):
        monkeypatch.setattr(check, name, lambda: True)
    monkeypatch.setattr(check, "tracked", tracked)
    monkeypatch.setattr(check, "ROOT", source.parent)


def _stub_detector(monkeypatch: pytest.MonkeyPatch, detector: ModuleType) -> None:
    def load_table(_path: pathlib.Path) -> ModuleType:
        return detector

    monkeypatch.setattr(check, "load_table", load_table)


def test_gate_refuses_explicit_missing_word_table(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "clean.txt"
    source.write_text("clean input", encoding="utf-8")
    _stub_other_stages(monkeypatch, source)
    fallback = tmp_path / "available-table.py"
    fallback.write_text("", encoding="utf-8")
    monkeypatch.setenv(check.TABLE_ENV, str(fallback))
    monkeypatch.setattr(check, "TABLE_GUESSES", (fallback,))
    missing = tmp_path / "missing-table.py"
    monkeypatch.setattr(sys, "argv", ["check.py", "--word-table", str(missing)])

    assert check.main() == 1
    output = capsys.readouterr().out
    assert "DIRTY-WORDS CHECK DID NOT RUN" in output
    assert f"Tried, and it is not a file: {missing}" in output
    assert "GATE FAILED: dirty-words (no word table)" in output


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_gate_recognizing_detector_accepts_clean_and_rejects_dirty(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    dirty: bool,
) -> None:
    source = tmp_path / "sample.txt"
    marker = "synthetic-vocabulary-marker"
    source.write_text(marker if dirty else "clean input", encoding="utf-8")
    _stub_other_stages(monkeypatch, source)
    table = tmp_path / "table.py"
    table.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check.py", "--word-table", str(table)])
    detector = ModuleType("synthetic_detector")
    examined: list[str] = []

    def hits_in(text: str) -> list[tuple[int, str, str]]:
        examined.append(text)
        return [(1, marker, "clean input")] if text in {check.CONTROL, marker} else []

    monkeypatch.setattr(detector, "hits_in", hits_in, raising=False)
    _stub_detector(monkeypatch, detector)

    assert check.main() == int(dirty)
    assert examined == [check.CONTROL, source.read_text(encoding="utf-8")]
    output = capsys.readouterr().out
    if dirty:
        assert "FAIL  sample.txt" in output
        assert "GATE FAILED: dirty-words" in output
    else:
        assert "Gate clean." in output


def test_gate_refuses_inert_detector_before_trusting_clean_input(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "clean.txt"
    source.write_text("clean input", encoding="utf-8")
    _stub_other_stages(monkeypatch, source)
    table = tmp_path / "inert-table.py"
    table.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check.py", "--word-table", str(table)])
    detector = ModuleType("inert_detector")
    examined: list[str] = []

    def hits_in(text: str) -> list[tuple[int, str, str]]:
        examined.append(text)
        return []

    monkeypatch.setattr(detector, "hits_in", hits_in, raising=False)
    _stub_detector(monkeypatch, detector)

    assert check.main() == 1
    assert examined == [check.CONTROL]
    output = capsys.readouterr().out
    assert "the detector found nothing in the poisoned control line" in output
    assert "GATE FAILED: dirty-words" in output
