"""Opt-in S2 contracts against one explicitly confirmed, fresh local MySQL schema.

The operator owns the temporary server and creates a NEW empty database for
each run. This module does not create/drop databases, truncate tables, read
application configuration, start processes, or contact the default MySQL port.
Successful and failed runs retain their synthetic records for inspection.

Required environment:
  ELYSIUM_TEST_MYSQL_S2_ISOLATED=1
  ELYSIUM_TEST_MYSQL_HOST=127.0.0.1
  ELYSIUM_TEST_MYSQL_PORT=<explicit non-default port>
  ELYSIUM_TEST_MYSQL_DATABASE=elysium_s2_test_<8-40 lowercase letters/digits/_>
  ELYSIUM_TEST_MYSQL_S2_DATABASE_CONFIRM=<the exact same database name>
  ELYSIUM_TEST_MYSQL_USER=<dedicated test account>
  ELYSIUM_TEST_MYSQL_PASSWORD=<test account secret, never printed>

The upgrade/restore case additionally needs separate S2_UPGRADE and
S2_CANDIDATE DATABASE, DATABASE_CONFIRM, USER and PASSWORD variables under
the ELYSIUM_TEST_MYSQL_ prefix. Each role must name a different fresh schema.

Run only this file with -n 0 --no-cov after the lifecycle owner grants a test
window. An unconfigured opt-in is skipped; an opted-in ambiguous target fails.
This tests real MySQL DDL/DML/locks, not a mocked SQL or vector backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from plugins.life_engine.memory.indexing import DocumentIdentityConflict
from plugins.life_engine.memory.managed_documents import (
    index_readiness,
    project_current_document,
)
from plugins.life_engine.memory.nodes import generate_file_node_id
from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.memory import open_mysql_memory_storage
from plugins.life_engine.storage.memory.contracts import (
    ManagedDocumentIndexSnapshot,
    MemoryStorageBundle,
)
from plugins.life_engine.storage.memory.schema import (
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA_VERSION,
)
from plugins.life_engine.storage.migration.copy_authority import (
    MySQLCopyAuthorityRegistry,
    open_mysql_copy_runtime,
)
from plugins.life_engine.storage.migration.subject_history import (
    capture_subject_history,
    export_subject_history,
    import_subject_history,
    verify_subject_history_bundle,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentHead,
    SubjectDocumentMutation,
    SubjectDocumentMutationCommit,
    SubjectDocumentStorePort,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from plugins.life_engine.storage.subject_schema import (
    _MYSQL_SUBJECT_AUTHORITY,
    _MYSQL_SUBJECT_PROJECTION_LEASES,
    _MYSQL_SUBJECT_REFERENCES,
    _MYSQL_SUBJECT_SCHEMA,
    SUBJECT_SCHEMA_VERSION,
)
from src.kernel.storage import MySQLMigrationRunner
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    create_mysql_storage_engine,
)

pytestmark = pytest.mark.integration
_PREFIX = "life_engine_workspace/"
_Commit = SubjectDocumentCommit | SubjectDocumentMutationCommit


def _isolated_config(
    role: Literal["lifecycle", "upgrade", "candidate"] = "lifecycle",
) -> MySQLStorageConfig:
    """Reject unsafe targets before an engine or network connection exists."""
    enabled = os.environ.get("ELYSIUM_TEST_MYSQL_S2_ISOLATED", "")
    if not enabled:
        pytest.skip("S2 real MySQL contract was not explicitly enabled")
    if enabled != "1":
        raise ValueError("S2 isolated MySQL opt-in must equal 1")
    base = "ELYSIUM_TEST_MYSQL"
    if role != "lifecycle":
        base += "_S2_" + role.upper()
    database = os.environ.get(base + "_DATABASE", "")
    if not re.fullmatch(r"elysium_s2_test_[a-z0-9_]{8,40}", database):
        raise ValueError("S2 requires an explicitly named isolated test database")
    confirmation = (
        "ELYSIUM_TEST_MYSQL_S2_DATABASE_CONFIRM"
        if role == "lifecycle" else base + "_DATABASE_CONFIRM"
    )
    if os.environ.get(confirmation) != database:
        raise ValueError("S2 database confirmation does not match the target")
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    if host != "127.0.0.1":
        raise ValueError("S2 test server must bind explicit IPv4 loopback")
    port_text = os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "")
    if not port_text.isascii() or not port_text.isdigit():
        raise ValueError("S2 test port must be explicitly configured")
    port = int(port_text)
    if not 1024 <= port <= 65535 or port in {3306, 33060}:
        raise ValueError("S2 rejects default or invalid MySQL ports")
    user = os.environ.get(base + "_USER", "")
    if not user or user != user.strip():
        raise ValueError("S2 test account must be explicitly configured")
    return MySQLStorageConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=os.environ.get(base + "_PASSWORD", ""),
        ssl_mode="disabled",
        pool_size=4,
        max_overflow=0,
        application_query_timeout_seconds=15,
        innodb_lock_wait_timeout_seconds=10,
    )


async def _assert_empty_schema(engine: Any, database: str) -> None:
    """Never turn a target-name assertion into permission to reuse old data."""
    async with engine.connect() as connection:
        selected = await connection.scalar(text("SELECT DATABASE()"))
        if selected != database:
            raise ValueError("S2 server selected a different database")
        count = await connection.scalar(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        ))
        if int(count or 0) != 0:
            raise ValueError("S2 refuses a nonempty database; allocate a fresh schema")


@asynccontextmanager
async def _isolated_runtime(
    config: MySQLStorageConfig | None = None,
) -> AsyncIterator[StorageBackendRuntime]:
    """Open real authority only after proving the confirmed schema is empty."""
    config = config or _isolated_config()
    engine = create_mysql_storage_engine(config)
    runtime = None
    try:
        await _assert_empty_schema(engine, config.database)
        identity = uuid4().hex
        registry_id = f"s2-mysql-{identity}"
        now = datetime.now(UTC).isoformat()
        generation = BackendGeneration(
            generation_id=f"s2-mysql-generation-{identity}",
            backend=BackendKind.MYSQL,
            schema_version=1,
            source_snapshot_sha256="5" * 64,
            root_hashes={"synthetic-s2": "6" * 64},
            frontiers={"subject": 0, "memory": 0},
            created_at=now,
            verified_at=now,
            status=GenerationStatus.VERIFIED,
        )
        registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
        await registry.register_generation(generation)
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id=registry_id,
                authority_provider="mysql",
                authority_owner_id=f"s2-test-owner-{identity}",
                authority_lease_seconds=300,
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="S2_PRIVATE_TEST_PASSWORD",
                    ssl_mode=config.ssl_mode,
                    pool_size=4,
                    max_overflow=0,
                    query_timeout_seconds=15,
                    lock_wait_timeout_seconds=10,
                ),
            ),
            environment={"S2_PRIVATE_TEST_PASSWORD": config.password},
        )
        yield runtime
    finally:
        try:
            if runtime is not None:
                try:
                    await runtime.revoke_authority()
                finally:
                    await runtime.close()
        finally:
            await engine.dispose()


class _Bridge:
    """Supply only real selected ports to the production projection bridge."""

    def __init__(
        self, store: SubjectDocumentStorePort, memory: MemoryStorageBundle
    ) -> None:
        self._subject_document_store = store
        self._subject_document_store_required = True
        self.memory = memory

    def _require_memory_storage(self) -> MemoryStorageBundle:
        return self.memory


def _create(
    path: str, occurrence: str, content: bytes, *, binding: int = 0
) -> AppendSubjectDocumentVersion:
    return AppendSubjectDocumentVersion(
        logical_path=path,
        expected_revision=0,
        expected_head_version_id="",
        expected_document_id="",
        expected_binding_revision=binding,
        content_bytes=content,
        occurrence_id=occurrence,
        recorded_by="synthetic-s2-test",
        recorded_source="synthetic-s2-tool-call",
        declared_owner="synthetic-test-owner",
        provenance_status="semantic_source_missing",
        encoding="utf-8-sig",
        newline_style="crlf",
    )


def _mutate(
    head: SubjectDocumentHead,
    operation: Literal["rename", "delete", "copy"],
    occurrence: str,
    target: str = "",
) -> SubjectDocumentMutation:
    return SubjectDocumentMutation(
        operation=operation,
        logical_path=head.logical_path,
        expected_document_id=head.document_id,
        expected_revision=head.revision,
        expected_head_version_id=head.current_version_id,
        expected_binding_revision=head.binding_revision,
        occurrence_id=occurrence,
        recorded_by="synthetic-s2-test",
        recorded_source="synthetic-s2-tool-call",
        target_logical_path=target,
        semantic_actor_id="synthetic-s2-consciousness",
        semantic_source_id="synthetic-s2-tool-call",
        occurred_at="2026-09-07T00:00:00+00:00",
    )


def _snapshot(commit: _Commit) -> ManagedDocumentIndexSnapshot:
    head, version = commit.head, commit.version
    path = head.logical_path.removeprefix(_PREFIX)
    return ManagedDocumentIndexSnapshot(
        document_id=head.document_id,
        version_id=version.version_id,
        path=path,
        document_revision=head.revision,
        binding_revision=head.binding_revision,
        content_sha256=version.content_hash,
        content=None if head.deleted else version.content_bytes.decode(
            version.encoding or "utf-8"
        ),
        deleted=head.deleted,
        title=path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
    )


async def _assert_current(memory: MemoryStorageBundle, commit: _Commit) -> None:
    expected = _snapshot(commit)
    metadata = await memory.document_index.get_document_metadata(expected.path)
    assert metadata is not None and not metadata.is_deleted
    assert (
        metadata.node_id,
        metadata.subject_document_id,
        metadata.subject_version_id,
        metadata.subject_document_revision,
        metadata.subject_binding_revision,
        metadata.subject_content_sha256,
    ) == (
        "subject-file:" + expected.document_id,
        expected.document_id,
        expected.version_id,
        expected.document_revision,
        expected.binding_revision,
        expected.content_sha256,
    )


async def _assert_server_namespace_serialization(
    runtime: StorageBackendRuntime, bridge: _Bridge, current: _Commit, suffix: str
) -> SubjectDocumentMutationCommit:
    """A different adapter waits on MySQL while the index can commit inside it."""
    store = bridge._subject_document_store
    peer = await open_subject_document_store(runtime)
    command = _mutate(
        current.head, "rename", f"s2:fenced:{suffix}",
        _PREFIX + f"notes/{suffix}/fenced.md",
    )
    assert runtime.engine is not None
    submitted = asyncio.Event()
    waiter = None

    def observe(
        _connection: Any, _cursor: Any, statement: str,
        _parameters: Any, _context: Any, _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split())
        if (
            "SELECT version FROM subject_document_schema_migrations" in normalized
            and "FOR UPDATE" in normalized
        ):
            submitted.set()

    try:
        async with store.workspace_namespace_fence():
            event.listen(runtime.engine.sync_engine, "before_cursor_execute", observe)
            try:
                waiter = asyncio.create_task(
                    peer.mutate_document(command), name="s2_mysql_namespace_waiter"
                )
                await asyncio.wait_for(submitted.wait(), timeout=5)
                done, _ = await asyncio.wait({waiter}, timeout=0.15)
                assert not done, "independent subject writer bypassed namespace fence"
                # This is a second real transaction. A self-deadlock between the
                # authority/shared lock and subject mutex fails the bounded wait.
                result = await asyncio.wait_for(
                    bridge.memory.document_index.project_managed_document(
                        _snapshot(current)
                    ),
                    timeout=5,
                )
                assert result.idempotent_replay
                assert await store.get_document_head(current.head.document_id) == current.head
                assert not waiter.done()
            finally:
                event.remove(runtime.engine.sync_engine, "before_cursor_execute", observe)
        return await asyncio.wait_for(waiter, timeout=10)
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


async def _assert_same_existing_head_has_one_winner(
    runtime: StorageBackendRuntime,
    store: SubjectDocumentStorePort,
    original: _Commit,
    suffix: str,
) -> SubjectDocumentCommit:
    """Release independent adapters together and retain exactly one CAS update."""
    peer = await open_subject_document_store(runtime)
    assert peer is not store
    before = verify_subject_history_bundle(await capture_subject_history(runtime))
    commands = [
        replace(
            _create(
                original.head.logical_path, f"s2:cas-{label}:{suffix}",
                f"synthetic same-head contender {label}\r\n".encode(),
            ),
            expected_document_id=original.head.document_id,
            expected_revision=original.head.revision,
            expected_head_version_id=original.version.version_id,
            expected_binding_revision=original.head.binding_revision,
        )
        for label in ("a", "b")
    ]
    barrier = asyncio.Barrier(3)

    async def update(
        adapter: SubjectDocumentStorePort, command: AppendSubjectDocumentVersion
    ) -> SubjectDocumentCommit:
        await barrier.wait()
        return await adapter.append_version(command)

    tasks = [
        asyncio.create_task(update(adapter, command), name=f"mysql-cas-{index}")
        for index, (adapter, command) in enumerate(zip((store, peer), commands))
    ]
    try:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=15
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    winners = [
        (command, result)
        for command, result in zip(commands, outcomes)
        if isinstance(result, SubjectDocumentCommit)
    ]
    losers = [
        command
        for command, result in zip(commands, outcomes)
        if isinstance(result, SubjectDocumentConflict)
    ]
    assert len(winners) == len(losers) == 1, [
        type(result).__name__ for result in outcomes
    ]
    winner_command, winner = winners[0]
    loser_command = losers[0]
    assert winner.head.document_id == original.head.document_id
    assert winner.head.revision == original.head.revision + 1
    assert winner.head.binding_revision == original.head.binding_revision
    assert winner.version.parent_version_id == original.version.version_id
    assert winner.version.content_bytes == winner_command.content_bytes
    assert await peer.get_version(original.version.version_id) == original.version
    assert await peer.get_head(original.head.logical_path) == winner.head
    assert await store.get_document_operation(loser_command.occurrence_id) is None
    history = await peer.list_document_history(original.head.document_id)
    assert {version.occurrence_id for version in history} == {
        original.version.occurrence_id, winner_command.occurrence_id,
    }
    after = verify_subject_history_bundle(await capture_subject_history(runtime))
    appended_tables = {
        "subject_document_versions", "subject_document_head_events",
        "subject_projection_outbox", "subject_document_operations",
    }
    assert after.table_counts == {
        table: count + int(table in appended_tables)
        for table, count in before.table_counts.items()
    }
    for adapter in (store, peer):
        assert await adapter.append_version(winner_command) == winner
        with pytest.raises(SubjectDocumentConflict):
            await adapter.append_version(loser_command)
    replayed = verify_subject_history_bundle(await capture_subject_history(runtime))
    assert replayed.table_counts == after.table_counts
    assert replayed.table_roots == after.table_roots
    reopened = await open_subject_document_store(runtime)
    assert await reopened.get_version(original.version.version_id) == original.version
    assert await reopened.get_document_head(original.head.document_id) == winner.head
    assert await reopened.get_document_operation(loser_command.occurrence_id) is None
    return winner


@pytest.mark.timeout(180)
async def test_real_mysql_s2_lifecycle_identity_history_and_namespace_fence() -> None:
    """Exercise schema5/memory15 with synthetic bytes and no workspace disk."""
    async with _isolated_runtime() as runtime:
        store = await open_subject_document_store(runtime, initialize_schema=True)
        memory = await open_mysql_memory_storage(runtime, initialize_schema=True)
        bridge = _Bridge(store, memory)
        suffix = uuid4().hex
        original = _PREFIX + f"notes/{suffix}/original.md"
        renamed = _PREFIX + f"notes/{suffix}/renamed.md"
        copy_path = _PREFIX + f"notes/{suffix}/copy.md"
        relative = original.removeprefix(_PREFIX)
        legacy = await memory.document_index.upsert_document(
            relative, "synthetic legacy history", "legacy"
        )
        neighbor = await memory.document_index.upsert_document(
            f"notes/{suffix}/neighbor.md", "synthetic neighbor", "neighbor"
        )
        legacy_edge = await memory.legacy_graph.create_or_update_edge(
            legacy.node_id, neighbor.node_id, "relates",
            bidirectional=False, reason="synthetic legacy relation",
        )
        command = _create(
            original, f"s2:create:{suffix}", b"\xef\xbb\xbfs2original exact\r\n"
        )
        first = await store.append_version(command)
        assert await store.append_version(command) == first
        assert (await store.get_version(first.version.version_id)).content_bytes == command.content_bytes
        projected = await project_current_document(bridge, first.head.document_id)
        assert projected.node_id == "subject-file:" + first.head.document_id
        await _assert_current(memory, first)
        managed_edge = await memory.legacy_graph.create_or_update_edge(
            projected.node_id, neighbor.node_id, "relates",
            bidirectional=False, reason="synthetic managed relation",
        )

        update = replace(
            command,
            expected_document_id=first.head.document_id,
            expected_revision=first.head.revision,
            expected_head_version_id=first.version.version_id,
            expected_binding_revision=first.head.binding_revision,
            occurrence_id=f"s2:update:{suffix}",
            content_bytes=b"\xef\xbb\xbfs2revision exact revised\r\n",
        )
        revised = await store.append_version(update)
        assert revised.version.parent_version_id == first.version.version_id
        await project_current_document(bridge, revised.head.document_id)
        await _assert_current(memory, revised)
        with pytest.raises(SubjectDocumentConflict):
            await store.append_version(replace(update, occurrence_id=f"s2:stale:{suffix}"))
        with pytest.raises(SubjectDocumentConflict, match="identity"):
            await store.append_version(replace(update, content_bytes=b"different"))
        async with store.workspace_namespace_fence():
            with pytest.raises(DocumentIdentityConflict, match="stale"):
                await memory.document_index.project_managed_document(_snapshot(first))
            with pytest.raises(DocumentIdentityConflict, match="same managed revision"):
                await memory.document_index.project_managed_document(
                    replace(_snapshot(revised), content="different indexed body")
                )
        await _assert_current(memory, revised)

        copy_command = _mutate(revised.head, "copy", f"s2:copy:{suffix}", copy_path)
        copied = await store.mutate_document(copy_command)
        assert copied.head.document_id != revised.head.document_id
        assert copied.version.content_bytes == revised.version.content_bytes
        assert copied.version.content_hash == revised.version.content_hash
        assert copied.version.semantic_actor_id is None
        assert copied.version.semantic_source_id is None
        assert copied.version.occurred_at is None
        assert copied.version.provenance_status == "semantic_source_missing"
        assert copied.version.change_context["copied_from_version_id"] == revised.version.version_id
        assert copied.version.change_context["copy_actor_id"] == "synthetic-s2-consciousness"
        assert (await store.mutate_document(copy_command)).idempotent_replay
        await project_current_document(bridge, copied.head.document_id)
        await _assert_current(memory, copied)

        rename_command = _mutate(
            revised.head, "rename", f"s2:rename:{suffix}", renamed
        )
        moved = await store.mutate_document(rename_command)
        assert moved.head.document_id == revised.head.document_id
        assert moved.version == revised.version
        assert moved.version.logical_path == original
        assert await store.get_head(original) is None
        await project_current_document(bridge, moved.head.document_id)
        await _assert_current(memory, moved)
        assert await memory.document_index.get_document_metadata(relative) is None

        delete_command = _mutate(moved.head, "delete", f"s2:delete:{suffix}")
        deleted = await store.mutate_document(delete_command)
        assert deleted.head.deleted
        assert await store.get_head(renamed) is None
        await project_current_document(bridge, deleted.head.document_id)
        released = await store.get_path_binding(renamed)
        assert released is not None and released.document_id is None
        replacement = await store.append_version(_create(
            renamed, f"s2:reuse:{suffix}", b"s2replacement new occupant\r\n",
            binding=released.revision,
        ))
        assert replacement.head.document_id != deleted.head.document_id
        await project_current_document(bridge, replacement.head.document_id)
        await _assert_current(memory, replacement)

        assert await store.append_version(command) == first
        assert (await store.mutate_document(rename_command)).head == moved.head
        assert (await store.mutate_document(delete_command)).idempotent_replay
        assert await store.get_head(renamed) == replacement.head
        assert await store.get_document_head(deleted.head.document_id) == deleted.head
        old_history = await store.list_document_history(first.head.document_id)
        assert {item.version_id for item in old_history} == {
            first.version.version_id, revised.version.version_id,
        }
        assert await store.list_history(renamed) == [replacement.version]
        async with store.workspace_namespace_fence():
            replay = await memory.document_index.project_managed_document(_snapshot(deleted))
            assert replay.idempotent_replay and not replay.indexed
        await _assert_current(memory, replacement)

        views = await memory.legacy_graph.get_lineage_node_views(
            [legacy.node_id, projected.node_id, "subject-file:" + replacement.head.document_id]
        )
        assert views[legacy.node_id].is_deleted
        assert views[legacy.node_id].subject_document_id == ""
        assert views[legacy.node_id].snippet == "synthetic legacy history"
        assert views[projected.node_id].is_deleted
        assert views[projected.node_id].snippet == ""
        assert views[projected.node_id].subject_version_id == revised.version.version_id
        assert [item.edge_id for item in await memory.legacy_graph.get_edges_from(
            legacy.node_id
        )] == [legacy_edge.edge_id]
        assert [item.edge_id for item in await memory.legacy_graph.get_edges_from(
            projected.node_id
        )] == [managed_edge.edge_id]
        assert await memory.legacy_graph.get_edges_from(
            "subject-file:" + replacement.head.document_id
        ) == []
        detailed = await memory.document_index.search_detailed(
            "s2replacement", collection=None, chunk_collection=None, top_k=10,
            enable_association=False, file_types=None, time_range_days=0,
            now=None, workspace_path=None, emit_visual_event=None,
        )
        assert len(detailed.results) == 1
        hit = detailed.results[0]
        assert (hit.node_id, hit.document_id, hit.version_id, hit.content_sha256) == (
            "subject-file:" + replacement.head.document_id,
            replacement.head.document_id, replacement.version.version_id,
            replacement.version.content_hash,
        )
        assert (await index_readiness(bridge))["status"] == "ready"

        fenced = await _assert_server_namespace_serialization(
            runtime, bridge, replacement, suffix
        )
        assert fenced.head.document_id == replacement.head.document_id
        assert (await index_readiness(bridge))["status"] == "pending_rebuild"
        await project_current_document(bridge, fenced.head.document_id)
        await _assert_current(memory, fenced)
        assert (await index_readiness(bridge))["status"] == "ready"

        # Reopen both real adapters, reapply idempotent DDL, and inspect old
        # identities without path lookup or reinterpreting retained bytes.
        before = verify_subject_history_bundle(await capture_subject_history(runtime))
        reopened = await open_subject_document_store(runtime, initialize_schema=True)
        rebuilt_memory = await open_mysql_memory_storage(runtime, initialize_schema=True)
        after = verify_subject_history_bundle(await capture_subject_history(runtime))
        assert before.table_counts == after.table_counts
        assert before.table_roots == after.table_roots
        assert before.table_counts["subject_documents"] == 3
        assert before.table_counts["subject_document_versions"] == 4
        assert (await reopened.get_version(first.version.version_id)).content_bytes == command.content_bytes
        assert await reopened.get_document_head(first.head.document_id) == deleted.head
        await _assert_current(rebuilt_memory, fenced)
        retained = await rebuilt_memory.legacy_graph.get_node_by_id(projected.node_id)
        assert retained is not None and retained.is_deleted
        assert retained.subject_document_id == first.head.document_id
        assert retained.subject_version_id == revised.version.version_id
        assert runtime.engine is not None
        async with runtime.engine.connect() as connection:
            assert await connection.scalar(text(
                "SELECT MAX(version) FROM subject_document_schema_migrations"
            )) == SUBJECT_SCHEMA_VERSION == 5
            assert await connection.scalar(text(
                "SELECT MAX(version) FROM life_memory_schema_migrations"
            )) == MEMORY_SCHEMA_VERSION == 15
            assert await connection.scalar(text(
                "SELECT COUNT(*) FROM memory_chunks WHERE node_id = :node_id"
            ), {"node_id": legacy.node_id}) > 0

        raced = await _assert_same_existing_head_has_one_winner(
            runtime, reopened, fenced, suffix
        )
        assert (await index_readiness(bridge))["status"] == "pending_rebuild"
        await project_current_document(bridge, raced.head.document_id)
        await _assert_current(rebuilt_memory, raced)
        assert (await index_readiness(bridge))["status"] == "ready"


async def _seed_mysql_v4_v14(runtime: StorageBackendRuntime) -> tuple[str, bytes, str]:
    """Build historical shapes by forward migrations, never by rollback/drop."""
    assert runtime.engine is not None
    await MySQLMigrationRunner(
        runtime.engine,
        table_name="subject_document_schema_migrations",
        lock_name="elysium:subject-document-schema",
    ).apply((
        _MYSQL_SUBJECT_SCHEMA, _MYSQL_SUBJECT_REFERENCES,
        _MYSQL_SUBJECT_PROJECTION_LEASES, _MYSQL_SUBJECT_AUTHORITY,
    ))
    await MySQLMigrationRunner(
        runtime.engine,
        table_name="life_memory_schema_migrations",
        lock_name="elysium:life-memory-schema",
    ).apply(MEMORY_MIGRATIONS[:14])
    path = _PREFIX + "notes/upgrade-old.bin"
    raw = b"\xef\xbb\xbfsynthetic old\r\n\x00\xff"
    memory_path = "notes/upgrade-retired.md"
    node_id = generate_file_node_id(memory_path)
    params = {
        "path": path,
        "raw": raw,
        "hash": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "time": datetime(2026, 8, 1, 1, 2, 3, 456789, tzinfo=UTC).replace(tzinfo=None),
        "node": node_id,
        "memory_path": memory_path,
        "path_hash": hashlib.sha256(memory_path.encode()).hexdigest(),
        "memory_hash": hashlib.sha256(b"synthetic legacy index\r\n").hexdigest(),
        "memory_body": "synthetic legacy index\r\n",
        "zeros": "0" * 64,
        "ones": "1" * 64,
    }
    statements = (
        """INSERT INTO subject_documents
            (document_id, logical_path, declared_owner, current_version_id, revision)
            VALUES ('s2-old-doc', :path, NULL, 's2-old-version', 1)""",
        """INSERT INTO subject_document_versions
            (version_id, document_id, logical_path, parent_version_id, occurrence_id,
             semantic_actor_id, semantic_source_id, occurred_at, recorded_by,
             recorded_source, recorded_at, provenance_status, content_bytes,
             content_hash, byte_length, byte_fidelity, encoding, newline_style,
             change_context_json)
            VALUES ('s2-old-version', 's2-old-doc', :path, '', 's2-old-write',
             NULL, NULL, NULL, 'synthetic-old-recorder', 'synthetic-old-import',
             :time, 'semantic_source_missing', :raw, :hash, :size, 'exact_bytes',
             NULL, NULL, '{}')""",
        """INSERT INTO subject_document_head_events
            (head_event_id, document_id, previous_version_id, next_version_id,
             occurrence_id, actor_id, source_id, occurred_at, authority_epoch,
             change_context_json)
            VALUES ('s2-old-head', 's2-old-doc', '', 's2-old-version',
             's2-old-write', 'synthetic-old-recorder', 'synthetic-old-import',
             :time, 1, '{}')""",
        """INSERT INTO subject_projection_outbox
            (head_event_id, document_id, logical_path, version_id, content_hash,
             state, attempt_count, created_at, confirmed_at, last_error,
             lease_owner, lease_until, revision)
            VALUES ('s2-old-head', 's2-old-doc', :path, 's2-old-version', :hash,
             'failed', 2, :time, NULL, 'synthetic old projection failure',
             'synthetic-expired-owner', :time, 3)""",
        """INSERT INTO subject_authority_decisions
            (decision_occurrence_id, authority_occurrence_id, candidate_id,
             candidate_revision, candidate_sha256, candidate_occurrence_id,
             actor_consciousness_instance_id, expected_subject_revision, target_path,
             accepted_content_sha256, occurred_at, previous_subject_revision,
             new_subject_revision, document_version_id, document_revision,
             command_sha256, committed_at)
            VALUES ('s2-old-decision', 's2-old-authority', 'synthetic-old-candidate',
             1, :ones, 's2-old-candidate-occurrence', 'synthetic-old-consciousness',
             :zeros, 'memory', :hash, :time, :zeros, :ones,
             's2-old-version', 1, :ones, :time)""",
        """INSERT INTO memory_nodes
            (node_id, node_type, file_path, file_path_sha256, content_hash,
             document_content, title, created_at, updated_at, is_deleted,
             legacy_fts_present)
            VALUES (:node, 'file', :memory_path, :path_hash, :memory_hash,
             :memory_body, 'synthetic retired title', 1000.0, 1001.0, TRUE, FALSE)""",
        """INSERT INTO memory_chunks
            (chunk_id, node_id, chunk_index, content_hash, content, title,
             created_at, updated_at)
            VALUES ('s2-old-chunk', :node, 0, :memory_hash, :memory_body,
             'synthetic retired title', 1000.0, 1001.0)""",
    )
    async with runtime.unit_of_work() as uow:
        for statement in statements:
            await uow.session.execute(text(statement), params)
    async with runtime.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT MAX(version) FROM subject_document_schema_migrations"
        )) == 4
        assert await connection.scalar(text(
            "SELECT MAX(version) FROM life_memory_schema_migrations"
        )) == 14
        assert await connection.scalar(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND "
            "((table_name = 'subject_documents' AND column_name = 'binding_revision') "
            "OR (table_name = 'memory_nodes' AND column_name = 'subject_document_id'))"
        )) == 0
    return path, raw, node_id


async def _legacy_mysql_evidence(runtime: StorageBackendRuntime) -> dict[str, Any]:
    """Compare every old authority field and retained index body after upgrade."""
    queries = {
        "document": (
            "SELECT document_id, logical_path, declared_owner, current_version_id, "
            "revision FROM subject_documents"
        ),
        "version": "SELECT * FROM subject_document_versions",
        "head_event": "SELECT * FROM subject_document_head_events",
        "decision": "SELECT * FROM subject_authority_decisions",
        "outbox": (
            "SELECT outbox_id, head_event_id, document_id, logical_path, version_id, "
            "content_hash, state, attempt_count, created_at, confirmed_at, last_error, "
            "lease_owner, lease_until, revision FROM subject_projection_outbox"
        ),
        "node": (
            "SELECT node_id, node_type, file_path, content_hash, document_content, "
            "title, created_at, updated_at, is_deleted, legacy_fts_present "
            "FROM memory_nodes"
        ),
        "chunk": "SELECT * FROM memory_chunks",
    }
    assert runtime.engine is not None
    async with runtime.engine.connect() as connection:
        return {
            name: [dict(row) for row in (
                await connection.execute(text(query))
            ).mappings()]
            for name, query in queries.items()
        }


@pytest.mark.timeout(180)
async def test_real_mysql_s2_forward_upgrade_and_full_history_candidate_restore(
    tmp_path: Path,
) -> None:
    """Upgrade old shapes, then restore all eight tables under real copy fencing."""
    source_config = _isolated_config("upgrade")
    target_config = _isolated_config("candidate")
    if source_config.database == target_config.database:
        raise ValueError("S2 upgrade source and candidate must be separate new schemas")
    directory = tmp_path / "synthetic-subject-history"
    async with _isolated_runtime(source_config) as source:
        old_path, raw, old_node_id = await _seed_mysql_v4_v14(source)
        before = await _legacy_mysql_evidence(source)
        store = await open_subject_document_store(source, initialize_schema=True)
        memory = await open_mysql_memory_storage(source, initialize_schema=True)
        assert await _legacy_mysql_evidence(source) == before
        head = await store.get_head(old_path)
        assert head is not None and head.document_id == "s2-old-doc"
        assert head.current_version_id == "s2-old-version"
        assert head.binding_revision == 1 and not head.deleted
        legacy_task = await store.get_projection_task(old_path, "s2-old-version")
        assert legacy_task is not None and legacy_task.binding_revision == 1
        # Reproduce an already-recorded original v5 upgrade. The metadata-only
        # repair must run even though the migration runner correctly skips v5.
        async with source.unit_of_work() as uow:
            await uow.session.execute(text(
                "UPDATE subject_projection_outbox SET binding_revision = 0 "
                "WHERE head_event_id = 's2-old-head'"
            ))
        await open_subject_document_store(source, initialize_schema=True)
        repaired_task = await store.get_projection_task(old_path, "s2-old-version")
        assert repaired_task == legacy_task
        assert await _legacy_mysql_evidence(source) == before
        old_version = await store.get_version("s2-old-version")
        assert old_version.content_bytes == raw
        assert old_version.semantic_actor_id is None
        assert old_version.semantic_source_id is None
        assert old_version.occurred_at is None
        assert old_version.encoding is None and old_version.newline_style is None
        old_node = await memory.legacy_graph.get_node_by_id(old_node_id)
        assert old_node is not None and old_node.is_deleted
        assert old_node.subject_document_id == ""
        assert source.engine is not None
        async with source.engine.connect() as connection:
            assert await connection.scalar(text(
                "SELECT file_path_sha256 FROM memory_nodes WHERE node_id = :node"
            ), {"node": old_node_id}) is None

        copied = await store.mutate_document(_mutate(
            head, "copy", "s2-upgrade-copy", _PREFIX + "notes/upgrade-copy.bin"
        ))
        moved = await store.mutate_document(_mutate(
            head, "rename", "s2-upgrade-rename", _PREFIX + "notes/upgrade-moved.bin"
        ))
        deleted = await store.mutate_document(_mutate(
            moved.head, "delete", "s2-upgrade-delete"
        ))
        binding = await store.get_path_binding(moved.head.logical_path)
        assert binding is not None and binding.document_id is None
        replacement = await store.append_version(_create(
            moved.head.logical_path, "s2-upgrade-reuse", b"synthetic replacement\r\n",
            binding=binding.revision,
        ))
        exported = await export_subject_history(source, directory)
        assert len(exported.table_counts) == 8
        assert all(count > 0 for count in exported.table_counts.values())
        assert exported.table_counts["subject_documents"] == 3
        assert exported.table_counts["subject_document_versions"] == 3

    # The source writer is now revoked and closed. The candidate can never
    # acquire active authority through this independent copy-control lease.
    target_engine = create_mysql_storage_engine(target_config)
    candidate = None
    token = None
    registry = MySQLCopyAuthorityRegistry(target_engine)
    try:
        await _assert_empty_schema(target_engine, target_config.database)
        run_id = "s2-full-history-" + uuid4().hex
        await registry.create_run(
            run_id=run_id,
            source_manifest_sha256=exported.bundle_sha256,
            source_snapshot_sha256=exported.bundle_sha256,
            writer_frozen=False,
            metadata={"scope": "synthetic-subject-eight-tables", "generation_eligible": False},
        )
        token = await registry.acquire(
            run_id, expected_epoch=0, owner_id="s2-full-history-copy", lease_seconds=300
        )
        candidate = open_mysql_copy_runtime(
            registry, token, backend_identity=target_config.safe_identity
        )
        await open_subject_document_store(candidate, initialize_schema=True)
        imported = await import_subject_history(directory, candidate)
        assert not imported.idempotent_replay
        assert imported.table_counts == exported.table_counts
        assert imported.table_roots == exported.table_roots
        replay = await import_subject_history(directory, candidate)
        assert replay.idempotent_replay
        assert replay.bundle_sha256 == exported.bundle_sha256

        # A new connection pool, registry and adapter must see the same lease
        # and exact rows; no state is recovered from an adapter-local cache.
        await candidate.close()
        candidate = None
        target_engine = create_mysql_storage_engine(target_config)
        registry = MySQLCopyAuthorityRegistry(target_engine)
        candidate = open_mysql_copy_runtime(
            registry, token, backend_identity=target_config.safe_identity
        )
        reopened = await open_subject_document_store(candidate, initialize_schema=True)
        assert (await import_subject_history(directory, candidate)).idempotent_replay
        restored = verify_subject_history_bundle(await capture_subject_history(candidate))
        assert restored.table_counts == exported.table_counts
        assert restored.table_roots == exported.table_roots
        assert await reopened.get_version("s2-old-version") == old_version
        assert (await reopened.get_version(copied.version.version_id)).content_bytes == raw
        assert await reopened.get_document_head("s2-old-doc") == deleted.head
        assert await reopened.get_head(moved.head.logical_path) == replacement.head
        assert candidate.writer_role == StorageWriterRole.CANDIDATE_COPY
        assert candidate.authority_registry is None and candidate.authority_token is None
        assert candidate.generation is None
        async with target_engine.connect() as connection:
            outbox = (await connection.execute(text(
                "SELECT state, attempt_count, lease_owner, lease_until, revision, "
                "binding_revision "
                "FROM subject_projection_outbox WHERE head_event_id = 's2-old-head'"
            ))).one()
            assert tuple(outbox) == (
                "failed", 2, "synthetic-expired-owner",
                datetime(2026, 8, 1, 1, 2, 3, 456789, tzinfo=UTC).replace(tzinfo=None),
                3, 1,
            )
            assert await connection.scalar(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'storage_authority_registry'"
            )) == 0
        completed = await registry.complete(token, verification={
            "verified": True, "generation_eligible": False,
            "table_counts": restored.table_counts, "table_roots": restored.table_roots,
        })
        token = None
        assert completed["state"] == "copied"
    except BaseException as error:
        if token is not None:
            try:
                await registry.fail(token, reason="S2 synthetic restore failed: " + type(error).__name__)
            except Exception as cleanup_error:  # noqa: BLE001 - preserve original test failure
                error.add_note("Copy lease cleanup failed: " + type(cleanup_error).__name__)
        raise
    finally:
        if candidate is not None:
            await candidate.close()
        await target_engine.dispose()
