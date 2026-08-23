# Repository guidance

## Purpose and layout

localswim is a dependency-free Python 3.14 application, built and run with uv, that
serves a local swimlane board from one JSON snapshot.

- `src/localswim/board_state.py` owns the board model, validation, serialization,
  policy checks, storage helpers, and command-line client.
- `src/localswim/api_endpoint.py` owns the loopback HTTP service and its embedded
  browser UI. It is the sole writer while a board is live.
- `src/localswim/rules.json` is the checked legacy/default policy used for controlled
  schema migrations. New boards persist their resolved policy inside the board.
- `pyproject.toml` is the single build, dependency, Ruff, formatter, and Pyright
  configuration source; `uv.lock` is committed for reproducible development and CI.
- `examples/board-description.example.json` and `examples/permissions.example.json`
  are generic human-readable initialization inputs. `examples/board.example.json` is
  their checked, empty schema-4 output.
- The parallel `examples/*.terry-workflow.json` files preserve Terry's preferred
  personal workflow, also used by FGA, without making it the generic product default.
- `docs/ORIENTATION.md` is the contributor map for architecture, data flow, schemas,
  invariants, test coverage, and Windows/Codex environment details.
- `CONTRIBUTING.md` defines the enforced GitHub branch and pull-request policy.
- `tests/` is the behavioral suite; `check.py` is the complete development gate.
- `src/localswim/vendor/typefaces/inter/` contains unmodified, licensed font binaries.
  Do not edit, re-subset, or rename them casually.

## Safety and product invariants

- Keep the service bound to loopback. Do not add remote exposure, external runtime
  services, or browser network dependencies without an explicit product decision.
- Preserve the single-writer design, per-process bearer credential, `If-Match`
  revision checks, schema validation, monotonic revisions, flushed temporary writes,
  and atomic snapshot replacement.
- Board files can contain private identities, descriptions, comments, and history.
  Keep local boards under ignored `boards/` or outside this public checkout; never add
  real board data, credentials, or service descriptors to source control.
- Automatic commits and pushes are opt-in. Do not enable `--autopush`, configure
  remotes, commit, or push unless the user explicitly asks.
- Treat each schema-4 board's embedded policy as executable permission data, not UI
  decoration. Initialization resolves lane and actor display names once into stable
  IDs; runtime authorization uses only those persisted IDs.
- Generate a lane ID once from its initial display name, reject collisions, and never
  regenerate it after a label rename. Use the distinct offline label-rename and lane-ID
  migration operations for those different intents.
- Keep durable product identifiers agent-neutral. Do not put a human assistant,
  automation product, model, or vendor name into lane, field, or protocol IDs.
- An automation agent must not promote a backlog card to `ready_for_work` without
  explicit permission for that specific card, even though the edge exists for that
  exceptional case. It must never move its own work to `completed`.
- Keep the checked generic and Terry-workflow examples distinct. Terry's seven-lane
  workflow is an intentional public example of how he works, not a universal default.

## Code conventions

- Target exactly the Python version in `.python-version` for development and CI
  (currently the latest 3.14 patch, 3.14.7). Keep the package contract at
  `>=3.14,<3.15`; update the pin when a later 3.14 patch is GA, and move the range to
  3.15 only after 3.15 is GA and verified. A new runtime dependency requires clear
  justification and an explicit decision.
- Use uv for Python installation, locking, synchronization, execution, and builds.
  Do not introduce pip/venv/requirements-file instructions. Change dependencies in
  `pyproject.toml`, refresh `uv.lock`, and commit both.
- Fully annotate every function and method signature. Ruff checks annotation presence;
  Pyright checks correctness.
- Follow `pyproject.toml`: Ruff `ALL`, Ruff formatting, 100-column lines, modern
  syntax, sorted imports, `pathlib` for paths, and Pyright strict mode. The documented
  ignores remove formatter conflicts or known low-value policy checks; do not weaken
  checks to make a change pass.
- Preserve UTF-8, LF endings, and the repository's US English/house vocabulary.
- Maintain the existing JSON boundary behavior: duplicate keys, invalid types, bad
  references, illegal transitions, and malformed input should fail with useful,
  field-specific messages. Preserve unknown-field rejection where a structure defines
  it explicitly, especially transition edge objects; do not claim every board object
  currently rejects additional keys.
- Keep protocol and schema changes explicit. Update implementation, rules/example
  data, tests, and README together when a public route or persisted shape changes.
- Preserve compatibility routes only where the code intentionally documents them;
  do not invent broad legacy aliases.
- Treat `README.md`, `docs/ORIENTATION.md`, `AGENTS.md`, `rules.json` descriptions,
  implementation docstrings, and tests as complementary documentation layers. Update
  every affected layer when a schema, route, invariant, or workflow changes.

## Verification

Run focused tests while iterating, then run the complete gate before handing off a
substantial change:

```console
uv run --frozen pytest -q
uv run --frozen python check.py --word-table path/to/claude-dirty-words.py
uv build --no-sources --clear
```

`check.py` validates tracked-file line endings, Ruff `ALL`, Ruff formatting, strict
Pyright, pytest, actionlint with ShellCheck, and the borrowed US English vocabulary
table. The word table is intentionally not copied here; its absence is a gate failure,
not a skipped check. If it is available through the `CLAUDE_WORD_TABLE` environment
variable or a documented neighboring checkout, `uv run --frozen python check.py` is
sufficient. See `docs/TOOLING.md` for tool choices, pins, installation, and rationale.

For a narrow change, run the relevant test module first, but do not describe Ruff alone
as type-checking or pytest alone as the complete gate.

On this Windows machine, Codex is authorized to use `C:\Temp` for scratch data. The
inherited user temp directory is not readable by the sandbox account, so run pytest
with an external project-specific base temp:

```console
$env:UV_CACHE_DIR='C:\Temp\localswim-uv-cache'
uv run --frozen pytest -q --basetemp C:/Temp/localswim-codex-pytest -o cache_dir=C:/Temp/localswim-codex-pytest-cache
```

Do not put `--basetemp` inside this checkout: the suite intentionally verifies that
boards outside the source checkout remain supported, so an in-repository temp root
changes that test's premise. Writing to `C:\Temp` may still require the normal Codex
sandbox approval even though the project author has authorized its use.

The interactive user's uv cache is also outside the Codex sandbox. For every Codex uv
command, set `UV_CACHE_DIR=C:\Temp\localswim-uv-cache` as shown above; do not call tools
directly from `.venv/Scripts`. If the pinned interpreter is absent, install it with:

```console
$env:UV_CACHE_DIR='C:\Temp\localswim-uv-cache'
uv python install 3.14.7
uv sync --locked
```

## Git in the Codex sandbox

The workspace may be owned by the interactive Windows account while Codex commands run
as a sandbox account. Configure this clone once so ordinary Git commands accept it:

```console
git config --global --add safe.directory C:/Projects/localswim
```

After that setting exists, Git accepts the clone. The sandbox account may still be
unable to read the interactive user's global excludes file, so use these warning-free
forms while working here:

```console
git -c core.excludesFile=/dev/null status --short
git -c core.excludesFile=/dev/null diff --check
git -c core.excludesFile=/dev/null diff -- <paths>
```

If the global setting is not available in a fresh sandbox, add both overrides:

```console
git -c safe.directory=C:/Projects/localswim -c core.excludesFile=/dev/null status --short
```

The excludes override disables only the inaccessible user-level ignore file; this
repository's `.gitignore` remains in effect. Do not change repository Git configuration
merely to suppress a sandbox-only warning.

## Branch and publishing policy

GitHub permits direct `main` pushes only from `TerryOtt` and the installed
`chatgpt-codex-connector` GitHub App. Local Codex sessions use Terry's Git credential
and therefore appear to GitHub as `TerryOtt`; GitHub cannot audit them as a separate
actor. Cloud Codex appears under the App identity. Every other independently
authenticated actor must work on a lower-case kebab-case `feature/<terse-description>`
branch and submit a pull request to `main`.

The direct-push exception is capability, not standing authorization. Codex must still
wait for an explicit user request before committing or pushing. When authorized to
publish directly, use plain non-interactive Git commands from `main`; when preparing
work for any other contributor, follow `CONTRIBUTING.md`.

Terry gave Codex standing authorization on 2026-08-22 to commit and push in-scope
localswim changes needed to keep the FGA localswim integration healthy and
reproducible. That authorization includes building and reinstalling local wheels. It
does not authorize publishing packages to a registry, creating GitHub releases, or
including unrelated user changes.

The active server-side rule must match `.github/rulesets/main.json`, and
`.github/workflows/contribution-policy.yml` supplies its required branch-name check.
The repository merge settings must match `.github/repository-settings.json`: only
squash merges are permitted, both required checks gate a PR, per-PR auto-merge is
available, and merged same-repository branches are deleted. For every non-bypass PR,
the one current approval RFC 2119 MUST come from GitHub user `TerryOtt`;
`.github/CODEOWNERS` and the ruleset's required code-owner review enforce that
requirement. Approval by anyone else does not satisfy it. The two `always` bypass
actors can intentionally bypass all PR requirements as part of their direct-main
capability; no other actor can.

Do not create duplicate rulesets: inspect GitHub first and update the existing rule by
ID. The current live rule ID and exact update command are recorded in
`CONTRIBUTING.md`. The GitHub CLI token needs both `repo` and `workflow` scopes to push
changes under `.github/workflows/`; refresh it with
`gh auth refresh -h github.com -s repo -s workflow` when necessary. Never print, copy,
or persist the `gh` authentication token.
