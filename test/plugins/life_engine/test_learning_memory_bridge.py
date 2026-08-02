"""Reflection-to-memory integration without code-authored cognitive judgment."""

from __future__ import annotations

import json
from typing import Any

from plugins.life_engine.learning.models import Evidence, EvidenceKind, Insight
from plugins.life_engine.learning.reflection import ReflectionEngine
from plugins.life_engine.learning.store import InsightStore


class _MemoryService:
    def __init__(self) -> None:
        self.interpretations: list[tuple[Any, tuple[Any, ...]]] = []

    async def record_memory_interpretation(
        self,
        interpretation: Any,
        *,
        sources: tuple[Any, ...],
    ) -> Any:
        self.interpretations.append((interpretation, sources))
        return interpretation


def _engine(tmp_path, memory_service: Any) -> ReflectionEngine:
    return ReflectionEngine(
        store=InsightStore(tmp_path),
        workspace_path=tmp_path,
        memory_service=memory_service,
        cooldown_seconds=0,
    )


def _insight(claim: str) -> Insight:
    return Insight.create(
        category="自由类别",
        claim=claim,
        rationale="来自测试经历",
        topic_key="自由主题",
        initial_evidence=[
            Evidence.create(
                kind=EvidenceKind.SELF_OBSERVATION,
                description="一次具体经历",
                source_ref="event-1",
            )
        ],
    )


async def test_reflection_appends_source_linked_interpretation(
    tmp_path,
    monkeypatch,
) -> None:
    memory = _MemoryService()
    engine = _engine(tmp_path, memory)
    payload = {
        "insights": [
            {
                "category": "关系理解",
                "claim": "我现在把那次沉默理解为整理想法",
                "rationale": "来自这次对话",
                "constraints": "只描述这一次经验",
                "topic_key": "沉默",
                "initial_evidence": "对方随后明确解释了原因",
                "source_ref": "event-42",
                "reinforces": "",
            }
        ]
    }

    async def _call(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(engine, "_call_llm", _call)
    created = await engine._run_reflection(
        user_prompt="reflect",
        source_event_ids=["event-42"],
        reflection_type="interaction",
    )

    assert len(created) == 1
    interpretation, sources = memory.interpretations[0]
    assert interpretation.content == payload["insights"][0]["claim"]
    assert interpretation.subject_id == "learning_topic:沉默"
    assert {item.entity_ref for item in sources} == {
        "life_event:event-42",
        "event-42",
    }


async def test_only_explicit_reinforces_merges_evidence(tmp_path, monkeypatch) -> None:
    memory = _MemoryService()
    engine = _engine(tmp_path, memory)
    existing = _insight("旧洞察")
    assert engine._store.add_insight(existing)
    payload = {
        "insights": [
            {
                "category": "任意",
                "claim": "这次经历印证旧洞察",
                "rationale": "主体明确作出关联",
                "constraints": "",
                "topic_key": "",
                "initial_evidence": "新经历",
                "source_ref": "event-2",
                "reinforces": existing.insight_id,
            }
        ]
    }

    async def _call(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(engine, "_call_llm", _call)
    created = await engine._run_reflection(
        user_prompt="reflect",
        source_event_ids=["event-2"],
        reflection_type="interaction",
    )

    assert created == []
    assert len(engine._store.list_all()) == 1
    assert len(engine._store.get_insight(existing.insight_id).evidence) == 2
    assert len(memory.interpretations) == 1


async def test_similar_text_without_explicit_relation_stays_distinct(
    tmp_path,
    monkeypatch,
) -> None:
    memory = _MemoryService()
    engine = _engine(tmp_path, memory)
    assert engine._store.add_insight(_insight("沉默可能是在整理想法"))
    payload = {
        "insights": [
            {
                "category": "关系理解",
                "claim": "沉默也可能是在组织语言",
                "rationale": "另一段经历",
                "constraints": "",
                "topic_key": "沉默",
                "initial_evidence": "独立经历",
                "source_ref": "event-3",
                "reinforces": "",
            }
        ]
    }

    async def _call(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(engine, "_call_llm", _call)
    created = await engine._run_reflection(
        user_prompt="reflect",
        source_event_ids=["event-3"],
        reflection_type="interaction",
    )

    assert len(created) == 1
    assert len(engine._store.list_all()) == 2
