"""The gate. Run it before committing; the pre-commit hook runs it for you.

    uv run --frozen python check.py
    uv run --frozen python check.py --word-table <path to claude-dirty-words.py>

**This repository had NO gate at all until 2026-08-19**, and it was found the way
these things always are: a `Stop` hook caught Claude writing the British spelling of
"license" in a sentence to Terry, and the sweep that followed found the same word
already committed in `board_state.py`. **941 lines of prose-heavy Python went in on one day
with nothing reading any of it.**

| Where | What watches it |
|---|---|
| What Claude SAYS | `~/.claude/hooks/claude-dirty-words-stop.py`, every turn |
| `FlickrGroupAddr/backend-api` | `scripts/claude-dirty-words.py`, under `npm run check` |
| **This repository, until now** | **nothing** |

**The `Stop` hook watches speech, not files** -- its own docstring says so. So it could
catch the word in a sentence and could never have caught the identical word sitting in
`board_state.py`.

## The word table is BORROWED, not copied

**`hits_in` is imported from FlickrGroupAddr's `scripts/claude-dirty-words.py`**, which
is the canonical table. **A second copy would drift, and the table is the whole tool.**

**That repository is PUBLIC, which is what makes this work in CI.** `claude-config` --
where the `~/.claude` wrapper lives -- is PRIVATE, so reaching it from a public
repository's workflow would need a token stored as a secret. **A spell-checker is not
worth a credential in a public repository**, and the public table makes the question
moot.

## A MISSING TABLE FAILS LOUDLY. IT IS NEVER SKIPPED

**This is the design constraint, not a detail.** A gate that quietly does not run is
worse than no gate, because it reads as a pass. An absent table prints a banner and
**exits non-zero**, exactly as a real finding would.

## The detector is PROVEN TO BITE on every run

**A poisoned control line goes through it first.** A checker pointed at the wrong thing
and a checker that finds nothing are indistinguishable from outside, so a clean result
is only believable after something known-bad has come back dirty.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

ROOT = pathlib.Path(__file__).resolve().parent
RULE = "=" * 70
ACTIONLINT_VERSION = "1.7.12"
SHELLCHECK_VERSION = "0.11.0"
PYTHON_TARGETS = ("src", "check.py", "tests")

# **A line that MUST produce hits.** If it ever comes back clean the import silently
# broke and every other result in this run is worthless.
CONTROL = "The colour of the behaviour we recognise."  # DIRTY-WORDS-EXEMPT: control line

# Where the canonical table might be, in order. **The env var comes first so CI can
# say exactly where it checked the repository out**, and the rest are the places a
# working copy sits on Terry's machine.
TABLE_ENV = "CLAUDE_WORD_TABLE"
TABLE_GUESSES = (
    pathlib.Path(r"X:\Projects\FlickrGroupAddr 2026-08\scripts\claude-dirty-words.py"),
    ROOT.parent / "FlickrGroupAddr 2026-08" / "scripts" / "claude-dirty-words.py",
    ROOT.parent / "backend-api" / "scripts" / "claude-dirty-words.py",
)


def find_table(explicit: str | None) -> pathlib.Path | None:
    """The word table, or None. Explicit beats the env var beats the guesses."""
    if explicit:
        path = pathlib.Path(explicit)
        return path if path.is_file() else None
    from_env = os.environ.get(TABLE_ENV)
    if from_env:
        path = pathlib.Path(from_env)
        return path if path.is_file() else None
    return next((p for p in TABLE_GUESSES if p.is_file()), None)


def load_table(path: pathlib.Path) -> ModuleType:
    """Import the checker module so `hits_in` can be borrowed from it."""
    spec = importlib.util.spec_from_file_location("word_table", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import a word table from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tracked(suffixes: tuple[str, ...]) -> list[pathlib.Path]:
    """Every git-tracked file with one of these suffixes.

    **`git ls-files` rather than a glob**, so an untracked scratch file cannot fail the
    gate and a tracked file cannot hide from it.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 -- Git is the required repository tool.
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        return []
    # Decode filesystem names only after preserving Git's raw pathname boundaries.
    # fsdecode supports Windows UTF-8 paths and POSIX surrogate-escaped bytes.
    names = (os.fsdecode(name) for name in out.stdout.split(b"\0") if name)
    paths = (ROOT / name for name in names if name.endswith(suffixes))
    return [path for path in paths if path.is_file()]


def non_lf_tracked(lines: str) -> list[str]:
    """Paths Git classifies as tracked text with CRLF or mixed working-tree EOLs."""
    bad: list[str] = []
    for line in lines.splitlines():
        metadata, separator, path = line.partition("\t")
        if not separator:
            continue
        working = next((field for field in metadata.split() if field.startswith("w/")), "")
        if working in {"w/crlf", "w/mixed"}:
            bad.append(path)
    return bad


def run_line_endings() -> bool:
    """Reject non-Unix endings in tracked text while leaving binary files alone."""
    print("  line-endings")
    done = subprocess.run(
        ["git", "ls-files", "--eol"],  # noqa: S607 -- fixed developer-tool command
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print("    FAIL -- git ls-files --eol did not run\n")
        return False
    bad = non_lf_tracked(done.stdout)
    for path in bad:
        print(f"    FAIL  {path}: tracked text must use LF")
    print(f"    {'ok' if not bad else f'FAIL -- {len(bad)} file(s)'}\n")
    return not bad


def run_ruff() -> bool:
    """Lint, and say plainly whether it passed."""
    print("  ruff")
    done = subprocess.run(  # noqa: S603 -- fixed project targets under this interpreter
        [sys.executable, "-m", "ruff", "check", *PYTHON_TARGETS], cwd=ROOT, check=False
    )
    ok = done.returncode == 0
    print(f"    {'ok' if ok else 'FAIL'}\n")
    return ok


def run_format() -> bool:
    """Require Ruff's canonical formatting without modifying the working tree."""
    print("  ruff-format")
    done = subprocess.run(  # noqa: S603 -- fixed project targets under this interpreter
        [sys.executable, "-m", "ruff", "format", "--check", *PYTHON_TARGETS],
        cwd=ROOT,
        check=False,
    )
    ok = done.returncode == 0
    print(f"    {'ok' if ok else 'FAIL'}\n")
    return ok


def run_pyright() -> bool:
    """Typecheck, and say plainly whether it passed.

    **RUFF CHECKS THAT AN ANNOTATION EXISTS. PYRIGHT CHECKS THAT IT IS TRUE**, which is
    Terry's global rule, and this gate proved it the hour pyright was added.

    **It found a real defect on its first run**: `api_endpoint.py`'s `/assign` route passed
    `str(body["owner"])` -- a value the browser controls -- into a parameter annotated
    `Actor`. Ruff had been clean on that line every day. **`board_state.as_actor` now narrows
    it at the boundary.**

    **It MUST run with `cwd=ROOT`.** The strict configuration, `src` package root, test
    imports, and project execution environment are all resolved from `pyproject.toml`.
    Running against an arbitrary current directory can type-check a different file set
    and make a clean result meaningless.
    """
    print("  pyright")
    done = subprocess.run([sys.executable, "-m", "pyright"], cwd=ROOT, check=False)
    ok = done.returncode == 0
    print(f"    {'ok' if ok else 'FAIL'}\n")
    return ok


def run_tests() -> bool:
    """Run the behavioral suite after static checks establish it can import."""
    print("  pytest")
    done = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, check=False)
    ok = done.returncode == 0
    print(f"    {'ok' if ok else 'FAIL'}\n")
    return ok


def run_workflows() -> bool:
    """Lint GitHub Actions YAML and every embedded shell fragment."""
    print("  actionlint + shellcheck")
    actionlint = shutil.which("actionlint")
    shellcheck = shutil.which("shellcheck")
    if actionlint is None or shellcheck is None:
        missing = [
            name
            for name, executable in (("actionlint", actionlint), ("shellcheck", shellcheck))
            if executable is None
        ]
        print(f"    FAIL -- missing required tool(s): {', '.join(missing)}\n")
        return False

    action_version = subprocess.run(  # noqa: S603 -- resolved, pinned developer tool
        [actionlint, "-version"], capture_output=True, text=True, check=False
    )
    shell_version = subprocess.run(  # noqa: S603 -- resolved, pinned developer tool
        [shellcheck, "--version"], capture_output=True, text=True, check=False
    )
    versions_ok = (
        action_version.returncode == 0
        and action_version.stdout.splitlines()[:1] == [ACTIONLINT_VERSION]
        and shell_version.returncode == 0
        and f"version: {SHELLCHECK_VERSION}" in shell_version.stdout
    )
    if not versions_ok:
        print(
            "    FAIL -- want actionlint "
            f"{ACTIONLINT_VERSION} and ShellCheck {SHELLCHECK_VERSION}\n"
        )
        return False

    done = subprocess.run(  # noqa: S603 -- resolved, pinned developer tools
        [actionlint, "-no-color", "-shellcheck", shellcheck], cwd=ROOT, check=False
    )
    ok = done.returncode == 0
    print(f"    {'ok' if ok else 'FAIL'}\n")
    return ok


def missing_table_banner(explicit: str | None) -> None:
    """Say loudly that nothing was checked. **Silence here would read as a pass.**"""
    print(RULE)
    print("  DIRTY-WORDS CHECK DID NOT RUN")
    print("  NOTHING WAS CHECKED FOR US ENGLISH OR HOUSE VOCABULARY.")
    print()
    print("  The word table is FlickrGroupAddr/backend-api scripts/claude-dirty-words.py")
    print(f"  Point at it with --word-table, or set {TABLE_ENV}.")
    if explicit:
        print(f"  Tried, and it is not a file: {explicit}")
    else:
        for guess in TABLE_GUESSES:
            print(f"  Tried: {guess}")
    print(RULE + "\n")


def run_words(table: pathlib.Path, files: list[pathlib.Path]) -> bool:
    """Apply the borrowed table to every tracked prose-bearing file."""
    mod = load_table(table)

    # **PROVE THE DETECTOR FIRES before believing any clean result.**
    if not list(mod.hits_in(CONTROL)):
        print("  dirty-words")
        print("    FAIL -- the detector found nothing in the poisoned control line.")
        print("    Every clean result this run would be worthless.\n")
        return False

    print(f"  dirty-words, {len(files)} files, table at {table}")
    bad = 0
    for path in files:
        hits = list(mod.hits_in(path.read_text(encoding="utf-8", errors="replace")))
        if hits:
            print(f"    FAIL  {path.relative_to(ROOT)}")
            for number, word, better in hits:
                print(f"          line {number}: {word!r} should be {better!r}")
                bad += 1
    print(f"    {'ok' if not bad else f'FAIL -- {bad} problem(s)'}\n")
    return not bad


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Behavior, static-analysis and prose gate. Run before committing."
    )
    parser.add_argument(
        "--word-table", default=None, help="path to FlickrGroupAddr's scripts/claude-dirty-words.py"
    )
    args = parser.parse_args()

    print(f"\n{RULE}\n  localswim gate\n{RULE}\n")
    failures: list[str] = []

    if not run_line_endings():
        failures.append("line-endings")

    if not run_ruff():
        failures.append("ruff")

    if not run_format():
        failures.append("ruff-format")

    if not run_pyright():
        failures.append("pyright")

    if not run_tests():
        failures.append("pytest")

    if not run_workflows():
        failures.append("actionlint/shellcheck")

    table = find_table(args.word_table)
    files = tracked((".py", ".md", ".json", ".toml", ".yaml", ".yml"))
    if table is None:
        missing_table_banner(args.word_table)
        failures.append("dirty-words (no word table)")
    elif not files:
        print("  dirty-words\n    FAIL -- git ls-files matched nothing\n")
        failures.append("dirty-words (no files matched)")
    elif not run_words(table, files):
        failures.append("dirty-words")

    print(RULE)
    print(f"  GATE FAILED: {', '.join(failures)}" if failures else "  Gate clean.")
    print(RULE + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
