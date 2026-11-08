# V1 UI/UX changes: end-of-V2 parity handoff

**Tracking: #0241. Evidence window: 2026-11-07 20:12:24 UTC through 2026-11-08 20:12:24 UTC (November 7–8, 3:12:24 p.m. New York).**

Terry wants these user-facing improvements available **by the end of V2**. This is planning input for the current M18 owner, **not additional M18 implementation scope**. Place each requirement in an already planned future milestone where it fits, or add a closing milestone for the remainder. Preserve the approved sequence, prerequisite gates and Terry's control of acceptance.

The target is a largely drop-in experience: familiar controls, understandable state, retained work and the same meaningful outcomes. V2 may use its own architecture. This report neither authorizes immediate implementation nor requires copying V1 source, JSON storage, transport, cache internals or weaker guarantees over existing V2 contracts.

## Scope and evidence boundary

The committed baseline is [d25dc54](https://github.com/TerryOtt/localswim/commit/d25dc54c04f7c3961b741055d36a1b8c0b5ceb1d), the shared-runtime source revision at the cutoff. The sections below distinguish its changes from pre-window behavior and the still-pending #0240 follow-up. This is a source-and-record audit, not a new browser qualification or claim that V2 already implements these outcomes.

Earlier improvements remain relevant context, but were not all delivered in these 24 hours. Optimistic local edits, per-card conflict checks, stable Add identities, ordinary drawer approval, saved navigation/drafts and gateway performance work predate the window. Their broader evidence is in the [#0215 recovery report](2026-11-07-v1-scale-recovery-draft.md). During this window that report was revised and finalized for planning intake; those documentation commits do not represent additional product features.

## 1. Live updates without routine browser polling

[Browser SSE, #0220](https://github.com/TerryOtt/localswim/commit/d4db7be0a7756de21a6366e47faf01636df86bc2) replaced the browser's recurring board/status and workspace-catalog polling with pushed change metadata. The selected board still obtains an authoritative snapshot; notifications announce when another read is needed. Both Human and Bot changes reach the browser.

The visible benefit is a board and Attention sidebar that keep up without repeatedly downloading unchanged data. Returning after a disconnect refreshes current state; an open but quiet stream is normal. This does not mean all server-side health/catalog sampling disappeared.

Acceptance behaviors to preserve:

- Bootstrap and reconnect must reconcile current board/catalog state, including changes across the subscribe boundary.
- Navigation must prevent an old board's callbacks or responses from repainting the new one. Preserve its search, selected card, scrolling and drafts.
- Burst updates may coalesce, but cannot leave the final committed state unseen. A failed snapshot read must recover without requiring an unrelated later edit.
- Idle browser traffic and work should be bounded; inactive boards should not accumulate unnecessary background readers.

The [reader-thread cleanup follow-up, #0225](https://github.com/TerryOtt/localswim/commit/4607d5d2d3598f0d7c96cf33d679536338561cd1) improves restart/stream teardown reliability. It adds no new control, but belongs to the dependable navigation/reconnect experience. Evidence includes `test_browser_eventual_consistency.py`, `test_board_events.py`, `test_workspace_events.py` and `test_workspace_relay_cleanup.py`.

## 2. Clearer connection and refresh status

[Status simplification, #0221](https://github.com/TerryOtt/localswim/commit/818e6d4b4d9f34ba58d10c3c648e448f24408267) removed **Last confirmed sync**. Its increasing age on an unchanged board misleadingly suggested stale data. The bar retains **Last card update**, the clock and independent error information. The [spacing correction](https://github.com/TerryOtt/localswim/commit/8445e628e0750289a70f116fd60273dab8039582) also removes the deleted field's empty grid column.

- The connection dot and **CONNECTED** badge stay green while the stream is open, even with no data events.
- Disconnect receives ten seconds of grace. Failed reconnect attempts cannot renew it indefinitely; a successful connection restores green immediately.
- Transport health is distinct from successful board loading. Real board-read errors remain visible through ordinary repaints.
- The clock and remaining status fields retain stable layout rather than shifting as values change.

Later cache work adds **REFRESHING**, pale green with dark text, and **REFRESH FAILED**, plus a consistently sized badge. These label selected-board readiness, not a save receipt. The committed dot behavior has a pending correction described in section 8.

## 3. Instant return to a recent board, with a truthful refresh boundary (#0229, #0239)

[Recent-board caching](https://github.com/TerryOtt/localswim/commit/cf280e4a4435881cafc47a52145d70d31a9afda1) lets navigation paint a previously confirmed board immediately instead of presenting only a loading placeholder. It then refreshes that selected board. The [indicator refinement](https://github.com/TerryOtt/localswim/commit/d25dc54c04f7c3961b741055d36a1b8c0b5ceb1d) gives the interim state its pale-green treatment.

The V1 limits are eight boards, 96 MiB of serialized snapshots and 30 minutes without viewing, in per-tab memory. Reload drops that snapshot cache; saved session drafts remain. These are documented V1 bounds, not mandatory V2 implementation constants.

- Cached content is a last confirmed view, never proof that pending local work saved.
- Drafts remain editable, but new mutations are refused until the fresh view and its transition settle. Existing admitted requests retain their original identity and conflict checks.
- Fresh reconciliation visibly moves changed cards, fades arrivals/removals and clips temporary card images to the board/lane viewport. Reduced-motion users skip the animation; settlement has a bounded lifetime.
- Refresh failure keeps the saved view visible and read-only with **Retry**. A stale response or obsolete animation completion cannot reopen writes or clear a newer failure.
- Fresh reads cannot lower an already acknowledged revision. Navigation preserves board-specific state without reintroducing full-board polling for cached or pinned boards.

Evidence: `test_recent_board_cache.py` and `test_recent_board_refresh.py` cover limits, stale state, mutation gating, failure, navigation and settlement.

## 4. Recoverable unconfirmed saves and responsive backups (#0223–#0224)

[Save recovery and backup isolation](https://github.com/TerryOtt/localswim/commit/cc4d3151a5f4b4956f5f524a2e88077284ceae23) extends the earlier optimistic-write behavior. A lost response can mean the operation committed: the interface must not translate uncertainty into either failure or success.

**Review unconfirmed saves** fetches current state and offers card inspection. The user checks an acknowledgement before clearing the warning. Clearing does not retry, cancel, undo or confirm the request. Submitted comment/title/description/new-card text remains under **Saved request drafts** until explicitly discarded, separate from newer unsent editor text.

- Pending intent survives reload; conflicting restored edits retain their submitted text.
- An ordinary move into Completed does not prove that an uncertain approval command succeeded.
- A definitely refused write remains refused even if a later read fails.
- Closing the drawer preserves its unsent comment and does not cancel an already admitted write. Old recovery controls cannot act on another board after navigation.
- No blind replay of comments, uploads or compound actions. Retain V2's own receipt/deduplication rules where stronger.

Backup work now holds edit admission only long enough to capture coherent board bytes; attachment verification/copying and Git operations happen outside it. The UX objective is continued editing during slow backups, with truthful independent backup failure reporting. V2 need not copy the temporary-worktree mechanism. Evidence: `test_browser_save_recovery.py` and `test_snapshot_publish.py`.

## 5. Dependency renumbering and one-click lane approval

[Lane actions, #0226–#0228](https://github.com/TerryOtt/localswim/commit/72beda939654879767c4b71ad1df323f3bf9e9cc) add two separately enabled capabilities.

**Dependency-first ticket numbers:** the lane-header sort glyph opens a preview; only **Apply** commits. Order blockers before dependents through complete blocking paths, including paths outside the selected lane. Refuse relevant cycles; break eligible ties by old ticket. Permute only the selected tickets' existing number pool.

Stable card identities, content, relationships and attachment access survive. Historical numbers in prose are not rewritten. This differs from priority-based visual ordering and does not change priority inheritance. A stale preview must be regenerated; a no-op causes no mutation. V1's schema-6/ticket-history and attachment-origin mechanism records this safely, but is not a V2 storage prescription.

**Approve all:** an enabled Ready for review lane offers a thumbs-up action. One explicit Human click approves every nonarchived card in that lane atomically, adding the configured approval comment and moving each to Completed. There is no preview. The exact rendered membership, original card versions and board revision must still match; otherwise nothing changes. Empty lanes do nothing. Bot and CLI cannot perform this approval action.

Both controls default off in generic boards; the Terry workflow opts in for Backlog sorting and Ready for review approval. Configuration alone performs no reorder or approval. Preserve this distinction between exposing a control and authorizing its action.

## 6. Lane controls that are readable and easy to hit (#0230, #0232–#0234, #0236)

[Control sizing](https://github.com/TerryOtt/localswim/commit/cf280e4a4435881cafc47a52145d70d31a9afda1), [empty-state labels](https://github.com/TerryOtt/localswim/commit/d231b56557646c73aff7c13edd0929d215daabdc), [unified states](https://github.com/TerryOtt/localswim/commit/e91468fa1c2762b65dffccbdaa2576f1e7b9e826) and [faster hover](https://github.com/TerryOtt/localswim/commit/5b575ffdf4e7e3929f29bd7b9918623b1c59c89b) refine the lane actions:

- Use 44 × 44 pixel hit areas, compact centered icons and visible keyboard focus. Approval uses a consistent SVG thumbs-up.
- Sorting is dark gray; approval is green. Enabled hover reverses to a white background with the action color as ink, transitioning in 0.1 seconds. Reduced motion removes the transition.
- Disabled controls remain visibly gray and non-actionable. Their subtle darker-gray hover keeps the white glyph and the same 0.1-second transition without implying availability. Preserve strong contrast and visible keyboard focus.
- Both empty-lane controls explain **Disabled; no cards in swimlane** after the shared-state refinement. Sorting is also disabled with only one card.

Preserve accessible action names and the clear distinction between previewed sorting and immediate approval. Evidence: `test_ticket_order_browser.py` and `test_lane_approval_browser.py`.

## 7. Attach files while creating a card (#0237)

[Create-time attachments](https://github.com/TerryOtt/localswim/commit/d25dc54c04f7c3961b741055d36a1b8c0b5ceb1d) adds multiple file selection to **New card**. The form explains the 10 MiB per-file limit and that uploads follow creation.

- Create the card once, then upload sequentially. The combined workflow is explicitly not atomic.
- Show per-file progress/outcomes; keep successful uploads and pending files when a later upload fails. **Continue uploads** resumes known refusals without creating a second card or resending successful files.
- For an unconfirmed upload, fresh saved metadata must positively identify one matching attachment before treating it as saved. No match or ambiguous matches cannot justify replay.
- Navigation keeps selected file objects in memory. Reload retains descriptive/outcome state, but requires reselection of remaining files; mismatched contents cannot silently substitute another file.
- Reject oversize selection before creating the card. Keep the form open while uploads remain unresolved; closing does not undo the created card or completed uploads.

Evidence: `test_create_attachments_browser.py` covers partial success, refusal, uncertainty, ambiguity, navigation, reload controls and changed file contents.

## 8. After-cutoff follow-through: independent breather health (#0240)

At the cutoff, #0240 was **In Progress**, with an uncommitted implementation under review. It is not part of the d25dc54 shipped baseline. After the cutoff, [8980882](https://github.com/TerryOtt/localswim/commit/8980882a931eca503b859c1ff9816efbd8a7aa83) committed the follow-through; owner acceptance remains pending.

The follow-through separates the breathing dot from the readiness badge: cached-board refresh should not turn the dot red when SSE is healthy, while the badge still says **REFRESHING** or **REFRESH FAILED**. It also adds ten seconds of initial connection grace on a fresh page, without extending that grace for repeated failed attempts. The badge semantics remain unchanged. The parent reports 140 focused tests and a Chrome delayed-stream/data check passed; the parent subsequently confirmed deployment of shared build 8980882. Track owner acceptance separately from the fixed-window inventory.

## 9. Owner notices and retired monitoring UX (#0227, #0231, #0235, #0238)

[Compact notices](https://github.com/TerryOtt/localswim/commit/72beda939654879767c4b71ad1df323f3bf9e9cc), [committed-operation labels](https://github.com/TerryOtt/localswim/commit/d231b56557646c73aff7c13edd0929d215daabdc), [grouping](https://github.com/TerryOtt/localswim/commit/e91468fa1c2762b65dffccbdaa2576f1e7b9e826) and [single-time formatting](https://github.com/TerryOtt/localswim/commit/d25dc54c04f7c3961b741055d36a1b8c0b5ceb1d) make owning-chat intake easier to scan.

Tickets are grouped by committed operation in this fixed order: Created, Contents modified, Comment added, Ticket numbers reordered, Moved lanes, Reviewed & approved, Archived, Restored. Empty groups disappear; numbers are unique and ascending within each group, padded to at least four digits. A ticket may occur in several groups. This is lifecycle grouping, not chronological event order.

Approval wording comes only from the actual approval command, never copied prose or an ordinary move. Notices exclude private card prose and credentials. Subsecond bursts use one floored UTC **Time**; consumers query through Time + two seconds. Longer intervals use outward-rounded **Start/End**. Full board identity and sanitized activity remain necessary checks; hints do not authorize work.

[Legacy watcher retirement, #0222](https://github.com/TerryOtt/localswim/commit/818e6d4b4d9f34ba58d10c3c648e448f24408267) removes the competing watcher/pool operation path. The underlying notifier (#0219) predates this window. Service-owned, opt-in Human-only notices retain the 15-second quiet window and best-effort single-owner delivery. Browser updates still include Bot edits. Preserve historical files and V2's separate administrative-notifier/product-notice contracts; do not mechanically discard stronger V2 delivery guarantees.

## V2 planning receipt and acceptance checklist

**RECOMMENDED:** map every section to existing future work first, and use a new closing milestone only for uncovered remainder. The completion deadline is **end of V2, not M18**.

For each area, record **covered**, **planned**, or **intentional difference**, with a concrete milestone/card, behavioral test/evidence, prerequisite readiness and any owner decision. Include:

- Live board/sidebar propagation, reconnect and inactive-board work bounds.
- Connection/readiness/error distinctions, stable status layout and the #0240 decision.
- Recent-board return, safe refresh/retry/animation and retained drafts.
- Uncertain-save inspection, preserved submitted text and responsive backups.
- Dependency preview/apply and atomic Human-only lane approval.
- Accessible control sizing, disabled explanations and hover/focus states.
- Creation attachments, partial outcomes, reload and non-replay recovery.
- Scannable owner notices, truthful operation labels and one supported monitoring path.

This audit does not certify V2 parity. Record the durable mapping and schedule changes on the V2 intake/plan and link back to #0241. The M18 owner receives this for planning; implementation begins only under the appropriate future cards and normal gates.
