"""Run the complete repository gate before committing.

    uv run --frozen python check.py

Validate tracked text line endings, Python lint/format/types, behavior, and workflows.
All stages run even when an earlier check fails; every stage is mandatory.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
RULE = "=" * 70
ACTIONLINT_VERSION = "1.7.12"
SHELLCHECK_VERSION = "0.11.0"
PYTHON_TARGETS = ("src", "check.py", "tests")


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Behavior and static-analysis gate. Run before committing."
    )
    parser.parse_args()

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

    print(RULE)
    print(f"  GATE FAILED: {', '.join(failures)}" if failures else "  Gate clean.")
    print(RULE + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
