# Life Storage Phase 3: Life Event Ledger

Date: 2026-08-04

## Outcome

The selectable storage foundation now has a backend-neutral, append-only Life
Event ledger contract and fenced local/MySQL SQL adapters. This phase does not
activate selectable storage, migrate production data, or change the running
Elysium process.

Delivered:

- `LifeEventStorePort` with atomic append, occurrence idempotency, ordered reads,
  explicit retention-gap handling, revisioned consumer cursors, and bounded
  health diagnostics;
- coherent local/MySQL factories that consume one existing
  `StorageBackendRuntime` and cannot independently choose a backend;
- versioned SQLite/MySQL schema for the immutable ledger, revisioned consumer
  offsets, retention metadata, and a transactionally populated export outbox;
- database-level update/delete protection for authoritative event rows;
- visibility-aware export requests: shared/public requests become pending,
  private requests remain held;
- database-time recording, generation fencing on every committing unit of work,
  and three-attempt bounded deadlock/lock retry;
- an opt-in real-MySQL contract which refuses to run without an explicitly
  isolated database.

## Preserved semantics

- Producer sequence remains `source_sequence`; database ingest position is the
  durable ordering token.
- Same occurrence plus the same canonical payload is an idempotent replay.
  Same occurrence plus different evidence raises
  `LifeEventOccurrenceConflict`; it is never overwritten or ignored.
- Numeric holes in an auto-generated position sequence do not imply history
  loss. A gap is raised only from the persisted retention floor.
- Consumer progress is compare-and-swap over both position and revision, cannot
  regress, and cannot advance beyond the authoritative ledger frontier.
- JSONL is not promoted to authority. Existing local files remain untouched and
  are retained for compatibility/export until the later integration and copy
  phases are explicitly verified.

## Verification

- Ruff: passed.
- Compile check: passed.
- Diff whitespace check: passed.
- Local Life Event contract: 5 passed.
- Real MySQL Life Event contract: 1 skipped because the available remote account
  is restricted to the shared `elysium` database and no disposable test schema
  is available.

The skipped MySQL case is a safety decision, not a claimed pass. The shared
database was not used for fixed-schema destructive contract testing.

## Remaining work

- Wire the new port into the active Life Event bus without changing the default
  inert/local behavior.
- Copy from a frozen SQLite snapshot, verify identity/hash/frontier, and keep the
  original source immutable.
- Bridge or replace the legacy shared-sync outbox consumer against the new
  export outbox.
- Run the same MySQL contract in a disposable database.
- Complete reverse export and end-to-end local/MySQL startup validation in a
  user-approved maintenance window.
