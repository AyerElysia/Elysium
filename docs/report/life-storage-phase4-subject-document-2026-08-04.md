# Life Storage Phase 4A: Subject Document Authority

Date: 2026-08-04

## Outcome

Elysium now has a backend-neutral, exact-byte `SubjectDocumentStorePort` with
first-class local and MySQL adapters. This delivery records immutable document
versions and treats the current head as a revision-CAS projection; it does not
reduce Markdown files to unversioned text columns and does not infer an author
or semantic source when the old files do not prove one.

Delivered:

- immutable exact-byte versions with SHA-256, byte length, encoding/newline
  diagnostics, explicit byte-fidelity and provenance status;
- deterministic document/version/head-event identities, occurrence
  idempotency, same-identity conflict detection and stable history/head
  pagination;
- atomic version + head event + head CAS + projection-outbox transactions;
- local database triggers and MySQL schema migrations v1-v3, including six
  foreign keys and leased projection work;
- candidate-copy authority integration and a repeatable operator command;
- reverse export to a new directory with an incomplete marker, per-file
  verification and a checksummed manifest;
- an independent read-only MySQL/snapshot/export auditor;
- a conflict-safe workspace projector: it writes only when the existing file
  equals the known parent, and otherwise records failure without overwriting;
- a workspace observer: changed external bytes are appended as a new exact
  observation with unknown semantic actor/source, never treated as an edit of
  immutable history.
- selected runtime integration: file tools and Memory Witness commit before
  parent-hash projection; targeted outbox state is checked explicitly so a
  terminal projection failure cannot be mistaken for an external file edit.

The selected logical namespace is explicit, not inferred from arbitrary
workspace files:

- `life_engine_workspace/SOUL.md`;
- `life_engine_workspace/USER.md`;
- `life_engine_workspace/MEMORY.md`;
- `life_engine_workspace/diaries/**`;
- `diaries/**`.

The two diary roots remain distinct, so equal relative filenames cannot
silently collide.

## Preserved semantics

- `declared_owner` is a technical ownership declaration, not a judgment about
  the content or value of a document.
- `semantic_actor_id`, `semantic_source_id` and `occurred_at` remain nullable.
  Snapshot observations without proof are marked `semantic_source_missing`.
- A head can move only when both expected revision and expected parent version
  match. Concurrent or stale writers fail explicitly.
- Old versions and head events cannot be updated or deleted through the domain
  adapter. Production schema initialization requires database immutability;
  the available remote account cannot create triggers, so the online shadow is
  explicitly application-enforced and generation-ineligible.
- Projection failure never mutates history. A file that differs from the known
  parent is preserved byte-for-byte and reported as a conflict.
- Re-observing unchanged bytes is a no-op. Re-observing changed bytes appends a
  deterministic, retry-safe observation.

## Real shadow copy and reverse-export evidence

Read-only source snapshot:

`C:\Temp\Data\ElysiumBackups\life-domain-20260804T0615Z-candidate`

The snapshot manifest is valid but records `writer_frozen=false`; this run can
therefore prove round-trip fidelity but cannot authorize a backend switch.

- Declared documents: 1,404.
  - external diary files: 128;
  - Life Engine diary files: 1,273;
  - SOUL/USER/MEMORY root files: 3.
- Exact bytes: 10,316,470.
- MySQL documents / versions / head events / outbox rows: each 1,404.
- Missing or extra paths: 0.
- Orphan versions/outbox rows: 0.
- Copy conflicts: 0.
- Source, MySQL and reverse-export root:
  `d4c83a81d8df0895898ced696ba0ef63167281224faecff25ca9ce99f7cca966`.
- Copy run: `subject-shadow-v1-77435387f4acc59e`, state `copied`.
- Schema run: `subject-schema-v3-77435387f4acc59e`, state `copied`;
  migrations v1, v2 and v3 are present.
- Reverse export:
  `C:\Temp\Data\ElysiumBackups\subject-reverse-20260804T0806Z`.
- Reverse manifest SHA-256:
  `df2548a287ce7d1b05d18fb4b7103be20d57f531e6002f48465f5db4dcc63d5a`.
- Independent auditor result: `verified=true`, mismatch count 0, incomplete
  marker absent.

The outer command wrapper reached its initial ten-minute observation timeout
while the fenced Linux migration process was still progressing. The exact PID,
lease and MySQL progress were checked read-only; the process was not killed or
restarted. It retained the same valid candidate token, completed 1,404/1,404,
performed the reverse export and sealed the run normally. No active authority
or Elysium process was changed.

All 1,404 projection requests intentionally remain pending. Applying a stale
online snapshot over the live workspace would be unsafe; projection is reserved
for a user-approved, frozen activation window after current external changes
have first been observed.

## Verification

- Local subject contract: exact BOM/CRLF bytes, idempotency, identity conflict,
  revision/head CAS, concurrency, immutable history, stable pagination,
  projection lease CAS and unsafe-path rejection.
- Snapshot migration: selection boundaries, checksum failure, idempotent copy,
  aggregate root and reverse export.
- Workspace loop: create, parent-matched replacement, external divergence,
  append-only observation and idempotent confirmation.
- Ruff, compile and diff whitespace checks: passed.
- Real MySQL destructive contract: intentionally skipped because the available
  account has only the shared `elysium` schema, not a disposable isolated test
  database. The non-destructive real shadow copy and independent audit passed.

## Remaining integration work

- Current subject heads were cross-referenced without relabelling historical
  `memory_artifact_versions`; the latter remain text-derived Memory evidence.
- The subject store, observer and projector now consume the one runtime owned by
  `LifeEngineService`; they neither open nor close an independent engine.
- Declared file-tool writes and Memory Witness projections use durable
  write-ahead followed by parent-hash workspace projection. Generic shell is
  fail-closed while selected Subject storage is enabled because arbitrary shell
  cannot prove write-ahead.
- Run the shared local/MySQL contract in a disposable MySQL database with
  trigger privileges.
- Repeat from a user-approved frozen snapshot before generation verification or
  activation.

Until those gates and the wider cross-domain activation gates pass,
`storage.enabled=false` remains required.
