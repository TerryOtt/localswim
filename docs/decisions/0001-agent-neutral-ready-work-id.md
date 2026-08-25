# ADR 0001: Use an agent-neutral Ready For Work lane ID

- Date: 2026-08-22
- Status: Accepted owner override
- Decision owner: Terry Ott

## Context

The Ready For Work lane already had an agent-neutral display label, but its persisted
lane ID named one particular automation product. That ID appears in board current
states, append-only lane-history endpoints, transition rules, the REST and CLI
contract, fixtures, tests, and documentation.

## Conventional recommendation and objection

Codex recommended retaining the stable persisted ID and continuing to present the
agent-neutral display label. That was the conventional compatibility choice: changing
a durable identifier requires rewriting audit history, invalidates unmigrated boards,
and changes request and response values used by API and CLI clients. The existing label
already kept the product name out of the browser UI.

## Owner override

After that objection was stated, Terry explicitly directed: “yeah, shut it down and do
the change. We are early in work, Make it in localswim repo as well (if it exists).”
His justification is that this is the inexpensive point to remove an automation-product
name from durable identifiers before more boards and integrations depend on it.

## Decision

The canonical lane ID is `ready_for_work`. Board schema 3 uses that ID in current card
state and every lane-history endpoint. There is no runtime compatibility alias.

`localswim-cli <board> lane migrate <old-lane-id> ready_for_work` is the explicit offline
schema-2 migration. It refuses a listening board port, holds the board lock, rewrites
only exact card-state and lane-history endpoint values, validates and replays the full
schema-3 result, increments the revision, and atomically replaces the snapshot.

## Knowingly accepted tradeoffs

- Existing schema-2 boards must be migrated before this localswim version can load
  them.
- Migration rewrites the board's stored current states and historical lane endpoints;
  the state-store Git history remains the durable before-and-after audit.
- Existing API or CLI consumers that send the former value must update immediately.
- API `v001` changes before a supported public release rather than carrying a legacy
  alias indefinitely.
- Repository Git history still contains the former identifier. Rewriting published Git
  history is outside this decision and was explicitly not recommended.
