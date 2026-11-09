# Product intent: a dashboard the human chooses to consult

- Agreed: 2026-11-08
- Product owner: Terry Ott

> I interact with localswim, I don't want localswim to interact with ME.

> I initiate contact always and forever.

The human initiates contact. Localswim is a work dashboard that helps Terry see
how work is going across projects when he has time and interest. It must not
turn that visibility into a demand for attention when he turns away.

## The primary glance

The pinned-project overview is Terry's primary scan area. Its job is to answer:

- Which of my pinned projects need attention?
- Is anything waiting for my input?
- Is finished work ready for me to review?

Keep project names and these status counts easy to read, stable and quick to
compare. These answers must be available directly in the overview without
opening each board or clicking to reveal them: the glance itself is the
interaction. Secondary controls should not crowd that overview. The selected
board provides the detail when the human chooses to investigate.

For example, Terry sees a blue review count of 2 on a pinned project and chooses
to inspect it. The current milestone has two parts ready for review, two in
progress and three queued behind dependencies: seven parts in total. He can
then interpret the two reviewable parts as progress within that milestone.

The badge itself remains a board-wide Ready for Review count. The human uses
the visible milestone context to interpret it; the example does not define a
new automatic completion percentage or demand that he review immediately.

In this example, absent a new blocker or a need for his input, Terry can leave
the work running while the blue count grows from 2 toward 7. He chooses to
review the completed milestone as a batch. Partial review readiness is useful
progress information, not an instruction to stop other work and review now.
This review cadence does not change the board's authorization or transition
rules.

The same view supports a human sanity check over time. If progress remains at
2 of 7 longer than he expects for the milestone's timebox, Terry can choose to
investigate whether the agent is stuck. The dashboard supplies visible facts;
it does not decide that delay requires an interruption or invent an automatic
completion estimate from the card count.

Agents maintain their own work state through the shared CLI. That makes the
dashboard useful without the human asking every chat for a status report.
The resulting counts are visible evidence, not an automatic diagnosis of agent
liveness: Terry decides what their pace means in context.

A flat review count can still include substantial debugging, failed experiments
and learning. It measures reviewable results, not all effort or intermediate
progress. A later increase makes the resulting deliverables visible.

## Recent delivery and historical perspective

The Completed lane is a scannable view of recent delivery. Terry calls hiding
older completed tickets "absolutely clutch": with 180 tickets visible, the lane
became unusable. By default, cards are hidden once they have spent at least 24
hours in Completed; when their lane-entry time is unknown, they remain visible.

The work remains available through **Unhide old**. Terry can choose to inspect
accumulated achievements when a large backlog makes the remaining work seem
endless: "We have a lot left but never forget how far we've come." This is
user-chosen inspection, not an automatic morale notification. Preserve both the
recent delivery view and access to historical work as V2 parity intent; this
describes existing behavior, not a new feature.

## Continuity between milestones

The board also carries work history across owning-agent changes. At a milestone
boundary, a fresh agent can combine the completed milestone records and project
documentation with a handoff from its predecessor. The human should not have to
reconstruct that context from scratch for every new chat.

Keep status, relationships, decisions and historical evidence trustworthy for
both audiences: the human scanning current work and the next agent learning how
the project reached its current state. This complements, rather than replaces,
the project's source documentation and direct agent handoff.

## Trust makes the glance useful

Terry reads this overview in fractions of a second. Its value depends on being
able to trust it, especially as boards grow. Summaries must come from accepted
data, distinguish unknown or stale information from a known zero, and avoid
presenting pending persistence as confirmed success.

Fast recognition does not require every backend update to arrive instantly.
Several seconds of update latency can be acceptable; a reassuring but misleading
picture is not. Reliability, honest freshness and recovery are part of the core
product value, not optional polish.

## Available when wanted, quiet when left alone

- Keep useful project, lane and card information easy to inspect. Counts,
  priorities, waiting states and progress help the human understand the work.
- Preserve the quiet browser-tab attention count. Terry explicitly accepts it
  as a passive cue he can notice when he chooses to look.
- Do not send unsolicited desktop/browser notifications, audible dings, phone
  or email alerts, or use focus-stealing or window-raising behavior to summon
  the human.
- A blocked card, a growing queue or a high priority does not itself authorize
  interrupting Terry. The dashboard makes those facts available for inspection.
- Background refresh and cache maintenance may quietly keep the dashboard
  useful. Invisible to the human does not mean that background work must stop;
  it means that work must not demand his attention.

## Respond clearly to actions the human initiates

Quiet operation must not make the application ambiguous after a click, paste or
submission. Those actions are already an invitation to respond.

- Acknowledge accepted input promptly, including operations that take time.
- Distinguish a draft, work in progress, confirmed persistence and an uncertain
  outcome. Do not imply that data was saved before it was confirmed.
- Show validation problems, failures and recovery choices in the application
  without losing the human's input or silently replaying uncertain writes.
- Keep status truthful. Calm presentation does not justify hiding stale data,
  broken connections or failed persistence from a human who is using the app.
- Keep feedback proportional to the action and visible in its working context.
  Upload progress and an acknowledgement toast are appropriate responses to an
  attachment the human just selected or pasted.

## Keep automation coordination separate

The service-owned board-change notices sent to the owning Codex agent have a
different audience and purpose. Terry explicitly values that automation: his
board edits tell the agent that work or approvals are available.

Preserve that separately authorized path and its actor filtering. It is not a
reason to add human reminders or turn the web client into an alerting platform.
Queue delivery also does not establish that an agent finished the work.

## Apply the principle to future changes

During design and review, ask:

- Is this information available when the human chooses to inspect the work?
- Did the human initiate the action this feedback responds to?
- Will the application remain quiet when he turns away?
- Is the feedback honest about freshness and persistence?

These are product constraints, not a particular implementation architecture.
V2 may improve the experience while preserving this human-controlled attention
model. The [client cache policy](decisions/0003-byte-limited-client-board-cache.md)
supports quick returns to useful information; it does not authorize interruptions.

The retirement of the former Desktop Alerts feature is tracked by V1 #0248.
