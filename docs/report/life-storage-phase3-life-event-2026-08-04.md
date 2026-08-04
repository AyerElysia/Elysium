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
- an independent candidate-copy registry with database-time leases, fencing,
  immutable run identity, monotonic progress, append-only conflict evidence,
  and no ability to activate the production authority registry;
- byte-preserving snapshot import. Legacy payload JSON is never decoded and
  re-encoded during migration; MySQL schema v2 stores the exact JSON text in
  checked `LONGTEXT` while retaining its source SHA-256;
- audited reverse export to a new SQLite directory with an incomplete marker,
  exact payload/position restoration, independent root verification, and a
  checksummed manifest. Existing files are never overwritten.

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
- Candidate-copy authority and active runtime authority are separate. A copied
  online snapshot cannot become `verified` when `writer_frozen=false`, even if
  every record matches.

## Real candidate copy and round-trip evidence

The already verified online backup at
`C:\Temp\Data\ElysiumBackups\life-domain-20260804T0615Z-candidate` was used as
a read-only source. It remains `writer_frozen=false` and therefore is not
generation-eligible.

- Source events: 86,094; copied events: 86,094.
- Consumer cursors: 1; `memory_witness` frontier: 86,093; imported revision: 1.
- Per-record occurrence, source identity, position, exact payload bytes, and
  payload SHA-256: independently checked, 0 mismatches.
- Source/target aggregate root:
  `019dea557c32ce26bd04de97353144baa94270b9d08513dd659ee9a595d3241b`.
- Copy run: `life-event-shadow-v2-77435387f4acc59e`, state `copied`, conflicts
  0, active generation unchanged (`None`).
- Reverse export:
  `C:\Temp\Data\ElysiumBackups\life-event-reverse-20260804T0735Z`.
  Independent SQLite comparison found 86,094 equal rows, 0 mismatches,
  `PRAGMA integrity_check=ok`, no incomplete marker, and a valid manifest hash
  `dfb92f325f430f4b3417d85c7da245de2d4c9e5860f7cf71f8d97954b68d2cbf`.

The first real copy attempt correctly exposed a migration bug: decoding a
legacy event into the current dataclass added five newer empty fields and
changed the evidence hash. That run remains failed with conflict evidence. Its
500 derived target rows were deleted only after a read-only proof showed every
row was exactly the faulty re-encoding of source positions 1-500; the immutable
source remained intact, and the corrected v2 run used a new audit identity.

The remote account cannot create MySQL triggers (`ERROR 1419` under binary
logging). Production schema initialization therefore remains fail-closed by
default when database immutability cannot be installed. The non-frozen shadow
copy explicitly used application-level immutability, cannot become verified,
and was never activated.

## Verification

- Ruff: passed.
- Compile check: passed.
- Diff whitespace check: passed.
- Local Life Event contract including exact copy and reverse export: 6 passed.
- Candidate-copy MySQL contract: passed in an explicitly opted-in, bounded run
  against the shared schema; its test event/outbox rows were removed after
  exact ownership assertions, while audit runs were retained.
- Real MySQL Life Event contract: 1 skipped because the available remote account
  is restricted to the shared `elysium` database and no disposable test schema
  is available.

The skipped MySQL case is a safety decision, not a claimed pass. The shared
database was not used for fixed-schema destructive contract testing.

## Remaining work

- Wire the new port into the active Life Event bus without changing the default
  inert/local behavior.
- Repeat the copy from a user-approved frozen snapshot before any generation can
  be marked verified.
- Bridge or replace the legacy shared-sync outbox consumer against the new
  export outbox.
- Run the same MySQL contract in a disposable database.
- Complete cross-domain reverse export and end-to-end local/MySQL startup
  validation in a user-approved maintenance window.
