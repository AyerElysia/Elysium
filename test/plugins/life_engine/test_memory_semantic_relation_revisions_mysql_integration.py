"""Opt-in real MySQL v15-to-v16 relation ledger and two-connection CAS test.

Requires an explicitly isolated, fresh database on a non-default loopback port.
It never drops, truncates, or downgrades an existing database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.memory.living import SemanticRelation
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.memory import open_mysql_memory_storage
from plugins.life_engine.storage.memory.mysql import MySQLLivingMemoryStore
from plugins.life_engine.storage.memory.schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_MIGRATIONS,
    _semantic_relation_revision_completion_conditions,
)
from plugins.life_engine.storage.models import BackendKind
from src.kernel.storage import canonical_json
from src.kernel.storage.engine import create_mysql_storage_engine
from src.kernel.storage.migration_runner import (
    MigrationPostconditionError,
    MySQLMigrationRunner,
)
from test.plugins.life_engine.test_memory_storage_mysql_integration import (
    _generation,
    _mysql_config,
)


def _legacy_values(relation: SemanticRelation) -> dict:
    body = asdict(relation)
    for key in (
        "owner_subject_id",
        "root_relation_id",
        "parent_relation_id",
        "revision",
        "operation",
    ):
        del body[key]
    return {
        **{key: value for key, value in body.items() if key != "metadata"},
        "metadata_json": canonical_json(body["metadata"]),
        "source_ref_sha256": hashlib.sha256(relation.source_ref.encode()).hexdigest(),
        "target_ref_sha256": hashlib.sha256(relation.target_ref.encode()).hexdigest(),
        "payload_sha256": hashlib.sha256(canonical_json(body).encode()).hexdigest(),
    }


async def _insert_legacy(connection, relation: SemanticRelation) -> None:
    values = _legacy_values(relation)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    await connection.execute(
        text(
            f"INSERT INTO memory_semantic_relations ({columns}) VALUES ({placeholders})"
        ),
        values,
    )


async def _relation_completion_diagnostics(engine) -> dict:
    """Only synthetic relation-schema metadata; never connection credentials."""
    statements = {
        "columns": (
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "CHARACTER_SET_NAME, COLLATION_NAME, EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memory_semantic_relations' "
            "ORDER BY ORDINAL_POSITION"
        ),
        "indexes": (
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memory_semantic_relations' "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        ),
        "foreign_keys": (
            "SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION, "
            "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, f.UPDATE_RULE, f.DELETE_RULE "
            "FROM information_schema.KEY_COLUMN_USAGE k "
            "JOIN information_schema.REFERENTIAL_CONSTRAINTS f "
            "ON f.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
            "AND f.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
            "WHERE k.CONSTRAINT_SCHEMA = DATABASE() "
            "AND k.TABLE_NAME = 'memory_semantic_relations' "
            "ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
        ),
        "checks": (
            "SELECT c.CONSTRAINT_NAME, c.CHECK_CLAUSE, t.ENFORCED "
            "FROM information_schema.CHECK_CONSTRAINTS c "
            "JOIN information_schema.TABLE_CONSTRAINTS t "
            "ON t.CONSTRAINT_SCHEMA = c.CONSTRAINT_SCHEMA "
            "AND t.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
            "WHERE c.CONSTRAINT_SCHEMA = DATABASE() "
            "AND t.TABLE_NAME = 'memory_semantic_relations' ORDER BY c.CONSTRAINT_NAME"
        ),
        "engine": (
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memory_semantic_relations'"
        ),
    }
    result = {"conditions": {}, "metadata": {}}
    async with engine.connect() as connection:
        for (
            name,
            condition,
        ) in _semantic_relation_revision_completion_conditions().items():
            value = await connection.scalar(
                text(f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END")
            )
            result["conditions"][name] = int(value) == 1
        for name, statement in statements.items():
            result["metadata"][name] = [
                dict(row)
                for row in (await connection.execute(text(statement))).mappings()
            ]
    return result


class _ConnectionTraceStore(MySQLLivingMemoryStore):
    def __init__(self, runtime, connection_ids):
        super().__init__(runtime)
        self.connection_ids = connection_ids

    async def _write(self, operation):
        async def traced(session):
            self.connection_ids.add(
                int(await session.scalar(text("SELECT CONNECTION_ID()")))
            )
            return await operation(session)

        return await super()._write(traced)


@pytest.mark.timeout(180)
async def test_mysql_relation_revision_migration_cas_page_and_reopen() -> None:
    config = _mysql_config()
    if os.environ.get("ELYSIUM_TEST_MYSQL_RELATION_ISOLATED") != "1":
        pytest.skip("relation migration test requires its own fresh isolated database")
    if config.host not in {"127.0.0.1", "localhost"} or config.port == 3306:
        pytest.skip(
            "relation migration test requires a dedicated non-default loopback port"
        )

    engine = create_mysql_storage_engine(config)
    suffix = uuid4().hex
    registry_id = f"relation-integration-{suffix}"
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    generation = _generation()
    settings = StorageFactorySettings(
        enabled=True,
        authoritative_backend=BackendKind.MYSQL,
        backend_generation=generation.generation_id,
        schema_version=8,
        registry_id=registry_id,
        authority_provider="mysql",
        authority_owner_id=f"relation-writer-{suffix}",
        authority_lease_seconds=180,
        mysql=MySQLBackendSettings(
            host=config.host,
            port=config.port,
            database=config.database,
            user=config.user,
            password_env="TEST_RELATION_MYSQL_PASSWORD",
            ssl_mode=config.ssl_mode,
        ),
    )
    environment = {"TEST_RELATION_MYSQL_PASSWORD": config.password}
    runtime = None
    legacy = SemanticRelation(
        relation_id=f"legacy-{suffix}",
        source_ref=f"subject-file:legacy-source-{suffix}",
        target_ref=f"subject-file:legacy-target-{suffix}",
        predicate="synthetic legacy predicate",
        reason="  synthetic old reason  ",
        actor="legacy-window",
        recorded_at="2026-09-08T01:00:00+00:00",
        consciousness_instance_id="legacy-window",
        stream_scope="synthetic-stream",
        metadata={"case": "synthetic legacy v15"},
    )
    try:
        async with engine.connect() as connection:
            existing = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'life_memory_schema_migrations'"
                )
            )
            assert int(existing) == 0, (
                "test requires a fresh database; no downgrade is allowed"
            )
        await registry.register_generation(generation)
        runtime = await open_storage_backend(settings, environment=environment)
        runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_memory_schema_migrations",
            lock_name="elysium:life-memory-schema",
        )
        immutable_runner = MySQLMigrationRunner(
            runtime.engine,
            table_name="life_memory_immutability_schema_migrations",
            lock_name="elysium:life-memory-immutability",
        )
        assert (await runner.apply(MEMORY_MIGRATIONS[:15])).current_version == 15
        assert (
            await immutable_runner.apply(MEMORY_IMMUTABILITY_MIGRATIONS[:3])
        ).current_version == 3
        async with runtime.unit_of_work() as uow:
            await _insert_legacy(uow.session, legacy)
        async with runtime.engine.connect() as connection:
            before = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_semantic_relations WHERE relation_id = :identity"
                        ),
                        {"identity": legacy.relation_id},
                    )
                )
                .mappings()
                .one()
            )
            checksums_before = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT version, checksum FROM life_memory_schema_migrations ORDER BY version"
                        )
                    )
                ).all()
            )
            immutable_before = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT version, checksum FROM life_memory_immutability_schema_migrations "
                            "ORDER BY version"
                        )
                    )
                ).all()
            )

        try:
            stores = await open_mysql_memory_storage(runtime, initialize_schema=True)
        except MigrationPostconditionError:
            diagnostics = await _relation_completion_diagnostics(runtime.engine)
            print(
                "S3_RELATION_MYSQL_COMPLETION "
                + json.dumps(diagnostics, sort_keys=True)
            )
            raise
        async with runtime.engine.connect() as connection:
            after = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM memory_semantic_relations WHERE relation_id = :identity"
                        ),
                        {"identity": legacy.relation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert {key: after[key] for key in before} == before
            assert after["owner_subject_id"] is None
            assert after["root_relation_id"] is None
            assert after["parent_relation_id"] is None
            assert after["revision"] == 1 and after["operation"] == "add"
            assert (
                tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT version, checksum FROM life_memory_schema_migrations "
                                "WHERE version <= 15 ORDER BY version"
                            )
                        )
                    ).all()
                )
                == checksums_before
            )
            assert (
                tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT version, checksum FROM life_memory_immutability_schema_migrations "
                                "WHERE version <= 3 ORDER BY version"
                            )
                        )
                    ).all()
                )
                == immutable_before
            )
        assert await stores.living.get_relation(legacy.relation_id) == legacy
        assert await stores.living.append_relation(legacy) == legacy
        assert (await runner.apply(MEMORY_MIGRATIONS)).applied_versions == ()
        assert (
            await immutable_runner.apply(MEMORY_IMMUTABILITY_MIGRATIONS)
        ).applied_versions == ()

        identity = f"owned-root-{suffix}"
        root = replace(
            legacy,
            relation_id=identity,
            source_ref=f"subject-file:owned-source-{suffix}",
            target_ref=f"subject-file:owned-target-{suffix}",
            actor="window-one",
            consciousness_instance_id="window-one",
            owner_subject_id="elysia",
            root_relation_id=identity,
            metadata={"source_occurrence_id": identity},
        )
        assert await stores.living.append_relation(root) == root
        variants = tuple(
            replace(
                root,
                relation_id=f"child-{index}-{suffix}",
                parent_relation_id=root.relation_id,
                revision=2,
                operation="revise",
                predicate=f"synthetic reconsidered predicate {index}",
                reason=f"synthetic reconsideration {index}",
                actor=f"window-{index + 2}",
                consciousness_instance_id=f"window-{index + 2}",
                recorded_at="2026-09-08T02:00:00+00:00",
                metadata={"source_occurrence_id": f"revise-{index}-{suffix}"},
            )
            for index in range(2)
        )
        connection_ids: set[int] = set()
        ports = [_ConnectionTraceStore(runtime, connection_ids) for _ in range(2)]
        results = await asyncio.gather(
            *(
                port.append_relation(candidate)
                for port, candidate in zip(ports, variants)
            ),
            return_exceptions=True,
        )
        winners = [value for value in results if isinstance(value, SemanticRelation)]
        failures = [value for value in results if isinstance(value, Exception)]
        assert len(connection_ids) >= 2, (
            "CAS must be exercised by distinct MySQL connections"
        )
        assert len(winners) == len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert str(failures[0]) == "SemanticRelationStaleParent"
        winner = winners[0]
        assert winner.actor != root.actor
        assert await stores.living.list_relations(
            root.source_ref, current_only=True
        ) == [winner]
        for candidate in variants:
            if candidate != winner:
                assert await stores.living.get_relation(candidate.relation_id) is None

        page = await stores.living.page_relations(root.source_ref, limit=1)
        assert page.relations == (root,) and page.matching_count == 2
        assert page.frontier_count == 3 and page.current_relation_ids == ()
        assert page.has_more and page.next_offset == 1
        second = await stores.living.page_relations(
            root.source_ref,
            limit=1,
            offset=page.next_offset,
            expected_frontier_count=page.frontier_count,
        )
        assert second.relations == (winner,)
        assert second.current_relation_ids == (winner.relation_id,)
        assert not second.has_more and second.next_offset is None
        assert (
            await stores.living.page_relations(root.source_ref.upper())
        ).matching_count == 0
        assert (
            await stores.living.page_relations(root.source_ref + " ")
        ).matching_count == 0

        withdrawal = replace(
            winner,
            relation_id=f"withdraw-{suffix}",
            parent_relation_id=winner.relation_id,
            revision=3,
            operation="withdraw",
            reason="synthetic explicit withdrawal",
            recorded_at="2026-09-08T03:00:00+00:00",
            actor="window-four",
            consciousness_instance_id="window-four",
            metadata={"source_occurrence_id": f"withdraw-{suffix}"},
        )
        assert await stores.living.append_relation(withdrawal) == withdrawal
        with pytest.raises(RuntimeError, match="SemanticRelationPageFrontierConflict"):
            await stores.living.page_relations(
                root.source_ref,
                offset=1,
                expected_frontier_count=page.frontier_count,
            )
        assert await stores.living.list_relations(root.source_ref) == [
            root,
            winner,
            withdrawal,
        ]
        current = await stores.living.page_relations(root.source_ref, current_only=True)
        assert current.relations == () and current.matching_count == 0
        assert current.frontier_count == 4
        assert await stores.living.list_relations(
            legacy.source_ref, current_only=True
        ) == [legacy]

        for statement in (
            "UPDATE memory_semantic_relations SET revision = 99 WHERE relation_id = :identity",
            "UPDATE memory_semantic_relations SET owner_subject_id = 'other' WHERE relation_id = :identity",
            "DELETE FROM memory_semantic_relations WHERE relation_id = :identity",
        ):
            with pytest.raises(DBAPIError, match="MemoryAuthorityRecordImmutable"):
                async with runtime.unit_of_work() as uow:
                    await uow.session.execute(
                        text(statement), {"identity": root.relation_id}
                    )
        rollback = replace(legacy, relation_id=f"rollback-{suffix}")
        with pytest.raises(RuntimeError, match="SyntheticTransactionRollback"):
            async with runtime.unit_of_work() as uow:
                await _insert_legacy(uow.session, rollback)
                raise RuntimeError("SyntheticTransactionRollback")
        assert await stores.living.get_relation(rollback.relation_id) is None

        await runtime.revoke_authority()
        await runtime.close()
        runtime = None
        runtime = await open_storage_backend(settings, environment=environment)
        reopened = await open_mysql_memory_storage(runtime, initialize_schema=False)
        assert await reopened.living.get_relation(legacy.relation_id) == legacy
        assert await reopened.living.list_relations(root.target_ref) == [
            root,
            winner,
            withdrawal,
        ]
        assert (
            await reopened.living.list_relations(root.target_ref, current_only=True)
            == []
        )
        assert await reopened.living.append_relation(withdrawal) == withdrawal
        async with runtime.engine.connect() as connection:
            old_hash = await connection.scalar(
                text(
                    "SELECT payload_sha256 FROM memory_semantic_relations WHERE relation_id = :identity"
                ),
                {"identity": legacy.relation_id},
            )
            assert old_hash == before["payload_sha256"]
    finally:
        if runtime is not None:
            await runtime.revoke_authority()
            await runtime.close()
        await engine.dispose()
