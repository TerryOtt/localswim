# V1 scale recovery: lessons and test cases for V2

**Draft 2 — 2026-11-07. Working discussion copy; not sent to V2.**

Tracking card: **#0215**. This draft records the accepted work in #0210–#0214 and
#0216–#0218, #0219 and #0220. Terry approved the notifier and SSE cards and moved
them to Completed before this update.
It will be amended after Terry's further discussion. It does not authorize V2
implementation changes or alter existing V2 acceptance gates.

## What happened

Terry reported that localswim was slow the previous day and started failing
today. The gateway was visibly less responsive, the file-read counter sometimes
exceeded seven seconds, and some mutation responses timed out even though the
change later appeared in the board. Monitor failures added uncertainty.

V1 is usable again, and the repairs passed substantially larger synthetic
workloads. Terry subsequently reported that the gateway was vastly improved and
accepted #0210–#0214 and #0216–#0218. Further discussion led to the simpler owner
notifier in #0219 and the browser SSE path in #0220, both now accepted. That is
useful operational confirmation alongside the tests.

The main lesson for V2 is work amplification. A cheap-looking sidebar, health
check, or change detector can repeatedly trigger expensive work elsewhere. SQLite
removes the need to parse a whole JSON file for each read, but it does not by
itself prevent full-board queries, unnecessary payloads, repeated rendering, or
unbounded background work.

We did not measure a formal throughput curve proving a particular nonlinear
algorithmic rate. We observed a scale cliff and identified repeated work and
serialized paths that can make latency rise sharply as queues fill.

## How the work multiplied

The former visible-board timer ran every **400 ms**. The workspace sidebar
refreshed every **two seconds**. An ordinary selected-board gateway request also
rediscovered JSON candidates across the workspace, loaded the selected board,
and obtained an admission-aware browser session before forwarding the request.

At the intended cadence, a quiet gateway tab could cause roughly **11 logical
reads/parses per second of its selected board** and **3.5 of every other board**.
Those are code-path estimates, not measured physical disk reads: Windows can
cache the bytes, and slow requests reduce the achieved rate. Parsing, validation,
history verification, copying and allocation still consume resources.

The multiplier was approximately:

`workspace board count × open clients × refresh frequency × work behind each request`

The filesystem monitor was a separate contributor. Its native watcher did not
itself repeatedly parse JSON. It also performed frequent housekeeping/health
checks and, after a quiet burst, ran a CLI activity query that loaded and verified
the board. Older health paths could additionally perform admission work. Native
watching alone therefore did not make the complete monitoring pipeline cheap.

The displayed file-read age also obscured diagnosis: it advanced after the browser
finished a successful polling cycle, including a changed-board download and render.
It was not a direct measurement of how long one disk read took.

## The use case mattered as much as the implementation

Terry clarified the actual operating model: **one Human, one browser, one tab,
on localhost**. Bot writes through the CLI. Updates within a few seconds are fine.
There is still concurrent Human/Bot intent to protect, but there is no requirement
for a many-user collaboration platform or cross-tab coordination.

The agent notification use case is narrower still: best-effort notice to the one
Codex chat owning that board and repository when Terry changes the board. Its
destination changes explicitly at milestone handoff. It does not require a durable
notification cursor, missed-message catch-up, multiple owners or delivery replay.

The initial request named a solution—a CLI file reader—before that problem was
fully stated. Native watching, pooled intake and recovery controls made the reader
more efficient, but they were more machinery than this use case needed. The writer
already knew which operation had committed and who performed it.

The final change moved discovery to the producer: after persistence, hand a small
signal to background delivery. Apply that principle separately to chat notices and
the browser. Slow consumers must not hold the writer. These two consumers have
different filtering and recovery needs; sharing a principle does not make their
delivery guarantees identical.

## What V1 changed

The following subsections describe the final accepted behavior. Five-second board
polling and ten-second sidebar polling were useful interim repairs in
#0210–#0214; #0220 subsequently removed those browser timers.

### Gateway

- Resolve the selected board without a workspace-wide JSON scan.
- Reuse unchanged recognition and sidebar summaries.
- Recheck path, file and writer identity; cached facts do not become authority.

### Board service

- Reuse admission only for identical bytes and accepted policy.
- Centralize periodic disk-health work while subscribed; pause it when the last
  listener leaves.
- Avoid repeated parse/audit work and unnecessary full-board copies.

### Freshness

- Separate stream connection health, successful disk admission, accepted browser
  snapshots and Git backup status.
- Use an admitted content marker so timestamp-preserving and policy-only changes
  are visible.

### Browser

- Load an authoritative snapshot, then use SSE metadata to announce changes.
- Fetch a snapshot on a content change or reconnect; no routine browser
  status/catalog polling.

### Writes

- Project pending edits locally.
- Enforce original card revisions inside the atomic board transaction; genuine
  same-card conflicts reject the second writer.

### Recovery

- Retain stable Add identities, drafts and acknowledged revision floors.
- Read back uncertain outcomes before a bounded conditional retry.
- CLI/proxy writes and uncertain additive/compound actions are not blindly replayed.

### Attachments

- Read only immutable attachment metadata under the board lock; perform blob
  reading and integrity checks outside it.
- Avoid copying the whole board for one image/download.

### Diagnostics

- Keep unsafe captured output away from response/lifecycle paths.
- Preserve visible startup refusals and use safe output before interpreter startup
  in background launchers.

### Owner notifier

- Send committed Human-edit metadata to one saved chat through a background worker.
- Resolve the current target at delivery; one bounded attempt, no notification
  journal or retry.

### Legacy monitors

- Keep the native watcher/pool improvements available for compatibility.
- They are not the normal owner-notification path and remain off.

### Attribution

- Exclude Bot commits from owner chat nudges; include both Human and Bot commits
  in browser updates.
- Keep bounded sanitized activity evidence for inspection.
- Unknown coverage is not proof of Bot-only activity.

### Transaction and legacy feed boundaries

The transaction boundary remains the **board**. A move, relationship change or
priority reconciliation can affect several cards; those effects must commit
together. Card revisions reject stale intent. Separate lane locks were not needed
to obtain safe behavior. Independent boards remain independent.

The **legacy monitor feed** is an in-memory evidence cache, not a durable journal.
A writer commit performs one exact-byte verification; ordinary feed queries use
strong file metadata. Restart, retention, external-change and other coverage gaps
remain explicit. Native events are hints, not one-for-one transaction records.
Production legacy monitors were left off. The legacy notifier's at-least-once
delivery has a duplicate window between queue acknowledgement and status persistence.
That is a legacy monitor contract, not a guarantee of the new owner notifier.
Unsupported transient manual JSON writes overwritten outside the sole-writer
protocol cannot be reconstructed from the feed.

### Owner chat notifications: #0219

The sole writer submits bounded metadata only after a successful Human commit:
board identity/label, an edit interval and ticket numbers. Bot commits do not
produce chat nudges. Configuration changes do not edit board JSON or generate a
board-change notice.

A background worker coalesces a burst until **15 seconds of quiet**, loads the
current enabled state and target, then invokes public `codex queue` once. Settings
reads, notice serialization and subprocess delivery run outside the writer path. A slow
or failed delivery cannot turn a committed edit into a failed edit.

The CLI provides `notifications status --json`, `target THREAD_UUID`, `enable`
and `disable`. Settings persist outside board JSON in the service account's OS
profile. Retargeting preserves enabled state; disabling retains the target. Use
the identity-checked CLI interface, and inspect status after an uncertain write
instead of blindly replaying it. Activation remains explicit.

This is process-lifetime best effort. Pending bursts can be lost on shutdown;
failed or uncertain sends are dropped. There is no durable notification cursor,
startup catch-up or resend. One authorized idle-chat smoke test showed that
`codex queue` can immediately start a turn. Queue acceptance is not proof that the
agent consumed the notice or finished work. Windows requires native `codex.exe`
on the service's PATH.

The notifier and legacy watcher/pool are not run together for the same board.
Existing legacy target, cursor, pending-alert and lock files were preserved.
Owner settings were also preserved through the SSE rollout, including an already
enabled V2 target; the rollout did not activate notifications on every board.

### Browser updates: #0220

The browser loads a snapshot and keeps one SSE connection open. In workspace mode,
that stream combines the selected board's status with sidebar/catalog updates.
Persisted changes by **either Human or Bot** wake background metadata production.
The stream carries small notices, not the full board or a card-delta protocol.
The browser fetches current state when `contentVersion` changes or on reconnect.
Revision alone is insufficient because an admitted external change may preserve it.

Existing HTTP mutation commands, card/board revision checks, acknowledged revision
floors, optimistic projections and draft handling remain. SSE does not acknowledge
an edit before persistence, replace conflict detection or authorize mutation replay.
Reconnection recovers current state; it does not replay a durable event history.

The board's shared disk-health producer checks every **five seconds while it has
subscribers**; the catalog producer checks every **ten seconds while subscribed**.
Committed changes can wake production sooner. Leaving a board pauses its sampling,
so visiting many boards does not leave each one rereading JSON indefinitely. An
already admitted read may finish. A returning view wakes production promptly.

A prebuilt **five-second heartbeat performs no JSON I/O**. Connection health is
separate from **Last confirmed sync**, which records the browser's last accepted
and rendered authoritative snapshot. Server `readAt` remains a separate disk-read
diagnostic. Removing browser polling does not mean all disk-health checks vanished.

Latest-only metadata and bounded writes keep slow consumers off the writer.
Shutdown wakes streams, interrupts upstream connections and joins producers before
writer ownership is released. An older running board that lacks `/events` receives
one bounded, read-only status upgrade hint, then normal reconnection.

SSE fit the local use case: server-to-browser notices over an ordinary HTTP
response, with browser-supported reconnection. Edits already had an HTTP API;
they did not need a second bidirectional command channel. No new dependency,
external push service, daemon, framework or cross-tab coordinator was added.

### A related transport finding

Qualification exposed Windows connection resets when an early HTTP refusal closed
before the POST body arrived. An isolated delayed-body probe reproduced that race;
the bounded response-cleanup fix returned all **600/600** probe refusals correctly.
That is transport evidence, not a throughput or universal delivery guarantee.

The service now sends and half-closes the refusal, then discards raw input within
**64 KiB and an absolute 200 ms budget**. Board cleanup occurs after the mutation
gate is released. Discarded bytes are never parsed, forwarded or replayed. It does
not promise delivery against unlimited uploads. Pre-existing test-helper retries
were removed so a lost response cannot silently replay a test mutation.

## What was exercised

The read-only inventory sampled **19 recognized workspace boards**. The largest
had **412 total cards**, including completed and archived cards. The largest
canonical stored card was **297,815 bytes**. Thus the requested 10× targets were
**4,120 cards** and at least **2,978,150 bytes in one card**.

“Combined” means a high-card-count board containing an oversized card. It does
not mean every one of the 4,120 cards has the maximum-sized content.

| Workload | Total cards | Largest stored card | Board bytes |
| --- | ---: | ---: | ---: |
| Automated count + content matrix | 4,120 | 3,475,740 | 9,868,020 |
| Browser card-content case | 412 | 3,029,238 | 5,117,192 |
| Browser card-count case | 4,120 | 4,660 | 20,636,670 |
| Browser combined case | 4,120 | 3,029,238 | 23,680,086 |

These are distinct fixtures. The automated combined matrix does not itself prove
the separate 20 MiB qualification. That was covered by dedicated admission/catalog
tests and browser fixtures. The dense browser workspace additionally contained
**24 boards**, each initially **22,116,128 bytes**, **401 cards** and **4,800 comments**.

The automated combined matrix included **1,373 archived cards**, **2,746 completed
cards** and substantial comment/ownership history. Completed and archived counts
overlap; they are not disjoint categories. The browser combined case rendered
2,747 non-archived cards while retaining all 4,120 in the data model.

Observed results included:

- Three browser stress views loaded together. The oversized 300-comment card
  opened and saved.
- A four-second delayed Add showed pending state and persisted once.
- A dropped successful POST response became a gateway 502. Readback confirmed
  the original stable Add identity; it did not create a duplicate.
- A same-card stale edit was rejected with its draft retained. An unrelated-card
  change allowed the valid edit to complete.
- Warm admission tests submitted 32 actual status reads of a 20 MiB board across
  eight workers, without repeating parsing, audit verification or full-board copying.
  Protected reads remained serialized by the store lock.
- In the automated combined case, **three warm status reads together took about
  19 ms**. This is a total for three reads, not 19 ms per read.
- Cover plus maximum-size **10 MiB** download checks performed no full-board copy.
- Native monitor qualification exercised **12 × 20 MiB atomic replacements**
  under a shared parent subscription on Windows.
- The monitor's actual two-writer/child-CLI/native-pool test suppressed bot-only
  bursts and produced one fake test alert for each human/mixed burst. It sent no
  real Codex test notifications.
- Missed-event reconciliation, blocked delivery, query/write overlap, owner UUIDs,
  pending recovery, drain and member-failure isolation were tested.

The final browser diagnostic sample contained 93 bounded records. Response-time
medians were **639 ms catalog**, **155 ms status**, and **948 ms snapshot**;
observed maxima were **3,144 / 2,009 / 4,408 ms**, respectively. These observations
were collected during concurrent validation. They are not an SLA, throughput
guarantee, or end-to-end input-latency measurement. Large views can still require
seconds to render.

The last 20 sampled events retained the same parse and verification counters:
unchanged refreshes did not repeat that expensive work.

Those timings and the three-view stress run belong to the earlier polling-era
qualification; they are not measurements of the new SSE path or a product
requirement to support multiple browser windows.

The #0220 follow-up used a **23,680,236-byte** synthetic board with **4,120 total
cards**, rendering **2,747 non-archived cards** in one real browser tab. It verified:

- A Human edit saved, and a Bot CLI edit appeared through the stream.
- A graceful board-service restart recovered the view and retained its draft.
- Switching away and back also retained the draft; no browser console errors
  were observed.
- An idle observation spanning multiple server-health cycles recorded **zero
  additional browser board, status or catalog GETs**. This does not mean zero
  central disk-health reads while the board was subscribed.
- Automated tests proved that the last disconnect pauses sampling, Bot wakes with
  no subscribers perform no metadata I/O, and a returning view refreshes promptly.
- Focused tests covered cache/subscription races, slow consumers, bounded shutdown,
  lost responses, pending preference retries, legacy upgrade and reconnect recovery.

Browser diagnostics used the shared Valkey/Webdis queue. Request counters were
evidence of work, not proof of HTTP success: the fixture's diagnostic status field
was a marker. DOM assertions, saved-state recovery and product tests established
correctness. The owned mailbox was exported and deleted. No real board content or
credentials were included in those messages.

## V2 implications

**RECOMMENDED: carry these behavioral scenarios into V2 qualification now, then
agree explicit operating budgets before calling scale readiness complete.**

Several mechanisms below already exist in V2. The matrix asks for their workload
and failure coverage to be extended; it does not claim each mechanism is absent.

### Gateway and catalog

Make selected-board work independent of unrelated board payload size. A sidebar
should request small committed projections, not materialize every card/history
object. Qualify the actual one-tab workload first, including navigation across many
registered boards. Extra-client stress can be headroom testing without expanding
the product's user model.

The V2 documents checked for Draft 1 described bounded catalog scheduling,
current-authority SQL snapshots, late-join coalescing and five-second visible
polling. Those contracts still govern V2 until an explicitly approved change.
Use the SSE evidence to review whether producer-driven notices better fit its
local workload; this report does not authorize replacing its transport or caches.

Measure jobs queued/running, rows examined/returned, bytes returned and queue age.
A cache must not let a late join inherit stale permissions or a prior board identity.

### Board APIs and browser

Use narrow reads and bounded history/comment pages. Test whether a large drawer
or attachment request causes unrelated full-board reads or copies. Count database
work as well as wall time; SQLite can still be asked to do unnecessary work.

Keep atomic multi-card changes and stale-card rejection. Exercise the Human and
Bot writers with delayed responses, lost responses, conflicts and navigation.
Preserve typed input and distinguish local pending state from confirmed persistence.
Confirm that stale readbacks cannot lower a known committed revision.

For a push design, test the bootstrap/subscription boundary, reconnect with the same
revision but different content, stale navigation callbacks, and snapshot failure
followed by healthy metadata. A pending preference retry must not depend on an
unrelated catalog change after a polling timer is removed. Heartbeats cannot stand
in for data acceptance or storage health. Unsubscribed boards must stop periodic
work rather than accumulate background readers as the user navigates.

V2's existing receipt and uncertainty contracts control retry behavior. V1's
conditional browser recovery is evidence for test scenarios, not blanket permission
to add replay to V2. In particular, additive comments, uploads and approvals need
positive deduplication/proof before uncertain submissions can be repeated.

Attachments need their own dimension: metadata/history cardinality, visible preview
count, decode/download work and maximum individual blob size. Storing blobs outside
the database/JSON does not make their surrounding request path free.

### Activity and monitors/notices

Use committed application activity and the established V2 notice/receipt contracts
as the notification boundary. Filesystem notifications were useful hints for V1's
JSON replacement; a database-file notification should not substitute for a proved
application commit or event cut.

Start with the actual delivery requirement. If the requirement is the same
single-owner, best-effort nudge, review whether durable cursors, catch-up and
delivery recovery are required or merely inherited from an earlier solution.
Do not remove V2's existing guarantees without an explicit contract decision.
Keep chat attribution separate from browser propagation: Bot-only edits should
not nudge their own agent, but they must still update Terry's browser.

Keep intake independent of slow processing and delivery. Bound concurrency, queued
work and per-board coalescing. Test simultaneous bursts, slow sinks, missed signals,
restart, retention gaps and writes that arrive during drain. One failed board must
not strand another board's pending work.

Test fairness and control latency separately from the concurrency cap. Serial work
can let slow sinks delay later boards; a memory bound is not a delivery deadline.

Do not port V1 time or target semantics mechanically. The V2 activity CLI checked
for Draft 1 documents an exclusive lower bound and inclusive upper bound; V1's report windows
are inclusive. V2 notices also retain original targets and delivery cuts for pending
work, and local inbox acceptance is separate from external delivery. Test those
existing contracts explicitly.

### Diagnostics and process lifetime

Separate service reachability, accepted storage state, last actual observation,
browser render completion and delivery status. A responsive socket does not prove
storage freshness, and a lost response does not prove a failed write.

Slow or blocked diagnostic output must not hold a transaction, response, startup
gate or graceful drain. Include captured/full-output destinations in native lifecycle
tests. Establish safe output in the background launcher before interpreter/runtime
startup; an application safeguard cannot run before application entry.

## Suggested early qualification matrix

| Dimension | Cases to retain or add |
| --- | --- |
| Data shape | Many small cards; one comment/history-heavy card; high total count including completed/archived cards; combined case; relationship/hierarchy-heavy variants. |
| Workspace | Many registered boards; one active view while other boards grow; navigation; one slow or unavailable member. Keep optional concurrency headroom distinct from the product use case. |
| Idle | No changes for an extended interval; count browser requests, SQL work, allocations and metadata checks. Verify inactive boards stop periodic work and heartbeats do no storage I/O. |
| Writes | Human/Bot overlap; same-card conflict; unrelated-card update; multi-card operation; save succeeds but response is lost. |
| Browser | Large initial view, history pages, search, drawer edits, drafts, board switching, stream disconnect/reconnect, failed snapshot recovery and retained pending state. Measure download and rendering separately. |
| Attachments | Many references/history records, lazy previews, 10 MiB file, integrity refusal and download during a concurrent card change. |
| Notices | Human-only chat nudges versus all-actor browser updates; explicit target changes; slow sink off the writer; loss/restart and drain. Test cursor/retention/replay guarantees only where the approved contract requires them. |
| Host | Safe detached launch, output backpressure, graceful stop/reload, storage errors before/after commit, and concurrent background backup work. |

Use work-count assertions alongside p50/p95/p99 and maximum latency observations.
Track CPU, memory, rows, payload bytes, copying, queue age and browser render work.
Keep authoritative product assertions separate from diagnostic transport. Synthetic
fixture construction must not dominate the measurement or quietly change its premise.

## Evidence and boundaries

- `66c3101`: #0210–#0214; final gate **2,172 passed, one platform-specific skip**.
- `06f543c`: #0216–#0218; final gate **2,271 passed, one platform-specific skip**.
- `cd973e5`: #0219; final gate **2,374 passed, one platform-specific skip**.
- `d4db7be`: #0220; final gate **2,429 passed, one platform-specific skip**.
- Source/wheel builds, independent reviews and clean shared activation were recorded
  for these releases. These are separate runs/scopes, not additive unique-test totals.
- Native filesystem watching was qualified on Windows; other OS watcher backends
  were not exercised here. The owner-notification live test covered one idle Codex
  chat in this Windows installation. No power-loss durability claim is made.
- Detailed machine-local evidence remains under `artifacts/v1-performance-qualification`
  and `artifacts/v1-monitor-qualification`, plus the newer
  `artifacts/v1-owner-notifications-qualification` and `artifacts/v1-sse-qualification`;
  raw production board content and credentials
  are not included in this document.
- V2 context checked: its implementation README, workspace catalog decision, M11
  activity CLI runbook, and M13/M14 notice recovery/control runbooks. Those documents
  establish context, not a claim of new V2 scale qualification.
- This report supplies observations and behavioral acceptance ideas. Do not copy V1
  source/tests/internal designs into V2. Prior V1 exposure is disclosed; no independent
  clean-room certification is claimed.

For V1 operation and architecture context, see the [README](../../README.md) and
[orientation](../ORIENTATION.md).

## Discussion still to add

- Which workspace, client, active-card and retained-history envelopes should V2 commit
  to, and which latency/resource budgets should qualify them?
- Which additional data shapes or failure sequences does Terry want represented?
- Which notice-delivery and retention guarantees should be tested beyond the current
  contracts, including the boundary between local acceptance and external delivery?
- Which V2 transport/notice contracts should change, if any, after reviewing the
  clarified local single-user use case? No such V2 change is approved by this draft.
- Which operational lessons belong in product behavior versus test/diagnostic tooling?

This is Draft 2. Keep it unsent until Terry finishes the discussion and authorizes
delivery to V2.
