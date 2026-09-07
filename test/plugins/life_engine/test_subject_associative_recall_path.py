"""Synthetic, tool-driven two-hop reachability; never subject recall benefit.

The walker receives only an actual search result and subsequent tool results.
It has no target IDs, expected answers or semantic classifier. Its FIFO policy
tests reachability, not the subject's judgment of reasons or strength. All data
and services are temporary. No model, formal storage or external send is used.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.managed_documents import project_current_document
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.memory.tools import NucleusRelationsTool
from plugins.life_engine.service import registry as service_registry
from plugins.life_engine.storage.subject_contracts import AppendSubjectDocumentVersion
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from test.plugins.life_engine.test_minimal_subject_file_continuity import (
    _ACTOR_ID,
    _OCCURRED_AT,
    _STREAM_ID,
    _memory_plugin,
)
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store

pytestmark = pytest.mark.asyncio
QUERY = "s3isolatedindigoentry"
MAX_STEPS = 24
MAX_BYTES = 262144


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bind(plugin: Any, identity: str) -> NucleusRelationsTool:
    tool = NucleusRelationsTool(plugin=plugin)
    tool._bind_runtime_context(
        stream_id=_STREAM_ID,
        message=SimpleNamespace(
            stream_id=_STREAM_ID,
            message_id=f"synthetic:{identity}",
            time=_OCCURRED_AT,
            extra={},
        ),
        tool_call_id=f"synthetic:relation:{identity}",
    )
    tool._life_source_instance_id = _ACTOR_ID
    tool._life_source_occurrence_id = f"synthetic:{identity}"
    tool._life_source_occurred_at = _OCCURRED_AT
    tool._runtime_task_name = "life_chatter"
    return tool


def _record(
    trace: list[dict[str, Any]], action: str, result: Any, **extra: Any
) -> None:
    trace.append({"action": action, "bytes": len(_json(result).encode()), **extra})
    assert len(trace) <= MAX_STEPS
    assert sum(row["bytes"] for row in trace) <= MAX_BYTES


async def _view(
    plugin: Any,
    ref: str,
    trace: list[dict[str, Any]],
    *,
    current: bool = True,
    budget: int = 16384,
) -> dict[str, Any]:
    tool = _bind(plugin, "view")
    cursor = ""
    serialized = ""
    pages: list[dict[str, Any]] = []
    for _ in range(MAX_STEPS):
        ok, payload = await tool.execute(
            action="view",
            entity_ref=ref,
            current_only=current,
            max_bytes=budget,
            continuation=cursor,
        )
        assert ok, payload
        assert len(_json(payload).encode()) <= budget
        _record(trace, "view", payload, ref=ref)
        if payload.get("serialization") == "canonical-json-utf8":
            serialized += payload["content"]
            if payload["page_complete"]:
                assert (
                    hashlib.sha256(serialized.encode()).hexdigest()
                    == payload["content_sha256"]
                )
                pages.append(json.loads(serialized))
                serialized = ""
        else:
            pages.append(payload)
        cursor = payload.get("continuation", "")
        if not cursor:
            assert not serialized and pages
            merged = dict(pages[0])
            merged["semantic_relations"] = [
                r for p in pages for r in p["semantic_relations"]
            ]
            return merged
    raise AssertionError("relation read exceeded declared budget")


async def _read(
    plugin: Any, path: str, trace: list[dict[str, Any]], *, exact_ref: str = ""
) -> dict[str, Any]:
    selector = {"file_ref": exact_ref} if exact_ref else {"path": path}
    ok, payload = await LifeEngineReadFileTool(plugin=plugin).execute(
        **selector, limit=0, max_bytes=8192
    )
    assert ok, payload
    assert payload["source_authority"] == "subject_document_store"
    assert not payload.get("continuation")
    _record(
        trace,
        "read",
        payload,
        document_id=payload["document_id"],
        version_id=payload["subject_version_id"],
        sha256=payload["file_content_sha256"],
    )
    return payload


@asynccontextmanager
async def _material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, long_strength: bool = False
):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, outer, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = LifeMemoryService(
            tmp_path / "memory",
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await memory.initialize()
        outer._memory_service = memory
        monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: outer)
        docs: dict[str, Any] = {}
        bodies = {
            "entry": f"Synthetic entry only: {QUERY}. No ordinary links.\n",
            "bridge": "Synthetic transit note. Shared tag: silver.\n",
            "target": "Source: north depot. Voucher: MANGO-41.\n",
            "distractor": "Source: upland depot. Shared tag: silver. Voucher: MANGO-14.\n",
            "withdrawn": "Synthetic obsolete branch, preserved in history.\n",
        }
        try:
            for name, body in bodies.items():
                commit = await store.append_version(
                    AppendSubjectDocumentVersion(
                        logical_path=f"life_engine_workspace/notes/association-path/{name}.md",
                        expected_revision=0,
                        expected_head_version_id="",
                        content_bytes=body.encode(),
                        occurrence_id=f"synthetic-association-path:{name}",
                        recorded_by="synthetic-protocol-fixture",
                        recorded_source=__name__,
                        provenance_status="semantic_source_missing",
                        encoding="utf-8",
                        newline_style="lf",
                        change_context={
                            "synthetic": True,
                            "not_subject_authored": True,
                        },
                    )
                )
                docs[name] = commit.version
                await project_current_document(memory, commit.version.document_id)
            strengths = {
                "entry-bridge": "合成关系强弱原话；不是爱莉实际判断。"
                * (60 if long_strength else 1),
                "bridge-target": "合成：想进一步查来源时可沿此线索，不代表内容更真。",
                "entry-distractor": "合成：只是共同标签，需要区分来源。",
                "bridge-entry": "合成反向联系，测试环路预算。",
                "entry-withdrawn": "合成待撤回分支。",
            }
            relations: dict[str, dict[str, Any]] = {}
            for name, strength in strengths.items():
                source, target = name.split("-")
                ok, payload = await _bind(plugin, name).execute(
                    action="add",
                    source_ref="subject-file:" + docs[source].document_id,
                    target_ref="subject-file:" + docs[target].document_id,
                    relation_type="Synthetic navigation only",
                    reason="Synthetic protocol fixture, not a real subject judgment.",
                    subject_strength=strength,
                )
                assert ok, payload
                relations[name] = payload
            obsolete = relations["entry-withdrawn"]
            ok, closed = await _bind(plugin, "withdraw").execute(
                action="withdraw",
                root_relation_id=obsolete["root_relation_id"],
                parent_relation_id=obsolete["relation_id"],
                reason="Synthetic explicit withdrawal, keep history.",
            )
            assert ok, closed
            yield memory, plugin, outer, docs, strengths, relations
        finally:
            await memory.close()


async def _walk(
    plugin: Any, roots: list[str], trace: list[dict[str, Any]], *, follow: bool
) -> list[dict[str, Any]]:
    queue = deque(roots)
    visited: set[str] = set()
    reads: list[dict[str, Any]] = []
    while queue:
        ref = queue.popleft()
        if ref in visited:
            continue
        assert len(visited) < 8
        visited.add(ref)
        page = await _view(plugin, ref, trace)
        assert page["entity_ref"] == ref
        reads.append(
            await _read(
                plugin, page["file_path"], trace, exact_ref=ref if "@" in ref else ""
            )
        )
        if follow:
            # No predicate/strength parsing, expected target, or answer-aware stop.
            queue.extend(
                relation["counterpart_ref"] for relation in page["semantic_relations"]
            )
    return reads


@pytest.mark.parametrize("seed", (7, 29))
@pytest.mark.parametrize("arm", ("direct", "one_hop", "explicit_walk"))
async def test_relation_only_two_hop_path_exceeds_direct_and_one_hop_reachability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
    seed: int,
    arm: str,
) -> None:
    async with _material(tmp_path, monkeypatch) as (memory, plugin, _, docs, _, _):
        trace: list[dict[str, Any]] = []
        direct = await memory.search_memory(
            QUERY, top_k=1, enable_association=False, return_bundles=False
        )
        assert len(direct) == 1 and direct[0].document_id == docs["entry"].document_id
        _record(trace, "search", [asdict(item) for item in direct])
        candidates = direct
        if arm == "one_hop":
            candidates = await memory.expand_living_document_associations(
                direct,
                context_key="synthetic-association-path",
                random_seed=seed,
                limit=6,
                enable_semantic_relations=True,
                enable_corecall=False,
            )
            _record(trace, "one_hop", [asdict(item) for item in candidates])
        if arm == "explicit_walk":
            roots = ["subject-file:" + item.document_id for item in candidates]
            reads = await _walk(plugin, roots, trace, follow=True)
        else:
            # Search already returns authorized paths. Do not charge the direct
            # arms for an unnecessary relation view just to resolve that path,
            # or expose graph neighbours to the ordinary baseline controller.
            reads = [await _read(plugin, item.file_path, trace) for item in candidates]
        acquired = {item["document_id"]: item for item in reads}
        assert len(acquired) == len(reads), "cycle caused duplicate reads"
        assert docs["withdrawn"].document_id not in acquired
        for version in docs.values():
            if version.document_id in acquired:
                assert (
                    acquired[version.document_id]["file_content_sha256"]
                    == version.content_hash
                )
                assert (
                    acquired[version.document_id]["subject_version_id"]
                    == version.version_id
                )
        reached = docs["target"].document_id in acquired
        assert reached is (arm == "explicit_walk")
        expected_count = {"direct": 1, "one_hop": 3, "explicit_walk": 4}[arm]
        assert len(acquired) == expected_count
        record_property(
            "association_path_protocol",
            _json(
                {
                    "class": "synthetic_protocol_only",
                    "arm": arm,
                    "seed": seed,
                    "model_calls": 0,
                    "subject_benefit": None,
                    "target_reached": reached,
                    "read_documents": len(acquired),
                    "exact_source_identity_mismatch": 0,
                    "distractor_exposed": docs["distractor"].document_id in acquired,
                    "steps": len(trace),
                    "response_bytes": sum(item["bytes"] for item in trace),
                    "policy": "common exact reader/budget; optional relation walk; no expected answers or semantic selection",
                    "trace": trace,
                }
            ),
        )


async def test_relation_view_continuation_preserves_strength_and_withdrawn_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _material(tmp_path, monkeypatch, long_strength=True) as (
        _,
        plugin,
        _,
        docs,
        strengths,
        relations,
    ):
        ref = "subject-file:" + docs["entry"].document_id
        trace: list[dict[str, Any]] = []
        current = await _view(plugin, ref, trace, budget=4096)
        assert len(trace) > 1
        rows = {row["relation_id"]: row for row in current["semantic_relations"]}
        row = rows[relations["entry-bridge"]["relation_id"]]
        assert row["metadata"]["subject_strength"] == strengths["entry-bridge"]
        assert (
            row["reason"] == "Synthetic protocol fixture, not a real subject judgment."
        )
        assert row["actor"] == _ACTOR_ID and row["stream_scope"] == _STREAM_ID
        assert relations["entry-withdrawn"]["relation_id"] not in rows
        history = await _view(plugin, ref, [], current=False)
        assert relations["entry-withdrawn"]["relation_id"] in {
            r["relation_id"] for r in history["semantic_relations"]
        }
        assert any(r["operation"] == "withdraw" for r in history["semantic_relations"])


async def test_stable_relation_selector_is_not_a_fake_exact_file_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _material(tmp_path, monkeypatch) as (_, plugin, _, docs, _, _):
        stable = "subject-file:" + docs["bridge"].document_id
        ok, rejected = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=stable, limit=0
        )
        assert not ok, rejected
        page = await _view(plugin, stable, [])
        read = await _read(plugin, page["file_path"], [])
        assert read["document_id"] == docs["bridge"].document_id
        exact = stable + "@" + read["subject_version_id"]
        ok, pinned = await LifeEngineReadFileTool(plugin=plugin).execute(
            file_ref=exact, limit=0
        )
        assert ok, pinned
        assert pinned["file_content_sha256"] == docs["bridge"].content_hash


async def test_exact_relation_walk_does_not_float_to_a_new_current_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _material(tmp_path, monkeypatch) as (memory, plugin, _, docs, _, _):
        entry, target = docs["entry"], docs["target"]
        entry_ref = f"subject-file:{entry.document_id}@{entry.version_id}"
        target_ref = f"subject-file:{target.document_id}@{target.version_id}"
        ok, added = await _bind(plugin, "exact-edge").execute(
            action="add",
            source_ref=entry_ref,
            target_ref=target_ref,
            relation_type="Synthetic fixed historical relationship",
            reason="Fixture binds two exact historical bytes, not every future version.",
            subject_strength="Synthetic original wording, not a score.",
        )
        assert ok, added
        changed = await memory._subject_document_store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=target.logical_path,
                expected_revision=1,
                expected_head_version_id=target.version_id,
                content_bytes=b"Synthetic later update. Voucher: LEMON-99.\n",
                occurrence_id="synthetic-association-path:target-later",
                recorded_by="synthetic-protocol-fixture",
                recorded_source=__name__,
                provenance_status="semantic_source_missing",
                encoding="utf-8",
                newline_style="lf",
                change_context={"synthetic": True, "not_subject_authored": True},
            )
        )
        await project_current_document(memory, target.document_id)
        reads = await _walk(plugin, [entry_ref], [], follow=True)
        assert len(reads) == 2
        result = next(
            item for item in reads if item["document_id"] == target.document_id
        )
        assert result["subject_version_id"] == target.version_id
        assert result["file_content_sha256"] == target.content_hash
        assert result["subject_version_id"] != changed.version.version_id
        stable = await _view(plugin, "subject-file:" + entry.document_id, [])
        assert added["relation_id"] not in {
            r["relation_id"] for r in stable["semantic_relations"]
        }


async def test_two_hop_path_survives_real_memory_connection_close_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _material(tmp_path, monkeypatch) as (memory, plugin, outer, docs, _, _):
        root = "subject-file:" + docs["entry"].document_id
        before = await _walk(plugin, [root], [], follow=True)
        await memory.close()
        reopened = LifeMemoryService(
            tmp_path / "memory",
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=memory._subject_document_store,
            subject_document_store_required=True,
        )
        await reopened.initialize()
        outer._memory_service = reopened
        try:
            after = await _walk(plugin, [root], [], follow=True)
            identity = lambda row: (
                row["document_id"],
                row["subject_version_id"],
                row["file_content_sha256"],
            )
            assert [identity(row) for row in after] == [identity(row) for row in before]
            assert len(after) == 4
        finally:
            await reopened.close()
            outer._memory_service = memory
