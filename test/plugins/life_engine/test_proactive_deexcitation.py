"""Proactive heartbeat de-excitation contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.curiosity import CuriositySignal
from plugins.life_engine.drives.rules import DEFAULT_RULES
from plugins.life_engine.prompts.sections import (
    DEFAULT_HEARTBEAT_SECTIONS,
    AttentionOpportunitySection,
    SectionContext,
)
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.streams.manager import ThoughtStreamManager


def _service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(SimpleNamespace(config=config))


def test_open_clue_alone_does_not_reset_heartbeat_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 4
    service._thought_manager = SimpleNamespace(list_active=lambda: [object()])

    service._update_heartbeat_idle_count([])

    assert service._state.idle_heartbeat_count == 5


def test_passive_thought_list_and_rest_do_not_reset_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 2

    service._update_heartbeat_idle_count(
        [
            ("nucleus_manage_thought_stream", {"action": "list"}),
            ("nucleus_rest_heartbeat", {"duration_minutes": 30}),
        ]
    )

    assert service._state.idle_heartbeat_count == 3


def test_subject_chosen_thought_mutation_resets_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._state.idle_heartbeat_count = 7

    service._update_heartbeat_idle_count(
        [("nucleus_manage_thought_stream", {"action": "advance"})]
    )

    assert service._state.idle_heartbeat_count == 0


def test_active_clues_have_no_periodic_impulse_rule() -> None:
    rule_names = {rule.name for rule in DEFAULT_RULES}

    assert "thought_deepen" not in rule_names
    assert "curiosity_engage" not in rule_names


def test_default_heartbeat_has_one_attention_opportunity_provider() -> None:
    section_ids = [section.section_id for section in DEFAULT_HEARTBEAT_SECTIONS]

    assert section_ids.count("attention_opportunity") == 1
    assert "thought_streams" not in section_ids
    assert "curiosity" not in section_ids
    assert "leisure_opportunities" not in section_ids


def test_attention_opportunity_merges_clues_without_action_choice(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service._thought_manager = ThoughtStreamManager(workspace_path=str(tmp_path))
    service._thought_manager.create(title="那段旋律")
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

    text = asyncio.run(AttentionOpportunitySection().render(ctx))

    assert text is not None
    assert text.count("attention_opportunity") == 1
    assert text.count("那段旋律") == 1
    assert text.count("雨声里的停顿") == 1
    assert "nucleus_manage_thought_stream" not in text
    assert "action=create" not in text
    assert "保持原样或安静结束本轮同样是完整决定" in text
