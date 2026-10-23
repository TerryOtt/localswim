# localswim

> This branch is a nonwatched transaction-ownership qualification candidate.
> It is not installed in the active shared package. Adoption and rollback require
> separate approval and coordinated quiescence of every consumer of a board.

A small, local swimlane application for one human and one automation agent. Each board
lives in one JSON snapshot. Open an individual board or a workspace that switches among
boards with an Attention sidebar. It binds only to loopback and has no core runtime dependency
beyond Click, the mature command-line parser used for its nested CLI.

## Quick start

Requirements:

- [Python 3.15.0](https://www.python.org/downloads/release/python-3150/) and [uv](https://docs.astral.sh/uv/) 0.13.1, as pinned for development and CI
- A modern browser
- Git only if you want build identifiers or opt-in automatic pushes

Clone the repository and create a private board from the checked description and
name-based permission examples:

~~~text
uv run --frozen localswim-cli boards/my-project.json board init examples/board-description.example.json examples/permissions.example.json
~~~

Then start the service:

~~~console
uv python install
uv sync --locked
uv run --frozen localswim boards/my-project.json
~~~

Open the URL it prints, normally **http://127.0.0.1:8792/**. The port comes from the
board file so different projects can have stable bookmarks. The --port option provides
a temporary override.

The boards/ directory is ignored by Git and is the only permitted location for board
files kept inside this public checkout. A board may instead live in a separate
repository or directory.

Live services verify a snapshot's existing audit rules before exposing its board and
policy. Rejected external edits retain the last accepted memory but report a failed
disk read; they do not produce a healthy status or silently repair invalid history.
A later valid snapshot can be admitted normally.

## Multiple boards in one workspace

Start localswim with the directory containing your private boards:

~~~console
uv run --frozen localswim path/to/state-store --port 8810
~~~

A directory starts the workspace; a JSON file still starts an individual board. The
workspace defaults to **http://127.0.0.1:8810/**. It discovers schema-4 boards below the
directory and presents one active board at a time. Existing healthy board services are
reused; absent services start on available loopback ports. If a writer is restarting,
the gateway waits for its rendezvous to recover instead of starting another writer.
Recovery is serialized separately for each canonical board, so one waiting board does
not hold up another healthy board. Explicit workspace cleanup closes new acquisitions;
cleanup attempts every tracked child and escalates a captured child from terminate to
kill only after its bounded wait expires. An incomplete cleanup raises one sanitized
aggregate error and retains unresolved child/launch pairs for a later retry. Normal
gateway exit still leaves board writers running.
The CLI continues to use each board's JSON path and its own service descriptor.

Discovery reads at most 8 MiB (8,388,608 bytes) plus one detection byte from each
regular JSON candidate. This ceiling leaves room above representative local boards
while bounding unrelated or damaged files. Unreadable, oversized, invalid JSON and
nonboard files are omitted; a recognized board that fails validation remains visible
with an error. This is a discovery limit, separate from attachment and API limits;
it does not bound the number of files or total directory traversal time.

A stable request uses one discovery scan. If an ordinary atomic save replaces the
snapshot during endpoint acquisition, the gateway allows at most two fresh scans of
that same key and validates the replacement before reuse. It keeps the current
launch gate and child wait; it does not resend a mutation. Unsafe paths, invalid
replacement boards and continued churn still fail visibly. Standard error records
fixed discovery retry/recovery/refusal events and attempt counts without board
paths, card content or capabilities; redirect it to a private log when launching.

The Attention sidebar gives every project three fixed count columns: **Blocked** (red),
**Needs Terry** (amber), and **Ready for Review** (blue). A zero count leaves its column
blank, so a project with two blocked cards and one ready for review has an empty middle
column. The circles remain vertically aligned across every project row. Padded, rounded
column labels use the same colors as their corresponding circles. Column labels,
accessible names, and full counts convey their meaning alongside color; large counts
may display as `99+`. Archived cards are excluded. Collapse the sidebar when lane width
matters; its state is remembered in that browser window. The expanded sidebar starts
at 312 pixels wide, its minimum. Drag its right edge wider, or focus the separator and
use Left/Right arrows (Shift for larger steps), Home for the minimum, or End for the
maximum. Double-click the edge to reset; Escape cancels an active drag. Width is saved
per window, including across reloads. Resizing the window limits the visible width to
leave 320 pixels for the board where space permits; widening it restores your choice.

Pin frequently used boards to keep them in a **Pinned** group above **Other boards**.
Pinned boards keep your manual order. Drag a pinned board's grip relative to another
pinned board, or focus the grip and use Arrow Up/Down or Home/End to reorder it. Pin and
unpin controls move boards between groups; dragging only reorders the pinned group.
Compact group labels and a subtle divider distinguish the groups while the attention
count columns stay aligned. Pin choices and order are saved in
`localswim-gateway.json` at the workspace root. They are shared across browser windows
and clients and survive browser and gateway restarts. Existing window-local pins are
imported once, from the first nonempty legacy list, before shared pins are initialized.
Sidebar collapse and width, search, selection, scrolling, and drafts remain local to each window.
Failed preference saves show an error and support retry; concurrent changes are refreshed
and reconciled against the gateway revision.

Other boards sort by the latest card edit's exact timestamp, most recent first, then
descending sum of the three attention counts, then ascending board name without case
sensitivity. Attention counts break ties only when edit timestamps are identical;
stable keys break ties between identical names. Time passing alone cannot change the
order. Counts and activity refresh
this order without changing your pinned order, active board, keyboard focus, or sidebar
search and scroll.

Card activity includes creation, moves, assignments, priorities, comments, relationships,
hierarchy, title/description edits, archives, and attachments, including edits to archived
cards. File touches and project-label changes do not count. Empty boards or boards with
no readable card timestamps sort after boards with known activity. Future timestamps
retain their exact recorded timestamp for sorting. The board-button tooltip reports elapsed
whole minutes since the edit, clamped to zero for future timestamps.

Board links are bookmarkable. A snapshot at `state-store/localswim/localswim.json` has a
workspace URL such as **http://127.0.0.1:8810/board/localswim/localswim**. The key is its
relative path without `.json`, so changing the displayed project name preserves the
bookmark. Switching boards updates the URL and contents without reloading the page.
Direct bookmarks, browser Back/Forward, and opening a link in another window work too.
Each window keeps its own per-board search, selected card, lane scrolling, and unsent
comment, editor, and new-card drafts in session storage.

Alerts are optional. Unavailable notification support, denied permission, storage
failures, and delivery errors do not interrupt board setup, rendering, or title
updates. If a preference cannot be saved, the page reports that it applies only
to the current page. Permission requests remain tied to your opt-in gesture.
A failed delivery is not attempted again on every unchanged repaint.

Card titles are keyboard buttons. Tab to a title and press Enter or Space to open
that card. The drawer receives focus, and closing it returns focus to the visible
source card. Board repaints preserve card focus by stable identity, including
reordering. If the card disappears or is filtered out, focus returns to card search.
Pointer dragging and independent controls inside a card remain separate actions.

The English application chrome declares its language. Restart warnings and the
Add card, Save description, and Comment buttons use text/background pairs with
at least 4.5:1 contrast, while configured accent colors remain unchanged for
decorative borders. This does not declare the language of user-authored content.

Unavailable or malformed boards display an error; an unknown bookmark never silently
opens another board. Reads time out after 20 seconds, with one poll in flight for each
board selection. If the workspace or board service stops, the page shows a recovery
message and **Retry**, preserving saved drafts and sidebar preferences. Restart the
workspace with the launch command above, then select Retry. A restarted gateway's
browser session is refreshed before further commands; an unavailable service must
not leave the interface on an endless Loading message.

Discovery excludes hidden directories, Git metadata, attachment
storage, and paths that escape the selected directory through symbolic links. Ordinary
JSON files that are not board snapshots are ignored. Board data stays in its original
location, including attachment files beside each snapshot.

A new workspace defaults to automatic publishing disabled. Starting the gateway with
`--autopush` saves `autopush: true` in `localswim-gateway.json`; later starts retain that
setting even when the flag is omitted. The setting publishes the gateway configuration
and enables automatic board publishing for newly started children. Existing board
services keep their own publishing policy. Gateway commits include only the configuration
file, with the same repository lock used by board publishers. Unrelated staged files
are excluded.

## Board configuration

The checked [board description](examples/board-description.example.json) lists users,
priorities, and lane display names. The separate
[permission input](examples/permissions.example.json) names people and lanes as a human
would. Initialization resolves those names, generates readable lane slugs once, rejects
collisions, and persists the resolved policy inside the new board. The checked
[example board](examples/board.example.json) is the resulting valid, empty snapshot.

The generic example stays deliberately small. A second checked
[Terry workflow description](examples/board-description.terry-workflow.json),
[permission input](examples/permissions.terry-workflow.json), and generated
[board](examples/board.terry-workflow.json) preserve Terry's preferred seven-lane
workflow, also used by FGA:

~~~console
uv run --frozen localswim-cli boards/my-project.json board init examples/board-description.terry-workflow.json examples/permissions.terry-workflow.json
~~~

Both Terry and Bot may create cards in Blocked and Needs Terry, and move cards
there directly from Backlog. Existing boards can receive these same additive grants
through their running service:

~~~console
uv run --frozen localswim-cli boards/my-project.json board grant-permissions --actor bot --actor terry --create-in blocked --create-in needs_terry_action --move backlog blocked --move backlog needs_terry_action
~~~

All actors, lanes, and moves are validated before any grant is applied. Existing
grants and other board data are preserved; never edit a live board snapshot directly.

That example carries Terry's working agreement, not merely his lane names:

| Lane | Terry workflow meaning |
|---|---|
| Backlog | Terry held the work back, or Bot does not yet have enough scope to do it. Umbrella cards may remain here while their executable children advance. |
| Ready For Work | Bot has enough scope to perform the work. Dependencies on other ready cards affect ordering, not readiness. |
| In progress | The one card Bot is actively performing. |
| Blocked | Neither Bot nor Terry can act until an external condition changes, such as vendor hardware arriving. |
| Needs Terry | Bot is waiting for Terry to answer, approve a recommendation, make a judgment call, or personally act. |
| Ready for review | Bot believes the work is complete and awaits Terry's review. |
| Completed | Terry reviewed and accepted the work. |

Terry delegates backlog triage and work ordering to Bot. Bot should promote every
sufficiently scoped card to Ready For Work, then take work one card at a time. A human
can approve a Ready for review card with the drawer's **Terry Reviewed & Approved**
action near the priority control (other boards use their human actor's display name).
It adds the `Reviewed & approved, moving to Completed.` comment and moves the card to Completed as one
authenticated, revision-checked action. The action is available only to a human browser
actor; Bot cannot approve its own work. After confirmed approval, the drawer closes
and retains any unsent comment draft for that card. Refused or failed approvals leave
it open; a delayed response cannot close a different card or board.

The important fields are:

| Field | Meaning |
|---|---|
| schema | Board format version; currently 4. |
| project | Name shown in the browser. |
| port | Loopback TCP port; defaults to 8792 if omitted. |
| policy | Resolved lanes, priorities, creation rights, and transition edges. |
| users | Valid identities, labels, classes (human or bot), and UI colors. |
| browserUser | Identity used for browser changes. |
| cliUser | Identity used for CLI changes. |
| defaultOwner | Owner assigned when a new card does not specify one. |
| revision | Monotonic write version; start a new board at 0. |
| nextTicket | Next display number; start an empty board at 1. |
| items | Cards; start an empty board with an empty list. |

Every user requires nonempty string `id`, `label`, `class` and `color` fields; class
must be `human` or `bot`. Numeric, Boolean, null and container values are refused with
the offending field path. Exact duplicate IDs are refused. Accepted strings retain
their case and contents: loading and initialization never trim, coerce or repair them.
This does not impose a new color format.

Priority IDs must be unique with their exact case preserved. A selected nonempty
`defaultOwner` must be a string identifying a user; omitted or falsy values keep
the existing owner fallback. The two schema-1 initialization inputs reject Boolean
schema markers. Both `1` and the previously accepted `1.0` remain supported.

User IDs are case-sensitive. The embedded policy's two transition actors MUST exactly
match browserUser and cliUser. Malformed JSON, ambiguous names, slug collisions, and
invalid fields fail with a path, line, or field-specific explanation.

## Daily use

Browser search matches literal subjects and ticket numbers, plus readable description
and comment text. Inline code retains literal operators such as `*` and `**`;
formatting markup is excluded from rendered-text searches. Comment attribution uses
DOM text boundaries, so actor IDs remain literal data even when they contain markup.

The drawer's card-title rename button supports Tab, Enter and Space. Saving or canceling
returns focus to the still-current control; a failed save retains the editor. Enter
during composition does not submit a partial title. Attachment controls remain bound
to their board and card while an operation is pending or the drawer switches cards.

The browser supports creating, moving, editing, assigning, prioritizing and relating
cards. The CLI can inspect the snapshot while the service is stopped or running:

~~~console
uv run --frozen localswim-cli boards/my-project.json board show
uv run --frozen localswim-cli boards/my-project.json board verify
uv run --frozen localswim-cli boards/my-project.json board show --json
~~~

The new-card dialog can attach any number of typed relationships to existing cards.
The card drawer can add and remove relationships or change an existing relationship's
type. Card creation plus its relationships is one revision, and a type change replaces
the old and new directions in one transaction; any refusal leaves the prior board
unchanged.

Inspect one card by stable ID or ticket number without dumping the board:

~~~console
uv run --frozen localswim-cli boards/my-project.json card show 137
uv run --frozen localswim-cli boards/my-project.json card show '#0137' --json
uv run --frozen localswim-cli boards/my-project.json card show upload-retry --include-comments
~~~

The focused report includes state, priority, owner, comment count, parent, children,
and directional relationships with each related card's current status. Its default
human and JSON forms omit detail and comment text. `--include-comments` is the explicit
opt-in for that selected card's description and comments; bounded activity remains the
audit-history interface.

Inspect the next cards across any set of lane IDs without exporting the whole board:

~~~console
uv run --frozen localswim-cli boards/my-project.json card next 5 --lane needs_terry_action --lane ready_for_review
uv run --frozen localswim-cli boards/my-project.json card next 10 --lane ready_for_work --lane needs_terry_action --json
~~~

Every lane and focused query uses **descending blocking depth, ascending priority
number, then ascending ticket number**. Blocking depth is the longest downstream chain
of `blocks` relationships across the whole board: A blocks B blocks C gives depths
A=2, B=1, C=0. A card with no downstream blockers has depth 0. Ticket number is the
unique tie-breaker. Lane order only filters candidates; it never changes the ranking. Each result includes focused identity and status, its parent, child count,
and directional relationships. Use `card show` when the full child list is useful. Detail
and comments remain absent unless `--include-comments` is supplied explicitly.

Find an existing card by concept without exporting the whole board:

~~~console
uv run --frozen localswim-cli boards/my-project.json card search "upload ambiguity"
uv run --frozen localswim-cli boards/my-project.json card search 137 --lane ready_for_work --lane needs_terry_action --json
uv run --frozen localswim-cli boards/my-project.json card search "private detail phrase" --include-comments
~~~

Search is case-insensitive. Stable IDs and subjects use substring matching; a numeric
or `#NNNN` query matches that exact ticket number. All lanes are searched unless a
repeatable `--lane` option filters them. Default matching and output exclude details and comments;
`--include-comments` explicitly adds both to the search surface and the results. Matches
use the same embedded-policy blocking-depth, priority, and ticket ordering as `card next`.

Card lookup checks exact stable IDs before ticket conversion, including IDs made entirely
of digits. Search retains its numeric ticket-query behavior. Numeric references retain
supported decimal digits; digit-like characters
that are not decimal digits and numbers beyond the interpreter's conversion limit
receive concise refusals. The command does not echo an oversized numeric input.

Catch up on the newest board comments without choosing a time boundary:

~~~console
uv run --frozen localswim-cli boards/my-project.json comments newest 10
uv run --frozen localswim-cli boards/my-project.json comments newest 10 --json
~~~

`comments newest N` is an explicit, bounded request for private comment text. It
returns at most N comments across the board, newest first, with each comment's ticket,
stable card ID, timestamp, and author. Its command scope prevents accidental
combination with a write, migration, or another report.

Time-bounded activity reports combine card creation, movement, assignment, priority,
rename, description change, comment, link, unlink, parent, and unparent events into one chronological stream:

~~~console
uv run --frozen localswim-cli boards/my-project.json activity since 2026-08-23T10:53:09-04:00
uv run --frozen localswim-cli boards/my-project.json activity between 2026-08-23T10:53:09-04:00 2026-08-23T11:09:02-04:00 --json
~~~

Bounds are inclusive and require RFC 3339 timestamps with an explicit UTC offset.
Output is intentionally safe for coordination: it identifies the card, event, actor,
and changed lane, owner, priority, relationship, or hierarchy, but never emits card
prose or comment text. Rename events retain only the stable card ID, actor, and time;
neither subject value is copied into audit history. Relationship events name the
caller-facing relationship kind plus the opposite ticket number and stable ID.
Description edits produce similarly actor-only `detail_changed` events; neither
description value is copied. This optional history starts with the first edit made
by a supporting build, without inventing earlier activity for old boards.
Hierarchy events name the before and after parent ticket endpoints, using the top level
when either endpoint has no parent. Comment events expose only their character count.
Events recorded in different history arrays at the same instant have deterministic
output order, not a recoverable causal order.

New audit events retain microsecond precision so a filesystem monitor can supply
consecutive, non-overlapping activity windows without repeating a boundary event.
Existing whole-second event timestamps remain valid and require no migration.

Sanitized activity output uses timestamp-only values derived from parsed instants;
tolerated legacy suffix text is excluded from output and refusal diagnostics.
Original timestamp strings remain unchanged in the board and full exports. Strict
activity parsing accepts the earliest valid aware UTC instant and reports indexed
conversion errors rather than confusing that value with the display fallback.

CLI mutations use the running REST service so browser and CLI writes share validation,
locking and revision checks:

~~~console
uv run --frozen localswim-cli boards/my-project.json card create docs "Write setup docs" --state ready_for_work
uv run --frozen localswim-cli boards/my-project.json card comment docs "First draft is ready"
uv run --frozen localswim-cli boards/my-project.json card move docs in_progress
~~~

Run **uv run --frozen localswim-cli --help** for the command groups, then append
`--help` to any group or leaf command for contextual help. CLI changes are attributed
to cliUser; browser changes are attributed to browserUser. Request bodies cannot
choose another identity. CLI stdout and stderr are always UTF-8, including when Windows
redirects them through a legacy system encoding.

Schema 4 embeds the complete resolved policy so a board's lane identities and
permissions travel atomically with its state. With the service stopped, upgrade a
schema-3 board with the policy it already uses:

~~~console
uv run --frozen localswim-cli boards/my-project.json board embed-policy path/to/rules.json
~~~

The earlier schema-2 Ready For Work migration also produces a schema-4 board:

~~~console
uv run --frozen localswim-cli boards/my-project.json lane migrate <old-lane-id> ready_for_work
~~~

The five offline structural operations (policy embedding, lane addition, label rename,
lane-ID migration and legacy lane migration) acquire the canonical launch gate and
hold exclusive writer ownership through the snapshot transaction and save. A running
service's lease prevents maintenance even when its listener is absent or uses a
different port. Maintenance also excludes service startup until it exits.

These operations retain the conservative configured-port listener refusal for older
services without leases. Stop the service before maintenance; do not delete ownership
lock files to force access. They validate and replay the complete result before
replacing the snapshot and increment its revision. Failure releases maintenance
admission; the existing snapshot-save outcome rules still apply.

The transaction-ownership candidate holds an operating-system lock on the persistent
`<board>.transaction.lock` file throughout load, mutation, and save. A canonical-path
thread mutex and the OS lock share one monotonic acquisition budget. Same-thread
recursion is refused. The sidecar is never removed, truncated, or replaced; age and
PID guesses cannot take ownership from a live holder. Process exit releases the OS
lock while the same sidecar remains available to the next owner.

A legacy `<board>.lock` refuses admission. This is not a rolling-upgrade protocol:
old and new consumers use different lock names and must never write the same board
together. Before adoption, stop and verify all board writers, offline maintenance
clients, and other transaction consumers; preserve backups and obtain explicit
approval for the concrete cutover. This candidate does not retire legacy locks or
migrate live boards. Rollback also requires all consumers to be quiescent. Persistent
transaction sidecars must not be deleted to force access.

The support boundary remains local filesystems and application-crash safety with
backups. It does not add network-filesystem, cross-host, multiuser, or power-loss
guarantees. Snapshot outcome reporting and conservative attachment-byte retention
remain in force even when cleanup fails after replacement.

Add a lane to a stopped board with explicit creation and bidirectional move rights.
Existing cards and their history remain in place:

~~~console
uv run --frozen localswim-cli boards/my-project.json lane add "New Lane" --create-for terry --create-for bot --connect-for terry --connect-for bot
~~~

Display renames and identity migrations are deliberately different operations:

~~~console
uv run --frozen localswim-cli boards/my-project.json lane rename-label ready_for_work "Selected Work"
uv run --frozen localswim-cli boards/my-project.json lane migrate-id ready_for_work selected_work
~~~

The first changes only presentation. The second atomically rewrites the embedded
policy, current card states, and lane-history endpoints. Neither operation derives an
existing ID from the current label.
The ID `completed` is reserved for completion semantics: identity migration refuses
both changing it to another ID and changing another ID to `completed`. Use
`lane rename-label completed "Done"` to change its displayed name instead.

## Archiving cards

Archive a card to hide it without deleting its contents or changing its lane:

~~~console
uv run --frozen localswim-cli boards/my-project.json card archive 137
uv run --frozen localswim-cli boards/my-project.json card unarchive 137
~~~

Both commands accept a stable ID or ticket number and require the live service. Either
authenticated board actor may archive or restore a card. Its ticket, lane, owner,
priority, description, comments, relationships, and audit history remain intact.
Repeated requests for the same archive state add no duplicate archive event. There is
no card deletion command or API operation.

Archived cards disappear from swimlanes, lane counts, browser search, and the default
`board show`, `card search`, `card next`, and `comments newest` results. Add
`--include-archived` to those CLI reports to include them. `card show REF` always finds
the card, identifies it as archived, and includes archive events in its JSON report.
Existing relationships can still open an archived card's browser drawer. Unarchiving
restores its visibility in the same lane; the existing 24-hour completed-card filter
still applies.

`board show --json` always exports the complete snapshot, including archived cards.
Verification and activity reports also retain archived cards; activity records
`archived` and `unarchived` events with the card identity, actor, and timestamp.
Archiving is an optional schema-4 extension: older boards start with no archived cards.
The snapshot stores a card's `archived` flag only when true and an append-only
`archiveHistory` array after the first archive operation.

## Card attachments and cover photos

Open a card and choose **Upload attachment**. Files up to **10 MiB each** are accepted;
a larger file opens an explanatory refusal dialog without changing the board.
PNG, JPEG, GIF, and WebP pictures preview in the drawer and on the swimlane card.
Choose **Set cover** on an image to select which picture appears on the card; without
an explicit cover, the first attached image supplies the preview. Other file types
remain downloadable attachments. **Remove** detaches a file and clears it as cover
when necessary. File bytes and audit history are retained for recovery.
Cards with attachments show a right-aligned paperclip and count, reserving space for
two digits. Larger totals remain fully visible. The exact count is also available to
assistive technology and in the tooltip; cards without attachments omit the indicator.

The bot uses the same live service:

~~~console
localswim-cli path/to/board.json card attachment add 29 path/to/picture.png
localswim-cli path/to/board.json card attachment list 29 --json
localswim-cli path/to/board.json card attachment cover 29 <attachment-guid>
localswim-cli path/to/board.json card attachment cover 29 --clear
localswim-cli path/to/board.json card attachment remove 29 <attachment-guid>
~~~

Files live beside the board at `attachments/0029/<GUID>.png`: card directories use
four-digit zero padding, stored filenames use a newly allocated GUID and the original
simple extension, and the original filename stays in metadata for display/download.
In the state-store this is `<repository>/<board directory>/attachments/0029/<GUID>.png`.
Copy the board directory, including `attachments/`, when moving or backing up a board.
With `--autopush`, the board snapshot and its audited attachment paths are committed
together; unrelated files and staged changes are excluded.

This is an optional schema-4 extension: cards without files retain their existing
shape. `attachments` records metadata (GUID, name, size, detected media type, SHA-256,
upload time, actor); `coverAttachment` records the chosen image GUID; `attachmentHistory`
records uploads, removals, and cover choices. CLI activity includes these actions and
GUIDs without filenames or file contents. Writes remain authenticated, revision-checked,
and atomic. A failure known to occur before snapshot replacement removes the new
upload. Once replacement may have happened, bytes are retained even if lock cleanup,
in-memory publication or the response fails. Inspect the card's attachments before
retrying an unsuccessful upload: its metadata may already be committed.

Attachment audit verification checks a consistent stored suffix for each GUID across
its history. Active metadata must match the latest applicable upload's actor, timestamp
and suffix. A valid removal and re-attachment can record a new upload attribution;
supported legacy omissions remain accepted. Conflicts are refused rather than repaired.

An uncertain or interrupted replacement may leave an unreferenced file, including
when the operating system refuses the replacement. These conservative orphan files
are not deleted automatically. This provides application-crash/interrupted-write
safety with backups; it does not promise the latest acknowledged edit survives
power loss or hardware failure. Keep JSON and retained attachment bytes together
when backing up. Audited Git paths remain unchanged; unaudited orphan files are not
silently added to that inventory.

Downloads validate size/digest and serve only current attachments; generic files,
including HTML/SVG, are forced to download rather than execute in the board origin.

## Dependency priority inheritance

A blocked unfinished card must have at least the urgency of every unfinished blocker
upstream of it. Completion is determined by the stable state ID `completed`, regardless
of its display label. Completed cards receive no automatic promotions and do not
transmit urgency through historical chains. Archived unfinished cards still inherit
priority; archived completed cards are exempt too.
A card with no blockers can have any priority, regardless of the cards it blocks.
For example, a P5 blocker may block a P0 card and stay P5. A P1 blocker linked to a
P5 card promotes the blocked card to P1. If the blocker later becomes P0, all blocked
descendants are promoted to P0 too.

Both `blocks` and `blocked_by` enforce the same ongoing rule. Adding or changing a
relationship accepts different priorities and promotes blocked cards as needed;
priority differences never prevent adding a relationship. Other relationship kinds
and parent/child hierarchy do not affect blocking order or priority inheritance.

Promotions are persisted with ordinary priority-history entries, attributed to the
actor who made the relationship or priority change, inside the same atomic revision.
A requested priority below an upstream blocker's urgency is clamped to that requirement,
and the command result explains the effective priority. Removing a link or lowering
a blocker's priority never silently demotes its descendants; a later explicit priority
change can lower a card if its remaining blockers permit that change.

Depths include all stored cards, including completed and archived cards, and all stored
links remain intact. Priority propagation excludes completed cards, so it stops across
completed intermediates even in cycles. Open members of a cycle continue to inherit
urgency from unfinished blockers in that group or upstream. The graph is traversed
iteratively, so long chains do not depend on Python's recursion limit. Completion never
demotes descendants, and reconciliation does not change completed priority or history.
If custom policy permits reopening a completed card, unfinished priority inheritance
resumes with audited promotions in the same transaction.

For explicit normalization, `localswim-cli <board> card set-priorities PRIORITY
REFERENCES...` applies one exact policy priority ID to 1–1000 selected card references
in a single revision-checked operation. Canonical duplicate references and selected
Completed cards are rejected; archived unfinished cards are allowed. The operation
evaluates all selected overrides together before applying dependency floors, so a
selected unfinished cycle can be lowered as a group. Unfinished blockers outside the
selection still impose their floors, and selected cards may therefore finish at a more
urgent effective priority than requested. The result reports each selected card's
effective priority and every actual change, including promotions to unselected cards.
Audit entries are written once per changed card, in ticket order, under the real
credential actor. A no-op writes no audit entries but follows the store's normal
revision policy. Supply `--expected-revision` to require a specific revision; without
it, the CLI fetches once and submits once. It does not refresh or retry after a conflict.
If a mutation request times out or its successful response cannot be decoded as a
JSON object, its outcome is unknown; inspect the board before retrying. The CLI does
not replay the mutation automatically. `--json` prints the structured result.

The live service reconciles older boards on startup, and reconciles valid externally
replaced snapshots when reading them, through audited, revision-checked transactions
attributed to `cliUser`. Offline CLI reads never rewrite the snapshot. If verification
finds an older blocked card below its required priority, start the service to reconcile it.
Controlled offline lane/policy migrations also reconcile dependency priorities in
their existing transaction. No new persisted fields or schema migration are required. When priorities use IDs
such as P0 through P5, their numeric values define urgency; named priority scales use
their embedded policy order.

## Transition permissions

The embedded policy is the allow-list for lanes, priorities, card creation, and
movement. Its generated edges are grouped under exactly two actor IDs:

~~~json
"edges": {
  "terry": [
    {
      "from": "backlog",
      "to": "ready_for_work",
      "description": "Why Terry may make this move."
    },
    {
      "from": "ready_for_work",
      "to": "in_progress",
      "description": "Terry may start selected work directly."
    }
  ],
  "bot": [
    {
      "from": "ready_for_work",
      "to": "in_progress"
    }
  ]
}
~~~

Each edge MUST contain string from and to lane IDs, MAY contain a string description,
and MUST contain no other fields. Unknown lanes, self-loops, duplicate actor edges and
duplicate JSON keys are rejected. Initialization inputs may use display names, but the
persisted policy and every runtime request use stable IDs.

Legacy/default external policy reads retry a finite number of times if file
metadata changes during the read. An unstable reload keeps the accepted permissions
and remains retryable. This is a metadata stability check, not byte authentication;
edits preserving the observed metadata are outside its detection guarantee.

## Data safety and Git

`localswim.api_endpoint` is the sole writer while a board is live. Every mutation
validates the model, increments revision, flushes a temporary file in the board's
directory and atomically replaces the previous snapshot. Stale writes receive HTTP 412
instead of overwriting newer state. A process-lifetime operating-system lock prevents
two services from owning the same board, even when started on different ports. A stopped
or crashed process releases ownership automatically; an unrelated listener is never
terminated to make room for a board service.

Board JSON contains identities, descriptions, comments and audit history. Keep it out
of this public source repository.

Saved ticket numbers, revisions and the next-ticket allocator must be genuine integers;
Boolean values are refused. Optional history fields accept strings or null, with lane,
owner and priority event shapes kept separate. Verification checks the recorded actor
for owner and priority events as well as lane moves. These checks detect malformed
records; they cannot authenticate a hand-edited history.

Automatic Git commits and pushes are OFF by default. Enable them only for a board stored
in an appropriate Git repository:

~~~console
uv run --frozen localswim --autopush path/to/board.json
~~~

When enabled, the server commits the board and its audited attachment paths after five quiet seconds and pushes
the board repository's current branch. Multiple board services sharing one Git
repository serialize their commit-and-push operations through one repository-scoped
process lock. A failed push is retried without requiring another board edit. A valid new
board that is not yet tracked is adopted on the first pass; unrelated tracked or
untracked files remain untouched. The server refuses ignored boards, non-repositories
and repositories without a remote; it cannot prove that a configured remote is private.
Quiet and retry intervals use a monotonic clock, so changing the system clock does not
shorten or extend them. Displayed timestamps still use wall-clock time; shutdown's final
flush does not wait for the asynchronous deadlines.

Large attachment histories do not expand into one process command line. Snapshot
status checks use bounded path batches; staging and commit receive exact literal
paths through temporary NUL-delimited pathspec files. Removed attachment files remain
part of that audited inventory. Unrelated staged files and unaudited directory contents
remain excluded, and any failed status batch refuses the snapshot attempt.

Stop a live service through its authenticated CLI rendezvous rather than terminating
its process:

~~~console
localswim-cli path/to/board.json board shutdown
~~~

The command stops accepting mutations, performs one final commit-and-push when
`--autopush` is enabled, and waits for the service descriptor to be removed. A failed
final push refuses shutdown so the live service and its diagnostic state remain
available.

## REST API

The browser and CLI use these loopback routes:

~~~text
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
POST /api/v001/shutdown
~~~

Attachment uploads POST `{name, data}` with base64 file bytes; removal POSTs
`{remove: true, attachmentId}` to the same singular route. Cover selection POSTs
`{attachmentId}` (or null to clear). Upload responses include the assigned attachment
metadata; oversized bodies/files return HTTP 413. Attachment read routes accept
`?download=1` to download an image instead of opening its inline preview.

Review approval POSTs an empty JSON object to `/api/v001/cards/<id>/approve`. It uses
the authenticated browser actor and `If-Match` revision, validates the review-to-
completed transition, appends exactly `Reviewed & approved, moving to Completed.`, and moves the card within
one transaction. Bot actors are refused even if a board's ordinary transition policy
allows that move. A stale or repeated request does not append a duplicate comment.

The batch priority route accepts exactly `{cards: [reference, ...], priority: ID}`
with 1–1000 reference strings and an exact stable embedded-policy priority ID. It
returns `result`, committed `revision`, selected-card `priorities` (effective values),
and ordered `changes` entries `{id, from, to}` for all actual changes, including
cascades. It rejects duplicate canonical aliases and any selected Completed card;
archived unfinished cards remain eligible. The combined selected overrides are
evaluated before floors are applied, while unfinished blockers outside the selection
can clamp effective priorities. The request uses the ordinary bearer credential and
`If-Match` revision boundary.

Single-card priority commands also preserve exact configured IDs, including named
and case-distinct IDs. Only after exact membership fails do standard lowercase
`p0` through `p5` aliases resolve to their configured uppercase counterparts.
Creation rejects empty required IDs or subjects before allocating a ticket. An
omitted HTTP creation priority uses the current locked policy default. Generated
IDs try the free title slug, then the actual next-ticket suffix, then unused
ordinal suffixes starting at `-0002`; explicit duplicate IDs still refuse.

Card and project commands require strings for their documented text/reference
fields; Boolean, numeric and container values are refused instead of coerced.
Creation's optional `id`, `priority`, `detail` and `owner` keep their existing
defaults for omission, null or an empty string. Parent omission, null or an empty
string clears the parent; cover omission or null clears the cover. Other required
string fields do not acquire a null default. A supplied relationship `remove`
must be a Boolean; omission means false, and a valid `replaces` retains precedence.
Attachment removal still selects only `remove: true`.

Missing required fields, malformed JSON and JSON whose nesting the decoder
cannot handle return 400. Wrong command field types return a field-specific 409
without echoing the supplied value. Refused commands do not save a snapshot or
advance its revision.

The board permission route accepts exactly this body, using stable actor and lane IDs:

~~~json
{"actors":["bot","terry"],"create":["blocked","needs_terry_action"],"moves":[{"from":"backlog","to":"blocked"},{"from":"backlog","to":"needs_terry_action"}]}
~~~

It requires the CLI credential and `If-Match`; browser credentials cannot change
permissions. The sole writer validates the entire request and adds all grants in
one revision-checked transaction, preserving existing permissions and board data.

A workspace also exposes:

~~~text
GET  /api/v001/workspace
GET  /api/v001/workspace/preferences
POST /api/v001/workspace/preferences
GET  /api/v001/workspace/boards/<encoded-key>/status
GET  /api/v001/workspace/boards/<encoded-key>/board
POST /api/v001/workspace/boards/<encoded-key>/cards/...
~~~

The catalog includes each board's stable key, project name, the three attention counts,
`lastCardEditMs` (epoch milliseconds or `null` when unknown), and any availability error. Proxy routes append the individual board API path after a
key encoded as one URL segment, for example `localswim%2Flocalswim`. The gateway forwards
requests to that board's sole writer, preserving revision checks and browser actor
attribution. Attachment links use the same origin and selected board.

Forwarded response entities are limited to **96 MiB**, inclusive, before JSON decoding.
Successful attachment downloads retain the **10 MiB** limit; the browser-session
metadata handshake remains limited to **64 KiB**. Unsupported or ambiguous HTTP framing,
unsupported content encoding, truncated bodies and oversized entities produce a small
sanitized 502 response. A failure after forwarding a POST has an unknown outcome:
inspect the board before retrying; the gateway never replays that mutation.
GET responses for the exact board route with HTTP 200 also have a **96 MiB** limit
on final decorated UTF-8 JSON, counted during encoding after CSS insertion and attachment
URL rebasing. Escaping and rebasing can make the result exceed the upstream entity size.
Supported JSON Unicode encodings remain accepted. Decoration failures return a small
sanitized 502 without a partial response. Other methods, statuses and nearby routes
retain their upstream response behavior.
These limits do not bound total gateway memory or promise that every supported snapshot
fits after decoration. The gateway closes each upstream connection after use.

The catalog also includes `preferences` and `gatewayPush`. Preferences use schema 1:
`revision`, `autopush`, `pinsInitialized`, and the ordered stable-key array
`pinnedBoards`. `gatewayPush` reports configuration publishing separately from the
selected board's publishing status. Both preference routes return the preference
snapshot directly. Preference POST requires the gateway browser bearer credential and
`If-Match: "revision-N"` using the preference revision, independent of board revisions.
Bodies are `{ "action": "pin", "key": "project/board" }`, the equivalent `unpin`,
`{ "action": "move", "key": "project/board", "target": "other/board", "after": true }`,
or `{ "action": "initialize", "pinnedBoards": ["project/board"] }`. Initialization
imports only the first nonempty legacy list while shared pins remain uninitialized.
A stale preference revision returns 412; clients refresh and reconcile pending actions.
If saved preferences cannot be read or validated, preferences reads, catalog reads and
otherwise valid preference writes return a sanitized 503 response. The gateway preserves
the saved bytes instead of replacing pins or publishing settings with defaults. Restoring
valid settings permits normal reads and the next write. Invalid JSON still returns 400;
semantic command validation follows the existing saved-state read and revision checks.

Only the `/api/v001` routes are API endpoints. For seamless upgrades of already-open
tabs, `/v1/status` and `/mtime` issue no-cache redirects to
`/api/v001/status`. Earlier board and mutation routes are not retained.

Mutations require the per-process bearer credential and an
If-Match: "revision-N" header. Credentials are published in a user-local temporary
service descriptor; they are not stored in the board. The shutdown route accepts only
the CLI credential and does not change the board revision.

Missing, malformed or unconvertible `If-Match` values receive the existing HTTP 428
response before command dispatch. Supported bare, quoted and `revision-` forms retain
nonnegative revisions, including zero, and the existing stale-revision checks.

CLI writes and shutdown verify that both the local descriptor and the live
browser-session identity name the requested canonical board before sending a POST.
An explicit revision skips the snapshot revision fetch, but still requires that
identity check. CLI commands continue to use the CLI credential; reading session
identity does not grant browser authority.

Descriptor files must be regular files. Service-acquisition descriptor, browser-session
and status metadata reads are limited to 64 KiB and reject malformed or excessively
nested JSON.
The gateway uses the same narrow validation boundary while retaining browser
capabilities, styles, legacy upgrade and owned-child cleanup checks. These limits do
not apply to full board payloads or attachment downloads.

A service can restart or rotate credentials between identity verification and a
write. Such a race may refuse the request; the identity check is not an atomic
session guarantee. An uncertain POST is never automatically repeated: inspect the
board before retrying deliberately.

HTTP read and framing failures produce controlled diagnostics. A known refusal
stays a refusal even when its response body cannot be read. An unusable successful
mutation or shutdown response has an unknown outcome: inspect the board before
retrying. The CLI never automatically repeats that POST.

The browser validates drag data before requesting a card move. It clears drag
decoration and refreshes authoritative state after a submitted move, containing
request, response-read, and refresh failures. An uncertain move is not replayed;
inspect the refreshed board before trying again.

## Live code updates

The status endpoint reports snapshot health separately from source and publishing
diagnostics. Each poll makes one protected admission attempt. Expected disk-read,
parse (including excessive JSON nesting), validation or timestamp failures return
HTTP 200 with `ok: false`, an
`unreadable` stamp and an error; source/build/restart and push metadata remain present.
Retained valid memory does not make failed disk admission healthy. Restoring a valid
snapshot allows a later poll to accept its state and policy without restarting.

The server watches its installed Python modules and workspace JavaScript/CSS assets. A valid
change is debounced, preflighted in a child Python process and applied by gracefully
re-executing the server. An invalid change leaves the healthy process running and
reports the preflight error. An individual-board restart drains active requests and
keeps its actual listening port, including a port assigned by `--port 0`. Startup and
restart coordination prevents another launch from taking ownership during replacement.
Process IDs and browser/CLI credentials may change; clients use the replacement service
descriptor and refresh browser credentials.

An unreadable source remains stale until a later successful digest comparison, even
if its modification time is unchanged. A canceled restart releases its scheduling slot
so a later source poll can try again; successful explicit shutdown remains authoritative.

Board connections allow 30 seconds of idle input. When HTTP server shutdown or close
starts, incomplete headers and bodies share an absolute two-second budget, observed
within a 100 ms read poll plus thread scheduling time. Trickling input cannot extend
that budget. Accepted commands still finish before writer ownership is released;
these transport limits do not bound command execution, disk writes or Git publishing.
Response writes retain their ordinary socket-operation timeout, including attachment
downloads. Any earlier command or publisher flush is outside the read-drain budget.

When upgrading from a build without lifetime writer ownership, stop and consolidate any
legacy duplicate board services once before launching the upgraded service. An overwritten
legacy descriptor cannot reveal every older writer.

A service started from a dirty checkout retains that truthful Build ID while work
remains. Once the checkout becomes clean, status polling checks at most once every two
seconds and adopts the clean commit only after two Git reads agree and both loaded
Python source digests still match disk. Changed source continues through the guarded
re-exec path; the metadata refresh never hides or overrides real code drift.

Open tabs detect a source upgrade and reload once. In workspace mode, reload checks use the
gateway build that served the page; another board service having a different build
does not trigger a browser reload. Gateway and board source-restart warnings remain
independent. The reload marker survives board navigation so a failed refresh cannot loop. Normal workspace board switching
uses SPA navigation and does not reload the browser document. Comment drafts, active editors, a
partially written card, search text and view state survive in per-tab session storage.

## Development

Before changing the implementation, read the
[contributor orientation](docs/ORIENTATION.md). It maps the components, data and
request flows, schemas, safety invariants, state-policy caveats, test boundaries, and
Windows/Codex environment details that are intentionally more technical than this user
guide. The branch and pull-request policy lives in
[CONTRIBUTING.md](CONTRIBUTING.md), and automation-specific working rules live in
[AGENTS.md](AGENTS.md). The reasoning and pins behind the development tools are in
[docs/TOOLING.md](docs/TOOLING.md).

Install the exact Python and locked development environment:

~~~console
uv python install
uv sync --locked
~~~

Run the complete gate:

~~~console
uv run --frozen python check.py
~~~

The gate runs LF line-ending validation, Ruff `ALL`, Ruff formatting, strict Pyright,
pytest, actionlint with ShellCheck, and the project's US English vocabulary check. The
vocabulary table comes from the public FlickrGroupAddr/backend-api repository; pass its
path with **--word-table path/to/claude-dirty-words.py** if it is not in a neighboring
checkout. actionlint 1.7.12 and ShellCheck 0.11.0 must be on `PATH`; installation details
are in [docs/TOOLING.md](docs/TOOLING.md). Sidebar ordering regression tests execute
the production JavaScript helpers with Node.js when it is available on `PATH`; those
tests skip when Node.js is absent. Node.js is optional test tooling, not a runtime
dependency.

Build reproducible source and wheel distributions with the declared backend, ignoring
any local uv dependency-source overrides:

~~~console
uv build --no-sources --clear
~~~

Enable the local pre-commit hook once per clone:

~~~console
git config core.hooksPath .githooks
~~~

GitHub Actions runs the same gate on pushes and pull requests. Text files are enforced
as UTF-8 with LF endings by .editorconfig, .gitattributes and the gate.

## Shared board-write monitor

Install the optional monitor extra with `uv sync --locked --extra monitor` or
`uv tool install --editable '.[monitor]'` from this checkout. Click remains the sole core dependency.
The explicit optional dependency decision is `watchfiles==1.3.0` (MIT): native
filesystem notifications and a trailing-edge quiet window avoid polling board
contents. Core CLI and board services do not import this package.

Run one monitor per board with distinct runtime paths:

```console
localswim-monitor --board-path /path/to/board.json --board-label "Project board" --thread-id THREAD_UUID --port 8793 --status-path /runtime/project-status.json --target-path /runtime/project-target.json --lock-path /runtime/project.lock
```

The port and all three runtime paths are required. The port is a compatibility
and startup hint; lifecycle checks follow the live service descriptor and validate
its board identity, so a relocated listener remains healthy and another board on
the old port cannot substitute for it. `--automation-actor` defaults to
`bot`; configure the board's persisted automation actor ID when it differs.
The default quiet window is 15 seconds; `--quiet-seconds` accepts up to 300 seconds.
Repeated launches retarget the existing monitor through its target file rather
than creating another owner. Keep lock files persistent even after exit.

After the quiet window, the monitor queries sanitized metadata through
`localswim-cli BOARD activity between --json START END`. A nonempty result is
suppressed only when every event belongs to the configured automation actor.
Human, mixed, unknown, empty, malformed and failed results queue an alert.
Filesystem signatures and activity-event counts differ and are never compared.
No board prose or service credentials enter monitor logs or status files.
An already queued alert should first inspect only its bounded activity metadata;
proven automation-only activity needs no card inspection or work resumption.

Ordinary startup acquires the monitor lock before validating and recovering saved
same-board activity. A saved failed pending burst and cursor survive a restart even
without a later board write; a changed file signature adds a covering interval.
Foreign, unreadable, unsupported or inconsistent recovery metadata causes startup to
refuse while preserving the status document for investigation. Inspect that document
and recover its uncertain interval before replacing it; do not simply delete it.
Delivery is at least once: a crash after queue acknowledgement but before status
persistence can duplicate an alert. This does not promise exactly-once delivery or
survival of sudden power loss.

Add `--reload` to the same launch command after deploying an updated package.
It requests replacement of the recorded monitor PID and owner UUID while the board stays
live. The old monitor continues observing writes, drains its pending quiet window,
and releases its OS lock. The replacement waits up to 330 seconds, owns that same
lock, and resumes the prior activity cursor; a write during handoff becomes a new
pending burst. The status document publishes `reloadSupported: true` and
`actorFilterVersion: 2`. A timeout fails rather than starting a duplicate.

If the lifetime lock is held before the owner's first status appears, the launcher waits
for a complete same-board identity and targets that owner. Each launch has a request UUID;
retries and timeout cleanup leave newer retarget, reload and stop requests untouched.
Cooperating target writers serialize updates through a persistent `<target path>.lock`
file, distinct from the lifetime lock and other runtime paths. Readiness waits do not
hold that request lock. Direct writes outside this protocol do not receive that guarantee.
Pre-UUID shared owners can still hand off through their older PID-based reload protocol
when their same-board status is complete. That one-time compatibility path cannot add
UUID checks to an already running old owner; replacement owners publish and enforce them.

The old FGA-local monitor predates this protocol and needs its existing graceful
board-stop/restart boundary once before future reloads can leave the board running.
Do not use PID termination or assume Windows byte locks survive process replacement.

Stop requests retain the existing target-file protocol: schema 1 plus `stopPid`
for the exact owner makes it wait until its board service has stopped before exit.
A normal monitor also exits after five consecutive failed service-health checks.
Status files are a sanitized handoff, not proof of a process or listener's health.

## License

MIT. See LICENSE.
