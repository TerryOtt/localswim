# Contributor orientation

This document is the technical map for humans and coding agents entering localswim.
`README.md` is the user guide, `AGENTS.md` is the working agreement for automation,
and the long module docstrings preserve the detailed decision history. This file
connects those pieces without requiring a new contributor to rediscover the system by
reading nearly 7,000 lines in an arbitrary order.

## The system in one page

An individual localswim service is one Python process serving one local board from one
JSON snapshot. A workspace adds a loopback gateway and Attention sidebar over those
independent services. It has one runtime dependency, Click, for the command-line tree
and makes no browser network request beyond loopback.

```text
browser UI ── bearer token + If-Match ─┐
                                       ├──> ThreadingHTTPServer
CLI mutation ── service descriptor ────┘          │
                                                  v
                                             BoardStore
                                                  │
                                  RLock + file lock + validation
                                                  │
                                                  v
                                    temporary JSON + fsync + replace
                                                  │
                                                  v
                                             board.json

description.json + permissions.json ── init ──> board.json
                                               state + resolved policy
CLI report ───────────────────────────────> validated read of board.json
```

The important boundary is `BoardStore.execute()`: every live mutation is serialized,
checked against the revision the caller saw, applied to an isolated candidate, audited,
saved, and only then published as the current in-memory snapshot.

Live reads also have an admission boundary. `BoardStore` initialization, snapshots,
and policy reload parse an isolated candidate without installing ambient actors,
apply only the existing audited dependency promotions in memory, and verify existing
audit invariants before publishing board and policy together. A candidate needing
promotions is read and verified again inside a revision-checked transaction before
one saved revision is adopted. An invalid audit never triggers a repair write.

Rejected disk state leaves accepted memory intact but raises a read error and records
`admission_error`; retained memory cannot make status healthy. Startup refuses before
serving an unadmitted board, and HTTP reads have no structural-load fallback. Credential
actors, owner normalization, user styles and page metadata use the accepted store
context. Existing once-only standalone-policy refusal diagnostics remain observable,
including when the board is rejected at the same time.

`Handler._status_get()` makes one protected policy/board admission attempt through
`BoardStore.reload_policy_if_changed(raise_on_error=True)`. Its default mode still
returns refusal diagnostics for existing callers. Status does not reload a second
independent candidate through `live_snapshot()`. Read/parse/admission failures and
the following file timestamp lookup share the status error boundary: HTTP 200,
`ok: false`, `mtime: 0`, `stamp: "unreadable"` and `error`, alongside independent
source/build/restart and push metadata. Decoder/policy nesting `RecursionError` is
treated as an expected input refusal, including recorded store admission diagnostics.
A later valid candidate restores healthy
status and refreshes accepted state/policy without a process restart.

An admission needing existing audited dependency promotions may still re-read under
the store's revision-checked transaction; this is not a promise of one filesystem
read or an atomic timestamp/content pair. Other unexpected programming exceptions,
such as `RuntimeError` and `TypeError`, are not blanket-caught as disk health errors.

Offline `load()` and `Board.from_json()` remain structural parsing operations, with
verification intentionally separate and legacy ambient installation enabled by default.
The live path explicitly uses `install_context=False`; this does not implement a pure
domain extraction, new audit rules, a generation protocol or supported backup restore.

## Repository map

| Path | Responsibility |
|---|---|
| `src/localswim/board_state.py` | Domain model, policy loading, validation, history replay, relationships, JSON storage, file locking, and report primitives. |
| `src/localswim/cli.py` | Click command tree, contextual help, and command-to-service dispatch. |
| `src/localswim/attachments.py` | Bounded attachment metadata, audit replay, raster detection, safe paths, flushed file writes, and content integrity. |
| `src/localswim/api_endpoint.py` | Loopback HTTP server, transactional store, REST boundary, embedded HTML/CSS/JavaScript, live reload, and optional Git publishing. |
| `src/localswim/service_ownership.py` | Per-board lifetime writer ownership and the shared startup/restart gate. |
| `src/localswim/workspace.py` | Directory discovery, workspace catalog and loopback gateway, board-service reuse and startup, and SPA deep-link serving. |
| `src/localswim/workspace_config.py` | Schema-1 gateway preferences, strict validation, atomic storage, and cross-process revisions. |
| `src/localswim/gateway_publish.py` | Configuration-only Git publishing with the shared board repository lock. |
| `src/localswim/workspace.js` / `workspace.css` | Attention sidebar, SPA navigation, fixed count columns, and per-board browser state. |
| `src/localswim/rules.json` | Checked legacy/default policy used by controlled migrations. |
| `pyproject.toml` | uv build metadata, Python contract, dependencies, Ruff/formatter policy, and strict Pyright policy. |
| `.python-version` / `uv.lock` | Exact development interpreter and cross-platform dependency lock. |
| `examples/*.example.json` | Generic human-readable initialization inputs and their checked empty schema-4 board. |
| `examples/*.terry-workflow.json` | Terry's preferred seven-lane workflow inputs and their checked empty schema-4 board. |
| `check.py` | Complete local/CI gate: line endings, Ruff, formatting, strict Pyright, pytest, workflow/shell linting, and US English vocabulary. |
| `tests/` | Domain, policy, serialization, store, API, CLI, docs, and gate behavior. |
| `src/localswim/vendor/typefaces/inter/` | Unmodified Inter WOFF2 subsets and their SIL OFL license. |
| `docs/TOOLING.md` | Development-tool choices, exact pins, exclusions, and Codex uv usage. |
| `.github/workflows/gate.yml` | Runs the same gate in CI and obtains the public canonical vocabulary table. |
| `.github/workflows/contribution-policy.yml` | Checks non-exempt pull-request source branch names without executing PR code. |
| `.github/CODEOWNERS` | Makes `TerryOtt` the sole code owner for every repository path. |
| `.github/rulesets/main.json` | Reproducible recipe for the server-side `main` branch ruleset. |
| `.github/repository-settings.json` | Reproducible recipe for merge methods, auto-merge availability, and branch cleanup. |
| `.githooks/pre-commit` | Opt-in local hook that executes the frozen gate through uv. |

There is no JavaScript build, template engine, or database service. Click is the sole
runtime dependency and supplies the nested command parser and help system. Python
packaging uses uv's native build backend and a `src` layout; run the console entry
points and development tools through `uv run --frozen` so the checked lock and
interpreter pin are honored.

## Workspace boundary

Passing a directory to `localswim` selects workspace mode; passing a JSON snapshot keeps
individual-board mode. The workspace default port is 8810. It discovers validated
schema-4 snapshots recursively, excluding hidden directories, Git metadata, attachment
storage, and symbolic-link escapes. Nonboard JSON is ignored; malformed schema-4 boards
remain visible as unavailable entries with an error instead of being partially loaded.

Each regular candidate has an 8 MiB (8,388,608-byte) discovery ceiling, with a
limit-plus-one read to detect growth beyond it. The ceiling is about 4.8 times the
largest representative snapshot measured when introduced. Unreadable, oversized,
invalid JSON, excessive JSON nesting and non-envelope files are omitted. A recognized
schema-4 envelope that fails board validation, including validation recursion, stays
in the catalog with an error. API and attachment limits are separate; discovery does
not promise a bound on total traversal or file count.

A stable proxy request resolves its board with one discovery scan. Its private candidate
retains file identity only for that request; it is neither a persistent cache nor
caller-supplied authorization. Direct path/endpoint calls perform their own safe
discovery. Before consumption and after waiting for board acquisition, containment,
symlink/junction membership and file identity are checked again. An ordinary atomic
replacement may refresh that same key at most twice per request, with fresh bounded
discovery and full board validation. The current launch gate, capability and child
wait remain intact. Unsafe paths, invalid replacements and exhausted churn refuse;
proxy mutations are never replayed. Fixed diagnostic event labels and attempt counts
go to standard error without paths, card content or capabilities.

A board key is its relative POSIX path without `.json`. For example,
`localswim/localswim.json` becomes `localswim/localswim`, with a readable
`/board/localswim/localswim` deep link. Project-label edits do not change that key.
Unknown or unavailable deep links show an explicit error, never a fallback board.

`GET /api/v001/workspace` returns workspace identity and a catalog of entries containing
`key`, `project`, `counts` (`blocked`, `needsHuman`, `review`), `lastCardEditMs` (epoch
milliseconds or `null`), and optional `error`.
The gateway proxies `/api/v001/workspace/boards/<encoded-key>/<board-api-path>` to the
selected board's existing `/api/v001/<board-api-path>`. Encode the key as one segment
(`localswim%2Flocalswim`), even though the browser deep link uses readable slashes.
Attachment URLs are rebased through that gateway as well.

The gateway reuses a healthy descriptor only when it identifies the correct board.
It coordinates launches with the same startup gate used by direct board starts and
hot restarts. Missing or refused rendezvous while the writer lease is busy triggers a
bounded recovery wait (15 seconds), never a duplicate writer. Healthy matching legacy
services are reused even when they predate the lease. A crash releases the OS lease so a new
child can start; an unrelated listener is never terminated or adopted.

Each canonical board has its own acquisition lock for endpoint recovery and startup.
A separate short bookkeeping lock protects the persistent lock map and child/launch-ID
pairs. Never wait for a board lock, probe the network, spawn, wait for, or clean up a
child while holding bookkeeping. Acquire board ownership before briefly entering
bookkeeping, never the reverse. Unrelated boards can therefore recover independently.
Explicit `Workspace.close()` closes new acquisitions, waits outside bookkeeping for
in-flight registration, and removes an owned pair only after its cleanup succeeds.
A failed cleanup remains retryable; replacement identities are never shut down.
Explicit cleanup uses the captured Popen for terminate, bounded wait, timeout-only
kill and bounded reap. It attempts later children after one failure, retains residual
pairs, and reports a sanitized aggregate error. A new launch must first clean any
previously owned residual handle. Normal gateway exit leaves writers running.

When no writer owns the board, it starts a child on an ephemeral loopback port;
that child publishes its actual port in the usual descriptor, so existing CLI mutations
still reach the same sole writer. The gateway never writes board JSON or attachment
files. Browser mutations use the board's browser credential and remain attributed to
`browserUser`; they never borrow the CLI identity. `If-Match` and the board service's
validation, audit, and atomic save remain authoritative.

The workspace root contains `localswim-gateway.json`, independent of board snapshots.
Its schema-1 object has exactly `schema`, `revision`, `autopush`, `pinsInitialized`, and
`pinnedBoards` (ordered unique stable keys). New gateways default to `autopush: false`;
`--autopush` persists true. Omitting the flag on later starts preserves true. Newly owned
children inherit the saved gateway setting; reusing a service preserves its publishing
configuration. A separate gateway publisher commits only this configuration file, using
the board publishers' Git common-directory lock and excluding unrelated staged files.

Catalog fields `preferences` and `gatewayPush` expose the authoritative settings and
gateway publishing status; selected-board publishing status remains independent.
`GET /api/v001/workspace/preferences` returns the settings snapshot directly.
`POST` at that route requires the gateway browser bearer and
`If-Match: "revision-N"` for the configuration revision, never a board revision. Commands
use `action`: `pin`/`unpin` with `key`, `move` with `key`, `target`, and Boolean `after`,
or `initialize` with `pinnedBoards`. Initialization adopts only the first nonempty legacy
list while `pinsInitialized` is false. Normal pin actions initialize shared preferences
even when their result is empty, preventing a later legacy import from restoring unpins.
Storage validates bounded JSON, rejects duplicate and unknown fields, and refuses symbolic
links or unsafe keys. Cross-process locking covers revision comparison, flushed temporary
writes, and atomic replacement. Stale revisions return 412; malformed commands return 400,
oversized requests 413, and storage failures 503. Missing `If-Match` returns 428.
Persisted read, decoding, size and schema failures are classified separately from command
validation. Preferences and whole-catalog GETs return a fixed, sanitized 503 response,
as do valid authenticated POSTs when saved settings are unavailable. No default catalog
or reset is fabricated; malformed bytes remain intact and valid restoration recovers.
Admission retains authentication, revision-header and JSON syntax checks before storage;
semantic command validation follows persisted-state admission and preference CAS.

The sidebar uses fixed grid columns for Blocked, Needs Terry, and Ready for Review.
Every project reserves all three slots, including blank zero-count slots. Thus a row
with two blocked cards and one review card retains an empty middle slot and shares the
same column coordinates as every other row. Labels and accessible full-count names
supplement red, amber, and blue circles; visible `99+` does not replace the true count.
Padded, rounded header labels share their corresponding circle color rules while
remaining centered on the same fixed columns, with space above and below the labels.
Counts exclude archived cards.

The Pinned group precedes Other boards. `state.pins` reflects the gateway's manually
ordered stable keys. Grip dragging and Arrow Up/Down/Home/End reorder only those keys;
pin/unpin controls move boards between groups. Native drag state accepts only known
pinned keys from the same window. Group headings and a subtle divider appear only for
matching nonempty groups; they never indent the fixed count grid. Keyboard moves
announce the resulting position through a polite live region.

Other boards sort by the latest audited card edit's exact epoch timestamp, descending,
then descending combined attention count and ascending case-insensitive project name.
Attention counts break ties only for identical timestamps. The comparator never reads
the current time, so an unchanged catalog cannot reorder as time passes. Stable keys resolve
identical-name ties; activity never changes the manual pinned order.
`last_card_edit_ms()` scans all item history/comments/attachment audit and the board's
relationship, parent, subject, detail, and archive audit arrays. Edits to archived cards
contribute recency; touching a snapshot or renaming the project does not. The shared
stamp parser tolerates unreadable legacy values. Missing activity sorts last; future
activity retains its exact recorded timestamp. Tooltips independently show elapsed whole
minutes, clamped to zero for future activity. Catalog polls refresh order while reusing existing
row elements and preserving keyboard focus, search, list scrolling, and active selection.
The first nonempty legacy session pin list is migrated conditionally, with duplicates
and absent keys discarded. The gateway becomes authoritative thereafter: manual order
and pin choices survive browser and gateway restarts and are shared across clients.
The browser serializes preference commands, refreshes and reconciles stale-revision
conflicts, and shows storage or transport errors while preserving pending choices.
Per-board drafts and sidebar collapse/width remain in window-local session storage.
The expanded sidebar defaults to a minimum of 312 pixels. Its focusable vertical
separator supports captured pointer dragging, Left/Right arrows (16 pixels, or 64 with
Shift), Home/End for the bounds, double-click to reset, and Escape to cancel a drag.
The rendered width is capped at workspace width minus 320 pixels, never below 312;
`state.sidebarWidth` retains the requested width through temporary viewport clamps.
Collapse uses 44 pixels and restores the expanded choice. Resizing changes CSS without
rebuilding the board, and existing observers realign lane headers. Attention columns
keep their fixed coordinates. Width changes never write shared gateway preferences.

Workspace navigation uses History API URLs and updates the active board in place.
Cold deep links and Back/Forward follow the same selection path. Per-board selected
card, filter, lane scroll, comment/editor drafts, and new-card drafts are stored in
session storage scoped to the window; one window's selection does not force another
window to change. Sidebar collapse is remembered per window. Source upgrades retain
the separate guarded reload mechanism; ordinary board selection must never use it.

Workspace JSON reads use a 20-second abort deadline and one active poll per navigation
epoch. Late errors from another epoch cannot replace the current board. The snapshot
marker advances only after painting and saved-state restoration succeed. Transport,
HTTP, and render failures show a stable recovery surface; catalog polling does not
restart a failed selection repeatedly. Explicit Retry is queued if a catalog refresh
is already running. Saved state remains protected until restoration succeeds, so
closing the error view cannot overwrite drawer/editor/create drafts.

Each successful catalog refresh also obtains the workspace browser session and checks
its identity before replacing the page's browser credential. This recovers credentials
rotated by a gateway restart without retrying mutations that might have already applied.
Workspace page reloads compare `PAGE_BUILD` with the normalized gateway catalog build,
never the selected writer's build. `WorkspaceUI.pageStatus()` retains gateway source
metadata independently of board availability; unknown or malformed identifiers never
fall back to a board ID. `renderLive()` evaluates it even when board polling is stopped.
The shared `source_status()` helper supplies build, staleness, restart and refusal
metadata to both standalone status and the workspace catalog. Proxied board status
keeps its original diagnostics; gateway and board restart warnings are labeled separately.
Navigation preserves the reload query marker and other query/hash state, so the one-reload
proof survives a route change. Selecting another board clears only the old board's source
warnings. Existing per-board capture and pending restoration preserve unsent drafts.

The gateway/board/monitor processes launched under Codex may stop when Codex exits;
restore the service and use Retry rather than treating cached monitor files as proof
that a listener or monitor process is still alive.

## Configuration and schema ownership

Three JSON shapes have separate version numbers because they change independently:

| Data | Schema | Owner and lifetime |
|---|---:|---|
| Board snapshot | 4 | Per project, durable, selected on the command line. |
| Embedded transition policy | 6 | Per board, durable, resolved during initialization. |
| Initialization description | 1 | Human-readable input; lane IDs are optional. |
| Initialization permissions | 1 | Human-readable input using user and lane names. |
| Service descriptor | 1 | Per running process, temporary, stored outside the board. |

The board owns all deployment-specific state and resolved behavior:

- project name and loopback port;
- users, labels, human/bot classes, and UI colors;
- `browserUser`, `cliUser`, and `defaultOwner`;
- monotonic `revision` and `nextTicket` values;
- cards, links, comments, item history, and relationship history;
- lane order, stable IDs, mutable labels, and lane creation rights;
- priority order, IDs, labels, and default;
- directed transition edges grouped under actor IDs.

`browserUser` and `cliUser` must be different. The rules must contain exactly two edge
actors, and those IDs must exactly match the configured browser and CLI users. A board
may list additional users as owners, but only the browser and CLI identities are
authenticated mutation actors.

The shared `_parse_users()` validates persisted snapshots and initialization inputs.
Each user requires nonempty string `id`, `label`, `class` and `color` values. Types
are checked before class membership and exact duplicate-ID detection, with errors
such as `users[2].id`. Class is exactly `human` or `bot`; accepted strings otherwise
retain their original contents. No trimming, case folding, string coercion, identity
repair or new color syntax validation occurs. Invalid initialization does not create
or replace its target, and parsing invalid persisted users does not rewrite bytes.

User and policy IDs join case-sensitively. Keep IDs lower-case: request values passed
through `as_actor()` are trimmed and lowercased before matching, while configured IDs
and policy keys are otherwise exact.

JSON parsing rejects duplicate object keys globally and reports parser line/column
locations. Validation is deliberately strict about required types, references,
identifiers, counters, and rule-edge fields. Do not assume every object rejects every
unknown key: rule edges explicitly do; the board loader currently tolerates additional
keys in several persisted objects.

Schema 3 renamed the legacy agent-specific Ready For Work lane ID to
`ready_for_work`; [ADR 0001](decisions/0001-agent-neutral-ready-work-id.md) records that
compatibility decision. Schema 4 embeds the resolved transition policy. A new board is
initialized from a description plus name-based permissions: lane names generate unique
readable slugs once, and all names are resolved to IDs before persistence. An optional
explicit lane ID exists for controlled imports. Label renames never regenerate IDs;
the separate lane-ID migration rewrites policy endpoints, current states, and audit
history atomically. [ADR 0002](decisions/0002-stable-generated-lane-slugs.md) records the
approved identity model and rejected alternatives.

Stable ID `completed` carries terminal semantics independently of its display label.
Lane-ID migration refuses it as either source or destination before reading or
rewriting a snapshot; label rename remains available for presentation changes.

`Rules` owns its canonical policy document independently of the caller's dictionaries
and lists. Public `document` and `TransitionPolicy.to_json()` results are detached
copies, including nested metadata. Their mutation cannot change later serialization
or the compiled permission grants. Immutable policy copies may safely share compiled
state; parsing and serialization retain the existing document shape and metadata.

## Cards, identity, and audit

Every card has two permanent handles:

- `id` is a stable machine slug used by links and API paths. Renaming a title does not
  change it.
- `ticket` is a monotonically allocated human reference rendered as `#0001`. Deleted
  or archived numbers must never be reused.

`nextTicket` is stored rather than derived and must remain above every existing ticket.
Card order is descending longest downstream blocking-chain depth, ascending priority
number, then monotonically allocated ticket number. The browser and focused CLI triage
queries share this comparator. Numeric P IDs compare numerically; named scales retain
policy order. Only canonical `blocks` edges contribute, across every lane and all stored
cards. Depths and strongly connected groups use the full stored graph, preserving links
and cycle ranks. Priority inheritance uses unfinished cards only: stable state ID
`completed` is exempt regardless of display label, and urgency does not pass through a
completed intermediate. Archived unfinished cards still inherit; archived completed
cards are exempt. The condensed DAG is processed without recursive traversal.

`blocking_order()` derives longest downstream depths for sorting from the entire stored
graph, then traverses in the opposite direction to inherit the highest urgency through
unfinished cards.
Relationship mutations and `set_priority()` promote blocked descendants with ordinary
priority audit entries in the actor's transaction. Requested lower urgency is clamped
to the upstream requirement before recording changes. A card with no blockers can have
any priority, even if its descendants are more urgent. Completed cards receive no
automatic promotions; reconciliation never changes their priority or history.
Completion does not demote descendants. If policy permits reopening, inheritance
resumes with audited promotions in the same transaction. Unlinking never demotes cards
automatically. Both caller-facing `blocks` and `blocked_by` accept different priorities
and apply the same ongoing blocker-to-blocked rule without rejecting a relationship.
`verify()` reports an unsatisfied dependency requirement. `BoardStore` reconciles old
boards at startup and replaced snapshots on read through its normal locked transaction,
using `cliUser` for these rule-adoption events. It advances revision only when a
promotion is needed; subsequent reads and restarts do not fabricate duplicate history.
Controlled offline lane/policy migrations also reconcile priorities before verification.
A failed transaction discards relationship changes and their priority cascades together.
The browser payload adds `blockingDepth` to cards and names the same three-part order;
CLI next/search JSON names `blockingDepthDesc`, `priority`, and `ticket` explicitly.
A priority mutation response includes the committed `priority`, letting the focused
drawer selector reflect a clamped request immediately without depending on blur.

The explicit batch operation `card set-priorities PRIORITY REFERENCES...` applies the
same requested priority to 1–1000 selected references within one locked transaction.
Canonical duplicate aliases and selected Completed cards are rejected; archived
unfinished cards are eligible. All selected overrides are evaluated together before
dependency floors, allowing a selected unfinished cycle to be lowered as one group.
Unfinished blockers outside the selection still impose floors, so the structured result
reports selected effective priorities and every actual ticket-ordered change, including
unselected cascades. The actor receives one history entry per actual change; a no-op
adds no history and follows the ordinary store revision policy. No refresh or retry is
performed after a conflict. A mutation timeout or a successful response that cannot
be decoded as a JSON object has an unknown outcome and requires inspection before
retry. The client does not replay the mutation automatically; read-only response
diagnostics remain separate.

Item history, relationship history, parent history, and comments are separate. All
three histories are machine-written and append-only; comments are human/bot prose.
Item history has three event shapes:

| Kind | Discriminant | Meaning |
|---|---|---|
| Lane | neither `ownerTo` nor `priorityTo` | Creation (`from` absent) or movement. |
| Owner | `ownerTo` present | Reassignment; ownership is a label, not authorization. |
| Priority | `priorityTo` present | Priority change. |

`Board.verify()` replays lane history, checks legal actors and chain continuity, and
compares the result to stored state. It also validates owner and priority targets in
their history entries and requires their actors to belong to the board's users, without
making ownership an authorization rule. Optional event fields must be strings or null;
lane, owner and priority fields cannot form mixed event shapes. Nested history/comment
refusals include their item indices. Tickets, revisions and the next-ticket allocator
reject Boolean values while retaining supported legacy defaults. Verification detects
direct state edits but cannot prove that a hand-edited history entry naming an actor on
a legal edge was really made by that actor.

Link and unlink actions append a board-level `relationshipHistory` entry containing the
timestamp, actor, caller-facing relationship direction, and both stable card IDs. It is
separate from current canonical `links` state so a removed link remains attributable.
The field is optional under schema 4: older boards begin recording with their first new
relationship mutation, and no timestamp or actor is fabricated for an existing link.

Parent changes similarly append a board-level `parentHistory` entry containing the
timestamp, actor, child stable ID, and nullable before/after parent IDs. The field is
optional under schema 4, and an existing parent receives no fabricated earlier event.
`Board.verify()` checks each recorded child's hierarchy chain and compares its last
event with current parent state.

Subject changes append a board-level `subjectHistory` entry containing only the
timestamp, actor, and stable card ID. The optional schema-4 field begins with the first
rename made by a supporting build; no earlier events are fabricated. Neither subject
value is duplicated into the event, so Git history remains the optional source for
comparing text when the operator has deliberately arranged versioned board storage.
Description changes similarly append optional schema-4 `detailHistory` entries with
only timestamp, actor, and stable card ID. This lets a board-write monitor attribute
`set-detail` mutations without reading or logging private prose; Git remains the
source for comparing old and new text. Older boards have no fabricated detail history.
Project edits and no-op writes still have no actor-only event, so the monitor
alerts on those even when Bot issued them.

Archiving is independent of lane state. `Item.archived` defaults to false and is omitted
from persisted cards while false. The optional schema-4 `archiveHistory` array records
each change as `{at, by, item, archived}`. Its parser rejects invalid booleans, timestamps,
unknown fields, and missing card references. Verification replays archive state from
false, checks the actor and alternating states, and compares it with every stored flag.
Both configured mutation actors may archive or restore a card in any lane; this grants
no additional lane transitions. No deletion operation exists, and all identifiers,
relationships, comments, and other history survive archiving.

## Relationships and hierarchy

Parent/child hierarchy is separate from general relationships because a tree needs one
parent per card and must reject cycles. `parent` stores the parent's stable card ID.

General links are stored once in canonical direction and their inverse is derived:

| Stored | Derived inverse |
|---|---|
| `blocks` | `blocked_by` |
| `duplicates` | `duplicated_by` |
| `references` | `referenced_by` |
| `relates_to` | `relates_to` |

This representation makes a half-written relationship impossible. Human comments
automatically add `references` links for `#1234` mentions; bot comments intentionally
do not, because bots commonly mention ticket numbers while explaining rather than
linking. An automatic reference uses the comment timestamp and human actor in
relationship history. Explicit CLI/API links remain available to either authenticated
actor.

The browser offers the same model in both creation and editing. A new card and all
relationships selected in its dialog execute on one isolated `BoardStore` candidate.
The drawer adds and removes one canonical link per request; changing a type uses
`Board.replace_link()` to validate the old and new directions before replacing the
stored row and appending its unlink/link audit pair. A refusal discards the candidate,
so no client has to coordinate two half-mutations.

## Lane policy and working conventions

Each board's embedded policy is its executable allow-list. The small generic example
is the product-oriented starting point. The separately checked Terry-workflow example
preserves Terry's preferred personal workflow, also used by FGA, whose lane meanings
are:

| Lane | Meaning |
|---|---|
| `backlog` | Terry held the work back, or Bot lacks enough scope to perform it. Umbrella cards may stay here while their executable children advance. |
| `ready_for_work` | Bot has enough scope to perform the work. Display label: Ready For Work. |
| `in_progress` | The one card Bot is actively performing. |
| `blocked` | Neither Bot nor Terry can act until an external condition changes. |
| `needs_terry_action` | Bot is waiting for Terry to answer, approve, decide, or personally act. |
| `ready_for_review` | Bot believes the work is done and awaits Terry's signoff. |
| `completed` | Work Terry accepted; terminal and append-only. |

Four conventions matter beyond a simple `may_move()` lookup:

1. Terry delegates triage to Bot. Bot should move every sufficiently scoped card from
   `backlog` to `ready_for_work` without waiting for per-ticket permission.
2. A dependency on another Ready For Work card changes execution order, not readiness.
   Bot selects and performs work one card at a time. Use `blocked` only when neither
   actor can act until something external changes, such as hardware arriving from a
   vendor.
3. Bot cannot sign off its own work. Only the browser/human actor has
   `ready_for_review -> completed`. The drawer's **Terry Reviewed & Approved** action
   performs that transition together with the exact `Reviewed & approved, moving to Completed.` comment in
   one revision-checked transaction; the human check applies even when a board's raw
   transition policy permits Bot to move to Completed. A confirmed success closes the
   source drawer through its existing draft-preserving close path. Refusals, failures,
   and delayed responses after switching cards or boards do not close the current drawer.
4. The human actor may move `ready_for_work -> in_progress` when starting selected
   work directly in the browser. Card ownership remains independent of movement
   permission.

Do not use owner as a second permission system. Either actor can move or reassign a
card regardless of its owner; transition policy alone grants movement.

## Persistence and concurrency

The storage sequence is designed so a reader sees the old complete snapshot or the new
complete snapshot:

1. Create `board.json.lock` with `O_CREAT | O_EXCL`.
2. Load and validate while holding the lock.
3. Mutate and verify the candidate.
4. Increment the revision for a service mutation.
5. Serialize indented UTF-8 JSON with a trailing LF to a same-directory PID temp file.
6. Flush and `fsync` the temp file.
7. Atomically replace the target.
8. Remove the lock.

Lock acquisition waits up to one second. A lock older than ten seconds is considered
abandoned and may be removed. Atomic replacement retries only transient Windows
`PermissionError` failures: six attempts, 40 ms apart. Other I/O failures surface.
Failures before replacement leave the prior snapshot in place; a failure after
entering replacement may have committed and must not be treated as proof of rollback.

The live store prepares its isolated publishable board before replacement where
practical. An in-process outcome tracker distinguishes definitely uncommitted,
committed and unknown metadata replacement. Cleanup or memory-publication failure
after replacement still propagates an error, but cannot justify deleting uploaded
bytes. Interruption before outcome bookkeeping remains unknown. Ordinary replacement
refusals also retain bytes conservatively; no temporary-path or readback heuristic
is used to infer rollback. This tracker is not a persisted recovery journal.

A fresh admitted read reconciles accepted board/policy memory with committed disk
state. Preserve the original failure and any compensation diagnostics; do not
acknowledge success or automatically retry an upload after an ambiguous failure.
The contract is application-crash/interrupted-write safety with backups, not latest
edit survival across power loss or hardware failure. No directory-sync or lock
ownership redesign is implied.

The server adds an in-process `RLock` around the filesystem lock. Two clients writing
the same revision produce one winner and one HTTP 412 response; a stale command never
overwrites newer state.

A separate process-lifetime OS lease in `service_ownership.py` protects the live writer
throughout the service lifecycle. Every board start acquires it before store construction, autopush,
listener binding, or descriptor publication. Its lock file remains on disk after release;
unlinking it would permit two processes to lock different files under the same name.
Process exit releases the lease without a stale-file age heuristic. The existing
`.workspace.lock` startup gate serializes direct starts, gateway starts, and hot restarts.
This ownership boundary is separate from the short snapshot transaction lock above and
the Git repository publishing lock.

The five offline structural operations share maintenance admission: `embed_policy`,
`add_lane`, `rename_lane_label`, `migrate_lane_id` and `migrate_lane` resolve the
canonical board path, acquire the launch gate, claim writer ownership, then enter the
snapshot transaction lock. They retain all three through validation and save, releasing
in reverse order on success or failure. The lazy ownership import avoids the existing
board-model/ownership module cycle; initialization and live mutation interfaces do not
change.

A service's held lease excludes maintenance even during a listener gap or when its
actual port differs from the configured snapshot port. Maintenance holding admission
excludes direct and workspace startup before writer construction. A restart's launch
gate likewise excludes maintenance during handoff. The configured-port listener check
remains as a conservative refusal for older services without a lease. Do not use a
`writer_busy` probe as admission or unlink persistent writer/gate files. This does not
change snapshot-lock stealing or introduce a live structural API.

Workspace discovery made before maintenance may need to be refreshed after the
snapshot changes; existing admission still refuses a stale discovery candidate.

A board hot restart holds the launch gate from before listener closure until the
replacement is healthy. It drains HTTP request threads, closes the listener, stops and
joins the board publisher, then hands off through an explicit child process on Windows
rather than assuming inherited byte
locks. The replacement uses the actual bound port, including an ephemeral `--port 0`
assignment. The old descriptor remains until replacement publication; bearer capabilities
rotate normally, while workspace launch identity is retained. PID continuity is not a
contract. A one-use capability scoped to the canonical board lets the replacement skip
only the launch gate; it must still acquire the writer lease. Explicit shutdown wins
over a pending restart. Failed replacement stops its owned child and releases ownership
and launch admission, leaving recovery to a later start. Gateway-only re-exec does not
own a board lease.

Source stat/read uncertainty is retryable until a digest comparison succeeds, including
same-mtime recovery; successful unchanged-mtime checks keep their cheap polling path.
A restart worker canceled by explicit shutdown releases the launch gate and clears its
abandoned scheduled flag under the restart lock. A successful handoff is not abandoned.

`BoardHttpServer` tracks accepted sockets that are still reading input. Ordinary
input inactivity is limited to 30 seconds. HTTP server shutdown or close fixes one
absolute two-second incomplete-input deadline; repeated shutdown calls and trickled bytes do
not reset it. A 100 ms raw-read poll observes expiration on Windows without poisoning
the buffered reader. A timer also shuts down sockets still in that read phase.
Admission atomically leaves the phase under its transport lock; response writes
restore the ordinary socket-operation timeout, including early refusals and downloads.
Short or expired bodies never enter command execution.

Non-daemon request workers are still joined before publisher shutdown, listener
handoff and writer-lease release. An admitted command retains that ownership through
save even if sending its response fails. The read budget adds a poll/scheduling
tolerance; it does not bound arbitrary command, disk or Git/publisher work, including
any publisher preflush before HTTP shutdown begins.

Legacy services started before lifetime ownership may need one-time consolidation during
upgrade. A previously overwritten descriptor cannot enumerate unknown older writers;
the lease cannot retroactively coordinate a process that never acquired it.

## Attachment storage and audit

Cards optionally persist `attachments`, `attachmentHistory`, and `coverAttachment`
without changing schema 4. `attachments.py` validates each GUID, printable original
filename, byte count (1 through 10 MiB), detected media type, SHA-256, timestamp, and
actor. Audit replay checks attachment membership and cover choice against current
state; activity emits action/GUID attribution without filenames or bytes.

`BoardStore.upload()` decodes a bounded body, checks the expected revision under the
existing writer lock, allocates a GUID, and flushes bytes before saving metadata.
A definitely pre-replacement failure removes the new unreferenced file. Committed
or uncertain outcomes retain bytes, including failures during transaction-lock
cleanup, memory publication and response delivery. A raised error is not proof that
attachment metadata is absent; inspect before retrying. Uncertain failures can leave
orphan files, which are not automatically collected or added to audited Git paths.
Stale revisions are refused before creating bytes or attachment metadata.
Paths resolve inside `<board directory>/attachments/<ticket:04d>/<GUID><extension>`;
GUID and extension validation and resolved containment checks reject path traversal.
Each upload gets a separate file, even for identical names or contents.

`remove_attachment()` detaches metadata and clears a selected cover in the same
transaction, retaining files and append-only history for recovery. `set_cover()` accepts
only active raster images. Read routes validate saved byte count and digest. PNG, JPEG,
GIF, and WebP are detected from bytes and may preview inline; everything else uses
`application/octet-stream`, attachment disposition, `nosniff`, and sandbox CSP.

Git snapshot paths comprise the board and exact per-card filenames from its audit,
including removed attachments. HTTP writes pause during snapshot staging/commit so
the committed metadata and references stay consistent; network push releases this
pause. The existing repository lock still serializes multiple board services. No
unrelated or unaudited attachment-directory files may join the commit.

The publisher bounds status command arguments and supplies add/commit paths through
literal NUL-delimited temporary pathspec files, avoiding Windows command-line overflow
as the attachment audit grows. Every status batch must succeed before staging starts.
The pathspec file is closed before Git opens it and is removed after success or failure.
These transport details preserve the same repository lock, mutation pause, exact audited
inventory and exclusion of unrelated staged files; they do not change Git filtering rules.

CLI `card attachment` offers `add`, `list --json`, `remove`, and `cover`/`cover --clear`.
The drawer offers upload, image previews, download, removal, and cover controls;
oversized selection and HTTP refusal use an accessible modal with the reason.
The first active image previews on the swimlane card when no explicit cover is set.
The card attachment indicator places a fixed-size SVG paperclip before a right-aligned,
tabular numeric field with a two-digit minimum width. Larger counts grow without capping
or overlap; zero attachments omit the indicator. Its accessible name and tooltip carry
the exact singular/plural count. Cover previews and comment badges remain independent.
Moving a board requires moving its attachment directory as well as the JSON snapshot.

## HTTP and CLI boundaries

The server binds only to `127.0.0.1`. Read routes are loopback-accessible without a
credential. Mutations require both a per-process bearer credential and
`If-Match: "revision-N"`.

Current API routes:

```text
GET  /api/v001/status
GET  /api/v001/browser-session
GET  /api/v001/board
POST /api/v001/cards
POST /api/v001/cards/priorities
POST /api/v001/cards/<id>/{move,comment,assign,priority,subject,detail,link,parent,archive,unarchive}
POST /api/v001/cards/<id>/approve
POST /api/v001/cards/<id>/attachment
POST /api/v001/cards/<id>/cover
GET  /api/v001/cards/<id>/attachments/<attachment-guid>
POST /api/v001/board/project
POST /api/v001/board/permissions
```

The server maps credentials to actors; request bodies cannot select who is acting. The
browser credential is embedded in the served page. The CLI credential is published in
a temporary service descriptor whose filename is a hash of the resolved, case-folded
board path. The descriptor lives under the system temp `claude-status` directory and
is removed only by the process that owns its token.

CLI and workspace clients share a small descriptor/metadata validation boundary in
`service_ownership.py`, using a lazy import from the board module to avoid the existing
module cycle. Descriptor admission requires schema 1, exact `127.0.0.1`, a non-Boolean
integer port in 1..65535, the canonical requested-board path and required capabilities.
Descriptor files must be regular; file and browser-session/status JSON reads use the
64 KiB metadata ceiling with one detection byte. Malformed or excessive nesting is a
controlled refusal. Full board responses and attachment payloads are not metadata.

CLI descriptor acquisition also checks the live browser-session `boardIdentity`
before ordinary mutation, explicit-revision priority batch or shutdown dispatch.
An explicit revision avoids the snapshot GET, not identity verification. CLI POSTs
still use only the descriptor's CLI credential; the probe does not grant browser
authority. Workspace consumers retain browser token/userCSS checks, legacy upgrade
handling and exact workspaceLaunch ownership checks during child cleanup.
Generic CLI mutations also refuse a snapshot revision that is not a nonnegative genuine
integer before dispatching any POST; zero remains valid.

Metadata failures produce sanitized refusal or unavailable/recovery behavior, without
including capabilities. Service replacement can occur after a successful identity
probe, so credential rotation may safely refuse the subsequent request. This does not
introduce an atomic session-generation protocol or automatic POST replay; uncertain
mutation outcomes still require inspection before any deliberate retry.

The approval route accepts an empty JSON object and uses the authenticated browser actor
and request revision. It validates the `ready_for_review -> completed` move before
appending `Reviewed & approved, moving to Completed.` and changing state in the same transaction. Bot actors
cannot invoke this special action even where the general transition policy permits that
move; stale, repeated, or failed requests do not leave a comment behind.

`board grant-permissions` adds creation lanes and moves for each selected actor
through `POST /api/v001/board/permissions`. Its exact body is
`{actors: [ID, ...], create: [laneID, ...], moves: [{from: laneID, to: laneID}, ...]}`.
Only the CLI credential may invoke it, with `If-Match`. Validate every actor, lane,
and move before adding grants in one sole-writer transaction; preserve unrelated
permissions, cards, and history. The Terry workflow template grants both actors
creation in Blocked and Needs Terry and moves to either lane from Backlog. See the
[README command example](../README.md#board-configuration) for live-board upgrades.

Useful refusal statuses are:

| Status | Meaning |
|---:|---|
| 400 | Malformed body or missing request field. |
| 401 | Missing or invalid process credential. |
| 404 | Unknown or retired route. |
| 409 | Domain, policy, validation, or storage refusal. |
| 412 | Revision is stale. Refresh and retry deliberately. |
| 428 | Missing or malformed `If-Match`. |

Only `/v1/status` and `/mtime` remain as no-cache redirects so an old tab can discover
the current build. Earlier board and mutation routes intentionally return 404.

CLI reports load the board directly and work while the service is stopped. Public CLI
mutations do not write the file directly: they locate the running service, fetch its
revision, and use the same REST mutation path as the browser. CLI mutations are always
attributed to `cliUser`; there is no impersonation flag.

`card archive REF` and `card unarchive REF` use the same authenticated, revision-checked
transaction boundary. Repeating the current archive state adds no duplicate audit entry;
as with other no-op mutations, a successful service transaction still advances revision.
`board show`, `card search`, `card next`, and `comments newest` exclude archived cards
before ordering and limits unless `--include-archived` is supplied. The complete
`board show --json` export, verification, activity, focused inspection, and relationship
endpoints retain archived cards. Focused JSON marks an archived card and includes its
archive events; referenced archived cards carry an archive marker too.

The CLI establishes UTF-8 for stdout and stderr before argument parsing. This is a
correctness boundary, not presentation polish: Windows may otherwise inherit CP1252,
commit a service mutation, and then raise while printing the Unicode move arrow,
turning a successful write into an apparent command failure.

`card show REF` uses the domain model's stable ID-or-ticket lookup and returns one focused
card report. It resolves the parent, children, and every relationship in the direction
the selected card sees it, including the opposite endpoint's current state, priority,
and owner. Both human and JSON defaults expose the subject and comment count but omit
detail and comment text. `--include-comments` opts into that private content for the one
selected card. The report is read-only, works without a running service, and changes no
persisted or REST schema.

`card next N --lane LANE [--lane LANE ...]` applies those same focused-report
boundaries to a bounded cross-lane triage query. It validates every lane against the embedded policy,
refuses audit drift, and orders all candidates with the same comparator as the browser:
descending blocking depth, ascending priority, then ticket number. Input lane order
is only a filter. JSON names that ordering explicitly and returns selection metadata
plus compact focused items. A result carries its parent and child count rather than
expanding a potentially large child subtree; `card show` owns full child expansion.
Detail and comments still require `--include-comments`.

`card search QUERY` locates existing cards without a board dump. It performs
case-insensitive substring matching over stable IDs and subjects plus exact matching
for numeric or `#NNNN` ticket references. It searches every lane by default; repeated
`--lane` options supply an embedded-policy-validated filter. Results use the same
blocking-depth, priority, and ticket total order and compact focused-item shape as `card next`.
`--include-comments` explicitly expands both the matching surface and returned fields to
details and comment text. Search refuses audit drift and changes no persisted or REST
schema.

`comments newest N` provides a bounded catch-up view without requiring a timestamp
cursor. It returns at most N comments across all cards in reverse chronological order,
including ticket, stable card ID, timestamp, author, and the explicitly requested
private comment text. Its JSON report names the limit and deterministic ordering, and
its command scope prevents combining it with another report or mutation.

`activity since` and `activity between` merge creation, movement, assignment,
priority, rename, description change, comment, link, unlink, parent, unparent,
archive, and unarchive records into an
inclusive chronological report. Their RFC 3339 bounds must carry an explicit UTC
offset.
Relationship events name the caller-facing kind and opposite ticket endpoint;
hierarchy events name nullable before/after parent ticket endpoints. The report
intentionally omits card subjects, details, and comment text; JSON output is therefore
composable without turning routine coordination into a prose export. Cross-array
events recorded at the same instant have a deterministic tie-break order, because the
snapshot cannot recover their original causal order.
The `detail_changed` event carries only ticket, stable card ID, timestamp, and actor.

New audit timestamps retain six fractional digits. That precision lets an external
filesystem monitor use its exact preceding-signature observation as the next inclusive
lower bound without repeating an earlier event that happened before the observation.
Whole-second timestamps from older builds remain valid RFC 3339 input and persisted
history; no synthetic migration changes their evidence.

## Board-data placement

A board contains identities, prose, comments, and audit history. Inside this public
source checkout it is accepted only beneath `boards/`, and only while Git confirms that
directory is ignored. Boards elsewhere remain supported and are the normal choice for
versioned/private project data.

Do not use a source-tree fixture path to test the "outside checkout" behavior: that
changes the premise of the safety check. The Codex test command therefore uses
`C:\Temp`, as documented in `AGENTS.md`.

## Browser UI behavior

The individual-board UI is an embedded `PAGE` string in `api_endpoint.py`. Workspace
mode adds locally served `workspace.js` and `workspace.css`; there is no separate asset
build or browser dependency download. Inter's Latin and Latin Extended subsets are served locally
with immutable caching; all board/page responses are no-store.

Important UI mechanics:

- Poll `/api/v001/status` every 400 ms and fetch the board only when its mtime changes.
- Treat a successful parse, not merely an HTTP response or `stat`, as proof of life.
- Repaint lanes wholesale while preserving unsent comment, editor, new-card, search,
  and view state.
- Use session storage to survive one guarded reload when the page build differs from
  the server build.
- Watch Python modules and workspace assets. Preflight changed Python in a child
  process, including workspace imports; JavaScript/CSS are not Python syntax inputs.
  Valid source triggers graceful server re-exec, while invalid Python leaves the healthy
  process running and reports why.
- Reload a complete externally replaced board snapshot, including its embedded policy;
  invalid state or policy is reported rather than partially applied.
- Search titles, descriptions, comments, and tickets entirely in the browser.
- Hide completed cards after 24 hours only when the lane-entry time is known.
- Hide explicitly archived cards regardless of the completed-card visibility toggle.
  The browser payload retains them for relationship navigation and open drawers, while
  lane/open counts and old-card counts exclude them. Archived drawers identify their status.
- Constrain the document to the viewport. The board fills space left by the actual
  header and optional error banner; lane card lists scroll independently. A narrow
  board can scroll horizontally inside its own region, and drawers, dialogs, and
  long error banners retain their own scrolling without document scrollbars.
  Keep project titles intact in split-monitor views by shrinking secondary status
  text and the search field instead of wrapping the header controls.
- Match lane headings and IN/OUT rows to the tallest rendered row of each kind,
  recalculating after repaint, resize, and font loading. Align card starts beneath
  the Completed visibility control, without retaining excess height after widening.
- Render priority labels at 12px bold on cards and in the drawer. Every P0-P5
  text/background pair exceeds the WCAG enhanced text-contrast requirement of 7:1.
- Derive draggable edges, creatable lanes, labels, priorities, actors, and colors from
  server data rather than duplicating policy in JavaScript.

The build ID is the short Git commit. A dirty checkout appends a fingerprint of the
loaded source files and `-dirty`; consequently, uncommitted source edits produce
distinct builds instead of sharing one generic dirty ID. If that checkout later becomes
clean without changing the loaded source bytes, status polling replaces the stale dirty
identity with the clean commit after two matching Git reads and a fresh source-digest
comparison. The Git probe is throttled to one attempt every two seconds while work
remains dirty. A source mismatch never takes this metadata-only path: the existing
preflight and graceful re-exec boundary owns it.

## Optional Git publishing

Individual-board `--autopush` is off unless explicitly supplied; workspace gateways
retain their saved setting as described above. When enabled, a daemon thread waits
five quiet seconds after the latest board write, commits the board and audited attachment
paths, and pushes
the board repository's current branch. Every service using the same Git common
directory serializes that whole operation with an operating-system file lock in
the common directory; process exit releases the lock without stale-lock recovery.
Dirty-write quiet periods and retry backoff use monotonic deadlines throughout the
publisher loop; wall-clock jumps affect neither. Status and commit timestamps remain
epoch-based, and synchronous final flush bypasses the asynchronous deadlines.

The first pass adopts an otherwise valid untracked board at that exact path. It neither
stages nor commits unrelated tracked or untracked files. It refuses boards that are
ignored, outside a Git repository, or in a repository with no remote. It cannot
establish that a configured remote is private. A failed push keeps the successful local
commit and schedules a retry without another board mutation. Even when its board path
is clean, a retry pushes outstanding repository history before reporting recovery. The
UI reports failures between attempts. The ignored `boards/` directory in this public
source checkout is therefore for local data, not for autopush.

`localswim-cli <board> board shutdown` is the graceful service boundary. The authenticated
request serializes behind active mutations, performs a final autopush immediately, and
shuts down only after that push succeeds. The service removes its rendezvous descriptor
after request threads close; the CLI waits for that removal before reporting success.

## Tests and development gate

The suite spans these boundaries:

| Module | Coverage focus |
|---|---|
| `test_board.py` | Card operations, transitions, history, links, hierarchy, and sorting. |
| `test_attachments.py` | Browser/API and CLI uploads, removal, cover selection, size bounds, exact Git staging, snapshot rollback, and immutable file reads. |
| `test_snapshot_publish.py` | Large audited inventories, bounded literal Git paths, unrelated staged-file exclusion, temporary pathspec cleanup, and publication failure/retry. |
| `test_dependencies.py` | Blocking depth, cross-lane ordering, transitive priority inheritance, cycles, deep chains, and atomic adoption. |
| `test_archiving.py` | Reversible visibility, archive persistence and audit, intact relationships, and filtered queries. |
| `test_serialization.py` | JSON validation, duplicate keys/IDs/tickets, atomic save, and locking. |
| `test_policy.py` | Policy immutability, strict edge parsing, actor matching, isolation, and reload. |
| `test_store.py` | Verified startup/read/reload admission, invalid-audit refusal, accepted actor context, policy diagnostics, transactionality, revision races, board placement, and autopush integration. |
| `test_api.py` | Real threaded loopback routes, auth, revisions, redirects, and code restart. |
| `test_workspace.py` | Gateway discovery/proxying, child publishing policy, and workspace UI contracts. |
| `test_service_ownership.py` | Exclusive lifetime leases, offline maintenance admission and startup exclusion, one-use launch handoff, real restart port preservation, shutdown precedence, and failed replacement cleanup. |
| `test_workspace_ownership.py` | Per-board recovery independence, canonical same-board serialization, close/registration and cleanup retry, replacement identity, busy-lease rendezvous, and shared launch coordination. |
| `test_gateway_preferences_api.py` | Gateway preference routes, authentication, independent revisions, and persisted settings. |
| `test_gateway_publish.py` | Configuration-only commits, unrelated staged-file exclusion, shared locking, and push recovery. |
| `test_workspace_config.py` | Preference validation, atomic cross-process revisions, legacy initialization, and safe storage paths. |
| `test_cli.py` | CLI-to-service mutations, activity reports, initialization, and offline migrations. |
| `test_docs.py` | Checked generic and Terry-workflow initialization inputs and generated boards. |
| `test_check.py` | Gate line-ending parser. |
| `conftest.py` | Standard isolated Terry/Bot cast and empty board fixtures. |

Use the exact Codex command from `AGENTS.md` so fixtures and pytest's cache live under
the authorized external scratch root. The complete project gate is:

```console
uv run --frozen python check.py --word-table path/to/claude-dirty-words.py
```

The gate runs LF validation, Ruff `ALL`, Ruff's formatter check, strict Pyright, pytest,
actionlint with ShellCheck, and the external US English/house vocabulary checker. Ruff
verifies that annotations exist; Pyright verifies their types. The vocabulary detector
first runs a known-bad control sentence and fails if the canonical table is missing.
The checker scans prose-bearing Git-tracked Python, Markdown, JSON, TOML, and YAML files.
Its NUL-delimited Git inventory preserves Unicode, spaces and other supported pathname
characters before filtering; it checks current working-tree content. A newly created
untracked document is not covered until it becomes tracked. Tool
pins and the rationale for not stacking redundant linters are in `docs/TOOLING.md`.

CI obtains the canonical word table from the public `FlickrGroupAddr/backend-api`
repository instead of copying it here. The local hook is inactive until a developer
runs `git config core.hooksPath .githooks`.

## Windows/Codex environment notes

The checkout may be owned by the interactive Windows user while commands run as the
Codex sandbox identity. No re-clone is needed. The one-time trust setting and the
per-command global-excludes override are documented in `AGENTS.md`.

The relevant rules are:

- global `safe.directory` trusts `C:/Projects/localswim`;
- `-c core.excludesFile=/dev/null` suppresses only the inaccessible user-level ignore
  file while preserving this repository's `.gitignore`;
- `C:\Temp` is the authorized external scratch root for Codex work on this machine;
- the GitHub CLI token needs `repo` and `workflow` scopes before a push may add or
  change Actions workflows;
- keep Git commands non-interactive and do not change repository configuration merely
  to compensate for sandbox-account warnings.

## Repository governance

The checked-in policy is detailed in `CONTRIBUTING.md`. GitHub's active ruleset, not
the documentation or JSON recipe by itself, is the enforcement boundary:

- `TerryOtt` (GitHub user ID `17037862`) may bypass the `main` ruleset and push
  directly.
- Local Codex uses Terry's credential and is covered by that same user bypass.
- Codex cloud uses the installed `chatgpt-codex-connector` GitHub App and has its own
  direct-push bypass.
- All other actors must reach `main` through a pull request whose source name matches
  `feature/<lower-case-kebab-description>`.
- The `contribution-policy` workflow performs that PR-source check without checking
  out or executing untrusted contributor code.

A local Codex session uses the Git credential supplied by its environment. In this
checkout that is Terry's credential, so GitHub sees local Codex pushes as `TerryOtt`.
OpenAI's installed `chatgpt-codex-connector` (GitHub App ID `1144995`) gives cloud
Codex a separately auditable GitHub identity and direct-push bypass. Terry's standing
order of 2026-09-12 authorizes all future code commits and pushes across all
repositories, including direct commits and pushes to `main`, without asking for
permission again. Codex must complete the required verification and preserve
unrelated user changes.

For every non-bypass PR, the main ruleset requires one approval of the current changes,
and that approval RFC 2119 MUST come from GitHub user `TerryOtt`.
`.github/CODEOWNERS` makes Terry the sole owner of every path, while the ruleset
requires code-owner review; another person's approval cannot satisfy the requirement.
The two `always` bypass actors can deliberately bypass all PR rules because that is
what permits their direct pushes. The rule also requires `contribution-policy` and the
existing `gate`, and only squash merge is allowed. Repository settings disable merge
commits and rebase merges, allow a maintainer to enable auto-merge on an individual PR,
and delete merged same-repository head branches. Native GitHub auto-merge does not
enroll every PR automatically: when selected for a PR, it performs the squash merge
after Terry's approval and both checks pass. If policy expectations change, update
CODEOWNERS, the GitHub rule, both JSON recipes, workflow, `CONTRIBUTING.md`, and this
section together.

## Documentation ownership

Keep each fact in the narrowest durable place:

- `README.md`: installation, operation, public API, and user-facing behavior.
- `CONTRIBUTING.md`: branch naming, pull requests, and GitHub ruleset maintenance.
- `docs/ORIENTATION.md`: contributor-level component map, data flow, invariants, and
  known sharp edges.
- `AGENTS.md`: concise rules an automation agent must follow while changing the repo.
- Embedded policy notes/descriptions: why a specific lane or actor edge exists.
- module/class/function docstrings: detailed implementation decisions and measured
  failure history closest to the code they constrain.
- tests: executable contracts.
- `src/localswim/vendor/typefaces/inter/README.md` and `LICENSE.txt`: font provenance
  and license.

When a schema, route, invariant, or workflow changes, update every affected layer in
the same change. Do not copy the full transition table or vocabulary list into another
document; link to the authoritative data so it cannot drift.

## Shared monitor ownership

`src/localswim/monitor.py` owns the optional `localswim-monitor` entry point,
portable lifetime lock, sanitized CLI attribution, per-board target/status files,
and cooperative reload. `tests/test_monitor.py` covers fail-open actor filtering,
event-count independence, quiet-window debounce, PID scoping, lock exclusion,
and cursor continuity during reload. Updated owners publish an `ownerId` UUID; reload
requests bind both PID and UUID. The launcher can rediscover the first status while the
existing owner holds the lifetime lock. A per-launch `requestId` and persistent
`<target path>.lock` serialize
cooperating target compare/write operations without holding that lock during readiness
waits. Superseded retries and timeout cleanup preserve newer target, reload or stop data.
Complete pre-UUID same-board status retains its historical PID-only cooperative upgrade
path; updated owners require their UUID. Out-of-protocol target writers do not share the
serialization guarantee. No process is forcibly terminated for reload.

Ordinary startup validates saved same-board pending activity only after taking ownership,
preserving failed dispatch and signature
gaps. Foreign or inconsistent metadata is retained and refused, including a pending
end that differs from the saved cursor. Queue acknowledgement and status persistence
are separate, so crash recovery has at-least-once duplicate windows. See README's shared board-write monitor
section for launch arguments and migration from the old FGA-local watcher.

## Private schema-5 preparation (#0129)

`write_protocol.py` provides an inert preparation boundary for the accepted restore
protocol. Production `SCHEMA`, initialization, load/save, HTTP, CLI, browser and
workspace behavior remain schema 4 / write protocol 1. There is no feature switch,
public schema-5 writer, migration command or runtime generation allocation.

The private envelope contains the existing `Board`, one canonical lowercase UUID
and an optional immutable latest maintenance receipt. It is not a `Board` and has
no `to_json` interface, so public save cannot silently discard its generation.
`_parse_schema5` and `_serialize_schema5` validate complete state using isolated
board policy/users and audit verification. Ordinary round trips allocate nothing
and preserve generation, revision and receipt. Preparation uses the same supported
priority adoption followed by audit verification as live candidate admission;
rejected candidates never install ambient actors or policy.

`_prepare_initial_schema5` retains the supplied initial revision;
`_prepare_migration_schema5` advances schema-4 revision exactly once. Both require
an explicit generation allocator, invoked once after successful admission. Future
restore planning must supply its independently allocated generation and final
reviewed revision; these helpers do not implement restore execution or receipts.

`WriteToken` accepts exactly `"generation-UUID-revision-N"`, including the quotes,
with a canonical lowercase UUID and a non-Boolean nonnegative integer revision.
`N` has no leading zeros except zero itself. Missing/malformed input raises
`InvalidWriteToken`; a valid differing generation or revision raises
`StaleWriteToken("state_changed")`. Future adapters map these to 428 and 412 and
must compare under the transaction lock before any mutation or attachment write.

A present `restoreReceipt` requires exactly `operationId`, `planDigest`,
`sourceSnapshotDigest`, `priorRawDigest`, `timestamp` and `authority`. Operation ID
is a canonical lowercase UUID; digests are lowercase 64-character SHA-256 hex.
`priorRawDigest` may instead be the explicit string `missing`, never null or an
omitted field. Timestamp is offset-bearing RFC 3339; authority is exactly
`local-maintenance`. An absent receipt is valid. Receipt data is validated and
copied into immutable values; returned JSON cannot change canonical receipt state.

Later adapter integration checklist:

- Keep this boundary private until server, CLI, browser and workspace preparations
  pass independent review and coordinated isolated release acceptance together.
- Add explicit stopped-board initialization/migration ownership and persistence;
  preserve generation and receipt in every supported maintenance/write path.
- Require the complete token under the writer transaction lock; never accept a
  schema-5 numeric fallback. Advertise protocol 2 and retain captured intent tokens
  across polls, credential refresh and gateway forwarding.
- Restore planning allocates/seals generation, revision, receipt time and content
  once; apply validates the same sealed bytes and performs no new allocation.
- Coordinate consumer rollout separately; this preparation authorizes no live
  migration, installation, restart, restore or backup configuration change.