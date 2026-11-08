# Hot-board navigation investigation

Ticket: #0243. Investigation only; production behavior is unchanged.

## Recommendation

**RECOMMENDED: pursue a bounded three-board production implementation, with a
256 MiB serialized-snapshot budget and an explicit budget-pressure fallback.**
The spike demonstrates a meaningful return-navigation benefit on both fixture
shapes. It also exposes a read-amplification defect under memory pressure, so
shipping the prototype or simply increasing its budget is not sufficient.

This is a moderate caching/recovery change with a small gateway subscription
extension, rather than a storage rewrite. Include selected-read deduplication,
foreground priority and a bounded background-refresh scheduler. Start with one
background slot and qualify its tradeoff against freshness. Keep the current
REFRESHING path whenever a retained board cannot honestly be promoted.

Keeping today's behavior is a reasonable lower-effort choice: Terry explicitly
accepts it. Ten-board support should be a separate experiment after the
three-board policy is sound; it is not qualified by this report.

## Decision scope

Terry wants to reduce the REFRESHING delay when switching among frequently used
boards. Existing behavior is acceptable. Two current boards would be useful,
three is the primary target, and ten is a stretch goal. This is a one-user,
one-browser-tab workload on a machine with at least 64 GB of RAM.

The investigation compares the deployed behavior, a smaller change that avoids
duplicate selected-board reads, and an isolated three-board live-cache spike.
It does not authorize production rollout.

## Experimental designs

### Selected-board read deduplication

- Retains the existing selected-board subscription and authoritative snapshot GET.
- Lets initial stream metadata seed the selected marker before deciding another
  read is necessary, with a one-shot 250 ms missing-metadata fallback.
- Does not keep background boards current or promise instant returns.

### Three-board live cache

- Multiplexes metadata for the selected board and two recent boards over one
  browser EventSource; no per-board browser SSE connection is required.
- Fetches a complete authoritative snapshot when retained metadata changes.
- Coalesces refreshes per board; only a connected, clean snapshot that meets the
  revision floor may be promoted without a GET.
- Disconnects, pending actions, dirty markers, missing snapshots and revision gaps
  retain the existing REFRESHING recovery path. Background reads cannot confirm
  unknown mutations or discard drafts.
- Expires retained watches 20 minutes after departure. Updates do not renew that
  deadline. Snapshot eviction cannot unsubscribe the selected board.
- Keeps the normal 96 MiB aggregate serialized-snapshot budget, with a private
  opt-in 256 MiB comparison. These are serialized bytes, not total browser heap.

## Fixtures and measurement boundaries

- Main fixture: 22,140,170 bytes on disk per board, 401 cards, 4,800 comments and
  401 lane-history entries; an initial decorated response is 23,633,539 bytes.
  Three responses total approximately 67.6 MiB and fit the 96 MiB cache budget.
- Detail fixture: 20,988,274 bytes on disk and a 41,947,679-byte decorated response.
  Three responses total approximately 120 MiB: they exceed 96 MiB but fit 256 MiB.
  This demonstrates why disk size alone cannot determine cache admission.
- Synthetic data only; no real board mutations or production configuration changes.
- Python 3.15.2; installed Chrome 157.0.8103.39; i7-13700H, 20 logical CPUs and
  68,374,552,576 bytes of physical RAM. Installed browser version is not an
  independent assertion about every already-running Chrome process.
- Browser readiness is a probe proxy: matching selected board/title/lane,
  mutation admission enabled, then two animation frames. It is not proof of
  completed painting or exhaustive editor correctness.
- Completed snapshot payload totals are captured after pending GETs drain.
  Individual readiness samples can precede completion of a trailing GET.
- Logical application read bytes are not physical disk I/O. Periodic counters
  have up to one second of endpoint uncertainty. Exact write totals come from
  successful responses, not delayed counters. Parse seams overlap and are not
  summed into a unique parse count.
- CPU includes measurement overhead; process working sets are endpoint readings,
  and Chrome's sampled JS heap is an estimate. Neither establishes peak total RAM.
- Sequential timings on this active workstation vary with host load; no causal
  two-versus-three board latency claim follows from those separate runs.
  Repository verification overlapped later browser runs. Request counts are the
  stronger comparative evidence; these timings are observations, not speedup SLAs.

## Backend watch cost

Final source-bound 45-second idle observations, rounded:

| Available boards | Watched boards | Health reads, MiB/s | Catalog reads, MiB/s | CPU seconds |
| --- | --- | --- | --- | --- |
| 2 | 1 | 4.21 | 3.75 | 1.02 |
| 2 | 2 | 8.43 | 3.75 | 1.03 |
| 3 | 1 | 3.74 | 5.61 | 1.30 |
| 3 | 3 | 12.65 | 5.62 | 1.72 |

Idle phases performed no snapshot GETs or board parsing/verification. All writers
were already started in both variants. These runs establish the retained-watch
cost floor; they do not include browser background refills or rendering.

The retained-watch churn phases confirmed 16 writes across two boards and 21
across three. Median status-GET-plus-comment-POST times were 0.378 and 0.390
seconds; maxima were 0.406 and 0.736 seconds. These are bounded observations,
not throughput guarantees or browser foreground-contention qualification.

## Browser comparison

Each main comparison includes twenty quiet warm returns after visiting every
board. GET counts below include requests that finish after the readiness marker.

### Existing behavior

- Two boards: median readiness proxy **4.29 seconds**, range 3.35–5.11 seconds.
- Three boards: median **1.54 seconds**, range 1.39–2.02 seconds.
- Both cases: **40 GETs for twenty warm returns**, two per return. That is about
  902 MiB of completed snapshot payload in each warm phase.

### Selected-read deduplication

- Two boards: median **2.26 seconds**, range 2.14–2.46 seconds.
- Three boards: median **2.42 seconds**, range 2.23–2.66 seconds.
- Both cases: **20 GETs for twenty warm returns**, one per return. This halves
  snapshot traffic in those phases, but these sequential timing runs do not
  establish a latency improvement over every baseline run.

### Live cache with the main fixture

- Two boards: median **0.54 seconds**, range 0.47–0.69 seconds.
- Three boards: median **0.18 seconds**, range 0.17–0.26 seconds.
- Both cases: **zero snapshot GETs for twenty warm returns**. Sampled peak JS heap
  estimates were approximately 511 and 687 MiB, including garbage and temporary
  objects; they are not the size of the retained cache alone.
- Warmup cost increased: five GETs for the two-board set and nine for three.
  The three-board selected-read comparator needed three warmup GETs. Membership
  changes conservatively reconnect and refill retained boards. Frequent quiet
  returns amortize that cost; infrequent returns or frequent background edits
  can make retention more expensive overall.

### Changes while three boards are retained

- A 35-second run confirmed **33 writes**, eleven per board, while the browser
  consumed pushed metadata and refreshed snapshots.
- The browser completed **33 additional snapshot GETs**, approximately 744 MiB
  of payload. Keeping boards current moves work into the background; it does
  not eliminate the cost of changed snapshots.
- Nine switches during the run and three final checks had no GET starts inside
  their readiness windows. The final visible views all matched revision 11.
- Those twelve readiness proxies ranged from 0.20 to 0.29 seconds. Writer
  status-GET-plus-POST median was 0.457 seconds, maximum 0.599 seconds. This is
  a bounded concurrent-use observation, not an isolated foreground UI write SLA.
- Up to three snapshot GETs were in flight together. The prototype coalesces
  per board but lacks a global background scheduler.

### Aggregate budget pressure

Three detail responses total about 120 MiB, despite each board being about
20 MiB on disk.

- **96 MiB: efficiency failure.** After eleven warmup requests, six cyclic
  returns started **36 more requests**, including trailing work. Across the
  whole run, 40 completed and seven failed or were aborted. Views recovered,
  but readiness alone hid expensive tails. Median readiness was 1.97 seconds.
- Cache eviction removes a watch; changing membership reconnects the combined
  stream; reconnection invalidates certifications and triggers more full GETs.
  Those fills can cause another eviction. Navigation re-admits the missing
  board and restarts this cascade.
- **256 MiB: twenty warm returns, zero return-phase GETs.** Median readiness
  was **0.28 seconds**, range 0.21–0.49 seconds. Warmup used nine GETs. The sampled
  JS heap estimate reached about 1.1 GiB, not merely the 120 MiB serialized size.
- A larger budget makes this fixture fit. It does not fix the policy for a
  future workload that exceeds that larger budget.

## Production acceptance gates

The spike is deliberately not release-ready. A production implementation should:

- Admit only affordable retention, or degrade to selected-only operation without
  an eviction/reconnect/refill cascade. Count failed, aborted and trailing
  requests. Under budget pressure, quiet cyclic navigation must perform no more
  snapshot GETs than selected-only navigation after declared warmup.
- Give selected work priority, cap background concurrency globally, and coalesce
  bursts to the latest marker rather than queueing every intermediate snapshot.
- Preserve per-board status, revision/content markers, minimum revision floors,
  pending drafts and uncertain writes. Background reads must never acknowledge
  an unrelated mutation. Disconnects and uncertified returns use REFRESHING.
- Keep the selected subscription independent of cache eviction. Enforce the
  departure-based TTL and prevent late completions from resurrecting expired
  entries. Qualify reconnect, eviction and TTL together in real browser tests.
- Retest both fixture shapes, selected-write responsiveness and idle/churn cost
  under the final scheduler. The spike's focused recovery tests and accelerated
  timer tests are useful evidence, but not a 20-minute real-browser soak or a
  production release qualification.

## Ten-board feasibility

Ten is not a demonstrated SSE or Windows ceiling. The spike deliberately caps
the controller at three boards, while its client/server watch sets and existing
raw-cache entry count cap at eight. Supporting ten requires coordinated changes
and separate qualification.

Arithmetic only, not measured ten-board results:

- Ten main responses total about 225.4 MiB, fitting 256 MiB with limited headroom.
  Ten detail responses total about 400 MiB and exceed that budget.
- Actual JS heap also includes object graphs, selected projections, parsing and
  temporary serialization. Abundant system RAM does not remove those costs.
- Ten 20 MiB boards sampled every five seconds imply about 40 MiB/s of logical
  health reads; ten-second catalog hashing adds about 20 MiB/s before changes.
- Merely raising the current cap could start nine background GETs together after
  reconnect. A real ten-board trial needs bounded scheduling, mixed response
  shapes, foreground latency measurements and expiry/recovery race checks.

## Evidence and verification

- [Sanitized numeric summary](evidence/0243/summary.json) contains reduced warm
  samples, drained phase totals, final backend phases, churn observations and
  exact helper/probe hashes. Raw source-bound records are retained privately
  with the ticket's reproducibility packet, `0243-hot-board-investigation-packet.zip`
  (114,011 bytes; SHA-256
  `d6b8f728571bf9dbbbb6f5d245cb6bf496f2e64b74a2a510b9a91e69a1186da1`).
  The packet's dynamic instrumentation is experimental tooling and has not
  passed the production strict-type/lint gate.
- Baseline source: `54f4b23183ebd0932eb12980dbad1f81b9d427b3`. Source provenance
  distinguishes raw package files from injected served JavaScript. Early
  backend runs are exploratory and superseded by final source-bound reruns.
- Independent spike reviews corrected selected-watch eviction, full per-board
  status restoration and the first-deadline missing-metadata fallback. Fresh
  re-reviews passed 29 hot-controller/workspace-event tests and 69 selected-read
  recovery/cache tests. Author hot-spike verification passed 75 focused tests.
  These are overlapping suites, not a sum of unique tests or a full release gate.
- Real Chrome actions used the shared diagnostic queue; the final larger-budget
  result was decoded and checked after transport. Queue outages interrupted the
  sequence and are excluded from navigation samples. Queue maintenance was
  separately authorized and is not a localswim cache deployment.
- The measurement method received independent review; final report review is
  recorded on #0243. All 43 recorded synthetic board/gateway ports were checked
  and had no listener after shutdown.

For reproduction, use the packet's helper through a frozen/offline uv environment
bound to the baseline or supplied experimental patch. Generate new private
runtime roots. Run two and three `comment` boards, then three `detail` boards.
Use both the gateway environment gate `LOCALSWIM_HOT_BOARDS_SPIKE=1` and browser
`?hotBoardsSpike=1` for the live spike; the larger budget additionally requires
`&hotBoardsBudgetMiB=256`. The smaller comparator uses `?selectedReadSpike=1`.
Keep warmup, quiet returns, concurrent edits and drained tails separate. Do not
apply the experimental patches to the shared live checkout or reuse real boards.
