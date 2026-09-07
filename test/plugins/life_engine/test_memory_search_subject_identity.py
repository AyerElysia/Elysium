"""Exact subject-file identities survive memory projection and delivery receipts."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.experience import EvidenceAwareMemoryResult
from plugins.life_engine.memory.lineage import MemoryBundle, MemoryEvidence, MemoryTrace
from plugins.life_engine.memory.recall_delivery import (
    get_memory_search_recall_delivery_coordinator,
)
from plugins.life_engine.memory.tools import LifeEngineSearchMemoryTool
from src.kernel.llm.payload import ToolResult
from test.plugins.life_engine.test_memory_search_recall_delivery import (
    _exact_receipt,
    _RecallStore,
    _tool,
)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    coordinator = get_memory_search_recall_delivery_coordinator()
    coordinator.reset_for_tests()
    instance = SimpleNamespace(instance_id="consciousness-alpha", is_active=True)
    runtime = SimpleNamespace(
        consciousness_registry=SimpleNamespace(
            get=lambda key: instance if key == instance.instance_id else None
        ),
        resolve_consciousness_instance=lambda stream: (
            "consciousness-alpha" if stream == "stream-alpha" else ""
        ),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service", lambda: runtime
    )
    yield
    coordinator.reset_for_tests()


def _evidence(index=0, *, document=None, version=None, content="shared snippet"):
    document = document or f"doc_{index}"
    version = version or f"ver_{index}"
    path = f"notes/{index}.md"
    file_ref = f"subject-file:{document}@{version}"
    return EvidenceAwareMemoryResult(
        record_id=f"subject-file:{document}",
        kind="document_evidence",
        content=content,
        rank_score=1.0 / (index + 1),
        confidence=None,
        source="document_search",
        provenance=(file_ref,),
        metadata={
            "file_path": path,
            "node_id": f"node_{index}",
            "document_id": document,
            "version_id": version,
            "document_revision": 2,
            "binding_revision": 3,
            "content_sha256": hashlib.sha256(
                f"exact full bytes {version}".encode()
            ).hexdigest(),
            "file_ref": file_ref,
        },
    )


def _assert_budget(payload):
    assert len(str(payload).encode()) <= payload["budget_bytes"]
    assert (
        len(json.dumps(payload, ensure_ascii=False).encode()) <= payload["budget_bytes"]
    )


async def _commit(payload):
    coordinator = get_memory_search_recall_delivery_coordinator()
    result = ToolResult(
        value=payload, call_id="identity-test", name="nucleus_search_memory"
    )
    text = result.to_text()
    delivery_id = payload["recall_delivery_binding"]["delivery_id"]
    coordinator.register_pending_tool_result(payload, text)
    assert await coordinator.commit_exact(
        delivery_id, _exact_receipt(text, delivery_id)
    )


async def test_exact_document_fields_reach_reader_and_only_receipted_recall(
    monkeypatch,
):
    evidence = _evidence()
    store = _RecallStore([evidence])
    tool = _tool(monkeypatch, store)
    ok, payload = await tool.execute("identity")
    assert ok, payload
    _assert_budget(payload)
    projected = payload["evidence_results"][0]
    assert projected["entity_ref"] == "subject-file:doc_0"
    assert "document:subject-file:" not in str(payload)
    for key, value in evidence.metadata.items():
        assert projected[key] == value
    assert not store.events and not store.episodes
    await _commit(payload)
    event = next(iter(store.events.values()))
    assert event.entity_ref == "subject-file:doc_0"
    assert event.source == evidence.source
    for key, value in evidence.metadata.items():
        assert event.metadata[key] == value
    assert event.metadata["exact_tool_result_delivered"] is True


async def test_legacy_record_keeps_its_existing_path_entity_reference(monkeypatch):
    evidence = EvidenceAwareMemoryResult(
        record_id="notes/legacy.md",
        kind="document_evidence",
        content="legacy snippet",
        rank_score=0.3,
        confidence=None,
        source="document_search",
    )
    store = _RecallStore([evidence])
    ok, payload = await _tool(monkeypatch, store).execute("legacy")
    assert ok, payload
    item = payload["evidence_results"][0]
    assert item["entity_ref"] == "document:notes/legacy.md"
    assert "document_id" not in item and "version_id" not in item
    await _commit(payload)
    event = next(iter(store.events.values()))
    assert event.entity_ref == "document:notes/legacy.md"
    assert "document_id" not in event.metadata


async def test_same_content_many_exact_links_page_without_losing_identities(
    monkeypatch,
):
    evidence = [_evidence(index) for index in range(30)]
    store = _RecallStore(evidence)
    tool = _tool(monkeypatch, store)
    continuation = ""
    delivered = {}
    frontiers = set()
    group_seen = False
    for _ in range(31):
        ok, payload = await tool.execute(
            "same-content", top_k=30, continuation=continuation
        )
        assert ok, payload
        _assert_budget(payload)
        frontiers.add(payload["frontier_sha256"])
        assert payload["original_items"] > 1
        for item in payload["canonical_items"]:
            group_seen |= item.get("link_group_count", 0) > 1
        for item in payload["evidence_results"]:
            assert item["entity_ref"] not in delivered
            delivered[item["entity_ref"]] = item
        await _commit(payload)
        continuation = payload["continuation"]
        if not continuation:
            break
    assert not continuation and group_seen and len(frontiers) == 1
    assert len(delivered) == len(evidence) == len(store.events)
    for item in evidence:
        projection = delivered[item.record_id]
        assert projection["file_ref"] == item.metadata["file_ref"]
    assert {event.entity_ref for event in store.events.values()} == set(delivered)


async def test_same_document_two_versions_keep_both_in_one_entity_recall(monkeypatch):
    evidence = [
        _evidence(0, document="doc_one", version="ver_old", content="old snippet"),
        _evidence(1, document="doc_one", version="ver_new", content="new snippet"),
    ]
    store = _RecallStore(evidence)
    ok, payload = await _tool(monkeypatch, store).execute("versions")
    assert ok, payload
    assert {item["version_id"] for item in payload["evidence_results"]} == {
        "ver_old",
        "ver_new",
    }
    await _commit(payload)
    assert len(store.events) == 1
    event = next(iter(store.events.values()))
    assert event.entity_ref == "subject-file:doc_one"
    assert {item["version_id"] for item in event.metadata["subject_file_versions"]} == {
        "ver_old",
        "ver_new",
    }


def _bundle(*, document="doc_one", version="ver_one", understanding="current"):
    fields = {
        "node_id": "node_one",
        "document_id": document,
        "version_id": version,
        "document_revision": 2,
        "binding_revision": 3,
        "content_sha256": "a" * 64,
        "file_ref": f"subject-file:{document}@{version}",
    }
    return MemoryBundle(
        query="bundle",
        current_understanding=understanding,
        primary_path="notes/same.md",
        primary_node_id="node_one",
        primary_document_id=document,
        primary_version_id=version,
        evidence=[
            MemoryEvidence(
                file_path="notes/same.md",
                title="evidence",
                snippet="evidence snippet",
                relation_reason="explicit relation",
                **fields,
            )
        ],
        history_trace=[
            MemoryTrace(
                relation="renames",
                file_path="archive/old.md",
                title="old",
                snippet="old snippet",
                reason="explicit history reason",
                **fields,
            )
        ],
    )


def test_bundle_and_all_child_links_keep_primary_and_exact_file_references():
    bundle = _bundle()
    records = LifeEngineSearchMemoryTool._projection_records([], [bundle])
    projected = [
        LifeEngineSearchMemoryTool._project_record(record, delivery="ref")
        for record in records
    ]
    _, direct, _, bundles = LifeEngineSearchMemoryTool._projection_indexes(projected)
    assert bundles[0]["primary_document_id"] == "doc_one"
    assert bundles[0]["primary_version_id"] == "ver_one"
    assert bundles[0]["primary_file_ref"] == "subject-file:doc_one@ver_one"
    assert direct[0]["file_ref"] == "subject-file:doc_one@ver_one"
    for record in projected:
        assert "content" not in record
        for link in record["links"]:
            assert link["primary_document_id"] == "doc_one"
            assert link["primary_version_id"] == "ver_one"
            if link["link_type"] != "bundle_current":
                assert (
                    link["document_id"] == "doc_one" and link["version_id"] == "ver_one"
                )


def test_bundle_identity_changes_on_path_reuse_and_version_change():
    def identity(bundle):
        records = LifeEngineSearchMemoryTool._projection_records([], [bundle])
        return records[0]["links"][0]["bundle_id"]

    original = identity(_bundle())
    assert original != identity(_bundle(document="doc_reused"))
    assert original != identity(_bundle(version="ver_changed"))
    assert original == identity(_bundle())


def test_bundle_page_without_current_text_keeps_primary_identity():
    bundle = _bundle(understanding="")
    records = LifeEngineSearchMemoryTool._projection_records([], [bundle])
    # Emulate a continuation containing only a history-reason record.
    history = next(
        record for record in records if record["content"] == "explicit history reason"
    )
    _, _, _, bundles = LifeEngineSearchMemoryTool._projection_indexes(
        [LifeEngineSearchMemoryTool._project_record(history, delivery="ref")]
    )
    assert bundles[0]["primary_path"] == "notes/same.md"
    assert bundles[0]["primary_document_id"] == "doc_one"
    assert bundles[0]["primary_version_id"] == "ver_one"


async def test_continuation_rejects_same_path_new_identity_even_with_same_snippet(
    monkeypatch,
):
    store = _RecallStore([_evidence(index) for index in range(30)])
    tool = _tool(monkeypatch, store)
    ok, first = await tool.execute("reuse", top_k=30)
    assert ok and first["continuation"]
    store.evidence[0] = _evidence(0, document="doc_reused", version="ver_reused")
    ok, rejected = await tool.execute(
        "reuse", top_k=30, continuation=first["continuation"]
    )
    assert not ok and "frontier changed" in rejected["error"]
    assert not store.events
