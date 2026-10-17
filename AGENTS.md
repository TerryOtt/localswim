# Repository guidance

## Purpose and layout

localswim is a Python 3.15 application, built and run with uv, that serves a local
swimlane board from one JSON snapshot. Click is its sole core runtime dependency;
native board monitoring is an optional extra.

- `src/localswim/board_state.py` owns the board model, validation, serialization,
  policy checks, storage helpers, and reporting primitives.
- `src/localswim/attachments.py` owns attachment metadata, replayable audit,
  bounded file validation, safe storage paths, and immutable file integrity.
- `src/localswim/monitor.py` owns optional shared board-write monitoring and graceful reload.
- `src/localswim/cli.py` owns the Click command tree and command-to-service boundary.
- `src/localswim/api_endpoint.py` owns the loopback HTTP service and its embedded
  browser UI. It is the sole writer while a board is live.
- `src/localswim/service_ownership.py` owns per-board lifetime writer leases and the
  startup/restart gate shared by individual services and workspace launches.
- `src/localswim/workspace.py` owns directory discovery, the workspace catalog,
  loopback gateway, board-service reuse/startup, and bookmarkable SPA entry routes.
- `src/localswim/workspace_config.py` owns schema-1 gateway preferences, atomic storage,
  and cross-process revision checks.
- `src/localswim/gateway_publish.py` owns configuration-only publishing using the
  shared board repository lock.
- `src/localswim/workspace.js` and `workspace.css` own the Attention sidebar,
  History API navigation, fixed count columns, and per-board browser state.
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
  and atomic snapshot replacement. Acquire the board lifetime OS lease before constructing
  its store, starting autopush, binding, or publishing its descriptor. Keep lease lock files
  persistent; never unlink an active or released OS-lock path. Coordinate all launches and
  restart handoffs through the shared startup gate. Drain HTTP workers before releasing
  ownership; retain the actual bound port, including `--port 0`, across hot restart.
  Use explicit child-process handoff on Windows; do not assume byte locks are inherited.
  Keep the old descriptor until replacement publication, preserve workspace launch identity,
  and rotate bearer credentials normally.
- Board files can contain private identities, descriptions, comments, and history.
  Keep local boards under ignored `boards/` or outside this public checkout; never add
  real board data, credentials, or service descriptors to source control.
- Automatic board commits and pushes are opt-in. Do not enable `--autopush` or
  configure remotes unless the user explicitly asks. Source commits and pushes follow
  Terry's standing authorization in the branch and publishing policy below.
- Treat each schema-4 board's embedded policy as executable permission data, not UI
  decoration. Initialization resolves lane and actor display names once into stable
  IDs; runtime authorization uses only those persisted IDs.
- Generate a lane ID once from its initial display name, reject collisions, and never
  regenerate it after a label rename. Use the distinct offline label-rename and lane-ID
  migration operations for those different intents.
- Keep durable product identifiers agent-neutral. Do not put a human assistant,
  automation product, model, or vendor name into lane, field, or protocol IDs.
- Board-specific working agreements decide how an allowed transition is used. In the
  Terry workflow, Bot triages sufficiently scoped work from `backlog` to
  `ready_for_work` without per-ticket permission and orders it one card at a time;
  dependencies between ready cards affect execution order, not readiness. Bot must
  never move its own work to `completed`.
  Human review approval validates the `ready_for_review -> completed` transition and
  appends the exact `Reviewed & approved, moving to Completed.` comment in one authenticated,
  revision-checked transaction. Bot actors cannot use this action even if policy allows
  the raw transition. Keep comment and move atomic; repeated or stale requests must not
  duplicate the comment. Close the source drawer only after confirmed success and
  preserve its comment draft; failures and stale navigation responses must not close
  another drawer.
- Keep the checked generic and Terry-workflow examples distinct. Terry's seven-lane
  workflow is an intentional public example of how he works, not a universal default.

- Cards may be archived and unarchived, never deleted. Preserve their IDs, tickets,
  lane states, relationships, contents, and history. Archive changes use the live
  service and append to `archiveHistory`; default lists hide archived cards, while
  focused inspection, full snapshot exports, verification, and activity retain them.

- Attachments are capped at 10 MiB per file and stored beside each board as
  `attachments/<four-digit zero-padded card number>/<GUID>.<extension>`. Preserve the
  original filename in metadata. Flush files before snapshot references and remove
  newly written files on transaction failure. Removal detaches files but retains
  bytes/history; only active attachments may be served or used as cover. Detect safe
  raster previews from bytes; generic files must download with `nosniff` and sandbox
  CSP. Git snapshots include exact audited attachment paths, never a directory glob.
  Browser and bot CLI uploads/removals use the sole live writer and revision checks.
  Card attachment counts use a right-aligned paperclip and tabular numeric field with
  two digits of reserved width, exact larger totals, and accessible count labels.
  Omit zero counts and preserve covers and comment badges.

- Order every lane by descending longest downstream blocking-chain depth, ascending
  priority number, then ascending ticket number. Blocked cards inherit the highest
  urgency among unfinished upstream blockers, with audited promotion in the same
  transaction. Completion is determined by stable state ID `completed`, never its
  display label. Completed cards receive no automatic promotions and do not transmit
  urgency through historical chains; archived unfinished cards still participate.
  Preserve all stored dependency links and derive depths from the full graph.
  A card with no blockers may have any priority, regardless of its descendants.
  Both `blocks` and `blocked_by` enforce this ongoing floor without rejecting different
  priorities. Removing links must not silently downgrade priorities. Only blocking
  relationships contribute; hierarchy and other links do not. Keep browser and CLI
  ordering consistent.
- Explicit batch priority changes use the live writer and one revision-checked store
  transaction. Evaluate all selected overrides together before applying unfinished
  dependency floors; preserve external floors and report effective selected priorities
  plus every actual change. Reject selected Completed cards and canonical duplicate
  references; archived unfinished cards remain eligible. Keep no-op audit behavior and
  timeout-unknown semantics explicit in user documentation. Never edit board JSON.

- Workspace mode must proxy each mutation to the board's existing sole writer with
  that board's browser actor credential and `If-Match`. Never write snapshots directly,
  impersonate `cliUser` for browser edits, or accept arbitrary upstream URLs. Reuse a
  healthy correct-board service; start absent services on available loopback ports. Wait
  boundedly for missing/refused rendezvous while the lifetime writer lease is busy; do not
  treat a temporary restart gap as permission to launch another writer or stop a foreign
  listener. A released crash lease permits recovery. Legacy writers without the lease
  require one-time consolidation; overwritten descriptors cannot identify all old writers.
  New gateways default to publishing disabled; `--autopush` persists true in the gateway
  configuration and applies to newly owned children. Retain that setting across restarts
  without the flag. Existing services retain their own publishing policy. Publish only
  `localswim-gateway.json` under the shared repository lock; exclude unrelated staged files.
- Stable board keys derive from relative snapshot paths, never mutable project labels.
  Support cold deep links and Back/Forward. Switch boards through SPA navigation without
  document reload, with explicit errors for missing or malformed boards. Keep drafts,
  filters, selected cards, lane scrolling, and sidebar state independent per window.
- The Attention sidebar's Blocked, Needs Terry, and Ready for Review counts MUST use
  three fixed columns across all project rows. Zero counts leave blank reserved slots;
  they never shift another circle into the empty column. Preserve category labels and
  accessible true counts alongside color and any capped visible count. Padded, rounded
  header labels must share the corresponding circle colors and fixed column centers.
- Show manually ordered Pinned boards above automatically sorted Other boards. Grip
  dragging and keyboard reordering affect only pinned stable keys; pin/unpin controls
  move boards between groups. Preserve manual order across activity updates and reloads
  across clients and browser/gateway restarts in the workspace-root gateway configuration.
  Gateway preferences have their own browser-authenticated, revision-checked API; never
  reuse a board revision. Serialize client actions, reconcile conflicts, and surface save
  errors without losing pending choices. Import only the first nonempty legacy pin list
  while shared pins remain uninitialized. Keep collapse, drafts, search, selection, and
  scrolling window-local. Group labels/dividers must not indent count columns.
  Expanded sidebar width defaults to a minimum of 312 pixels, resizes through a captured
  pointer or keyboard separator, and persists per window. Collapse preserves the expanded
  choice; viewport clamping reserves board space without replacing the requested width.
  Resize must preserve board drafts, navigation, scrolling, and fixed attention columns.
  Sort Other boards by the latest audited card edit's exact epoch timestamp, descending,
  then descending total attention count and ascending case-insensitive project name;
  stable keys resolve identical-name ties. Only identical timestamps tie on recency.
  The comparator must not depend on current time:
  an unchanged catalog keeps the same order as time passes. Include archived cards in
  recency, exclude them from counts, and never use snapshot file times or project renames
  as card edits. Unknown recency sorts last; future stamps retain their exact recorded timestamps.
  Tooltips show elapsed whole minutes, clamped to zero for future stamps. Preserve active
  selection, keyboard focus, drafts, search, and scrolling when rows reorder.
- Workspace discovery excludes hidden/Git/attachment directories and symbolic-link
  escapes. Serve assets locally. Source watching includes workspace assets, but Python
  syntax preflight must never treat JavaScript or CSS as Python input.
- Bound workspace reads and keep one poll in flight per navigation epoch. Show clear
  recovery and Retry for failed loads; never leave Loading indefinitely or lose queued
  retries. Advance the snapshot marker only after rendering/restoration succeeds.
  Compare workspace page build identity only with gateway catalog metadata, including
  when the selected board is unavailable. Preserve selected-board source diagnostics,
  independent gateway/board restart warnings, and the reload marker through navigation.
  Unknown gateway build metadata must never fall back to a board-service build.
  Preserve pending drafts throughout errors and refresh browser credentials after a
  gateway restart. Confirm actual listener/process health; saved monitor status can be
  stale after its launching process exits.

## Subagents on authorized tickets

Terry authorized up to five direct subagents (width 5), one level below the
primary agent (depth 1), on all tickets authorized to work, 2026-10-14.
Use parallel delegation where useful without requesting per-ticket permission.
Subagents must not spawn subagents. This does not authorize starting other tickets
or unrelated work. The primary retains responsibility for integration, required
verification, commits/pushes and handoff. Respect a lower current-session limit;
persisted configuration applies when loaded.

## Code conventions

- Target exactly the Python version in `.python-version` for development and CI
  (currently 3.15.0, verified stable on 2026-10-16). Keep the package contract at
  `>=3.15,<3.16`; update the pin when a later 3.15 patch is GA, and move the range to
  3.16 only after 3.16 is GA and verified. The original Click 8.4.2 dependency decision
  covered nested command scopes and contextual help; the current pin is 8.5.0. Any
  additional core runtime dependency still requires clear justification and an explicit decision.
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
- Preserve the CLI's explicit UTF-8 stdout/stderr boundary. Its Unicode status output
  must not depend on a Windows console or redirection target inheriting UTF-8.
- Maintain the existing JSON boundary behavior: duplicate keys, invalid types, bad
  references, illegal transitions, and malformed input should fail with useful,
  field-specific messages. Preserve unknown-field rejection where a structure defines
  it explicitly, especially transition edge objects; do not claim every board object
  currently rejects additional keys.
- Keep protocol and schema changes explicit. Update implementation, rules/example
  data, tests, and README together when a public route or persisted shape changes.
- Preserve compatibility routes only where the code intentionally documents them;
  do not invent broad legacy aliases.
- Description edits append sanitized `detailHistory` attribution (card ID, actor,
  timestamp only), enabling the shared board-write monitor to suppress proven
  bot-only description changes. Keep private old/new prose out of activity JSON;
  Git remains the optional text-diff audit. Older schema-4 boards omit this field
  until their first new edit. Unknown or unaudited writes still fail open to an alert.
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
uv python install 3.15.0
uv sync --locked
```

## Local inception-board coordination

Use `C:\Projects\localswim-state-store\localswim\localswim.json` as the machine-local
human/agent board for localswim itself. At the beginning of each Codex run that uses
this board, read its `port` value and request
`http://127.0.0.1:<port>/api/v001/status`. Reuse a healthy service; never launch a
duplicate. Board reads and mutations go through `localswim-cli`, never a direct JSON
edit. The service runs with `--autopush` because this board's private state-store
repository is its durable snapshot.

Whenever that service is running, a board-write monitor MUST also be running and
targeted at the active Codex thread. Use the shared installed `localswim-monitor`
command from `localswim[monitor]`, or `uv run --frozen --extra monitor
localswim-monitor` in this checkout. Pass the current board port and thread ID:

```console
localswim-monitor --board-path C:\Projects\localswim-state-store\localswim\localswim.json --board-label "localswim inception" --thread-id THREAD_UUID --port 8794 --status-path C:\Temp\localswim-inception-monitor-status.json --target-path C:\Temp\localswim-inception-monitor-target.json --lock-path C:\Temp\localswim-inception-monitor.lock
```

Use the current port rather than assuming the example's port. The port is a
compatibility/startup hint; monitor lifecycle health follows the live descriptor
and validates the board identity. Launch hidden with
`PYTHONIOENCODING=utf-8`, redirecting output/error to the corresponding board-specific
`.out.log`/`.err.log` files under `C:\Temp`. All runtime paths are mandatory and
must be distinct from other boards. Reinvocation retargets the current owner.
Add `--reload` after package updates to drain pending activity and replace the
monitor without stopping the board. The old FGA-local process cannot honor this
protocol until its one-time graceful restart. See README's monitor section.

The monitor never reads board content. After a trailing-edge 15-second quiet
window it queries sanitized `localswim-cli` activity metadata. Suppress a nonempty
result only when every event is by the configured automation actor (`bot` by
default). Human, mixed, unknown, empty, malformed and failed queries still alert.
Never require one activity event per observed filesystem write. For queued alerts,
inspect only activity metadata first; proven automation-only events need no card
inspection, work resumption or further action. Preserve Terry's alerts. Keep prose
and credentials out of logs. Routine alerts do not justify a GitHub visibility query.

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

Terry's standing order, issued 2026-09-12, authorizes all future code commits and
pushes across all repositories, including direct commits and pushes to `main`,
without asking for permission again. This applies across future tasks and supersedes
earlier requirements for per-commit or per-push approval. Complete the required
verification and preserve unrelated user changes. Use plain non-interactive Git
commands from `main`; when preparing work for any other contributor, follow
`CONTRIBUTING.md`.

Terry gave Codex standing authorization on 2026-08-22 to commit and push in-scope
localswim changes needed to keep the FGA localswim integration healthy and
reproducible. That authorization includes building and reinstalling local wheels. It
does not authorize publishing packages to a registry, creating GitHub releases, or
including unrelated user changes.

Terry's standing order, issued 2026-08-24, is that every localswim commit **MUST** be
pushed to its configured remote in the same agent turn. Never leave a local-only
localswim commit when that remote exists. If a push fails, report the failure
immediately and do not describe the change as fully handed off.

Terry's standing order, issued 2026-08-24, is that the FGA and localswim inception
board services **MUST NOT** retain a Build ID ending in `-dirty` after the source
checkout can safely be clean. Preserve a truthful suffix while tracked or untracked
work remains; never suppress or spoof it. Once authorized work is verified, committed,
and pushed, poll both status endpoints first. A service whose loaded Python sources
still match disk automatically replaces its stale dirty identity during status polling.
Use the supported hot-re-exec or graceful lifecycle boundary only when the loaded source
changed or the automatic refresh does not converge, then confirm both status endpoints
report clean Build IDs and healthy autopush.

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
