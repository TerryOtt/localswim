# localswim

A small, local swimlane application for one human and one automation agent. Each board
lives in one JSON snapshot. Open an individual board or a workspace that switches among
boards with an Attention sidebar. It binds only to loopback and has no core runtime dependency
beyond Click, the mature command-line parser used for its nested CLI.

## Quick start

Requirements:

- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/) and [uv](https://docs.astral.sh/uv/) 0.13.7, as pinned for development and CI
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
workspace defaults to **http://127.0.0.1:8810/**. For Windows launches requested through
Codex, use the on-demand `localswim Shared Gateway` task so the gateway runs
independently of Codex. Follow the
[detached launch and logging procedure](docs/ORIENTATION.md#shared-gateway-under-codex-on-windows)
when starting or recreating that task.

The gateway discovers supported schema-4 and schema-6 boards below the directory and presents one active
board at a time. Existing healthy board services are reused; absent services start
on available loopback ports. If a writer is restarting,
the gateway waits for its rendezvous to recover instead of starting another writer.
Recovery is serialized separately for each canonical board, so one waiting board does
not hold up another healthy board. Explicit workspace cleanup closes new acquisitions;
cleanup attempts every tracked child and escalates a captured child from terminate to
kill only after its bounded wait expires. An incomplete cleanup raises one sanitized
aggregate error and retains unresolved child/launch pairs for a later retry. Normal
gateway exit still leaves board writers running.
The CLI continues to use each board's JSON path and its own service descriptor.

Discovery reads at most 32 MiB (33,554,432 bytes) plus one detection byte from each
regular JSON candidate. This ceiling leaves room above representative local boards
while bounding unrelated or damaged files. Unreadable, oversized, invalid JSON and
nonboard files are omitted; a recognized board that fails validation remains visible
with an error. This is a discovery limit, separate from attachment and API limits;
it does not bound the number of files or total directory traversal time.

Ordinary selected-board requests resolve only that board's canonical key; they do
not rescan every JSON file. In-memory recognition and Attention summaries reuse
unchanged content. Metadata checks detect ordinary replacements, while bounded
byte-digest checks detect edits that preserve file size and timestamps. Unchanged
bytes do not require another full parse. The sidebar refreshes every ten seconds;
it still traverses filesystem metadata and checks content, without reparsing all
unchanged boards. Cached entries never authorize a path or a writer.

Containment, file identity and current service descriptors remain checked at use.
Descriptor-bound browser sessions are invalidated by replacement or board changes.
An ordinary atomic save may refresh the same selected key at most twice within the
original request budget. Unsafe paths, invalid replacements and exhausted churn
refuse; the gateway never resends a mutation.

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
Returning to a recently viewed board shows its last confirmed snapshot immediately
with a non-green **REFRESHING** indicator. A fresh read of that selected board
reconciles moved, added and removed cards before saving becomes available again.
Failed refreshes keep the old view visible with **Retry**. This per-tab memory
cache holds up to eight boards and 96 MiB of serialized snapshots, expires boards
after 30 minutes without viewing, and disappears when the page reloads. It adds
no background full-board polling for pinned or previously viewed boards.
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
opens another board. Reads time out after 20 seconds, with one read in flight for each
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
are excluded. Publishers wait for that lock even on simultaneous first use; the
persistent lock file may be empty and must remain in place after publishing.

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

The Ready for review lane can optionally show a single **Approve all** thumbs-up
button. It approves every nonarchived card in that lane in one revision-checked
transaction, adding the configured approval comment to each card. The action is
available only to the configured human browser actor; the CLI cannot approve cards.
It has no preview step. Empty lanes are no-ops. Configure the opt-in with
`board set-approval-button LANE --enabled [--message TEXT]`; disabling uses
`--disabled`. The optional message is trimmed and must contain 1–4000 characters;
omitting it when changing the enabled state preserves the current message. Generic
boards default to disabled; the Terry workflow example opts in for Ready for review.

The important fields are:

| Field | Meaning |
|---|---|
| schema | Board format version; 4 ordinarily, or 6 after an actual ticket reorder. |
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

Within a swimlane, cards appear in priority order, from P0 through P5 on the standard
scale. At the same priority, a card follows all its blockers in that lane. When more
than one card is eligible to appear next, the lowest ticket number wins. Blocking
cycles are placed together in ticket order. The ordering constraints use only links
between visible cards at the same priority within that lane.

The focused `card next` and `card search` commands use **ascending triage score**,
then ascending priority number and ticket number for ties:

```text
triageScore = ((priority number + 1) * 100) - blockedCardCount
```

P0 with no blocked cards scores 100; P1 blocking five cards scores 195. Count each
distinct unfinished downstream card once, across the whole board. Multiple paths
do not multiply the count, and cycles never count the card itself. Completed cards
stop the path and do not count; archived unfinished cards still participate.
Only `blocks` relationships contribute. A score can be zero or negative; it is not
clamped. Named priority scales use their existing policy rank in place of a P number.

Blockers inherit urgency from the cards they block, directly and transitively.
A P3 blocker of a P1 card becomes P1, and scoring uses that priority. Lane/search/visibility
filters and result limits do not change the global scores. Each result includes its
score, blocked-card count, focused identity/status, parent, child count and directional
relationships. Use `card show` for the full child list. Detail and comments remain
absent unless `--include-comments` is supplied explicitly.

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
use the same embedded-policy triage-score, priority, and ticket ordering as `card next`.

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

Offline structural operations (policy embedding, lane addition, retirement, label rename,
lane-ID migration and legacy lane migration) acquire the canonical launch gate and
hold exclusive writer ownership through the snapshot transaction and save. A running
service's lease prevents maintenance even when its listener is absent or uses a
different port. Maintenance also excludes service startup until it exits.

These operations retain the conservative configured-port listener refusal for older
services without leases. Stop the service before maintenance; do not delete ownership
lock files to force access. They validate and replay the complete result before
replacing the snapshot and increment its revision. Failure releases maintenance
admission; the existing snapshot-save outcome rules still apply.

Add a lane to a stopped board with explicit creation and bidirectional move rights.
Existing cards and their history remain in place:

~~~console
uv run --frozen localswim-cli boards/my-project.json lane add "New Lane" --create-for terry --create-for bot --connect-for terry --connect-for bot
~~~

Lane additions append by default. Supply exactly one of `--before LANE_ID` or
`--after LANE_ID` to insert beside an existing stable lane ID; display labels are
not accepted as anchors. Unknown anchors or both options refuse without a save.
For a reference lane at the far left, use the current first lane's ID:

~~~console
uv run --frozen localswim-cli boards/my-project.json lane add "Architecture Decisions" --before backlog --create-for terry --create-for bot
~~~

Use the supported `board shutdown` command for the specific board before offline
maintenance, wait for its writer to exit, and restart with its existing launcher and
configuration afterward. Do not delete locks or force-stop a writer. Preserve any
workspace ownership and saved notification targets. Inspect an uncertain save before retrying.

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

### Retire an empty lane

Stop the board service with `board shutdown` and wait for its writer to exit. Then
retire the lane by its stable ID and restart the board using the shared installation:

~~~console
localswim-cli boards/my-project.json lane retire old_lane
~~~

The lane must have zero active cards. The command protects `completed`. The retired
column disappears and creation or moves into it are refused. Its label, archived
cards, transition history and historical grants remain available for inspection
and audit. Use `card show` or `card search --include-archived` to find those cards.
To restore a retired-lane card, move it while archived to an active lane using an
existing permission, then unarchive it there.

The first retirement updates the embedded policy to schema 7 and increments the
board revision once. The snapshot remains board schema 4. A repeated retirement
leaves the snapshot and revision in place. Older installations that support only
policy schema 6 refuse a board after retirement. Use a retirement-aware shared
installation for the service and CLI before retiring a lane.

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
restores visibility in its current active lane, subject to the 24-hour completed-card
filter. For a card in a [retired lane](#retire-an-empty-lane), move it to an active
lane before unarchiving.

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

## Dependency-first ticket numbers

Use the live board service to preview renumbering within one active lane:

```powershell
uv run --frozen localswim-cli boards/my-project.json board reorder-tickets backlog --json
uv run --frozen localswim-cli boards/my-project.json board reorder-tickets backlog 21 8 34 --json
uv run --frozen localswim-cli boards/my-project.json board reorder-tickets backlog --cards-file selection.json --apply --expected-revision 42
```

`board reorder-tickets LANE [REFERENCES...]` previews by default. Omit references
for every unarchived card in that lane, or use `--cards-file` with a nonempty JSON
array of positive ticket numbers or card-reference strings (not both input forms).
References are an unordered selection, not a requested sequence. The operation
orders blockers before dependents using full direct and indirect `blocks` paths,
including paths through unselected cards and other lanes. Eligible ties keep the
old ticket order. A blocking cycle involving a selected card refuses the operation.
Only the selected cards' existing number pool is permuted; other tickets and
`nextTicket` stay unchanged. Selection is limited to 10,000 cards.

Review the old/new numbers, then use `--apply` to commit. `--expected-revision`
binds an earlier preview to its board revision; stale requests require a new
preview. `--json` returns the revision and complete plan. A no-op does not save,
advance the revision, or upgrade the schema. If a write outcome is uncertain,
inspect the board before retrying.

Stable card IDs retain their subjects, details, comments, ownership, lane,
priority, parents, relationships, and existing histories. Historical numeric
references in prose are not rewritten. Changed cards receive immutable
`originTicket` storage anchors so attachment folders remain in their original
physical location. The first actual reorder writes board schema 6 and appends
`ticketHistory`; later reorders append to that audit. Older writers that support
only schema 4 refuse schema 6. Use this version of the shared service and CLI
before applying. This does not activate the private schema-5 restore protocol.

The browser lane-header reorder glyph opens a preview; only **Apply** commits it.
Expose or hide it per lane through the live CLI:

```powershell
uv run --frozen localswim-cli boards/my-project.json board set-reorder-button backlog --enabled
uv run --frozen localswim-cli boards/my-project.json board set-reorder-button backlog --disabled
```

The setting is optional Boolean `reorderByDependencies` on the lane definition;
omission or `false` keeps the glyph hidden. Generic defaults remain off. The Terry
workflow template enables it for Backlog; existing boards require an explicit
configuration change. The glyph setting alone does not renumber or upgrade a board.
This numbering operation is separate from the existing priority-based lane display
order and does not change priority inheritance.

## Dependency priority inheritance

An unfinished blocker must be at least as urgent as every unfinished card it blocks.
If it blocks P3, P4, and P5 cards, its priority must be P3 or higher. Raising the P3
card to P1 promotes any less urgent blockers up the chain to P1. A blocker may be
more urgent than the cards it blocks.

Completion is determined by the stable state ID `completed`, regardless of its display
label. Completed cards are exempt from promotions and stop urgency propagation.
Archived unfinished cards participate.

Both `blocks` and `blocked_by` enforce the same ongoing rule. Adding or changing a
relationship accepts different priorities and promotes blockers as needed;
priority differences never prevent adding a relationship. Other relationship kinds
and parent/child hierarchy do not affect blocking order or priority inheritance.

Promotions are persisted with ordinary priority-history entries, attributed to the
actor who made the relationship or priority change, inside the same atomic revision.
A request to lower a blocker is clamped to the highest urgency among the unfinished
cards it blocks, and the command result explains the effective priority. Lowering a
blocked card or removing a link leaves its blockers' stored priorities in place.
A later explicit change may lower those blockers within their remaining requirements.

Depths include all stored cards, including completed and archived cards, and all stored
links remain intact. Priority propagation excludes completed cards, so it stops across
completed intermediates even in cycles. Open members of a cycle continue to inherit
urgency from unfinished cards in that group or downstream. The graph is traversed
iteratively, so long chains do not depend on Python's recursion limit. Completion leaves
stored priorities in place, and reconciliation preserves completed priority and history.
If custom policy permits reopening a completed card, unfinished priority inheritance
resumes with audited promotions in the same transaction.

For explicit normalization, `localswim-cli <board> card set-priorities PRIORITY
REFERENCES...` applies one exact policy priority ID to 1–1000 selected card references
in a single revision-checked operation. Canonical duplicate references and selected
Completed cards are rejected; archived unfinished cards are allowed. The operation
evaluates all selected overrides together before applying dependency floors, so a
selected unfinished cycle can be lowered as a group. Unfinished cards blocked by
the selection still impose their floors, and selected cards may therefore finish at a more
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
finds an older blocker below its required priority, start the service to reconcile it.
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
GET  /api/v001/events
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
POST /api/v001/board/ticket-order/{preview,apply,config}
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
evaluated before floors are applied, while unfinished cards blocked by the selection
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
GET  /api/v001/workspace/events?board=<encoded-key>
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

A refused CLI identity preflight appends `; preflight=<JSON>` to the existing
`nothing changed` error. The JSON reports the original check's phase, fixed
code/error category, elapsed milliseconds, `cli_preflight` probe mode, and
validated port/HTTP status when available. It excludes descriptor contents,
paths, credentials, response bodies and raw exception text. The check still uses
one admission-aware browser-session GET with the existing five-second socket
timeout. Failure stops dispatch; diagnostics add no retry or follow-up probe.
Later revision-fetch errors and uncertain POST outcomes retain their separate
messages and must not be interpreted as preflight failures.

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

## Live updates, pending edits, and conflicts

The browser initially reads the current board and then receives small Server-Sent
Event metadata for persisted Human and Bot changes. It fetches the board again when
its admitted content version changes and resnapshots on reconnect. Workspace mode
uses one combined stream for the catalog and selected board; standalone mode uses
one board stream. This is designed for one local browser tab shared by a Human and
Bot, with updates within a few seconds. It is separate from the Human-only owner
chat notification feature.

While SSE subscribers are present, one service-wide board health producer checks
every five seconds and one gateway catalog producer checks every ten seconds. After
the last listener closes, producers pause I/O (an in-flight read may finish); the
first returning view wakes them and resnapshots. Unchanged bytes reuse verified admission. Five-second stream heartbeats perform no
JSON reads. The connection indicator stays green while the event stream is open and
for ten seconds after a disconnection; board read errors are shown separately.
The server `readAt` is a separate
diagnostic for successful file read and admission. Fixed-width time fields retain at
least two digits for hours.

Streams hold only bounded latest state in process memory, with no journal, deltas
or replay; reconnect obtains a fresh snapshot. Shutdown wakes streams, closes upstream
connections and joins workers before releasing the writer lease. No shared browser
worker, cross-tab coordination, external service or framework is required.

Edits, moves and new cards can appear locally with a pending indicator before they
are persisted. Existing-card browser writes retain the card's original opaque
revision in `If-Match-Card`, alongside the board revision in `If-Match`. The writer
checks both inside one atomic board transaction. Conflicting edits to the same card
fail; unrelated board changes may be reread and retried once with the original card
revision. Adds retain one client-generated card ID across recovery attempts.

A lost response is not automatically treated as a failed save. Bounded readback
can confirm an already applied change or safely retry an unchanged target. Uncertain
additive or compound actions are not blindly repeated. Drafts and pending state
survive navigation/reload, and an older response cannot replace newer confirmed data.
Legacy API/CLI consumers keep their mandatory whole-board revision check; the card
header is an additive, more precise browser precondition.

Storage failures during a mutation return a sanitized uncertain-outcome response
(HTTP 503), because a replacement may already have committed. The CLI treats POST
5xx responses and explicit unknown-outcome codes as uncertain and never retries them
automatically. Validation refusals retain their existing 409/413 behavior.

## Live code updates

The status endpoint reports snapshot health separately from source and publishing
diagnostics. Each status request makes one protected admission attempt. Expected disk-read,
parse (including excessive JSON nesting), validation or timestamp failures return
HTTP 200 with `ok: false`, an
`unreadable` stamp and an error; source/build/restart and push metadata remain present.
Retained valid memory does not make failed disk admission healthy. Restoring a valid
snapshot allows a later health check to accept its state and policy without restarting.

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

The gate runs LF line-ending validation, curated Ruff lint, Ruff formatting,
strict Pyright, pytest, and actionlint with ShellCheck. Every stage is mandatory, and no external
repository is required. actionlint 1.7.12 and ShellCheck 0.11.0 must be on `PATH`;
installation details are in [docs/TOOLING.md](docs/TOOLING.md). Sidebar ordering tests execute
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

## One owning chat for board changes

For the simplest owner notification path, use the running board service's built-in
notifications. It needs no separate watcher, monitor pool or optional monitor extra.
Each board has one destination chat. New settings default to disabled.

```console
localswim-cli BOARD notifications status --json
localswim-cli BOARD notifications target THREAD_UUID
localswim-cli BOARD notifications enable
localswim-cli BOARD notifications disable
```

`BOARD` is the board snapshot path; `THREAD_UUID` is the destination Codex chat's
UUID. Setting a target preserves the enabled state, so an already enabled board
can hand off to a new owner with `target`. A newly configured target stays disabled
until `enable`; enabling requires a target. `disable` retains the target.
**Production notifications must stay off until explicitly
activated by Terry.** The commands above document the interface, not an activation
instruction.

Settings belong to the service's OS profile, outside board JSON. On Windows they
live under `%LOCALAPPDATA%/localswim/notifications/`; other systems use their user
configuration directory. Use the CLI against the live service rather than editing
those files. Status uses a CLI-authenticated GET with validated board identity;
a setting change follows that check with one POST. Configuration does not change
board revisions, history or existing monitor target, cursor and pending files.

After a committed human edit, the writer submits only bounded metadata: board
identity/label, activity interval, ticket numbers and operation hints. Automation-only edits produce
no owner notice. A background worker combines bursts until 15 seconds of quiet,
then loads the current enabled state and destination and calls the public
`codex queue` CLI once. It sends no card titles, descriptions, comments or credentials.
On Windows, the service's PATH must resolve native `codex.exe`; command wrappers
are refused so delivery can bound and stop its own child process.
The writer performs no notification configuration reads, delivery I/O or subprocess
launches. Notification failure cannot turn a committed edit into a failed edit.

Notices use a compact, human-readable format:

```text
localswim board-change notice

- Board: Example (`abcd...1234`)
- Start: 2026-11-08 15:10:27 UTC
- End: 2026-11-08 15:10:49 UTC
- Tickets: 0197 (Created), 0199 (Comment added), 0255 (Reviewed & approved), 0288 (Moved lanes)
```

Hints come from committed writer operations, never from the wording of a comment.
An ordinary lane move cannot masquerade as review approval. Other hints include
Contents modified, Archived, Restored and Ticket numbers reordered. A ticket can
carry several distinct hints when its burst includes several operations. They
help the owning chat choose focused activity or card reads; they do not replace
the activity check or authorize work.

Start is rounded down and end rounded up to whole seconds, retaining exact-second
endpoints. Ticket numbers are sorted numerically. The fingerprint is abbreviated;
use the configured board path and full identity returned by the CLI for validation.
Convert the displayed UTC bounds to RFC 3339 when querying activity, for example
`2026-11-08T15:10:27Z` through `2026-11-08T15:10:49Z`.

Delivery is best effort within the board service's process lifetime. There is no
durable cursor, startup catch-up or delivery retry; shutdown or a failed delivery
can lose a pending notice. A timeout can follow an accepted enqueue, so do not
blindly resend. An accepted result means queued, not that the owner finished work.
One Terry-authorized live idle-chat test against V2 M14 observed an immediate
turn/wake and `QUEUE_TEST_RECEIVED`; that bounded observation is not a general
completion guarantee. Receiving a notice does not authorize ticket work or bypass
board gates.

## Retired legacy watchers

Legacy watcher and pool commands have been removed, including native intake,
reload, retarget and stop compatibility. Stale launchers and Python module invocations
fail immediately with retirement guidance, including `--help` and `--stop`.
No optional extra or environment switch re-enables them. Use the shared built-in
`localswim-cli BOARD notifications` commands above.

Retirement preserves legacy target, cursor, pending-alert, status and lock files.
It does not migrate settings, modify boards, kill processes or launch workers.
Historical qualification reports retain their original source/protocol binding.

The bounded sanitized activity query remains available:
`localswim-cli BOARD activity monitor-between START END --json [--cursor CURSOR]`.
For an already queued alert, inspect that metadata first. Only a complete, nonempty
result containing exclusively the configured automation actor proves automation-only
activity; empty, unknown, incomplete or failed results do not. Preserve Human alerts
and normal ticket gates.

## License

MIT. See LICENSE.
