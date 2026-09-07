"""S3 service-layer contracts; synthetic data, no model or formal runtime.

Current-history and managed-identity cases use real temporary SQLite ports.
Bounded-window/control cases use explicit fake pages and metadata to count
calls; they assert routing contracts, never subject recall benefit.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.memory import managed_documents
from plugins.life_engine.memory.living import (
    AssociationSelection,
    SemanticRelation,
    SemanticRelationPage,
)
from plugins.life_engine.memory.search import SearchResult
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.storage.subject_contracts import AppendSubjectDocumentVersion
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store

pytestmark = pytest.mark.asyncio
_CONTEXT = "synthetic-s3-service"
_SEED_REF = "document:notes/seed.md"


def _relation(
    identity: str = "synthetic-relation", *, target: str = "document:notes/target.md"
) -> SemanticRelation:
    return SemanticRelation(
        relation_id=identity,
        source_ref=_SEED_REF,
        target_ref=target,
        predicate="synthetic original route",
        reason="Synthetic explicit relation fixture.",
        actor="synthetic-instance",
        consciousness_instance_id="synthetic-instance",
        owner_subject_id="synthetic-subject",
        recorded_at="2026-09-08T00:00:00+00:00",
        stream_scope="test:s3-service",
        metadata={"synthetic": True},
    )


def _direct(path: str = "notes/seed.md") -> SearchResult:
    return SearchResult(
        file_path=path,
        title="Synthetic seed",
        snippet="seed",
        relevance=1.0,
        source="direct",
    )


async def _expand(memory: LifeMemoryService, direct: list[SearchResult], **kwargs: Any):
    options = {
        "context_key": _CONTEXT,
        "random_seed": 7,
        "limit": 4,
        "enable_semantic_relations": True,
        "enable_corecall": False,
    }
    options.update(kwargs)
    return await memory.expand_living_document_associations(direct, **options)


async def _index_legacy_file(
    memory: LifeMemoryService,
    workspace: Path,
    path: str,
    content: str,
    *,
    title: str,
) -> None:
    # Real legacy search requires both its FTS projection and an eligible file.
    # Only this test's temporary workspace is populated; managed authority is
    # deliberately not enrolled until the lifecycle case explicitly does so.
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    await memory.upsert_document(path, content, title=title)


async def test_real_sqlite_current_expansion_revises_and_withdraws_without_erasing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = LifeMemoryService(
        tmp_path, vector_backend_enabled=False, index_worker_enabled=False
    )
    await memory.initialize()
    try:
        await _index_legacy_file(
            memory, tmp_path, "notes/seed.md", "s3serviceneedle entry", title="Seed"
        )
        await _index_legacy_file(
            memory, tmp_path, "notes/target.md", "exact target body", title="Target"
        )
        assert await memory.fts_search("s3serviceneedle", top_k=1)
        direct = await memory.search_memory(
            "s3serviceneedle", top_k=1, enable_association=False, return_bundles=False
        )
        assert len(direct) == 1 and direct[0].file_path == "notes/seed.md"
        living = memory.living_memory_store
        history_reader = living.list_relations
        page_reader = AsyncMock(wraps=living.page_relations)
        forbidden_history = AsyncMock(
            side_effect=AssertionError("expansion loaded all history")
        )
        monkeypatch.setattr(living, "page_relations", page_reader)
        monkeypatch.setattr(living, "list_relations", forbidden_history)
        monkeypatch.setattr(
            living,
            "choose_association_neighbours",
            AsyncMock(side_effect=AssertionError("disabled co-recall was read")),
        )

        first = await memory.record_memory_semantic_relation(_relation())
        added = await _expand(memory, direct)
        assert len(added) == 2 and added[1].file_path == "notes/target.md"
        assert "synthetic original route" in added[1].association_reason
        assert added[1].score_kind == "accessibility_rank_not_truth"
        revised = await memory.record_memory_semantic_relation(
            replace(
                first,
                relation_id="synthetic-revision",
                parent_relation_id=first.relation_id,
                revision=2,
                operation="revise",
                predicate="synthetic revised route",
                reason="Synthetic revised explanation.",
                recorded_at="2026-09-08T00:00:01+00:00",
            )
        )
        changed = await _expand(memory, direct)
        assert len(changed) == 2
        assert "synthetic revised route" in changed[1].association_reason
        assert "synthetic original route" not in changed[1].association_reason
        withdrawn = await memory.record_memory_semantic_relation(
            replace(
                revised,
                relation_id="synthetic-withdrawal",
                parent_relation_id=revised.relation_id,
                revision=3,
                operation="withdraw",
                reason="Synthetic withdrawal.",
                recorded_at="2026-09-08T00:00:02+00:00",
            )
        )
        assert await _expand(memory, direct) == direct
        assert await history_reader(_SEED_REF) == [first, revised, withdrawn]
        assert await history_reader(_SEED_REF, current_only=True) == []
        assert page_reader.await_count == 3
        assert all(
            call.kwargs["current_only"] is True for call in page_reader.await_args_list
        )
        assert all(call.kwargs["limit"] <= 100 for call in page_reader.await_args_list)
        forbidden_history.assert_not_awaited()
    finally:
        await memory.close()


async def test_real_legacy_path_relation_does_not_adopt_new_managed_occupant(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, subject, _):
        memory = LifeMemoryService(
            tmp_path / "memory",
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=subject,
            subject_document_store_required=True,
        )
        await memory.initialize()
        try:
            await _index_legacy_file(
                memory,
                tmp_path / "memory",
                "notes/seed.md",
                "s3oldpathseed",
                title="Seed",
            )
            await _index_legacy_file(
                memory,
                tmp_path / "memory",
                "notes/reused.md",
                "legacy occupant body",
                title="Legacy",
            )
            assert await memory.fts_search("s3oldpathseed", top_k=1)
            legacy = await memory.record_memory_semantic_relation(
                replace(
                    _relation(target="document:notes/reused.md"),
                    owner_subject_id=None,
                )
            )
            direct = await memory.search_memory(
                "s3oldpathseed", top_k=1, enable_association=False, return_bundles=False
            )
            assert len(direct) == 1 and direct[0].file_path == "notes/seed.md"
            prior = await _expand(memory, direct)
            assert [item.file_path for item in prior] == [
                "notes/seed.md",
                "notes/reused.md",
            ]
            assert not prior[1].document_id
            new = await subject.append_version(
                AppendSubjectDocumentVersion(
                    logical_path="life_engine_workspace/notes/reused.md",
                    expected_revision=0,
                    expected_head_version_id="",
                    content_bytes=b"s3newmanagedoccupant\n",
                    occurrence_id="synthetic:new-managed-occupant",
                    recorded_by="synthetic-protocol-fixture",
                    recorded_source="test_s3_relation_service",
                    provenance_status="semantic_source_missing",
                    encoding="utf-8",
                    newline_style="lf",
                )
            )
            await managed_documents.project_current_document(
                memory, new.version.document_id
            )
            assert await _expand(memory, direct) == direct
            current = await memory.search_memory(
                "s3newmanagedoccupant",
                top_k=1,
                enable_association=False,
                return_bundles=False,
            )
            assert (
                len(current) == 1 and current[0].document_id == new.version.document_id
            )
            assert current[0].version_id == new.version.version_id
            assert await memory.living_memory_store.list_relations(_SEED_REF) == [
                legacy
            ]
        finally:
            await memory.close()


def _page(
    rows: tuple[SemanticRelation, ...], *, total: int, offset: int, frontier: int
) -> SemanticRelationPage:
    has_more = offset + len(rows) < total
    return SemanticRelationPage(
        relations=rows,
        frontier_count=frontier,
        offset=offset,
        next_offset=offset + len(rows) if has_more else None,
        has_more=has_more,
        matching_count=total,
        current_relation_ids=tuple(row.relation_id for row in rows),
    )


def _controlled_service(monkeypatch: pytest.MonkeyPatch, *, total: int = 1):
    """Synthetic port window; no database, model, source files or live singleton."""
    relations = tuple(
        _relation(
            f"synthetic-{index:04}", target=f"document:notes/target-{index:04}.md"
        )
        for index in range(total)
    )
    frontier = total + 1000  # Global rows also include unrelated synthetic entities.

    async def page(
        entity_ref,
        *,
        current_only=False,
        limit=50,
        offset=0,
        expected_frontier_count=None,
    ):
        assert current_only is True and 1 <= limit <= 100
        assert expected_frontier_count in (None, frontier)
        rows = tuple(
            replace(row, source_ref=entity_ref)
            for row in relations[offset : offset + limit]
        )
        return _page(rows, total=total, offset=offset, frontier=frontier)

    living = SimpleNamespace(
        page_relations=AsyncMock(side_effect=page),
        choose_association_neighbours=AsyncMock(return_value=[]),
        list_relations=AsyncMock(
            side_effect=AssertionError("unbounded history fallback")
        ),
    )
    memory = LifeMemoryService(
        None, vector_backend_enabled=False, index_worker_enabled=False
    )
    memory._memory_storage = SimpleNamespace(
        living=living,
        document_index=SimpleNamespace(
            get_snippet=AsyncMock(return_value="synthetic snippet")
        ),
    )

    def metadata(_memory, entity_ref):
        path = entity_ref.removeprefix("document:")
        return SimpleNamespace(
            file_path=path, node_id=f"synthetic:{path}", title=path, is_deleted=False
        )

    metadata_reader = AsyncMock(side_effect=metadata)
    monkeypatch.setattr(
        managed_documents, "association_document_metadata", metadata_reader
    )
    return memory, living, metadata_reader


@pytest.mark.parametrize(
    ("semantic", "corecall"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_disabled_association_channel_never_reads_its_port(
    monkeypatch: pytest.MonkeyPatch,
    semantic: bool,
    corecall: bool,
) -> None:
    memory, living, _ = _controlled_service(monkeypatch)
    living.choose_association_neighbours.return_value = [
        AssociationSelection(
            entity_ref="document:notes/corecall.md",
            signals=("synthetic co-recall",),
            event_count=1,
            last_event_at="2026-09-08T00:00:00+00:00",
        )
    ]
    if not semantic:
        living.page_relations.side_effect = AssertionError("disabled semantic port")
    if not corecall:
        living.choose_association_neighbours.side_effect = AssertionError(
            "disabled co-recall port"
        )
    direct = [_direct()]
    result = await _expand(
        memory, direct, enable_semantic_relations=semantic, enable_corecall=corecall
    )
    assert len(result) == 1 + int(semantic) + int(corecall)
    assert living.page_relations.await_count == int(semantic)
    assert living.choose_association_neighbours.await_count == int(corecall)
    living.list_relations.assert_not_awaited()


@pytest.mark.parametrize("limit", [0, -1, -100])
async def test_nonpositive_limit_does_not_visit_any_neighbour_port(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    memory, living, metadata = _controlled_service(monkeypatch)
    living.page_relations.side_effect = AssertionError("zero-budget semantic lookup")
    living.choose_association_neighbours.side_effect = AssertionError(
        "zero-budget co-recall lookup"
    )
    direct = [_direct()]
    assert await _expand(memory, direct, limit=limit, enable_corecall=True) == direct
    living.page_relations.assert_not_awaited()
    living.choose_association_neighbours.assert_not_awaited()
    living.list_relations.assert_not_awaited()
    metadata.assert_not_awaited()


async def test_large_result_budget_still_requests_at_most_100_relation_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, living, _ = _controlled_service(monkeypatch, total=257)
    result = await _expand(memory, [_direct()], limit=10000)
    assert 1 <= len(result) - 1 <= 100
    calls = living.page_relations.await_args_list
    assert 1 <= len(calls) <= 2
    assert all(call.kwargs["limit"] == 100 for call in calls)
    assert all(call.kwargs["current_only"] is True for call in calls)
    if len(calls) == 2:
        assert calls[1].kwargs["expected_frontier_count"] == 1257
    living.list_relations.assert_not_awaited()


def _seed_for_offset(total: int, wanted: int) -> int:
    # Select deterministic test inputs for the documented offset policy. This
    # search does not inspect results or retry an evaluation until it succeeds.
    for seed in range(10000):
        digest = hashlib.sha256(f"{_CONTEXT}\0{seed}\0{_SEED_REF}".encode()).digest()
        if int.from_bytes(digest[:8], "big") % total == wanted:
            return seed
    raise AssertionError("deterministic test vector not found")


async def test_seeded_window_replays_and_can_reach_the_last_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, living, _ = _controlled_service(monkeypatch, total=257)
    tail_seed = _seed_for_offset(257, 256)
    tail = await _expand(memory, [_direct()], random_seed=tail_seed, limit=3)
    calls = list(living.page_relations.await_args_list)
    assert [call.kwargs.get("offset", 0) for call in calls] == [0, 256]
    assert calls[0].kwargs["expected_frontier_count"] is None
    assert calls[1].kwargs["expected_frontier_count"] == 1257
    assert [item.file_path for item in tail[1:]] == ["notes/target-0256.md"]
    living.page_relations.reset_mock()
    replay = await _expand(memory, [_direct()], random_seed=tail_seed, limit=3)
    assert replay == tail and living.page_relations.await_args_list == calls
    beginning = await _expand(
        memory, [_direct()], random_seed=_seed_for_offset(257, 0), limit=3
    )
    assert {item.file_path for item in beginning[1:]} == {
        "notes/target-0000.md",
        "notes/target-0001.md",
        "notes/target-0002.md",
    }
    living.list_relations.assert_not_awaited()


@pytest.mark.parametrize("between", ["window_pages", "seed_pages"])
async def test_frontier_change_propagates_without_history_fallback(
    monkeypatch: pytest.MonkeyPatch,
    between: str,
) -> None:
    total = 257 if between == "window_pages" else 1
    memory, living, metadata = _controlled_service(monkeypatch, total=total)
    first_rows = tuple(
        _relation(f"frontier-{index}", target=f"document:notes/frontier-{index}.md")
        for index in range(min(total, 3))
    )
    first = _page(first_rows, total=total, offset=0, frontier=300)
    living.page_relations.side_effect = [
        first,
        RuntimeError("SemanticRelationPageFrontierConflict"),
    ]
    direct = [_direct()]
    seed = _seed_for_offset(257, 256)
    if between == "seed_pages":
        direct.append(_direct("notes/second-seed.md"))
    with pytest.raises(RuntimeError, match="SemanticRelationPageFrontierConflict"):
        await _expand(memory, direct, random_seed=seed, limit=3)
    assert living.page_relations.await_count == 2
    assert (
        living.page_relations.await_args_list[1].kwargs["expected_frontier_count"]
        == 300
    )
    living.list_relations.assert_not_awaited()
    metadata.assert_not_awaited()
