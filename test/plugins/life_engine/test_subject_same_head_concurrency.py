"""Bounded same-head CAS race using only two local synthetic-store adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from plugins.life_engine.storage.migration.subject_history import (
    capture_subject_history,
    verify_subject_history_bundle,
)
from plugins.life_engine.storage.subject_contracts import (
    AppendSubjectDocumentVersion,
    SubjectDocumentCommit,
    SubjectDocumentConflict,
    SubjectDocumentStorePort,
)
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from test.plugins.life_engine.test_subject_document_storage_contract import (
    _command,
    _local_store,
)


async def test_independent_local_adapters_same_existing_head_have_one_winner(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _):
        original_command = _command(
            path="life_engine_workspace/notes/synthetic-cas.bin",
            occurrence="synthetic:cas-original",
            content=b"\xef\xbb\xbfsynthetic original\r\n\x00\xff",
        )
        original = await store.append_version(original_command)
        peer = await open_subject_document_store(runtime)
        assert peer is not store
        before = verify_subject_history_bundle(await capture_subject_history(runtime))
        commands = [
            replace(
                original_command,
                expected_document_id=original.head.document_id,
                expected_revision=original.head.revision,
                expected_head_version_id=original.version.version_id,
                expected_binding_revision=original.head.binding_revision,
                occurrence_id=f"synthetic:cas-{label}",
                content_bytes=f"synthetic contender {label}\r\n".encode(),
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
            asyncio.create_task(update(adapter, command), name=f"local-cas-{index}")
            for index, (adapter, command) in enumerate(zip((store, peer), commands))
        ]
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
            outcomes = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=10
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
            original_command.occurrence_id, winner_command.occurrence_id,
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
