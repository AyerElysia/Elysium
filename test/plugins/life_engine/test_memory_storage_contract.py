"""Shared contract tests for selectable Life Memory storage."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.epistemic import MemoryClaim, create_epistemic_schema
from plugins.life_engine.memory.experience import (
    ExperienceRecord,
    create_life_memory_schema,
)
from plugins.life_engine.memory.indexing import create_memory_schema
from plugins.life_engine.memory.living import (
    ArtifactHeadConflict,
    create_living_memory_schema,
    new_artifact_version,
)
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.memory import (
    DocumentIndexProjection,
    EpistemicMemoryStore,
    ExperienceLedgerStore,
    LegacyGraphStore,
    LivingMemoryStore,
    MemoryStoreRole,
    WitnessLedgerStore,
    create_local_memory_storage_bundle,
    memory_store_characterizations,
)
from plugins.life_engine.storage.memory import factory as memory_factory_module
from plugins.life_engine.storage.memory import mysql as mysql_memory
from plugins.life_engine.storage.memory import schema as memory_schema_module
from plugins.life_engine.storage.memory.schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_IMMUTABILITY_TRIGGER_CONTRACT,
    MEMORY_IMMUTABLE_TABLE_COLUMNS,
    MEMORY_IMMUTABLE_TABLES,
    MEMORY_MIGRATIONS,
    MEMORY_MIXED_TABLES,
    MEMORY_MUTABLE_TABLES,
    MEMORY_SCHEMA_VERSION,
    MEMORY_WITNESS_DELIVERY_IMMUTABLE_COLUMNS,
    MEMORY_WITNESS_DELIVERY_MUTABLE_COLUMNS,
    MEMORY_WITNESS_IMMUTABLE_COLUMNS,
    MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS,
    MemoryDatabaseImmutabilityError,
    MemoryImmutabilityPolicyError,
)
from plugins.life_engine.storage.models import BackendKind
from src.kernel.storage import CursorConflict


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    create_memory_schema(db)
    create_life_memory_schema(db)
    create_epistemic_schema(db)
    create_living_memory_schema(db)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL DEFAULT 0.5,
            base_strength REAL DEFAULT 0.5,
            reinforcement REAL DEFAULT 0.0,
            activation_count INTEGER DEFAULT 0,
            last_activated_at REAL,
            reason TEXT,
            created_at REAL NOT NULL,
            bidirectional INTEGER DEFAULT 1,
            UNIQUE(source_id, target_id, edge_type)
        );
        CREATE TABLE IF NOT EXISTS memory_corrections (
            correction_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            message TEXT NOT NULL,
            source TEXT DEFAULT 'user',
            created_at REAL NOT NULL,
            related_node_id TEXT,
            query TEXT DEFAULT '',
            stream_id TEXT
        );
        """
    )
    return db


def _experience(event_id: str = "event-1", sequence: int = 1) -> ExperienceRecord:
    return ExperienceRecord(
        event_id=event_id,
        source_event_id="producer-1",
        sequence=sequence,
        occurred_at="2026-08-04T10:00:00+08:00",
        recorded_at="2026-08-04T10:00:01+08:00",
        source="qq",
        channel="group",
        event_type="message",
        content="她记得这一刻",
        stream_id="qq:group:1",
        consciousness_instance_id="core",
        actor="user:1",
        metadata={"occurrence_id": event_id},
    )


def test_memory_characterization_is_ordered_and_engineering_only() -> None:
    items = memory_store_characterizations()

    assert [item.migration_order for item in items] == [10, 20, 30, 40, 50, 60]
    assert items[0].role == MemoryStoreRole.REBUILDABLE_PROJECTION
    assert items[-1].role == MemoryStoreRole.COMPATIBILITY_HISTORY
    assert all(item.name for item in items)
    assert not any(
        token in item.name
        for item in items
        for token in ("truth", "important", "emotion", "mature", "identity")
    )


def test_mysql_memory_migrations_are_explicit_and_ordered() -> None:
    assert MEMORY_SCHEMA_VERSION == max(
        migration.version for migration in MEMORY_MIGRATIONS
    )
    assert tuple(item.version for item in MEMORY_MIGRATIONS) == tuple(
        range(1, MEMORY_SCHEMA_VERSION + 1)
    )
    ddl = "\n".join(
        statement
        for migration in MEMORY_MIGRATIONS
        for statement in migration.statements
    )
    for table in (
        "memory_nodes",
        "memory_experiences",
        "memory_witnesses",
        "memory_artifact_versions",
        "memory_claims",
        "memory_edges",
        "memory_workspace_projection_events",
        "memory_workspace_projection_heads",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    for column in (
        "is_deleted",
        "event_date",
        "fts_content_hash",
        "embedding_content_hash",
        "embedding_model",
        "embedding_updated_at",
        "legacy_fts_present",
        "claim_token",
    ):
        assert f"ADD COLUMN {column}" in ddl
    assert "projection_path_sha256 CHAR(64)" in ddl
    assert "UNIQUE KEY uq_memory_witness_projection_path_hash" in ddl
    assert "UNIQUE KEY uq_memory_witness_projection_path (projection_path)" not in ddl
    assert ddl.count("`signal` VARCHAR(512)") == 2
    assert "content LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL" in ddl
    assert "life_memory_lossless_json_text_v1" in {
        migration.name for migration in MEMORY_MIGRATIONS
    }
    assert "MODIFY COLUMN metadata_json LONGTEXT NOT NULL" in ddl
    assert "ADD PRIMARY KEY (job_id, index_revision)" in ddl
    assert "UNIQUE KEY uq_memory_jobs_node_revision" in ddl
    assert "universal" not in ddl.lower()


def test_mysql_memory_immutability_classification_is_exhaustive() -> None:
    schema_ddl = "\n".join(
        statement
        for migration in MEMORY_MIGRATIONS
        for statement in migration.statements
    )
    created_tables = set(
        re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z0-9_]+)", schema_ddl)
    )
    immutable_tables = set(MEMORY_IMMUTABLE_TABLES)
    mutable_tables = set(MEMORY_MUTABLE_TABLES)

    assert immutable_tables.isdisjoint(mutable_tables)
    assert created_tables == immutable_tables | mutable_tables | set(MEMORY_MIXED_TABLES)
    assert set(MEMORY_IMMUTABLE_TABLE_COLUMNS) == immutable_tables

    trigger_ddl = "\n".join(
        statement
        for migration in MEMORY_IMMUTABILITY_MIGRATIONS
        for statement in migration.statements
    )
    for table in immutable_tables:
        assert f"{table}_immutable_update" in trigger_ddl
        assert f"{table}_immutable_delete" in trigger_ddl
        for column in MEMORY_IMMUTABLE_TABLE_COLUMNS[table]:
            assert f"OLD.`{column}` <=> NEW.`{column}`" in trigger_ddl
    for table in mutable_tables:
        assert f"{table}_immutable_update" not in trigger_ddl
        assert f"{table}_immutable_delete" not in trigger_ddl

    for column in MEMORY_WITNESS_IMMUTABLE_COLUMNS:
        assert f"OLD.{column} <=> NEW.{column}" in trigger_ddl
    for column in MEMORY_WITNESS_MUTABLE_PROJECTION_COLUMNS:
        assert f"OLD.{column} <=> NEW.{column}" not in trigger_ddl
    assert "memory_witnesses_immutable_delete" in trigger_ddl
    for column in MEMORY_WITNESS_DELIVERY_IMMUTABLE_COLUMNS:
        assert f"OLD.{column} <=> NEW.{column}" in trigger_ddl
    for column in MEMORY_WITNESS_DELIVERY_MUTABLE_COLUMNS:
        delivery_trigger = next(
            statement
            for statement in MEMORY_IMMUTABILITY_MIGRATIONS[-1].statements
            if "memory_witness_delivery_authority_immutable_update" in statement
        )
        assert f"OLD.{column} <=> NEW.{column}" not in delivery_trigger
    assert "memory_witness_delivery_immutable_delete" in trigger_ddl


@pytest.mark.asyncio
async def test_mysql_memory_schema_installs_separate_immutability_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applications: list[tuple[str, tuple[object, ...]]] = []
    validations = 0

    class _Runner:
        def __init__(
            self, _engine: object, *, table_name: str, **_kwargs: object
        ) -> None:
            self.table_name = table_name

        async def apply(self, migrations: tuple[object, ...]) -> None:
            applications.append((self.table_name, migrations))

    async def _validate() -> None:
        nonlocal validations
        validations += 1

    async def _verify(_runtime: StorageBackendRuntime) -> None:
        return None

    monkeypatch.setattr(memory_schema_module, "MySQLMigrationRunner", _Runner)
    monkeypatch.setattr(
        memory_schema_module,
        "_verify_memory_database_immutability",
        _verify,
    )
    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity="mysql://memory-contract",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=object(),  # type: ignore[arg-type]
        session_factory=None,
        _writer_validator=_validate,
    )

    await memory_schema_module.ensure_memory_storage_schema(runtime)

    assert validations == 2
    assert applications == [
        ("life_memory_schema_migrations", MEMORY_MIGRATIONS),
        (
            "life_memory_immutability_schema_migrations",
            MEMORY_IMMUTABILITY_MIGRATIONS,
        ),
    ]


@pytest.mark.asyncio
async def test_only_unfrozen_candidate_shadow_can_skip_memory_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applications: list[str] = []

    class _Runner:
        def __init__(
            self, _engine: object, *, table_name: str, **_kwargs: object
        ) -> None:
            self.table_name = table_name

        async def apply(self, _migrations: tuple[object, ...]) -> None:
            applications.append(self.table_name)

    async def _validate() -> None:
        return None

    monkeypatch.setattr(memory_schema_module, "MySQLMigrationRunner", _Runner)

    def _runtime(role: StorageWriterRole) -> StorageBackendRuntime:
        return StorageBackendRuntime(
            enabled=True,
            backend=BackendKind.MYSQL,
            backend_identity="mysql://memory-contract",
            generation=None,
            authority_registry=None,
            authority_token=None,
            engine=object(),  # type: ignore[arg-type]
            session_factory=None,
            _writer_validator=_validate,
            writer_role=role,
        )

    with pytest.raises(MemoryImmutabilityPolicyError, match="unfrozen"):
        await memory_schema_module.ensure_memory_storage_schema(
            _runtime(StorageWriterRole.ACTIVE),
            require_database_immutability=False,
            writer_frozen=False,
        )
    with pytest.raises(MemoryImmutabilityPolicyError, match="unfrozen"):
        await memory_schema_module.ensure_memory_storage_schema(
            _runtime(StorageWriterRole.CANDIDATE_COPY),
            require_database_immutability=False,
            writer_frozen=True,
        )

    await memory_schema_module.ensure_memory_storage_schema(
        _runtime(StorageWriterRole.CANDIDATE_COPY),
        require_database_immutability=False,
        writer_frozen=False,
    )

    assert applications == ["life_memory_schema_migrations"]


@pytest.mark.asyncio
async def test_active_memory_bundle_fails_closed_when_triggers_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing(_runtime: StorageBackendRuntime) -> None:
        raise MemoryDatabaseImmutabilityError("missing trigger contract")

    monkeypatch.setattr(
        memory_factory_module,
        "verify_memory_storage_immutability",
        _missing,
    )
    runtime = StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.MYSQL,
        backend_identity="mysql://memory-contract",
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=object(),  # type: ignore[arg-type]
        session_factory=None,
    )

    with pytest.raises(MemoryDatabaseImmutabilityError, match="missing"):
        await memory_factory_module.open_mysql_memory_storage(runtime)


@pytest.mark.asyncio
async def test_memory_immutability_verifier_detects_trigger_drift() -> None:
    class _Mappings:
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self._rows = rows

        def one_or_none(self) -> dict[str, str] | None:
            return self._rows[0] if self._rows else None

        def all(self) -> list[dict[str, str]]:
            return self._rows

    class _Result:
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self._rows = rows

        def mappings(self) -> _Mappings:
            return _Mappings(self._rows)

    class _Connection:
        async def execute(
            self,
            statement: object,
            _parameters: object = None,
        ) -> _Result:
            if "information_schema.TRIGGERS" in str(statement):
                rows: list[dict[str, str]] = []
                for name, event, table in MEMORY_IMMUTABILITY_TRIGGER_CONTRACT[:-1]:
                    if table == "memory_witnesses":
                        marker = "MemoryWitnessAuthorityImmutable"
                        protected_columns = MEMORY_WITNESS_IMMUTABLE_COLUMNS
                    elif table == "memory_witness_delivery_jobs":
                        marker = "MemoryWitnessDeliveryAuthorityImmutable"
                        protected_columns = MEMORY_WITNESS_DELIVERY_IMMUTABLE_COLUMNS
                    else:
                        marker = "MemoryAuthorityRecordImmutable"
                        protected_columns = MEMORY_IMMUTABLE_TABLE_COLUMNS[table]
                    comparisons = " ".join(
                        f"OLD.{column} <=> NEW.{column}" for column in protected_columns
                    )
                    rows.append(
                        {
                            "trigger_name": name,
                            "event_manipulation": event,
                            "action_timing": "BEFORE",
                            "event_object_table": table,
                            "action_statement": f"{comparisons} {marker}",
                        }
                    )
                return _Result(rows)
            return _Result(
                [
                    {
                        "version": str(migration.version),
                        "name": migration.name,
                        "checksum": migration.checksum,
                    }
                    for migration in MEMORY_IMMUTABILITY_MIGRATIONS
                ]
            )

    class _ConnectionContext:
        async def __aenter__(self) -> _Connection:
            return _Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Engine:
        def connect(self) -> _ConnectionContext:
            return _ConnectionContext()

    runtime = SimpleNamespace(engine=_Engine())

    with pytest.raises(MemoryDatabaseImmutabilityError, match="missing or drifted"):
        await memory_schema_module._verify_memory_database_immutability(
            runtime  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mysql_witness_projection_path_hash_never_replaces_full_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Mappings:
        def __init__(self, row: dict[str, str] | None) -> None:
            self._row = row

        def one_or_none(self) -> dict[str, str] | None:
            return self._row

    class _Result:
        def __init__(self, row: dict[str, str] | None) -> None:
            self._row = row

        def mappings(self) -> _Mappings:
            return _Mappings(self._row)

    class _Session:
        def __init__(self, row: dict[str, str] | None) -> None:
            self._row = row
            self.parameters: dict[str, str] = {}

        async def execute(
            self, _statement: object, parameters: dict[str, str]
        ) -> _Result:
            self.parameters = parameters
            return _Result(self._row)

    monkeypatch.setattr(mysql_memory, "_sha256", lambda _value: "a" * 64)
    free = _Session(None)
    digest = await mysql_memory._assert_projection_path_available(
        free,  # type: ignore[arg-type]
        witness_id="witness-new",
        projection_path="notes/new.md",
    )
    assert digest == "a" * 64
    assert free.parameters == {"projection_path_sha256": "a" * 64}

    collision = _Session(
        {"witness_id": "witness-old", "projection_path": "notes/old.md"}
    )
    with pytest.raises(
        mysql_memory.ImmutableMemoryRecordConflict,
        match="WitnessProjectionPathHashCollision",
    ):
        await mysql_memory._assert_projection_path_available(
            collision,  # type: ignore[arg-type]
            witness_id="witness-new",
            projection_path="notes/new.md",
        )

    same_witness_collision = _Session(
        {"witness_id": "witness-new", "projection_path": "notes/old.md"}
    )
    with pytest.raises(
        mysql_memory.ImmutableMemoryRecordConflict,
        match="WitnessProjectionPathHashCollision",
    ):
        await mysql_memory._assert_projection_path_available(
            same_witness_collision,  # type: ignore[arg-type]
            witness_id="witness-new",
            projection_path="notes/new.md",
        )


@pytest.mark.asyncio
async def test_mysql_immutable_insert_quotes_signal_identifier() -> None:
    class _Mappings:
        def one_or_none(self) -> None:
            return None

    class _Result:
        def mappings(self) -> _Mappings:
            return _Mappings()

    class _Session:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, statement: object, _parameters: object) -> _Result:
            self.statements.append(str(statement))
            return _Result()

    session = _Session()
    runtime = SimpleNamespace(
        enabled=True,
        backend=mysql_memory.BackendKind.MYSQL,
        engine=object(),
    )
    port = mysql_memory._MySQLPort(runtime)  # type: ignore[arg-type]
    inserted = await port._immutable_insert(
        session,  # type: ignore[arg-type]
        table="memory_corecall_events",
        identity_column="corecall_id",
        identity="corecall-1",
        values={
            "corecall_id": "corecall-1",
            "signal": "co-occurrence",
            "payload_sha256": "b" * 64,
        },
        payload_sha256="b" * 64,
    )

    assert inserted is True
    assert "`signal`" in session.statements[1]


@pytest.mark.asyncio
async def test_local_memory_bundle_satisfies_every_public_port() -> None:
    db = _database()
    try:
        bundle = create_local_memory_storage_bundle(lambda: db)

        assert isinstance(bundle.document_index, DocumentIndexProjection)
        assert isinstance(bundle.experiences, ExperienceLedgerStore)
        assert isinstance(bundle.witnesses, WitnessLedgerStore)
        assert isinstance(bundle.living, LivingMemoryStore)
        assert isinstance(bundle.epistemic, EpistemicMemoryStore)
        assert isinstance(bundle.legacy_graph, LegacyGraphStore)

        indexed = await bundle.document_index.upsert_document(
            "notes/contract.md",
            "可追溯的记忆正文",
            "contract",
        )
        replay = await bundle.document_index.upsert_document(
            "notes/contract.md",
            "可追溯的记忆正文",
            "contract",
        )
        assert indexed.chunks
        assert indexed.job_id
        assert replay.job_id == indexed.job_id
        assert len(await bundle.document_index.claim_jobs(limit=1)) == 1

        report = await bundle.experiences.append((_experience(),))
        replay_report = await bundle.experiences.append((_experience(),))
        assert report.inserted_count == 1
        assert replay_report.inserted_count == 0
        assert replay_report.existing[0].event_id == "event-1"

        witness = await bundle.witnesses.append(
            witness_id="witness-1",
            content="我见证了这一刻",
            consciousness_instance_id="core",
            perspective_subject_id="elysia",
            epistemic_kind="subjective_witness",
            source_kind="experience_window",
            stream_scope="qq:group:1",
            visibility="private",
            valid_from="2026-08-04T10:00:00+08:00",
            valid_to="2026-08-04T10:00:00+08:00",
            source_event_ids=("event-1",),
            source_sequence_start=1,
            source_sequence_end=1,
            projection_path="notes/witness-1.md",
        )
        assert witness.source_event_ids == ("event-1",)
        state = await bundle.witnesses.compare_and_advance_state(
            "core",
            expected_sequence=0,
            expected_revision=0,
            next_sequence=1,
        )
        assert (state["last_sequence"], state["revision"]) == (1, 1)
        with pytest.raises(CursorConflict):
            await bundle.witnesses.compare_and_advance_state(
                "core",
                expected_sequence=0,
                expected_revision=0,
                next_sequence=2,
            )

        first = new_artifact_version(
            logical_key="memory:self-view",
            artifact_kind="self_narrative",
            content="旧的理解",
        )
        await bundle.living.append_artifact(first, expected_head_revision=0)
        head = await bundle.living.get_artifact_head("memory:self-view")
        assert head is not None and head.revision == 1
        second = new_artifact_version(
            logical_key="memory:self-view",
            artifact_kind="self_narrative",
            content="新的理解",
            parent_artifact_ids=(first.artifact_id,),
        )
        with pytest.raises(ArtifactHeadConflict):
            await bundle.living.append_artifact(second, expected_head_revision=0)

        claim = MemoryClaim(
            claim_id="claim-1",
            subject_key="user:name",
            content="名字仍需保留来源",
            claim_kind="identity_claim",
            source="explicit_user",
            authority="explicit_user",
            valid_from="2026-08-04T10:00:00+08:00",
            valid_to="",
            recorded_at="2026-08-04T10:00:02+08:00",
        )
        assert await bundle.epistemic.append_claim(claim) == claim

        left = await bundle.legacy_graph.get_or_create_file_node(
            "notes/left.md",
            "left",
            "左侧记忆",
        )
        right = await bundle.legacy_graph.get_or_create_file_node(
            "notes/right.md",
            "right",
            "右侧记忆",
        )
        edge = await bundle.legacy_graph.create_or_update_edge(
            left.node_id,
            right.node_id,
            EdgeType.RELATES.value,
            strength=0.7,
        )
        assert edge.weight == pytest.approx(0.7)
        assert (await bundle.legacy_graph.get_edges_from(left.node_id))[
            0
        ].target_id == right.node_id
    finally:
        await asyncio.to_thread(db.close)


def test_mysql_memory_adapters_satisfy_new_pipeline_ports_structurally() -> None:
    experiences = object.__new__(mysql_memory.MySQLExperienceLedgerStore)
    witnesses = object.__new__(mysql_memory.MySQLWitnessLedgerStore)

    assert isinstance(experiences, ExperienceLedgerStore)
    assert isinstance(witnesses, WitnessLedgerStore)
