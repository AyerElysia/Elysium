"""Synthetic subject-owned relation revisions; no live service, model or data."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.memory.tools import NucleusRelationsTool
from plugins.life_engine.service import registry as service_registry
from plugins.life_engine.tools import managed_files
from test.plugins.life_engine.test_memory_semantic_relations import (
    _install_runtime,
    _MemoryService,
    _tool,
)


async def _add(tool: Any, **kwargs: Any) -> dict[str, Any]:
    ok, result = await tool.execute(
        action="add", relation_type="  联系由主体表达  ",
        reason="  明确的原始理由。  ", **kwargs,
    )
    assert ok, result
    return result


def _view_service(monkeypatch: pytest.MonkeyPatch, memory: _MemoryService) -> None:
    async def get_service(_self: Any) -> _MemoryService:
        return memory

    monkeypatch.setattr(NucleusRelationsTool, "_get_service", get_service)


async def test_cross_instance_revision_withdrawal_keeps_all_history_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    registry = _install_runtime(monkeypatch, memory)
    _view_service(monkeypatch, memory)
    first_tool = _tool(NucleusRelationsTool)
    first = await _add(
        first_tool, source_path="notes/a.md", target_path="notes/b.md",
        subject_strength="  暂时轻，但并非不重要  ",
    )
    assert first["owner_subject_id"] == "elysia"
    assert first["root_relation_id"] == first["relation_id"]
    assert first["parent_relation_id"] is None and first["revision"] == 1
    assert first["reason"] == "  明确的原始理由。  "
    assert first["relation_type"] == "  联系由主体表达  "

    registry.get_for_stream = lambda _stream: SimpleNamespace(
        instance_id="consciousness:later-window", is_active=True,
    )
    revision_tool = _tool(NucleusRelationsTool, tool_call_id="revision:two")
    ok, revised = await revision_tool.execute(
        action="revise", root_relation_id=first["relation_id"],
        parent_relation_id=first["relation_id"],
        relation_type="现在明确改为相互质疑", reason="第二个窗口亲自修订。",
    )
    assert ok, revised
    assert revised["owner_subject_id"] == "elysia"
    assert revised["actor"] == "consciousness:later-window"
    assert revised["revision"] == 2
    assert revised["metadata"]["subject_strength"] == "  暂时轻，但并非不重要  "
    assert memory.semantic_relations[0].predicate == "  联系由主体表达  "

    view = _tool(NucleusRelationsTool)
    ok, current = await view.execute(
        action="view", file_path="notes/a.md", current_only=True,
        depth=2, min_strength=0.4,
    )
    assert ok and current["view"] == "current"
    assert current["history_relation_count"] is None
    assert current["matching_relation_count"] == 1
    assert current["current_relation_ids"] == [revised["relation_id"]]

    withdrawn_tool = _tool(NucleusRelationsTool, tool_call_id="withdraw:three")
    ok, withdrawn = await withdrawn_tool.execute(
        action="withdraw", root_relation_id=first["relation_id"],
        parent_relation_id=revised["relation_id"], reason="我决定撤回这条联系。",
    )
    assert ok, withdrawn
    assert withdrawn["revision"] == 3
    assert withdrawn["predicate"] == revised["predicate"]
    ok, history = await view.execute(
        action="view", file_path="notes/a.md", depth=2, min_strength=0.4,
    )
    assert ok and history["view"] == "history"
    assert history["history_relation_count"] == 3
    assert history["current_relation_count"] == 0
    assert [row["operation"] for row in history["semantic_relations"]] == [
        "add", "revise", "withdraw",
    ]
    assert not any(row["is_current"] for row in history["semantic_relations"])

    ok, replayed = await revision_tool.execute(
        action="revise", root_relation_id=first["relation_id"],
        parent_relation_id=first["relation_id"],
        relation_type="现在明确改为相互质疑", reason="第二个窗口亲自修订。",
    )
    assert ok and replayed["idempotent_replay"]
    assert len(memory.recorded) == 3

    ok, error = await _tool(NucleusRelationsTool, tool_call_id="revive").execute(
        action="revise", root_relation_id=first["relation_id"],
        parent_relation_id=withdrawn["relation_id"],
        relation_type="不能在终止链上复活", reason="需用新 add 留下新判断。",
    )
    assert not ok and error["error"] == "SemanticRelationAlreadyWithdrawn"
    assert len(memory.recorded) == 3


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"root_relation_id": ""}, "SemanticRelationExplicitRootAndParentRequired"),
        ({"parent_relation_id": "missing"}, "SemanticRelationParentNotFound"),
        ({"root_relation_id": "another-root"}, "SemanticRelationLineageMismatch"),
        ({"source_path": "notes/c.md"}, "SemanticRelationEndpointsImmutable"),
        ({"relation_type": ""}, "relation_type 不能为空"),
        ({"reason": ""}, "reason 不能为空"),
    ],
)
async def test_revision_requires_exact_root_parent_and_explicit_subject_text(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], expected: str,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    request = {
        "action": "revise", "root_relation_id": first["relation_id"],
        "parent_relation_id": first["relation_id"],
        "relation_type": "新的关系", "reason": "新的理由。",
        **change,
    }
    ok, result = await _tool(NucleusRelationsTool, tool_call_id="revision").execute(**request)
    assert not ok and result["error"] == expected
    assert len(memory.recorded) == 1


@pytest.mark.parametrize(
    ("owner", "expected"),
    [(None, "SemanticRelationLegacyOwnerUnbound"), ("independent-other", "SemanticRelationOwnerMismatch")],
)
async def test_legacy_and_foreign_owners_cannot_be_revised(
    monkeypatch: pytest.MonkeyPatch, owner: str | None, expected: str,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    memory.semantic_relations[0] = replace(memory.semantic_relations[0], owner_subject_id=owner)
    ok, result = await _tool(NucleusRelationsTool, tool_call_id="revision").execute(
        action="revise", root_relation_id=first["relation_id"],
        parent_relation_id=first["relation_id"], relation_type="新理解", reason="明确理由。",
    )
    assert not ok and result["error"] == expected
    assert len(memory.recorded) == 1


async def test_stale_parent_is_not_retried_against_new_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    request = {
        "action": "revise", "root_relation_id": first["relation_id"],
        "parent_relation_id": first["relation_id"], "relation_type": "第一项修订",
        "reason": "两个不同 occurrence 明确选择相同父记录。",
    }
    ok, _ = await _tool(NucleusRelationsTool, tool_call_id="winner").execute(**request)
    assert ok
    ok, result = await _tool(NucleusRelationsTool, tool_call_id="stale").execute(**request)
    assert not ok and result["error"] == "SemanticRelationStaleParent"
    assert len(memory.recorded) == 2


async def test_same_occurrence_cannot_change_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    tool = _tool(NucleusRelationsTool)
    await _add(tool, source_path="notes/a.md", target_path="notes/b.md")
    ok, result = await tool.execute(
        action="add", source_path="notes/c.md", target_path="notes/d.md",
        relation_type="  联系由主体表达  ", reason="  明确的原始理由。  ",
    )
    assert not ok and result["error"] == "SemanticRelationOccurrenceConflict"
    assert len(memory.recorded) == 1


async def test_committed_append_with_lost_ack_recovers_exact_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    append = memory.record_memory_semantic_relation

    async def lost_ack(relation: Any) -> Any:
        await append(relation)
        raise OSError("synthetic acknowledgement loss")

    monkeypatch.setattr(memory, "record_memory_semantic_relation", lost_ack)
    result = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    assert result["idempotent_replay"]
    assert len(memory.recorded) == 1


def _reference_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[_MemoryService, Any]:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    _view_service(monkeypatch, memory)
    heads = {
        "doc_a": SimpleNamespace(
            document_id="doc_a", current_version_id="ver_a2",
            logical_path="life_engine_workspace/renamed/a.md",
        ),
        "doc_b": SimpleNamespace(
            document_id="doc_b", current_version_id="ver_b1",
            logical_path="life_engine_workspace/b.md",
        ),
    }
    descriptors = {
        "ver_a1": {"document_id": "doc_a", "version_id": "ver_a1", "logical_path": "life_engine_workspace/a.md"},
        "ver_a2": {"document_id": "doc_a", "version_id": "ver_a2", "logical_path": "life_engine_workspace/renamed/a.md"},
        "ver_b1": {"document_id": "doc_b", "version_id": "ver_b1", "logical_path": "life_engine_workspace/b.md"},
    }
    store = SimpleNamespace(
        heads=heads, descriptors=descriptors,
        get_document_head=AsyncMock(side_effect=lambda doc: heads.get(doc)),
        get_version_descriptor=AsyncMock(side_effect=lambda ver: descriptors[ver]),
        get_version=AsyncMock(side_effect=AssertionError("Relations must not load file blobs")),
    )
    service = service_registry.get_life_engine_service()
    service._selectable_storage_enabled = True
    service._subject_document_store = store
    monkeypatch.setattr(managed_files, "_get_workspace", lambda _plugin: tmp_path)
    return memory, store


@pytest.mark.parametrize("source_ref", ["subject-file:doc_a", "subject-file:doc_a@ver_a1"])
async def test_stable_and_exact_endpoints_stay_distinct_and_do_not_adopt_legacy_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_ref: str,
) -> None:
    memory, store = _reference_runtime(tmp_path, monkeypatch)
    first = await _add(
        _tool(NucleusRelationsTool), source_ref=source_ref,
        target_ref="subject-file:doc_b",
    )
    assert first["source_ref"] == source_ref
    assert memory.recorded[0].source_ref == source_ref
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", entity_ref=source_ref,
    )
    assert ok and result["semantic_relation_count"] == 1
    assert not result["legacy_compatibility_projection"]["available"]
    assert "legacy" not in memory.read_order
    store.get_version.assert_not_called()
    other_ref = "subject-file:doc_a" if "@" in source_ref else "subject-file:doc_a@ver_a1"
    ok, other = await _tool(NucleusRelationsTool).execute(action="view", entity_ref=other_ref)
    assert ok and other["semantic_relation_count"] == 0


@pytest.mark.parametrize(
    "bad_ref",
    [
        "document:a.md", "subject-file:doc_", "subject-file:doc_a@",
        "subject-file:doc_a@ver_a1@ver_a2", "subject-file:doc_a?latest=true",
        " subject-file:doc_a", "subject-file:doc_a@ver_a1 ",
    ],
)
async def test_bad_ref_fails_before_authority_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_ref: str,
) -> None:
    memory, store = _reference_runtime(tmp_path, monkeypatch)
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="add", source_ref=bad_ref, target_ref="subject-file:doc_b",
        relation_type="连接", reason="请求格式验证。",
    )
    assert not ok and result["error"] == "ManagedFileReferenceInvalid"
    assert memory.recorded == []
    store.get_document_head.assert_not_called()
    store.get_version_descriptor.assert_not_called()


@pytest.mark.parametrize("case", ["wrong_document", "foreign_namespace", "head_namespace", "path_alias"])
async def test_relation_reference_authorization_reuses_file_reader_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    memory, store = _reference_runtime(tmp_path, monkeypatch)
    if case == "wrong_document":
        store.descriptors["ver_a1"]["document_id"] = "doc_b"
    elif case == "foreign_namespace":
        store.descriptors["ver_a1"]["logical_path"] = "private/a.md"
    elif case == "head_namespace":
        store.heads["doc_a"].logical_path = "private/a.md"
    else:
        (tmp_path / "real.md").touch()
        (tmp_path / "a.md").symlink_to(tmp_path / "real.md")
    ok, _ = await _tool(NucleusRelationsTool).execute(
        action="add", source_ref="subject-file:doc_a@ver_a1",
        target_ref="subject-file:doc_b", relation_type="连接", reason="验证授权边界。",
    )
    assert not ok and memory.recorded == []


async def test_explicit_ref_and_path_cannot_be_silently_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, store = _reference_runtime(tmp_path, monkeypatch)
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", entity_ref="subject-file:doc_a", file_path="a.md",
    )
    assert not ok and result["error"] == "SemanticRelationReferencePathConflict"
    store.get_document_head.assert_not_called()
    assert memory.recorded == []


async def test_strength_is_optional_raw_subject_text_not_a_number_or_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    assert "subject_strength" not in first["metadata"]
    ok, result = await _tool(NucleusRelationsTool, tool_call_id="numeric").execute(
        action="add", source_path="notes/a.md", target_path="notes/b.md",
        relation_type="关系", reason="理由", subject_strength=0.99,
    )
    assert not ok and result["error"] == "SemanticRelationStrengthMustBeSubjectText"
    assert len(memory.recorded) == 1


async def test_long_relation_history_pages_reassemble_every_subject_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import json

    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    _view_service(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    original = memory.semantic_relations[0]
    memory.semantic_relations = [
        replace(
            original, relation_id=f"relation:page-{index}",
            root_relation_id=f"relation:page-{index}",
            predicate=f"第 {index} 个开放关系：并不自动等同",
            reason=("未改写的原话😀\n\"保留引号与换行\"  " * 45) + str(index),
        )
        for index in range(12)
    ]
    pieces = []
    cursor = ""
    previous_offset = 0
    snapshot_hash = ""
    for _ in range(150):
        ok, result = await _tool(NucleusRelationsTool).execute(
            action="view", file_path="notes/a.md", max_bytes=2048,
            continuation=cursor, depth=2, min_strength=0.4,
        )
        assert ok, result
        assert len(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")) <= 2048
        assert result["projection_kind"] == "exact_serialized_view_excerpt"
        assert result["serialization"] == "canonical-json-utf8"
        assert result["offset_bytes"] == previous_offset
        previous_offset = result["next_offset_bytes"]
        pieces.append(result["content"])
        if snapshot_hash:
            assert snapshot_hash == result["content_sha256"]
        snapshot_hash = result["content_sha256"]
        cursor = result["continuation"]
        if not cursor:
            assert result["complete"]
            break
    else:
        pytest.fail("Finite relation projection did not complete")
    raw = "".join(pieces).encode("utf-8")
    assert len(raw) == previous_offset == result["content_bytes"]
    assert hashlib.sha256(raw).hexdigest() == snapshot_hash
    complete = json.loads(raw)
    assert complete["history_relation_count"] == 12
    assert complete["current_relation_count"] == 12
    assert [row["reason"] for row in complete["semantic_relations"]] == [
        relation.reason for relation in memory.semantic_relations
    ]
    assert first["relation_id"] == original.relation_id


async def test_current_frontier_change_rejects_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    _view_service(monkeypatch, memory)
    first = await _add(
        _tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md",
    )
    memory.semantic_relations[0] = replace(
        memory.semantic_relations[0], reason="很长的原始关系理由😀" * 1000,
    )
    ok, first_page = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", current_only=True,
        max_bytes=2048, depth=2, min_strength=0.4,
    )
    assert ok and first_page["continuation"]
    ok, _ = await _tool(NucleusRelationsTool, tool_call_id="revised-frontier").execute(
        action="revise", root_relation_id=first["relation_id"],
        parent_relation_id=first["relation_id"],
        relation_type="最新关系", reason="最新理由较短。",
    )
    assert ok
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", current_only=True,
        max_bytes=2048, continuation=first_page["continuation"],
        depth=2, min_strength=0.4,
    )
    assert not ok and result["error"] == "SemanticRelationPageFrontierConflict"


async def test_large_mutation_receipt_is_bounded_and_full_subject_text_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    reason = "主体长原话😀\n保留所有原始字节。  " * 1200
    strength = "轻但仍然有意义 " * 800
    ok, receipt = await _tool(NucleusRelationsTool).execute(
        action="add", source_path="notes/a.md", target_path="notes/b.md",
        relation_type="  开放关系原话  ", reason=reason, subject_strength=strength,
    )
    assert ok, receipt
    assert receipt["receipt_only"]
    assert receipt["subject_content_preserved_in_history"]
    assert len(json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()) <= 16384
    assert "reason" not in receipt and "metadata" not in receipt
    assert memory.recorded[0].reason == reason
    assert memory.recorded[0].metadata["subject_strength"] == strength
    read_request = {**receipt["read_from"], "depth": 2, "min_strength": 0.4}
    cursor = ""
    pieces = []
    for _ in range(30):
        ok, page = await _tool(NucleusRelationsTool).execute(
            **read_request, continuation=cursor,
        )
        assert ok, page
        pieces.append(page["content"])
        cursor = page["continuation"]
        if not cursor:
            break
    else:
        pytest.fail("Long append receipt could not be fully continued")
    full = json.loads("".join(pieces))
    relation = full["semantic_relations"][0]
    assert relation["relation_id"] == receipt["read_until_relation_id"]
    assert relation["reason"] == reason
    assert relation["metadata"]["subject_strength"] == strength


@pytest.mark.parametrize("phase", ["append", "view"])
async def test_unknown_third_party_failure_never_returns_or_logs_subject_sql_parameters(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from plugins.life_engine.memory import tools as relation_tools

    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    message = "SQL query with secret-subject-reason and credential=never-show-this"
    if phase == "append":
        monkeypatch.setattr(
            memory, "record_memory_semantic_relation",
            AsyncMock(side_effect=RuntimeError(message)),
        )
        request = {
            "action": "add", "source_path": "notes/a.md", "target_path": "notes/b.md",
            "relation_type": "关系", "reason": "主体原话",
        }
    else:
        monkeypatch.setattr(
            memory, "page_memory_semantic_relations",
            AsyncMock(side_effect=RuntimeError(message)),
        )
        request = {"action": "view", "file_path": "notes/a.md"}
    logged = []
    monkeypatch.setattr(
        relation_tools.logger, "error", lambda *args, **kwargs: logged.append((args, kwargs)),
    )
    ok, error = await _tool(NucleusRelationsTool).execute(**request)
    assert not ok and error == {
        "error": "SemanticRelationOperationFailed",
        "error_type": "RuntimeError", "status": "failed",
    }
    assert message not in repr(error) and message not in repr(logged)
    assert "secret-subject-reason" not in repr(logged)
    assert logged and all(not kwargs.get("exc_info") for _, kwargs in logged)


async def test_legacy_projection_error_text_is_not_copied_into_successful_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    memory.legacy_result = {"error": "SQL parameters contain private-subject-text"}
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", depth=2, min_strength=0.4,
    )
    assert ok
    legacy = result["legacy_compatibility_projection"]
    assert not legacy["available"]
    assert legacy["error"] == "LegacyRelationProjectionUnavailable"
    assert "private-subject-text" not in repr(result)


@pytest.mark.parametrize("max_bytes", [0, True, 2047, 65537])
async def test_relation_view_rejects_invalid_byte_budget(
    monkeypatch: pytest.MonkeyPatch, max_bytes: Any,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    _view_service(monkeypatch, memory)
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=max_bytes,
        depth=2, min_strength=0.4,
    )
    assert not ok and result["error"] == "SemanticRelationViewByteBudgetInvalid"


async def test_relation_cursor_cannot_change_view_or_start_inside_utf8_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory.tools import (
        _bounded_relation_view,
        _RELATION_VIEW_PROJECTION,
        _relation_view_cursor,
    )

    payload = {"entity_ref": "subject-file:doc_a", "view": "history", "reason": "😀" * 3000}
    page = _bounded_relation_view(payload, max_bytes=2048, continuation="")
    with pytest.raises(ValueError, match="SemanticRelationContinuationInvalid"):
        _bounded_relation_view(
            {**payload, "view": "current"}, max_bytes=2048,
            continuation=page["continuation"],
        )
    cursor = _relation_view_cursor({
        "projection": _RELATION_VIEW_PROJECTION,
        "entity_ref": payload["entity_ref"], "view": "history",
        "sha256": page["content_sha256"],
        "offset_bytes": len('{"entity_ref":"subject-file:doc_a","reason":"'.encode()) + 1,
    })
    with pytest.raises(ValueError, match="SemanticRelationContinuationInvalid"):
        _bounded_relation_view(payload, max_bytes=2048, continuation=cursor)


async def test_relation_refs_survive_real_temp_file_rename_delete_path_reuse_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.storage.subject_factory import open_subject_document_store
    from test.plugins.life_engine.test_managed_file_lifecycle_tools import _patch_tool, _read, _write
    from test.plugins.life_engine.test_minimal_subject_file_continuity import _memory_plugin, _STREAM_ID
    from test.plugins.life_engine.test_subject_document_storage_contract import _local_store

    async with _local_store(tmp_path) as (runtime, store, _):
        plugin, service, _ = _memory_plugin(
            store, data_root=tmp_path / "data", monkeypatch=monkeypatch,
        )
        memory = _MemoryService()
        service._memory_service = memory
        monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)
        first = await _write(plugin, "a.md", "synthetic first document\n", "relref:create")
        second = await _write(plugin, "b.md", "synthetic second document\n", "relref:second")
        original = await _read(plugin, "a.md")
        tool = NucleusRelationsTool(plugin=plugin)
        tool._bind_runtime_context(
            stream_id=_STREAM_ID, tool_call_id="relation:temp-lifecycle",
            message=SimpleNamespace(
                message_id="message:relation", stream_id=_STREAM_ID,
                time="2026-09-08T00:00:00+00:00", extra={},
            ),
        )
        relation = await _add(
            tool, source_ref=original["file_ref"],
            target_ref=f"subject-file:{second['document_id']}",
        )
        ok, result = await _patch_tool(plugin, "relref:rename").execute(
            "*** Begin Patch\n*** Update File: a.md\n*** Move to: renamed/a.md\n*** End Patch\n",
            expected_versions={"a.md": original["expected_version"]},
        )
        assert ok, result
        renamed = await _read(plugin, "renamed/a.md")
        ok, result = await _patch_tool(plugin, "relref:delete").execute(
            "*** Begin Patch\n*** Delete File: renamed/a.md\n*** End Patch\n",
            expected_versions={"renamed/a.md": renamed["expected_version"]},
        )
        assert ok, result
        reused = await _write(plugin, "a.md", "a different document\n", "relref:reuse")
        assert reused["document_id"] != first["document_id"]
        reopened = await open_subject_document_store(runtime, initialize_schema=False)
        service._subject_document_store = reopened
        ok, historical = await tool.execute(action="view", entity_ref=original["file_ref"])
        assert ok and historical["semantic_relation_count"] == 1
        assert historical["semantic_relations"][0]["relation_id"] == relation["relation_id"]
        ok, new_file = await tool.execute(
            action="view", entity_ref=f"subject-file:{reused['document_id']}",
        )
        assert ok and new_file["semantic_relation_count"] == 0
        assert memory.recorded[0].source_ref == original["file_ref"]


async def test_relation_view_advances_bounded_row_pages_without_losing_head_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _MemoryService()
    _install_runtime(monkeypatch, memory)
    await _add(_tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md")
    original = memory.semantic_relations[0]
    memory.semantic_relations = [
        replace(
            original, relation_id=f"relation:row-{index}",
            root_relation_id=f"relation:row-{index}", reason=f"原始理由 {index}",
        )
        for index in range(120)
    ]
    memory.semantic_relations.append(replace(
        original, relation_id="relation:late-revision",
        root_relation_id="relation:row-0", parent_relation_id="relation:row-0",
        revision=2, operation="revise", reason="后继在另一页，不能误报原记录仍当前。",
    ))
    read_page = AsyncMock(wraps=memory.page_memory_semantic_relations)
    monkeypatch.setattr(memory, "page_memory_semantic_relations", read_page)
    all_rows = []
    cursor = ""
    for index in range(3):
        ok, page = await _tool(NucleusRelationsTool).execute(
            action="view", file_path="notes/a.md", max_bytes=65536,
            continuation=cursor, depth=2, min_strength=0.4,
        )
        assert ok, page
        assert page["page_complete"]
        assert page["storage_page"]["offset"] == index * 50
        assert page["matching_relation_count"] == 121
        assert page["storage_page"]["frontier_count"] == 121
        all_rows.extend(page["semantic_relations"])
        cursor = page["continuation"]
    assert not cursor and page["complete"]
    assert len(all_rows) == len({row["relation_id"] for row in all_rows}) == 121
    assert all_rows[0]["is_current"] is False
    assert all_rows[-1]["is_current"] is True
    assert [call.kwargs["limit"] for call in read_page.call_args_list] == [50, 50, 50]
    assert [call.kwargs["expected_frontier_count"] for call in read_page.call_args_list] == [
        None, 121, 121,
    ]


def _synthetic_storage_runtime() -> Any:
    return SimpleNamespace(
        enabled=True, backend="local",
        backend_identity="local:///synthetic-private-dataset-path",
        generation=SimpleNamespace(generation_id="synthetic-generation-one"),
        authority_token=SimpleNamespace(
            registry_id="synthetic-private-registry",
            authority_epoch=1, lease_until="2030-01-01T00:00:00+00:00",
            fencing_token="must-never-be-in-tool-output",
        ),
        writer_epoch=2,
    )


async def _first_long_page(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_MemoryService, Any, dict[str, Any]]:
    memory = _MemoryService()
    registry = _install_runtime(monkeypatch, memory)
    memory.storage_runtime = _synthetic_storage_runtime()
    await _add(_tool(NucleusRelationsTool), source_path="notes/a.md", target_path="notes/b.md")
    memory.semantic_relations[0] = replace(
        memory.semantic_relations[0], reason="很长且不截断的理由😀" * 500,
    )
    ok, page = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=2048,
        depth=2, min_strength=0.4,
    )
    assert ok and page["continuation"]
    return memory, registry, page


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("restart", "SemanticRelationContinuationAuthorityChanged"),
        ("generation", "SemanticRelationContinuationAuthorityChanged"),
        ("authority_epoch", "SemanticRelationContinuationAuthorityChanged"),
        ("inactive", "SemanticRelationActorIsNotActive"),
        ("actor", "SemanticRelationContinuationBindingMismatch"),
        ("budget", "SemanticRelationContinuationBindingMismatch"),
    ],
)
async def test_relation_continuation_rechecks_authority_actor_and_request(
    monkeypatch: pytest.MonkeyPatch, change: str, expected: str,
) -> None:
    memory, registry, first = await _first_long_page(monkeypatch)
    max_bytes = 2048
    if change == "restart":
        restarted = _MemoryService()
        restarted.semantic_relations = list(memory.semantic_relations)
        restarted.storage_runtime = memory.storage_runtime
        _install_runtime(monkeypatch, restarted)
    elif change == "generation":
        memory.storage_runtime.generation.generation_id = "synthetic-generation-two"
    elif change == "authority_epoch":
        memory.storage_runtime.authority_token.authority_epoch += 1
    elif change == "inactive":
        registry.active = False
    elif change == "actor":
        registry.get_for_stream = lambda _stream: SimpleNamespace(
            instance_id="another-authorized-window", is_active=True,
        )
    else:
        max_bytes = 4096
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=max_bytes,
        continuation=first["continuation"], depth=2, min_strength=0.4,
    )
    assert not ok and result["error"] == expected


async def test_normal_lease_renewal_keeps_cursor_valid_without_exposing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    memory, _, first = await _first_long_page(monkeypatch)
    memory.storage_runtime.authority_token.lease_until = "2030-01-01T01:00:00+00:00"
    memory.storage_runtime.authority_token.fencing_token = "renewed-secret-not-an-identity"
    ok, second = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=2048,
        continuation=first["continuation"], depth=2, min_strength=0.4,
    )
    assert ok, second
    assert second["offset_bytes"] == first["next_offset_bytes"]
    assert second["storage_page"]["authority_binding"] == first["storage_page"]["authority_binding"]
    text = json.dumps([first, second])
    for secret in (
        "synthetic-private-dataset-path", "synthetic-private-registry",
        "must-never-be-in-tool-output", "renewed-secret-not-an-identity",
        "synthetic-generation-one",
    ):
        assert secret not in text


async def test_relation_continuation_expiry_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.memory.tools import _decode_relation_view_cursor, _relation_view_cursor

    _, _, page = await _first_long_page(monkeypatch)
    state = _decode_relation_view_cursor(page["continuation"])
    state["issued_at"] -= 1801
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=2048,
        continuation=_relation_view_cursor(state), depth=2, min_strength=0.4,
    )
    assert not ok and result["error"] == "SemanticRelationContinuationExpired"


async def test_unrelated_append_also_invalidates_coarse_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, _, page = await _first_long_page(monkeypatch)
    memory.semantic_relations.append(replace(
        memory.semantic_relations[0], relation_id="relation:unrelated",
        root_relation_id="relation:unrelated", source_ref="document:notes/c.md",
        target_ref="document:notes/d.md",
    ))
    ok, result = await _tool(NucleusRelationsTool).execute(
        action="view", file_path="notes/a.md", max_bytes=2048,
        continuation=page["continuation"], depth=2, min_strength=0.4,
    )
    assert not ok and result["error"] == "SemanticRelationPageFrontierConflict"
