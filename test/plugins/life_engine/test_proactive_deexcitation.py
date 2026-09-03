"""Proactive heartbeat de-excitation contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.curiosity import CuriositySignal
from plugins.life_engine.prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    CuriositySection,
    OpportunitySection,
    SectionContext,
)
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent


def _service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(SimpleNamespace(config=config))


def test_open_clue_alone_does_not_reset_heartbeat_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 4

    service._update_heartbeat_idle_count([])

    assert service._state.idle_heartbeat_count == 5


def test_passive_proactive_query_and_rest_do_not_reset_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 2

    service._update_heartbeat_idle_count(
        [
            ("nucleus_proactive_query", {"resource": "attention"}),
            ("nucleus_rest_heartbeat", {"duration_minutes": 30}),
        ]
    )

    assert service._state.idle_heartbeat_count == 3


def test_subject_chosen_proactive_command_resets_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 7

    service._update_heartbeat_idle_count(
        [("nucleus_proactive_command", {"action": "attention.note"})]
    )

    assert service._state.idle_heartbeat_count == 0


def test_legacy_impulse_engine_has_no_runtime_surface(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service._cfg().drives.enabled is False
    assert not hasattr(service, "_impulse_engine")


def test_default_heartbeat_has_one_opportunity_page() -> None:
    section_ids = [section.section_id for section in DEFAULT_HEARTBEAT_SECTIONS]

    assert section_ids.count("opportunity_page") == 1
    assert "recent_handwritten_diaries" in section_ids
    assert "todo_board" in section_ids
    assert "capability_catalog" in section_ids
    assert "attention_opportunity" not in section_ids
    assert "file_care_opportunity" not in section_ids
    assert "thought_streams" not in section_ids
    assert "curiosity" not in section_ids
    assert "leisure_opportunities" not in section_ids


def test_attention_opportunity_merges_clues_without_action_choice(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service._proactive_authority = object()

    async def _page_attention(_query: object) -> object:
        return SimpleNamespace(
            items=(
                SimpleNamespace(
                    thread_id="melody",
                    status="open",
                    revision=1,
                    statement_excerpt="那段旋律",
                    excerpt_complete=True,
                    statement_bytes=12,
                ),
            )
        )

    async def _list_initiatives(*, include_released: bool = False) -> tuple[object, ...]:
        return ()

    service.page_attention_threads = _page_attention
    service.list_initiatives = _list_initiatives
    curiosity = service._get_curiosity_engine()
    asyncio.run(
        curiosity.save_signal(
            CuriositySignal(
                active=True,
                anchor="雨声里的停顿",
                why="还没有完全理解",
                unknown="那一刻为什么沉默",
            )
        )
    )
    ctx = SectionContext(
        service=service,
        config=service.plugin.config,
        today_str="2026-08-06",
    )

    text = asyncio.run(OpportunitySection().render(ctx))

    assert text is not None
    assert "### 机会页" in text
    assert "#### 你留下的线索" in text
    assert "#### 可见机会" in text
    assert text.count("那段旋律") == 1
    assert "雨声里的停顿" in text or "那一刻为什么沉默" in text
    assert "好奇牵引" not in text
    assert "nucleus_manage_thought_stream" not in text
    assert "action=create" not in text
    assert "get_skill" not in text
    assert "不是系统新建议" in text


def test_legacy_curiosity_section_has_no_subject_attribution_or_adoption_guide(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    asyncio.run(
        service._get_curiosity_engine().save_signal(
            CuriositySignal(
                active=True,
                anchor="仍有一个可检查的停顿",
                unknown="停顿前后发生了什么？",
            )
        )
    )
    ctx = SectionContext(
        service=service,
        config=service.plugin.config,
        today_str="2026-08-06",
    )

    text = asyncio.run(CuriositySection().render(ctx))

    assert text is not None
    assert "认知机会候选" in text
    assert "不是你的好奇" in text
    assert "同一主体" not in text
    assert "absorb_curiosity" not in text
    assert "nucleus_manage_thought_stream" not in text


@pytest.mark.parametrize(
    ("event_instance_id", "resolved_instance_id", "expected_instance_id"),
    [
        ("minecraft-main", "must-not-be-used", "minecraft-main"),
        (None, "chat_global", "chat_global"),
    ],
)
def test_epistemic_candidate_preserves_source_instance_or_stable_fallback(
    tmp_path: Path,
    event_instance_id: str | None,
    resolved_instance_id: str,
    expected_instance_id: str,
) -> None:
    service = _service(tmp_path)
    captured: dict[str, object] = {}

    class _Generator:
        async def review(self, **kwargs: object) -> CuriositySignal:
            captured.update(kwargs)
            return CuriositySignal.empty()

    async def _history(*_args: object, **_kwargs: object) -> str:
        return ""

    async def _meme() -> str:
        return ""

    async def _prefix() -> str:
        return ""

    service._get_curiosity_engine = lambda: _Generator()
    service._build_curiosity_prefix_prompt = _prefix
    service._build_curiosity_history_text = _history
    service._build_meme_awareness_text = _meme
    service.resolve_consciousness_instance = lambda _stream_id="": resolved_instance_id
    event = LifeEngineEvent(
        event_id="event-1",
        event_type=EventType.MESSAGE,
        timestamp="2026-08-06T12:00:00+08:00",
        sequence=7,
        source="test",
        source_detail="test",
        content="source content",
        stream_id="stream-1",
        source_instance_id=event_instance_id,
    )

    asyncio.run(service._run_curiosity_review(SimpleNamespace(), event))

    assert captured["source_event_id"] == "event-1"
    assert captured["source_stream_id"] == "stream-1"
    assert captured["source_instance_id"] == expected_instance_id
