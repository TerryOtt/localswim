# ADR 0002: Generate readable lane slugs once and persist them

- Date: 2026-08-22
- Status: Accepted
- Decision owner: Terry Ott

## Context

ADR 0001 exposed the cost of putting an automation product name into a durable lane ID:
the value appeared in current card state, audit history, permissions, REST requests, CLI
arguments, tests, and documentation. A follow-on proposal would have generated UUIDv4
lane IDs while letting initialization permissions use display names.

Codex objected that random identifiers would make board snapshots, API traffic, CLI
commands, Git diffs, and incident diagnosis needlessly opaque. Codex also objected to
recomputing an ID whenever a display label changes: a presentation rename would become
an identity and audit-history migration.

Terry dismissed the UUID proposal and approved the stable-slug recommendation.

## Decision

Schema-4 boards embed their complete resolved transition policy. The standard
initializer consumes two human-readable schema-1 inputs:

- a board description containing users, priorities, and lane display names; and
- a permission document whose creation and movement rules use user and lane names.

Initialization generates each omitted lane ID once by converting its initial display
name to a lower-case ASCII underscore slug. It rejects duplicate names and slug
collisions, resolves all permission names to stable IDs, validates the complete result,
and persists that policy in the same atomic board snapshot as its cards and history.

An optional explicit canonical slug is accepted for a controlled import or migration.
It is not a routine way to hand-maintain parallel names.

After initialization, identity and presentation have separate operations:

- `--rename-lane-label` changes only the display label and keeps the persisted ID;
- `lane migrate-id` atomically rewrites the embedded policy, current card states, and
  lane-history endpoints while the service is stopped.

An existing ID is never regenerated from its current label. Runtime authorization,
REST values, CLI values, persisted state, and history use only the stable ID.

## Consequences

- Ordinary boards get readable IDs without asking an operator to type a parallel key.
- Permission inputs remain readable during installation, while runtime permissions are
  unambiguous and independent of mutable labels.
- A label can intentionally stop resembling its old ID after a rename. That visible
  mismatch is preferable to silently breaking durable references.
- Non-ASCII names that cannot produce a nonempty ASCII slug require an explicit import
  ID. Transliteration stays dependency-free and deterministic.
- Schema-3 boards require the explicit offline `--embed-policy` migration. The earlier
  schema-2 lane rename migrates directly to schema 4.
- The checked `src/localswim/rules.json` remains only a controlled legacy/default
  migration input; a running schema-4 board authorizes from its embedded policy.

## Rejected alternatives

- UUIDv4 IDs: stable but hostile to humans reading or operating the local JSON system.
- Recompute the slug on every label change: readable at one instant but not a stable
  identity, and it turns presentation edits into history migrations.
- Require every installer to type both an ID and label: stable, but recreates the
  avoidable drift and product-name mistake that prompted this work.
