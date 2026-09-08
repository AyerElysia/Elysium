"""Learning capability admission and independently callable cognitive stages."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.learning.learn_tool import (
    LEARNING_SKILL_RELATIVE,
    NucleusLearnTool,
)
from plugins.life_engine.learning.opportunity_capability import (
    LEARNING_CAPABILITY_ID,
    LearningCapabilityReadOnly,
    LearningCapabilityUnavailable,
)
from plugins.life_engine.learning.tools import (
    LifeDistillSkillCandidateTool,
    LifeListInsightsTool,
    LifeProposeKnowledgeCandidateTool,
    LifeReflectNowTool,
    LifeRunIndependentAuditTool,
    LifeRunNextReflectionTool,
)
from plugins.life_engine.opportunity.execution_context import (
    bind_capability_execution,
)
from plugins.life_engine.opportunity.registry import CapabilityRuntimeState
from plugins.life_engine.service import registry as service_registry
from plugins.life_engine.storage.opportunity_contracts import (
    ProviderBinding,
    ProviderStatus,
    WorkflowChunk,
)


class _ReadStore:
    def list_all(self) -> list[Any]:
        return []

    def list_by_status(self, _status: str) -> list[Any]:
        return []

    def get_stats(self) -> dict[str, int]:
        return {"total": 0}


class _SharedState:
    def __init__(
        self,
        *,
        store: Any,
        skill_store: Any,
        writable: bool = True,
    ) -> None:
        self.initialized = True
        self.closed = False
        self.writable = writable
        self.store = store
        self.skill_store = skill_store
        self.decision_ledger = SimpleNamespace()
        self.contexts: list[Any] = []

    def require_writable(self) -> None:
        if not self.writable:
            raise RuntimeError("SharedLearningProjectionRuntimeReadOnly")

    @contextmanager
    def mutation_context(self, context: Any) -> Iterator[None]:
        self.require_writable()
        self.contexts.append(context)
        yield


class _Authority:
    def __init__(self, binding: ProviderBinding, content: str) -> None:
        self.binding = binding
        self.content = content

    async def get_provider(self, provider_id: str) -> ProviderBinding | None:
        assert provider_id == LEARNING_CAPABILITY_ID
        return self.binding

    async def read_workflow_chunk(
        self,
        workflow_id: str,
        revision: int,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> WorkflowChunk:
        assert workflow_id == self.binding.workflow_id
        assert revision == self.binding.workflow_revision
        raw = self.content.encode("utf-8")
        end = min(len(raw), offset_bytes + max_bytes)
        while end > offset_bytes:
            try:
                content = raw[offset_bytes:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            content = ""
        return WorkflowChunk(
            workflow_id=workflow_id,
            revision=revision,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            offset_bytes=offset_bytes,
            next_offset_bytes=end,
            total_bytes=len(raw),
            content=content,
            complete=end == len(raw),
        )


class _RuntimeRegistry:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    async def state(self, capability_id: str) -> CapabilityRuntimeState:
        return CapabilityRuntimeState(
            capability_id=capability_id,
            package_sha256="d" * 64,
            installed=self.enabled,
            enabled=self.enabled,
            operation_count=1,
        )


def _binding(
    content: str, *, status: ProviderStatus = ProviderStatus.ENABLED
) -> ProviderBinding:
    return ProviderBinding(
        provider_id=LEARNING_CAPABILITY_ID,
        status=status,
        revision=3,
        descriptor_version="1.0.0",
        descriptor_sha256="d" * 64,
        workflow_id="learning-workflow",
        workflow_revision=2,
        workflow_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        last_occurrence_id="provider:3",
        last_event_position=12,
        updated_at="2026-09-05T00:00:00+00:00",
    )


def _tool(tool_cls: type[Any], tmp_path: Any) -> Any:
    tool = tool_cls(
        plugin=SimpleNamespace(
            config=SimpleNamespace(
                settings=SimpleNamespace(workspace_path=str(tmp_path))
            )
        )
    )
    tool._bind_runtime_context(
        stream_id="chat_global",
        message=SimpleNamespace(message_id="message:1"),
        tool_call_id="tool:1",
    )
    tool._runtime_task_name = "core"
    tool._life_source_occurrence_id = "source:1"
    tool._life_source_instance_id = "chat_global"
    return tool


def _service(
    content: str,
    *,
    status: ProviderStatus = ProviderStatus.ENABLED,
    registry_enabled: bool = True,
    writable: bool = True,
) -> tuple[Any, Any, _SharedState, _Authority]:
    store = _ReadStore()
    scheduler = SimpleNamespace(
        store=store,
        skill_store=SimpleNamespace(),
        current_subject_revision=AsyncMock(return_value="a" * 64),
        submit_reflection=AsyncMock(return_value=[]),
        flush=AsyncMock(),
        run_next_reflection_once=AsyncMock(return_value=None),
        run_independent_audit_once=AsyncMock(return_value=[]),
        propose_knowledge_candidate_once=AsyncMock(return_value=False),
        distill_skill_candidate_once=AsyncMock(return_value=False),
        _projector_quiesced=False,
    )
    shared = _SharedState(
        store=store,
        skill_store=scheduler.skill_store,
        writable=writable,
    )
    authority = _Authority(_binding(content, status=status), content)
    runtime = SimpleNamespace(
        stores=SimpleNamespace(authority=authority),
        registry=_RuntimeRegistry(enabled=registry_enabled),
    )
    instance = SimpleNamespace(instance_id="chat_global", is_active=True)
    service = SimpleNamespace(
        opportunity_managed=True,
        _opportunity_runtime=runtime,
        _learning_scheduler=scheduler,
        _shared_learning_state=shared,
        resolve_consciousness_instance=lambda _stream: "chat_global",
        consciousness_registry=SimpleNamespace(get=lambda _actor: instance),
    )
    return service, scheduler, shared, authority


async def test_managed_old_tool_requires_exact_inflight_capability(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _scheduler, _shared, authority = _service("chosen workflow")
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)
    tool = _tool(LifeListInsightsTool, tmp_path)

    with pytest.raises(
        LearningCapabilityUnavailable,
        match="LearningCapabilityCallRequired",
    ):
        await tool.execute()
    with bind_capability_execution("life.memory_review"):
        with pytest.raises(
            LearningCapabilityUnavailable,
            match="LearningCapabilityCallRequired",
        ):
            await tool.execute()
    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await tool.execute()
    assert ok is True
    assert result["count"] == 0

    authority.binding = _binding(
        "chosen workflow",
        status=ProviderStatus.PAUSED,
    )
    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        with pytest.raises(
            LearningCapabilityUnavailable,
            match="NotInstalledOrEnabled",
        ):
            await tool.execute()


async def test_help_reads_exact_selected_workflow_without_reseeding(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "# 她选择的学习流程\n只在愿意时审计。\n"
    service, _scheduler, _shared, _authority = _service(content)
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)
    tool = _tool(NucleusLearnTool, tmp_path)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await tool.execute(action="help")

    assert ok is True
    assert result["content"] == content
    assert result["workflow_revision"] == 2
    assert (
        result["workflow_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    )
    assert result["subject_adopted"] is True
    assert not (tmp_path / LEARNING_SKILL_RELATIVE).exists()


async def test_nucleus_learn_dispatches_one_atomic_operation_in_same_execution(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scheduler, _shared, _authority = _service("chosen workflow")
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)
    tool = _tool(NucleusLearnTool, tmp_path)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await tool.execute(
            action="nucleus_run_independent_audit",
            arguments={"reason": "这次只选择独立审计。"},
        )

    assert ok is True
    assert result["action"] == "audit_once"
    assert result["automatic_follow_up"] is False
    scheduler.run_independent_audit_once.assert_awaited_once_with()
    scheduler.run_next_reflection_once.assert_not_awaited()
    scheduler.propose_knowledge_candidate_once.assert_not_awaited()
    scheduler.distill_skill_candidate_once.assert_not_awaited()


async def test_reflect_now_completes_inside_the_tracked_call(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scheduler, _shared, _authority = _service("chosen workflow")
    scheduler.submit_reflection.return_value = [
        SimpleNamespace(insight_id="insight:1", claim="candidate", category="open")
    ]
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await _tool(LifeReflectNowTool, tmp_path).execute(
            reflection_text="我现在选择认真看这段经历。",
        )

    assert ok is True
    assert "queued" not in result
    assert result["insights_count"] == 1
    scheduler.submit_reflection.assert_awaited_once()
    scheduler.flush.assert_awaited_once_with()
    scheduler.run_next_reflection_once.assert_not_awaited()


async def test_reflect_now_reports_queue_without_claiming_completion(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scheduler, _shared, _authority = _service("chosen workflow")
    scheduler.submit_reflection.return_value = None
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await _tool(LifeReflectNowTool, tmp_path).execute(
            reflection_text="先保留这段经历，冷却后再处理。",
        )

    assert ok is True
    assert result["queued"] is True
    assert result["insights_count"] == 0
    assert "不会自动继续" in result["note"]
    assert "run_next_reflection" in result["note"]
    scheduler.submit_reflection.assert_awaited_once()
    scheduler.flush.assert_awaited_once_with()
    scheduler.run_next_reflection_once.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool_cls", "method_name", "arguments", "expected_action"),
    [
        (
            LifeRunNextReflectionTool,
            "run_next_reflection_once",
            {},
            "run_next_reflection",
        ),
        (
            LifeRunIndependentAuditTool,
            "run_independent_audit_once",
            {"reason": "现在想核对候选"},
            "audit_once",
        ),
        (
            LifeProposeKnowledgeCandidateTool,
            "propose_knowledge_candidate_once",
            {"reason": "现在想整理观察"},
            "propose_knowledge_candidate",
        ),
        (
            LifeDistillSkillCandidateTool,
            "distill_skill_candidate_once",
            {"reason": "现在想整理方法"},
            "distill_skill_candidate",
        ),
    ],
)
async def test_atomic_learning_operation_runs_only_selected_stage(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    tool_cls: type[Any],
    method_name: str,
    arguments: dict[str, Any],
    expected_action: str,
) -> None:
    service, scheduler, shared, _authority = _service("chosen workflow")
    scheduler.run_independent_audit_once.return_value = []
    scheduler.propose_knowledge_candidate_once.return_value = True
    scheduler.distill_skill_candidate_once.return_value = True
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)
    tool = _tool(tool_cls, tmp_path)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await tool.execute(**arguments)

    assert ok is True
    assert result["action"] == expected_action
    getattr(scheduler, method_name).assert_awaited_once_with()
    for other in (
        "run_next_reflection_once",
        "run_independent_audit_once",
        "propose_knowledge_candidate_once",
        "distill_skill_candidate_once",
    ):
        if other != method_name:
            getattr(scheduler, other).assert_not_awaited()
    assert len(shared.contexts) == 1
    assert shared.contexts[0].actor_consciousness_instance_id == "chat_global"
    assert shared.contexts[0].source.endswith(expected_action)


async def test_read_only_projection_allows_observation_but_refuses_cognition(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scheduler, _shared, _authority = _service(
        "chosen workflow",
        writable=False,
    )
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, _result = await _tool(LifeListInsightsTool, tmp_path).execute()
        write_ok, write_result = await _tool(
            LifeRunIndependentAuditTool,
            tmp_path,
        ).execute(reason="不能越过失去的写者租约")
    assert ok is True
    assert write_ok is False
    assert LearningCapabilityReadOnly.__name__ in str(write_result)
    scheduler.run_independent_audit_once.assert_not_awaited()
    assert not (tmp_path / ".life_learning").exists()


async def test_event_only_recorder_does_not_hide_shared_read_projection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, scheduler, shared, _authority = _service(
        "chosen workflow",
        writable=False,
    )
    event_only = SimpleNamespace(projector_owner=False)
    service._learning_scheduler = event_only
    monkeypatch.setattr(service_registry, "get_life_engine_service", lambda: service)

    with bind_capability_execution(LEARNING_CAPABILITY_ID):
        ok, result = await _tool(LifeListInsightsTool, tmp_path).execute()
        write_ok, write_result = await _tool(
            LifeRunIndependentAuditTool,
            tmp_path,
        ).execute(reason="没有 projector claim 时不能运行认知写入。")

    assert ok is True
    assert result["count"] == 0
    assert write_ok is False
    assert LearningCapabilityReadOnly.__name__ in str(write_result)
    assert shared.store is scheduler.store
