# Development toolchain

localswim uses a deliberately small, strict toolchain. The standard is the highest
pedantry that still produces actionable findings, with one owner for each concern and
no duplicate checker added merely for quantity.

## Runtime, environment, and build

- **Python 3.14.7** is pinned in `.python-version`. It was the latest GA CPython when
  this toolchain was adopted. `pyproject.toml` accepts `>=3.14,<3.15` so 3.14 patch
  releases remain compatible; development and CI use the exact pin. Update the patch
  pin when a later 3.14 release is GA. Move the range only after Python 3.15 is GA and
  the complete gate passes there.
- **uv 0.12.5** installs Python, creates the environment, resolves and locks development
  dependencies, runs commands, and builds distributions. `[tool.uv].required-version`
  refuses silent behavior changes from a different uv release.
- **uv_build** is the native build backend, bounded to its current compatible minor
  (`>=0.12.5,<0.13`). The package uses the standard `src/localswim` layout, publishes a
  `py.typed` marker, and includes `rules.json` plus the bundled Inter assets.
- **Click 8.4.2** is the sole runtime dependency. Its BSD-3-Clause license is compatible
  with localswim's MIT license, and its established nested-command and contextual-help
  model replaces the increasingly brittle flat `argparse` option matrix. Terry
  explicitly accepted this dependency tradeoff on 2026-08-25.
- **`uv.lock` is committed.** Local development and CI use `--locked` or `--frozen` so
  a check cannot mutate or silently re-resolve the accepted environment.

Canonical commands:

```console
uv python install
uv sync --locked
uv run --frozen python check.py
uv build --no-sources --clear
```

`--no-sources` proves a release build from published metadata rather than accepting a
developer-only uv source override.

## Python analysis

| Tool | Pin | Job |
|---|---:|---|
| Ruff | 0.16.3 | Lint every stable rule (`select = ["ALL"]`) and own formatting/import order. |
| Pyright | 1.1.411 | Strict static typing, including unknown-type flow and unused symbols. |
| pytest | 9.1.1 | Runtime behavior and regression coverage. |

The Ruff exclusions are documented beside the configuration in `pyproject.toml`. They
cover formatter conflicts, project-wide prose policy, intentional CLI output, and
Unicode house style. File-level suppressions must carry a concrete reason. Enabling
`ALL` means a Ruff upgrade can add rules; upgrade changes therefore require a clean
review of new findings rather than an automatic version bump.

Pyright is intentionally strict. The one project-level diagnostic override permits
reassignment of uppercase module snapshots because rules and board identities reload
at runtime. Tests alone may access private restart/preflight seams. Neither exception
relaxes production value types, JSON boundary validation, or annotation completeness.

Mypy, Pylint, Flake8, isort, Black, Bandit, and pydocstyle are not added: their useful
checks are already owned by strict Pyright or Ruff, while a second implementation would
create conflicting configuration and duplicate findings. BasedPyright remains a
candidate only if it demonstrates actionable diagnostics beyond an upstream-strict,
zero-error Pyright run. Astral's ty remains pre-1.0 and is not a required gate while its
diagnostic and configuration contracts are still changing.

## Workflow and shell analysis

| Tool | Pin | Job |
|---|---:|---|
| actionlint | 1.7.12 | GitHub Actions schema, expressions, events, jobs, and step structure. |
| ShellCheck | 0.11.0 | Shell embedded in workflow `run` blocks, invoked by actionlint. |

Both executables must be on `PATH` for `check.py`; a missing or differently versioned
tool fails loudly. Download Windows binaries from the projects' official release pages:

- <https://github.com/rhysd/actionlint/releases/tag/v1.7.12>
- <https://github.com/koalaman/shellcheck/releases/tag/v0.11.0>

CI downloads the Linux archives directly, verifies their GitHub-published SHA-256
digests, and installs them before running the same gate. GitHub Actions themselves are
pinned to immutable commit SHAs with their human-readable release versions in comments.

## Codex on this Windows checkout

The project author has authorized `C:\Temp` for external scratch space. The sandbox
cannot use the interactive account's uv cache, so Codex should make the cache explicit
and keep every visible project invocation under uv:

```powershell
$env:UV_CACHE_DIR = 'C:\Temp\localswim-uv-cache'
uv run --frozen pytest -q `
  --basetemp C:/Temp/localswim-codex-pytest `
  -o cache_dir=C:/Temp/localswim-codex-pytest-cache
```

Do not invoke `.venv/Scripts/...` directly. See `AGENTS.md` for the Git sandbox flags,
full gate, publishing authority, and project invariants.
