# ADR 0003: Bound the client board cache by bytes and human recency

- Date: 2026-11-08
- Status: Accepted; implemented by #0247 (Ready for Review, pending Terry review)
- Decision owner: Terry Ott
- Tracking: V1 #0246, `client-board-cache-policy-adr`

## Context

Terry frequently switches among boards and wants useful board data ready when he
returns. The current REFRESHING behavior remains acceptable; this is a further
usability improvement, not an emergency repair.

The [#0243 investigation](../reports/2026-11-08-hot-board-live-cache-investigation.md)
exercised two and three large boards. Retained, current snapshots eliminated
return-time snapshot GETs in quiet runs. It also exposed an eviction/reconnection
cycle that amplified reads when the retained snapshots exceeded the byte budget.

Three was the tested working set, not a demonstrated architectural maximum. A
board-count limit is a poor substitute for resource policy: payload sizes vary,
and archiving cards preserves their content and history. Replacements must be
accounted for as boards grow or shrink.

Terry approved the policies below after the investigation. They supersede its
provisional three-board limit, 256 MiB budget recommendation and departure TTL.
The investigation's measurements and limitations remain historical evidence.

## Decision

### Capacity follows bytes; priority follows human selection

Use a byte-limited least-recently-used cache for retained board snapshots. There
is no fixed three-board product limit and no minimum retained board count.

- Recency means elapsed time since the human last selected the board for display.
  Sidebar clicks, deliberate Back/Forward navigation and directly opening a board
  all count. The initial displayed board receives a selection record.
- Background updates, snapshot reads, card edits and automatic repaints do not
  refresh recency. A noisy board receives no extra retention priority.
- Pinning a board in the sidebar does not pin its snapshot in memory.
- Record selection recency and evaluate it when choosing eviction victims; a
  running countdown timer for each board is not required.
- Do not populate the cache with never-visited boards merely because they appear
  in the catalog or are pinned.

### Account on every admission and replacement

The configurable budget limits the cache's accounted snapshot bytes. The
implementation must document a consistent, measurable accounting method. A
serialized-byte charge is not a claim about exact JavaScript heap consumption.

For a replacement, calculate projected usage as:

```text
current accounted bytes - replaced entry's charge + candidate's charge
```

- Re-account every accepted replacement; do not assume the original size stays
  valid or that every update increases size.
- Before retaining a candidate, evict the least recently selected eligible
  entries until the candidate fits. More than one eviction may be necessary.
- The board receiving a background update is itself an eligible victim. The
  update must not evict more recently selected boards merely to retain itself.
  If that board is selected for eviction, remove its prior cached snapshot and
  reject the replacement; do not continue evicting newer entries to re-admit it.
- An inactive candidate larger than the entire budget is not retainable. Do not
  evict otherwise useful entries trying to make that impossible admission fit.
  The active-board exception follows below.
- Eviction or rejected admission must not cause a refill/reconnection cascade.
  Removing one entry must not gratuitously download the surviving entries again.

### Protect working state and degrade to the active board

The displayed board's required model, editor drafts and pending or unconfirmed
operations are not disposable cache entries. Cache eviction must not discard,
misattribute or acknowledge them.

- Under pressure, retaining only the active board is a valid steady state.
- If the active model alone exceeds the cache target, it remains usable and all
  other retained snapshots are evicted. Avoid requiring an extra cached copy
  solely to satisfy a cache abstraction.
- A cache target therefore cannot be represented as a hard upper bound on all
  browser memory. Required working state and temporary processing also consume
  memory.
- Preserve existing revision checks, minimum revision floors, board identity,
  draft recovery and uncertain-write protections. A background refresh is not
  authority to confirm or replay an unrelated mutation.

### Retain by pressure and tab lifetime, not arbitrary age

There is no automatic age-based expiry while the tab remains open. An unused
board can remain cached for an hour if capacity permits; it stays the oldest
candidate for eviction until the human selects it again.

The snapshot cache is local to the browser tab and ephemeral across tab closure
or reload. It is not a persistent offline cache or a cross-tab pool. Clearing
snapshots must preserve the application's separate saved draft and recovery
records. Identity changes and invalidation still require appropriate handling;
an invalidated snapshot must never be represented as current.

### Keep retained boards current without competing with the foreground

Every retained board is eligible for background updates. There is no separate
three-board freshness limit hidden behind a larger cache.

- The displayed board takes priority over background refresh work.
- Bound background concurrency and coalesce superseded updates to the latest
  useful state. Extra cache capacity must not create an unbounded download burst.
- Brief background lag is acceptable. This decision does not create a
  sub-second synchronization requirement or an exact freshness SLA.
- A confirmed current snapshot may be displayed on return without an unnecessary
  snapshot GET. When currentness cannot be established, show the available view
  immediately with clear REFRESHING feedback while authoritative recovery runs.
- Refuse new mutations until the selected board satisfies the existing freshness
  and revision requirements. Preserve editable drafts and already-admitted
  operations through that recovery.
- Failed recovery must remain visible and retryable, not produce an illusion of
  a current, writable board.

### Configure and qualify the budget against the owner's machines

Terry accepts approximately **2 GiB of localswim browser-client memory** as a
reasonable allowance for this capability on his machines. This is a resource
qualification target, not a 2 GiB serialized-cache default and not a promise of
an exact process-wide cap.

- Expose a configurable cache-byte budget. Select its initial default through
  qualification, accounting for retained data, parsed active models, temporary
  copies, rendering and garbage-collection behavior.
- Report memory with an honest measurement boundary. A sampled heap estimate or
  serialized-byte sum does not establish exact total or peak browser memory.
- Keep the active-model exception explicit if required working state alone
  exceeds the target; do not silently discard working state to claim compliance.
- The primary layouts are Terry's large docked window on a 40-inch 21:9 5K2K
  ultrawide and fullscreen on an XPS 15 9530 with a 1920 x 1200 FHD+ panel.
  Browser viewport dimensions and operating-system scaling remain distinct.

## Engineering choices and qualification still required

The owner policies are settled. The implemented candidate charges serialized UTF-8
JSON bytes, uses human-selection LRU, and exposes a per-window MiB budget input.
Its default is 256 MiB, selected for the [qualified cases](../reports/2026-11-08-byte-lru-cache-qualification.md). Combined SSE metadata tracks retained boards;
one background snapshot read runs at a time and yields to foreground work. These
measurements include foreground stalls, sampled heap and unnecessary reads; the
serialized charge is not a process memory cap.

Qualification must cover growing and shrinking replacements, multiple evictions,
self-eviction of an old background board, oversized candidates, and active-only
operation. It must also cover rapid navigation, reconnects, stale completions,
drafts and unconfirmed operations under pressure. Retaining boards must not
recreate the investigation's read-amplification cycle.

Exercise representative large boards and larger retained sets, including the
owner's ten-board target when they fit the chosen budget. Ten boards is an
evaluation target, not a capacity guarantee established by the three-board spike.
Measure logical reads separately from physical disk I/O, and foreground response
separately from aggregate throughput.

## Consequences

- Useful capacity can increase for small boards and decrease for larger boards
  without a product rule declaring that exactly three boards matter.
- Human navigation determines retention priority; background activity cannot buy
  priority or indefinitely refresh an age deadline.
- Greater retention has a continuing refresh cost. The byte budget and bounded
  foreground-prioritized work control different resources and are both needed.
- The active-only fallback and protected recovery state make this a soft overall
  memory target, with a strict admission budget for optional retained snapshots.
- The implementation and its qualification are tracked by #0247; Completed
  investigation #0243 stays closed.

## Alternatives not adopted

- A permanent three-board ceiling: the investigation did not establish a failure
  threshold at four, and board count does not describe payload size.
- Age-based expiry or recency renewed by background updates: neither reflects
  the agreed human-interest policy.
- Evicting working drafts or the required active model to enforce a hard process
  memory number: this would sacrifice correctness or usability for accounting.
- Increasing the byte budget while retaining the prototype's eviction/refill
  cycle: a larger budget only moves that failure to a larger workload.
