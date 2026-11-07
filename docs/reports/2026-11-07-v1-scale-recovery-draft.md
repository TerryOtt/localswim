# V1 scale recovery: lessons and test cases for V2

**Draft 1 — 2026-11-07. Working discussion copy; not sent to V2.**

Tracking card: **#0215**. This draft records the accepted work in #0210–#0214 and
#0216–#0218.
It will be amended after Terry's further discussion. It does not authorize V2
implementation changes or alter existing V2 acceptance gates.

## What happened

Terry reported that localswim was slow the previous day and started failing
today. The gateway was visibly less responsive, the file-read counter sometimes
exceeded seven seconds, and some mutation responses timed out even though the
change later appeared in the board. Monitor failures added uncertainty.

V1 is usable again, and the repairs passed substantially larger synthetic
workloads. Terry subsequently reported that the gateway was vastly improved and
accepted #0210–#0214 and #0216–#0218. That is useful operational confirmation
alongside the tests.

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

## What V1 changed

| Area | Result |
| --- | --- |
| Gateway | Resolve the selected board without a workspace-wide JSON scan. Reuse unchanged recognition and sidebar summaries. Recheck path, file and writer identity; cached facts do not become authority. |
| Board service | Continue actual file reads, but reuse admission only for identical bytes and accepted policy. Avoid repeated parse/audit work and unnecessary full-board copies. |
| Freshness | Report the server's successful read time separately from browser completion. Use an admitted content marker so timestamp-preserving and policy-only changes are visible. |
| Browser | Poll the selected board every five seconds and the sidebar every ten. Preserve bounded, non-overlapping requests and immediate initial/user-action refresh where appropriate. |
| Writes | Project pending edits locally. Enforce original card revisions inside the atomic board transaction; genuine same-card conflicts reject the second writer. |
| Recovery | Retain stable Add identities, drafts and acknowledged revision floors. Read back uncertain outcomes before a bounded conditional retry. CLI/proxy writes and uncertain additive/compound actions are not blindly replayed. |
| Attachments | Read only immutable attachment metadata under the board lock; perform blob reading and integrity checks outside it. Avoid copying the whole board for one image/download. |
| Diagnostics | Keep unsafe captured output away from response/lifecycle paths. Preserve visible startup refusals and use safe output before interpreter startup in background launchers. |
| Monitor | Share verified native directory subscriptions, coalesce work per board and bound serial queries/delivery. Use cheap 30-second reconciliation rather than one-second housekeeping. |
| Attribution | Obtain bounded sanitized evidence from the writer without reparsing board content. Unknown coverage alerts rather than being mistaken for bot-only activity. |

The transaction boundary remains the **board**. A move, relationship change or
priority reconciliation can affect several cards; those effects must commit
together. Card revisions reject stale intent. Separate lane locks were not needed
to obtain safe behavior. Independent boards remain independent.

The monitor feed is an in-memory evidence cache, not a durable journal. A writer
commit performs one exact-byte verification; ordinary feed queries use strong file
metadata. Restart, retention, external-change and other coverage gaps remain
explicit. Native events are hints, not one-for-one transaction records. Production
monitors were left off; no production activation was part of these repairs.
At-least-once delivery has a duplicate window between queue acknowledgement and
status persistence. Unsupported transient manual JSON writes overwritten outside
the sole-writer protocol cannot be reconstructed from the feed.

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

## V2 implications

**RECOMMENDED: carry these behavioral scenarios into V2 qualification now, then
agree explicit operating budgets before calling scale readiness complete.**

Several mechanisms below already exist in V2. The matrix asks for their workload
and failure coverage to be extended; it does not claim each mechanism is absent.

### Gateway and catalog

Make selected-board work independent of unrelated board payload size. A sidebar
should request small committed projections, not materialize every card/history
object. Test workspace size and client count independently, then together.

V2 already documents bounded catalog scheduling, current-authority SQL snapshots,
late-join coalescing rules and five-second visible polling. Preserve those rules.
The question is whether they hold under this larger matrix, slow members, permission
changes and repeated overlapping clients—not whether to transplant V1's caches.

Measure jobs queued/running, rows examined/returned, bytes returned and queue age.
A cache must not let a late join inherit stale permissions or a prior board identity.

### Board APIs and browser

Use narrow reads and bounded history/comment pages. Test whether a large drawer
or attachment request causes unrelated full-board reads or copies. Count database
work as well as wall time; SQLite can still be asked to do unnecessary work.

Keep atomic multi-card changes and stale-card rejection. Exercise two writers and
multiple windows with delayed responses, lost responses, conflicts and navigation.
Preserve typed input and distinguish local pending state from confirmed persistence.
Confirm that stale readbacks cannot lower a known committed revision.

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

Keep intake independent of slow processing and delivery. Bound concurrency, queued
work and per-board coalescing. Test simultaneous bursts, slow sinks, missed signals,
restart, retention gaps and writes that arrive during drain. One failed board must
not strand another board's pending work.

Test fairness and control latency separately from the concurrency cap. Serial work
can let slow sinks delay later boards; a memory bound is not a delivery deadline.

Do not port V1 time or target semantics mechanically. V2's activity CLI currently
documents an exclusive lower bound and inclusive upper bound; V1's report windows
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
| Workspace | Many registered boards; selected-board work while other boards grow; one slow or unavailable member; overlapping clients and late joins. |
| Idle | No changes for an extended interval; count polls, SQL work, allocations, metadata checks and status writes. Verify work stays bounded. |
| Writes | Human/Bot overlap; same-card conflict; unrelated-card update; multi-card operation; save succeeds but response is lost. |
| Browser | Large initial view, history pages, search, drawer edits, drafts, board switching, reload and retained pending state. Measure download and rendering separately. |
| Attachments | Many references/history records, lazy previews, 10 MiB file, integrity refusal and download during a concurrent card change. |
| Notices | Bot/human/mixed attribution; complete/unknown/gapped coverage; retention; target changes; slow sink; restart and drain with a write arriving at the boundary. |
| Host | Safe detached launch, output backpressure, graceful stop/reload, storage errors before/after commit, and concurrent background backup work. |

Use work-count assertions alongside p50/p95/p99 and maximum latency observations.
Track CPU, memory, rows, payload bytes, copying, queue age and browser render work.
Keep authoritative product assertions separate from diagnostic transport. Synthetic
fixture construction must not dominate the measurement or quietly change its premise.

## Evidence and boundaries

- `66c3101`: #0210–#0214; final gate **2,172 passed, one platform-specific skip**.
- `06f543c`: #0216–#0218; final gate **2,271 passed, one platform-specific skip**.
- Source/wheel builds, independent reviews and clean shared activation were recorded
  for both releases. These are separate runs/scopes, not additive unique-test totals.
- Windows native notification execution was qualified. Other native OS backends were
  not exercised here. No native power-loss durability claim is made.
- Detailed machine-local evidence remains under `artifacts/v1-performance-qualification`
  and `artifacts/v1-monitor-qualification`; raw production board content and credentials
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
- Which operational lessons belong in product behavior versus test/diagnostic tooling?

This is Draft 1. Keep it unsent until Terry finishes the discussion and authorizes
delivery to V2.
