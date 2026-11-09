# Byte-limited board cache qualification

The November 8, 2026 qualification for #0247 passed independent review and the full project gate. [Accepted cache policy](../decisions/0003-byte-limited-client-board-cache.md)

The 256 MiB serialized-snapshot default is a conservative machine-qualified candidate against the owner's approximately 2 GiB browser-client intent, not a guarantee or a process memory cap. The measurements below cover the tested fixtures and session only. They do not establish a throughput bound or a universal board capacity.

## Method and source binding

Chrome 157.0.8103.39 ran in one window at 1906 × 1296 CSS pixels. The comment-heavy synthetic boards contained approximately 22.5 MiB of serialized cache data each. The separate detail-heavy run used two one-card boards with 20 MiB detail content each, decorated to approximately 40 MiB per board by history. Recorded evidence is in the private `artifacts/v1-byte-cache-qualification` directory: `final-2.json`, `final-3.json`, `final-10.json`, `final-detail.json`, the two run `state.json` files, pressure and churn snapshots, `foreground-write.json`, and the measurement file. Queue evidence from eight phases was validated and exported to `queue-evidence.json`; the owned mailbox was deleted.

The instrumented service recorded SHA-256 for `api_endpoint.py` as `979c94742f833e646526514cb02fe9c7c67ec6bcfdcf9c1db2b50d2869007703`. Raw `workspace.js` was `a5e6153165caf785052f93058358b13f80853ba649c4d4be313297acee4b0ad1`; the helper-injected bytes read and served during measurement were `9717d5f2d87c852971979dfd998642aecaaef4ac13f5bcda80f003b18ecef053`. The helper patches the read before the evidence hash is captured. The instrumented hash is not the raw source hash.

Private instrumentation SHA-256:

- `board_harness.py`: `6333199945f304bfbbfac5e3295455d68394b2e60b5395c2e41ffa2c336d5a46`
- `browser_probe.js`: `f9ae597c5c21bb765e002d2bfe8ed54650f57cdf1d345d5652c1f250a9d6be74`
- `change_fixture.py`: `cc86f8c72597e6699431dd3570bd3af37724b3fde53b62bc804d7711e93b9ba5`

This measured window does not qualify both of the owner's docked and laptop display layouts.

The shared development machine also had other agent work and automated tests running.
These are observed interactive timings, not an isolated performance benchmark.

## Navigation and memory

The table records the resource and navigation runs before the final selected-status metadata correction described below. Times are milliseconds to presented readiness, median and full observed range. Cold uses the first occurrence of each key; warm uses the last 20 samples of each final capture. These sequential captures share a session and are not independent trials. Ranges include outliers; no samples were discarded.

| Fixture | Cold median (range) | Warm median (range) | Snapshot GETs, cumulative | Cache charge | Peak sampled JS heap |
| --- | --- | --- | --- | --- | --- |
| 2 comment boards | 2,097.1 (1,948.3–2,245.8) | 294.2 (263.2–1,569.2) | 2 | 47,131,556 bytes | 343,147,799 bytes |
| 3 comment boards | 1,948.3 (1,489.8–2,245.8) | 261.7 (195.7–3,417.5) | 3 | 70,697,334 bytes | 343,147,799 bytes |
| 10 comment boards | 2,079.7 (1,489.8–2,245.8) | 362.0 (327.6–979.2) | 10 | 235,657,780 bytes | 534,760,588 bytes |
| 2 detail boards | 1,743.2 (1,698.0–1,788.3) | 126.0 (101.1–1,368.0) | 2 | 83,895,036 bytes | 683,323,513 bytes |

Each cold board had one snapshot GET. The last 20 warm selections in each capture required no additional snapshot GETs; no GETs failed in these quiet runs. The maximum live SSE count was one, although watch changes created replacement connections. The earlier cold duplicate GET was fixed and regression coverage retained.

The cache charge is serialized UTF-8 JSON, not actual JavaScript heap. Heap values are sampled browser-reported JS heap, not exact peak, total browser memory, or process-wide memory. They can include temporary JavaScript copies and reflect garbage collection, but sampling may miss peaks; non-JavaScript allocations are excluded. Application read bytes are not physical disk I/O; instrumentation adds overhead.

## Pressure and updates

- At a 230 MiB budget, an approximately 8 MiB growth of an older inactive board caused its own eviction: ten retained boards became nine. At 50 MiB, two remained; at 1 MiB, the active board remained usable and the saved draft was preserved.
- During 30.23 seconds of churn, 18 writes targeted three inactive boards. A separate foreground UI comment was confirmed visible and its submitted draft cleared. The browser recorded 21 snapshot GET attempts, 17 completed and four cancelled. This is real refresh overhead, not zero-cost background synchronization.
- The candidate preserves foreground priority and working state, but these bounded observations do not prove behavior for every workload or establish a freshness SLA.

Automated tests separately cover growing and shrinking replacements, oversized inactive candidates, multiple evictions, reconnect session fences, stale completion rejection, same-revision content changes, and protected drafts and uncertain save outcomes. Those provide automated regression evidence; they are not additional real-browser measurements.

## Final-source smoke verification

The final correction applies selected-board status metadata on the cached fast path and clears the prior board's age on navigation. Cache admission, accounting and background scheduling are unchanged. The earlier resource measurements retain their original source binding.

The final smoke used `api_endpoint.py` SHA-256 `e47d72e4bedf9c6d0655aefa9fa5820929e2094f0414d06ca5aa28b2d47680ef`, raw `workspace.js` SHA-256 `3dd1693ecea1c59c0e0c77393d13c17342f0bb19192083e548e05c4a710673dc`, and helper-injected `workspace.js` SHA-256 `f147b4a7eed555056b88c4d62e88ac72a80bc284e7535a189faa4c26b5229a6b`. The injected hash was independently reconstructed from the raw source.

`final-smoke.json` and `smoke-detail-2/state.json` record two detail-heavy boards with 83,895,036 accounted bytes. Two cold selections produced two successful snapshot GETs. Six warm returns produced no additional GETs and all reached readiness. The first warm return took 2,675.8 ms; the other five took 159.9–235.4 ms. Peak sampled JS heap was 598,342,646 bytes. The board-age label remained visible and selecting a fresh board replaced the previous board's older age.

In the #0249 browser check, the label stayed visible at the same horizontal
position across 135 sampled board-switch frames. The age value and unit faded
with the configured 150 ms ease-out and 100 ms ease-in. The exported queue
record is `age-transition-queue.json`. Automated tests cover reduced motion and
the case where the new board's snapshot arrives before its timestamp.

## Final verification

Independent qualification review supports the 256 MiB default for these measured cases with the stated memory and timing boundaries. Focused cache, refresh, eventual-consistency and workspace-event tests passed independently, 98 tests in total. The final combined #0247 and #0249 gate passed line endings, lint, formatting, strict type checks and workflow checks. Pytest reported 2,603 passed and one skipped. The source distribution and wheel built successfully.
