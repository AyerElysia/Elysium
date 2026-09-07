"""Reserved synthetic S3 protocol cases, never subject recall-benefit evidence.

Every parameter gets new temporary subject authority, Memory SQLite storage and
delivery coordinators. The common controller follows ordinary exact links in
all arms and never receives expected answers. Only the two association flags
change. The fixture author knows the expected bytes: these are held-back
protocol cases, not a blind model/subject evaluation. No model is invoked.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest

from plugins.life_engine.memory import boundary_resolver, recall_delivery
from plugins.life_engine.memory.boundary import (
    MemoryBoundaryManifest,
    MemoryBoundaryRepository,
    MemoryBoundarySegment,
)
from plugins.life_engine.memory.living import CoRecallEvent, SemanticRelation
from plugins.life_engine.memory.managed_documents import (
    project_current_document,
    result_identity,
)
from plugins.life_engine.memory.service import LifeMemoryService
from plugins.life_engine.storage.subject_contracts import AppendSubjectDocumentVersion
from plugins.life_engine.tools.file_tools import LifeEngineReadFileTool
from test.plugins.life_engine.test_memory_boundary_tools import (
    _install_runtime,
)
from test.plugins.life_engine.test_memory_boundary_tools import (
    _tool as _boundary_tool,
)
from test.plugins.life_engine.test_minimal_subject_file_continuity import _memory_plugin
from test.plugins.life_engine.test_subject_document_storage_contract import _local_store

CASES = ("multilayer", "past_current", "shared_sources")
SEEDS = (7, 29)
ARMS = {
    "A_index": (False, False),
    "B_semantic": (True, False),
    "C_corecall": (False, True),
}
MAX_STEPS = 12
MAX_RESULT_BYTES = 8192
MAX_TOTAL_BYTES = 65536
_TIME = "2026-09-08T00:00:00+00:00"
_EXACT_REF = re.compile(r"subject-file:[A-Za-z0-9_-]+@[A-Za-z0-9_-]+")
_QUERIES = {
    "multilayer": "s3needleuxred",
    "past_current": "s3needleviolet",
    "shared_sources": "s3needlesilver",
}
_QUESTIONS = {
    "multilayer": "Follow the layered index to the north archive voucher.",
    "past_current": "Which box was recorded before and after the revision? Cite both versions.",
    "shared_sources": "Which silver-tag voucher belongs to the harbor source, not the upland source?",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


@dataclass
class _Material:
    case: str
    versions: dict[str, dict[str, Any]]
    source_refs: tuple[str, ...]
    required_refs: tuple[str, ...]
    seed_ref: str
    context_key: str
    manifest_sha256: str
    boundary_uri: str
    boundary_repository: MemoryBoundaryRepository


@pytest.fixture(autouse=True)
def _private_delivery_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace, rather than clear, the process-local objects owned by other tests.
    monkeypatch.setattr(
        boundary_resolver,
        "_RECALL_COORDINATOR",
        boundary_resolver.MemoryBoundaryRecallCoordinator(),
    )
    monkeypatch.setattr(
        recall_delivery,
        "_SEARCH_RECALL_COORDINATOR",
        recall_delivery.MemorySearchRecallDeliveryCoordinator(),
    )


async def _prepare(store: Any, memory: LifeMemoryService, case: str) -> _Material:
    versions: dict[str, dict[str, Any]] = {}

    async def put(name: str, text: str, prior: Any = None) -> Any:
        revision = prior.head.revision if prior else 0
        commit = await store.append_version(
            AppendSubjectDocumentVersion(
                logical_path=f"life_engine_workspace/notes/s3/{case}/{name}.md",
                expected_revision=revision,
                expected_head_version_id=prior.version.version_id if prior else "",
                content_bytes=text.encode("utf-8"),
                occurrence_id=f"synthetic-s3:{case}:{name}:{revision + 1}",
                recorded_by="synthetic-protocol-fixture",
                recorded_source="test_s3_association_controls",
                provenance_status="semantic_source_missing",
                encoding="utf-8",
                newline_style="lf",
                change_context={"synthetic": True, "not_subject_authored": True},
            )
        )
        version = commit.version
        ref = f"subject-file:{version.document_id}@{version.version_id}"
        versions[ref] = {
            "path": version.logical_path,
            "document_id": version.document_id,
            "version_id": version.version_id,
            "sha256": version.content_hash,
            "text": text,
        }
        return commit, ref

    if case == "past_current":
        first, old_ref = await put(
            "ledger", "source: warehouse-log\nperiod: before\nbox: amber\n"
        )
        current, current_ref = await put(
            "ledger",
            "source: warehouse-log\nperiod: after\nbox: violet\n",
            first,
        )
        source_refs = required_refs = (old_ref, current_ref)
    else:
        source = "north-archive" if case == "multilayer" else "harbor"
        current, current_ref = await put(
            "ledger",
            f"source: {source}\nshared clue: silver tag\nvoucher: MANGO-41\n",
        )
        _, other_ref = await put(
            "other",
            "source: upland\nshared clue: silver tag\nvoucher: MANGO-14\n",
        )
        source_refs, required_refs = (current_ref, other_ref), (current_ref,)
    links = required_refs if case == "multilayer" else source_refs
    _, middle_ref = await put(
        "middle", "Synthetic ordinary source index.\n" + "\n".join(links) + "\n"
    )
    seed, seed_ref = await put(
        "entry",
        f"Synthetic entry key: {_QUERIES[case]}\nNext index: {middle_ref}\n",
    )
    for document_id in dict.fromkeys(item["document_id"] for item in versions.values()):
        await project_current_document(memory, document_id)
    relation = SemanticRelation(
        relation_id=f"synthetic-s3:{case}:relation",
        source_ref=f"subject-file:{seed.version.document_id}",
        target_ref=f"subject-file:{current.version.document_id}",
        predicate="synthetic explicit navigation",
        reason="Engineering fixture only; not an Elysia judgment.",
        actor="synthetic-protocol-fixture",
        recorded_at=_TIME,
        consciousness_instance_id="synthetic-protocol-fixture",
        stream_scope="test:s3",
        metadata={"synthetic": True},
    )
    await memory.record_memory_semantic_relation(relation)
    context_key = f"synthetic-s3/{case}"
    episode = await memory.begin_memory_recall(
        query="fixture preparation, not a model recall",
        episode_id=f"synthetic-s3:{case}:preparation",
        context_key=context_key,
        random_seed=0,
        recorded_at=_TIME,
    )
    corecall = CoRecallEvent(
        corecall_id=f"synthetic-s3:{case}:corecall",
        episode_id=episode.episode_id,
        context_key=context_key,
        signal="synthetic_shared_exposure",
        entity_refs=tuple(
            dict.fromkeys(
                (relation.source_ref,)
                + tuple(
                    f"subject-file:{versions[ref]['document_id']}"
                    for ref in source_refs
                )
            )
        ),
        actor="synthetic-protocol-fixture",
        reason="Synthetic accessibility, not truth.",
        recorded_at=_TIME,
        metadata={"synthetic": True},
    )
    await memory.append_memory_corecall(corecall)
    manifest = MemoryBoundaryManifest(
        boundary_id=f"synthetic-s3-{case}",
        manifest_revision=1,
        operation_occurrence_id=f"synthetic-s3:{case}:boundary",
        title="Synthetic source scope",
        scope="Protocol fixture only",
        current_meaning="No subject meaning is asserted by this fixture.",
        non_generalization="No subject recall-benefit claim is permitted.",
        actor_id="synthetic-protocol-fixture",
        consciousness_instance_id="synthetic-protocol-fixture",
        stream_scope="test:s3",
        decision_occurrence_id=f"synthetic-s3:{case}:decision",
        source_occurrence_id=f"synthetic-s3:{case}:source",
        subject_revision="c" * 64,
        segments=tuple(
            MemoryBoundarySegment.create(
                segment_id=f"source-{index}",
                title=f"Synthetic source {index}",
                content=versions[ref]["text"],
                source_refs=(ref,),
                source_occurrence_ids=(f"synthetic-s3:{case}:source:{index}",),
                scope="Reserved synthetic protocol source only",
                visibility="private",
            )
            for index, ref in enumerate(source_refs)
        ),
    )
    repository = MemoryBoundaryRepository(memory.living_memory_store)
    boundary = await repository.append(
        manifest, expected_head_revision=0, recorded_at=_TIME
    )
    initial = {
        "versions": versions,
        "relation": asdict(relation),
        "corecall": asdict(corecall),
        "boundary": manifest.canonical_json,
        "query": _QUERIES[case],
        "question": _QUESTIONS[case],
    }
    return _Material(
        case,
        versions,
        source_refs,
        required_refs,
        seed_ref,
        context_key,
        _sha(_json(initial)),
        boundary.exact_uri,
        repository,
    )


@asynccontextmanager
async def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str):
    async with _local_store(tmp_path) as (_, store, _):
        plugin, outer, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch
        )
        memory = LifeMemoryService(
            tmp_path / "memory-workspace",
            vector_backend_enabled=False,
            index_worker_enabled=False,
            subject_document_store=store,
            subject_document_store_required=True,
        )
        await memory.initialize()
        outer._memory_service = memory
        try:
            material = await _prepare(store, memory, case)
            yield memory, plugin, material
        finally:
            await memory.close()


def _step(trace: list[dict[str, Any]], action: str, payload: Any, **extra: Any) -> None:
    trace.append(
        {
            "step": len(trace) + 1,
            "action": action,
            "response_bytes": len(_json(payload).encode("utf-8")),
            **extra,
        }
    )
    assert len(trace) <= MAX_STEPS
    assert sum(item["response_bytes"] for item in trace) <= MAX_TOTAL_BYTES


async def _read_file(
    plugin: Any, ref: str, trace: list[dict[str, Any]]
) -> dict[str, Any]:
    ok, result = await LifeEngineReadFileTool(plugin=plugin).execute(
        file_ref=ref,
        limit=0,
        max_bytes=MAX_RESULT_BYTES,
    )
    assert ok, result
    assert isinstance(result, dict) and not result.get("continuation")
    _step(
        trace,
        "read_file",
        result,
        ref=ref,
        returned_ref=f"subject-file:{result['document_id']}@{result['subject_version_id']}",
        sha256=result["file_content_sha256"],
    )
    return result


def _record(
    record_property: Any,
    material: _Material,
    arm: str,
    seed: int,
    trace: list[dict[str, Any]],
    elapsed: float,
    **extra: Any,
) -> dict[str, Any]:
    reads = [item for item in trace if "ref" in item]
    acquired = {
        item["ref"]
        for item in reads
        if (
            item["ref"] == item["returned_ref"]
            and item["sha256"] == material.versions[item["ref"]]["sha256"]
        )
    }
    source_confusion = sum(
        item["ref"] != item["returned_ref"]
        or item["sha256"] != material.versions[item["ref"]]["sha256"]
        for item in reads
    )
    required = set(material.required_refs)
    result = {
        "evidence_class": "synthetic_protocol_only",
        "subject_recall_benefit": None,
        "split": "reserved_synthetic_protocol",
        "case": material.case,
        "arm": arm,
        "seed": seed,
        "material_sha256": material.manifest_sha256,
        "question_sha256": _sha(_QUESTIONS[material.case]),
        "budget": {
            "steps": MAX_STEPS,
            "result_bytes": MAX_RESULT_BYTES,
            "total_response_bytes": MAX_TOTAL_BYTES,
        },
        "model": None,
        "tokens": None,
        "model_calls": 0,
        "model_source_attribution": None,
        "correct_exact_evidence": len(acquired & required),
        "required_evidence": len(required),
        "not_found": sorted(required - acquired),
        "source_confusion": source_confusion,
        "distractor_evidence_exposed": len(
            (acquired & set(material.source_refs)) - required
        ),
        "first_required_evidence_step": next(
            (item["step"] for item in reads if item["ref"] in required), None
        ),
        "protocol_steps": len(trace),
        "response_bytes": sum(item["response_bytes"] for item in trace),
        "latency_seconds": round(elapsed, 6),
        "trace": trace,
        **extra,
    }
    record_property("s3_protocol_result", _json(result))
    assert source_confusion == 0
    assert not result["not_found"]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("arm", tuple(ARMS))
async def test_reserved_association_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
    case: str,
    seed: int,
    arm: str,
) -> None:
    async with _isolated(tmp_path, monkeypatch, case) as (memory, plugin, material):
        trace: list[dict[str, Any]] = []
        started = perf_counter()
        direct = await memory.search_memory(
            _QUERIES[case],
            top_k=1,
            enable_association=False,
            return_bundles=False,
        )
        _step(trace, "lexical_search", [asdict(item) for item in direct])
        assert (
            len(direct) == 1
            and result_identity(direct[0])["file_ref"] == material.seed_ref
        )
        semantic, corecall = ARMS[arm]
        expanded = await memory.expand_living_document_associations(
            direct,
            context_key=material.context_key,
            random_seed=seed,
            limit=4,
            enable_semantic_relations=semantic,
            enable_corecall=corecall,
        )
        _step(trace, "controlled_association", [asdict(item) for item in expanded])
        extra = expanded[len(direct) :]
        if arm == "A_index":
            assert extra == []
        elif arm == "B_semantic":
            assert extra and all(item.source == "semantic_relation" for item in extra)
        else:
            assert extra and all(item.source == "associated" for item in extra)
            assert all(
                "semantic_relation:" not in item.association_reason for item in extra
            )
        # This controller sees only actual retrieved refs and subsequently read
        # ordinary links. It never sees material.required_refs or source labels.
        queue = deque(result_identity(item)["file_ref"] for item in expanded)
        seen: set[str] = set()
        while queue and len(trace) < MAX_STEPS:
            ref = queue.popleft()
            if ref in seen:
                continue
            seen.add(ref)
            result = await _read_file(plugin, ref, trace)
            queue.extend(_EXACT_REF.findall(result["content"]))
        assert not queue, "fixture exceeds the predeclared common budget"
        _record(
            record_property,
            material,
            arm,
            seed,
            trace,
            perf_counter() - started,
            association_sources=[item.source for item in extra],
            policy="same FIFO exact-link walker; no model or answer-aware stopping",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("arm", ("file_ref", "boundary_exact_uri"))
async def test_reserved_boundary_transport_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
    case: str,
    seed: int,
    arm: str,
) -> None:
    async with _isolated(tmp_path, monkeypatch, case) as (_, plugin, material):
        trace: list[dict[str, Any]] = []
        started = perf_counter()
        if arm == "file_ref":
            for ref in material.source_refs:
                await _read_file(plugin, ref, trace)
        else:
            # The repository and exact reader are real; only the active runtime
            # binding is synthetic, and its recall writer rejects any commit.
            _install_runtime(monkeypatch, material.boundary_repository)
            tool = _boundary_tool(f"s3:{case}:{seed}")
            ok, provenance = await tool.execute(
                mode="provenance",
                exact_uri=material.boundary_uri,
                max_bytes=MAX_RESULT_BYTES,
            )
            assert ok, provenance
            assert not provenance.get("continuation")
            _step(trace, "boundary_provenance", provenance)
            for segment in json.loads(provenance["content"])["segments"]:
                (ref,) = segment["source_refs"]
                ok, payload = await tool.execute(
                    mode="segment",
                    exact_uri=material.boundary_uri,
                    segment_id=segment["segment_id"],
                    max_bytes=MAX_RESULT_BYTES,
                )
                assert ok, payload
                assert not payload.get("continuation")
                assert _sha(payload["content"]) == payload["content_sha256"]
                _step(
                    trace,
                    "boundary_segment",
                    payload,
                    ref=ref,
                    returned_ref=ref,
                    sha256=payload["content_sha256"],
                )
        _record(
            record_property,
            material,
            arm,
            seed,
            trace,
            perf_counter() - started,
            comparison="same preselected source scope, transport/provenance only; not retrieval benefit",
        )
