"""Repository gate behavior."""

import os
import subprocess
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


def _stub_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "run_line_endings",
        "run_ruff",
        "run_format",
        "run_pyright",
        "run_tests",
        "run_workflows",
    ):
        monkeypatch.setattr(check, name, lambda: True)


def _stub_other_stages(monkeypatch: pytest.MonkeyPatch, source: pathlib.Path) -> None:
    def tracked(_suffixes: tuple[str, ...]) -> list[pathlib.Path]:
        return [source]

    _stub_toolchain(monkeypatch)
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


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def _init_repo(repo: pathlib.Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "core.quotePath", "true")


def test_tracked_preserves_unicode_and_spaces_and_filters_inventory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    included = ["clean.md", "words with spaces.py", "日本語/café.md"]
    for name in [*included, "excluded.txt", "absent.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("clean input", encoding="utf-8")
        _git(tmp_path, "add", "--", name)
    (tmp_path / "absent.md").unlink()
    (tmp_path / "untracked.md").write_text("scratch input", encoding="utf-8")
    monkeypatch.setattr(check, "ROOT", tmp_path)

    assert set(check.tracked((".py", ".md"))) == {tmp_path / name for name in included}


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain tab or newline")
@pytest.mark.parametrize("name", ["tab\tname.md", "line\nname.md", "byte-\udcff.md"])
def test_tracked_preserves_supported_non_windows_pathnames(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _init_repo(tmp_path)
    path = tmp_path / name
    path.write_text("clean input", encoding="utf-8")
    _git(tmp_path, "add", "--", name)
    monkeypatch.setattr(check, "ROOT", tmp_path)

    assert check.tracked((".md",)) == [path]


def test_vocabulary_stage_rejects_dirty_unicode_working_tree_prose(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    clean = tmp_path / "clean.md"
    dirty = tmp_path / "café prose.md"
    marker = "synthetic-vocabulary-marker"
    clean.write_text("clean input", encoding="utf-8")
    dirty.write_text("clean input", encoding="utf-8")
    _git(tmp_path, "add", "--", clean.name, dirty.name)
    dirty.write_text(marker, encoding="utf-8")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    detector = ModuleType("synthetic_detector")
    examined: list[str] = []

    def hits_in(text: str) -> list[tuple[int, str, str]]:
        examined.append(text)
        return [(1, marker, "clean input")] if text in {check.CONTROL, marker} else []

    monkeypatch.setattr(detector, "hits_in", hits_in, raising=False)
    _stub_detector(monkeypatch, detector)
    files = check.tracked((".md",))

    assert set(files) == {clean, dirty}
    assert not check.run_words(tmp_path / "table.py", files)
    assert examined[0] == check.CONTROL
    assert sorted(examined[1:]) == sorted(["clean input", marker])
    assert f"FAIL  {dirty.name}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "git_available", [False, True], ids=["failed-inventory", "empty-inventory"]
)
def test_gate_visibly_refuses_failed_or_empty_inventory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    git_available: bool,
) -> None:
    if git_available:
        _init_repo(tmp_path)
    table = tmp_path / "table.py"
    table.write_text("", encoding="utf-8")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check.py", "--word-table", str(table)])
    _stub_toolchain(monkeypatch)

    assert check.main() == 1
    output = capsys.readouterr().out
    assert "FAIL -- git ls-files matched nothing" in output
    assert "GATE FAILED: dirty-words (no files matched)" in output
